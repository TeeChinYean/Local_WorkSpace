"""Minimal MCP client: JSON-RPC 2.0 over stdio, Streamable HTTP and legacy HTTP+SSE.

No dependency on the ``mcp`` SDK. All MCP I/O runs on one dedicated background event loop
(thread ``agent-mcp-loop``); request handlers on any other loop submit coroutines to it. That
keeps long-lived connections independent of the request that created them and avoids asyncio
subprocesses entirely (stdio servers use ``subprocess.Popen`` + reader threads), so it works
with uvicorn's SelectorEventLoop on Windows.
"""
from __future__ import annotations

import asyncio
import collections
import concurrent.futures
import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlsplit

import httpx

from .proc import IS_WIN, decode_output, kill_tree, popen_kwargs

PROTOCOL_VERSIONS = ["2025-06-18", "2025-03-26", "2024-11-05"]
CONNECT_TIMEOUT = 30.0
CALL_TIMEOUT = 120.0
MAX_LIST_PAGES = 50
STDERR_LINES = 40
ERROR_RETRY_SEC = 60.0   # an errored server is retried lazily after this cool-down (or via restart)
_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}
MASK = "***"
CLIENT_INFO = {"name": "turbovec-gateway", "version": "1.0.0"}
TRANSPORTS = ("stdio", "http", "sse")


class McpError(Exception):
    pass


# ============================================================================ SSE parsing
async def iter_sse(resp: httpx.Response):
    """Yield (event, data) from an SSE response."""
    event, data = "", []
    async for line in resp.aiter_lines():
        line = line.rstrip("\r")
        if not line:
            if data:
                yield event or "message", "\n".join(data)
            event, data = "", []
            continue
        if line.startswith(":"):
            continue
        field, _, value = line.partition(":")
        if value.startswith(" "):
            value = value[1:]
        if field == "event":
            event = value
        elif field == "data":
            data.append(value)
    if data:
        yield event or "message", "\n".join(data)


# ============================================================================ transports
class _Transport:
    on_message: Callable[[Dict[str, Any]], None]
    on_close: Callable[[str], None]

    def __init__(self):
        self.on_message = lambda m: None
        self.on_close = lambda r: None

    async def start(self) -> None:
        raise NotImplementedError

    async def send(self, msg: Dict[str, Any]) -> None:
        raise NotImplementedError

    async def close(self) -> None:
        pass

    def close_sync(self) -> None:
        pass

    def detail(self) -> str:
        return ""


class StdioTransport(_Transport):
    def __init__(self, command: str, args: List[str], env: Dict[str, str], cwd: Optional[str] = None):
        super().__init__()
        self.command, self.args, self.env, self.cwd = command, list(args or []), dict(env or {}), cwd
        self.proc: Optional[subprocess.Popen] = None
        self.stderr: collections.deque = collections.deque(maxlen=STDERR_LINES)
        self._wlock = threading.Lock()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._closing = False

    def detail(self) -> str:
        return "\n".join(self.stderr)

    @staticmethod
    def resolve_command(command: str, env: Dict[str, str]) -> str:
        """shutil.which with the merged PATH (finds npx.cmd / uvx.exe on Windows via PATHEXT)."""
        if os.path.isabs(command) or os.sep in command or (os.altsep and os.altsep in command):
            if os.path.exists(command):
                return command
        found = shutil.which(command, path=env.get("PATH") or env.get("Path") or os.environ.get("PATH"))
        if not found:
            raise McpError(f"找不到命令: {command}（请确认已安装并在 PATH 中）")
        return found

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        env = dict(os.environ)
        env.update({str(k): str(v) for k, v in self.env.items()})
        exe = self.resolve_command(self.command, env)
        argv = [exe] + [str(a) for a in self.args]

        def spawn():
            return subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    env=env, cwd=self.cwd or None, bufsize=0, **popen_kwargs(True))
        try:
            self.proc = await asyncio.to_thread(spawn)
        except OSError as e:
            raise McpError(f"无法启动进程 {exe}: {e}")
        threading.Thread(target=self._read_stdout, name="agent-mcp-stdout", daemon=True).start()
        threading.Thread(target=self._read_stderr, name="agent-mcp-stderr", daemon=True).start()

    def _post(self, fn, *a) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            loop.call_soon_threadsafe(fn, *a)
        except RuntimeError:
            pass

    def _read_stdout(self) -> None:
        proc = self.proc
        try:
            for raw in iter(proc.stdout.readline, b""):
                line = raw.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line.decode("utf-8", errors="replace"))
                except ValueError:
                    self.stderr.append("[stdout] " + decode_output(line)[:300])
                    continue
                for m in (msg if isinstance(msg, list) else [msg]):
                    if isinstance(m, dict):
                        self._post(self.on_message, m)
        except Exception:
            pass
        code = None
        try:
            code = proc.wait(timeout=2)
        except Exception:
            pass
        if not self._closing:
            tail = self.detail()
            self._post(self.on_close, f"MCP 进程已退出 (exit code {code})" + (f"\n{tail}" if tail else ""))

    def _read_stderr(self) -> None:
        try:
            for raw in iter(self.proc.stderr.readline, b""):
                s = decode_output(raw.rstrip(b"\r\n"))
                if s.strip():
                    self.stderr.append(s[:500])
        except Exception:
            pass

    def _write(self, data: bytes) -> None:
        with self._wlock:
            if self.proc is None or self.proc.stdin is None or self.proc.poll() is not None:
                raise McpError("MCP 进程未运行" + (f"\n{self.detail()}" if self.stderr else ""))
            try:
                self.proc.stdin.write(data)
                self.proc.stdin.flush()
            except (BrokenPipeError, OSError, ValueError) as e:
                raise McpError(f"写入 MCP 进程失败: {e}")

    async def send(self, msg: Dict[str, Any]) -> None:
        data = (json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8")
        await asyncio.to_thread(self._write, data)

    def close_sync(self) -> None:
        self._closing = True
        p = self.proc
        if p is None:
            return
        try:
            if p.stdin:
                p.stdin.close()
        except Exception:
            pass
        try:
            p.wait(timeout=1.0)
        except Exception:
            pass
        kill_tree(p, grace=0.5)
        for s in (p.stdout, p.stderr):
            try:
                s and s.close()
            except Exception:
                pass

    async def close(self) -> None:
        await asyncio.to_thread(self.close_sync)


class HttpTransport(_Transport):
    """Streamable HTTP: POST each JSON-RPC message; response is JSON or an SSE stream."""

    def __init__(self, url: str, headers: Dict[str, str]):
        super().__init__()
        self.url, self.headers = url, {str(k): str(v) for k, v in (headers or {}).items()}
        self.session_id: Optional[str] = None
        self.protocol_version: Optional[str] = None
        self.client: Optional[httpx.AsyncClient] = None

    async def start(self) -> None:
        # dedicated client: never share the llama client (it carries the local llama API key)
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(CALL_TIMEOUT, connect=10.0), follow_redirects=True)

    def _headers(self) -> Dict[str, str]:
        h = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
        h.update(self.headers)
        if self.session_id:
            h["Mcp-Session-Id"] = self.session_id
        if self.protocol_version:
            h["MCP-Protocol-Version"] = self.protocol_version
        return h

    async def send(self, msg: Dict[str, Any]) -> None:
        assert self.client is not None
        try:
            async with self.client.stream("POST", self.url, json=msg, headers=self._headers()) as resp:
                sid = resp.headers.get("mcp-session-id")
                if sid:
                    self.session_id = sid
                if resp.status_code in (202, 204):
                    return
                if resp.status_code >= 400:
                    body = (await resp.aread()).decode("utf-8", errors="replace")
                    if resp.status_code in (401, 403):
                        raise McpError(f"MCP 服务器拒绝访问 (HTTP {resp.status_code})，请检查 headers 中的认证信息")
                    raise McpError(f"MCP 服务器返回 HTTP {resp.status_code}: {body[:300]}")
                ctype = resp.headers.get("content-type", "").lower()
                want = msg.get("id")
                if "text/event-stream" in ctype:
                    async for _ev, data in iter_sse(resp):
                        try:
                            obj = json.loads(data)
                        except ValueError:
                            continue
                        done = False
                        for m in (obj if isinstance(obj, list) else [obj]):
                            if isinstance(m, dict):
                                self.on_message(m)
                                if want is not None and m.get("id") == want and ("result" in m or "error" in m):
                                    done = True
                        if done:
                            break
                else:
                    body = await resp.aread()
                    if not body.strip():
                        return
                    try:
                        obj = json.loads(body)
                    except ValueError:
                        raise McpError(f"MCP 服务器返回了非 JSON 响应: {body[:200]!r}")
                    for m in (obj if isinstance(obj, list) else [obj]):
                        if isinstance(m, dict):
                            self.on_message(m)
        except httpx.HTTPError as e:
            raise McpError(f"连接 MCP 服务器失败: {type(e).__name__}: {e}")

    async def close(self) -> None:
        if self.client is not None:
            if self.session_id:
                try:
                    await self.client.delete(self.url, headers=self._headers(), timeout=3.0)
                except Exception:
                    pass
            try:
                await self.client.aclose()
            except Exception:
                pass
            self.client = None


class SseTransport(_Transport):
    """Legacy HTTP+SSE (protocol 2024-11-05): GET the SSE stream, POST to the `endpoint` event URL."""

    def __init__(self, url: str, headers: Dict[str, str]):
        super().__init__()
        self.url, self.headers = url, {str(k): str(v) for k, v in (headers or {}).items()}
        self.client: Optional[httpx.AsyncClient] = None
        self.endpoint: Optional[str] = None
        self._task: Optional[asyncio.Task] = None
        self._endpoint_fut: Optional[asyncio.Future] = None

    async def start(self) -> None:
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(CALL_TIMEOUT, connect=10.0, read=None), follow_redirects=True)
        self._endpoint_fut = asyncio.get_running_loop().create_future()
        self._task = asyncio.create_task(self._run())
        try:
            self.endpoint = await asyncio.wait_for(asyncio.shield(self._endpoint_fut), CONNECT_TIMEOUT)
        except asyncio.TimeoutError:
            raise McpError("等待 SSE endpoint 事件超时")

    async def _run(self) -> None:
        reason = "SSE 连接已断开"
        try:
            h = {"Accept": "text/event-stream"}
            h.update(self.headers)
            async with self.client.stream("GET", self.url, headers=h) as resp:
                if resp.status_code != 200:
                    raise McpError(f"SSE 连接失败 (HTTP {resp.status_code})")
                async for ev, data in iter_sse(resp):
                    if ev == "endpoint":
                        ep = urljoin(self.url, data.strip())
                        a, b = urlsplit(ep), urlsplit(self.url)
                        same = a.netloc == b.netloc or (a.port == b.port and a.hostname in _LOOPBACK_HOSTS
                                                        and b.hostname in _LOOPBACK_HOSTS)
                        if not same:
                            raise McpError("SSE endpoint 指向了不同的主机，已拒绝")
                        if not self._endpoint_fut.done():
                            self._endpoint_fut.set_result(ep)
                        continue
                    try:
                        obj = json.loads(data)
                    except ValueError:
                        continue
                    for m in (obj if isinstance(obj, list) else [obj]):
                        if isinstance(m, dict):
                            self.on_message(m)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            reason = str(e) if isinstance(e, McpError) else f"SSE 连接异常: {type(e).__name__}: {e}"
            if self._endpoint_fut and not self._endpoint_fut.done():
                self._endpoint_fut.set_exception(McpError(reason))
        if self._endpoint_fut and not self._endpoint_fut.done():
            self._endpoint_fut.set_exception(McpError(reason))
        self.on_close(reason)

    async def send(self, msg: Dict[str, Any]) -> None:
        if not self.endpoint or self.client is None:
            raise McpError("SSE 连接未就绪")
        h = {"Content-Type": "application/json"}
        h.update(self.headers)
        try:
            r = await self.client.post(self.endpoint, json=msg, headers=h, timeout=CALL_TIMEOUT)
        except httpx.HTTPError as e:
            raise McpError(f"发送消息失败: {e}")
        if r.status_code >= 400:
            raise McpError(f"MCP 服务器返回 HTTP {r.status_code}: {r.text[:300]}")

    async def close(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except BaseException:
                pass
        if self.client:
            try:
                await self.client.aclose()
            except Exception:
                pass


# ============================================================================ session
class McpSession:
    def __init__(self, transport: _Transport):
        self.t = transport
        self._id = 0
        self._pending: Dict[int, asyncio.Future] = {}
        self.closed: Optional[str] = None
        self.server_info: Dict[str, Any] = {}
        self.protocol_version = ""
        self.capabilities: Dict[str, Any] = {}
        transport.on_message = self._on_message
        transport.on_close = self._on_close

    def _on_close(self, reason: str) -> None:
        self.closed = reason or "连接已关闭"
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_exception(McpError(self.closed))
        self._pending.clear()

    def _on_message(self, msg: Dict[str, Any]) -> None:
        mid = msg.get("id")
        if "method" not in msg and mid is not None:
            fut = self._pending.get(mid)
            if fut is not None and not fut.done():
                fut.set_result(msg)
            return
        if "method" in msg and mid is not None:  # server -> client request
            method = msg.get("method")
            if method == "ping":
                reply = {"jsonrpc": "2.0", "id": mid, "result": {}}
            elif method == "roots/list":
                reply = {"jsonrpc": "2.0", "id": mid, "result": {"roots": []}}
            else:
                reply = {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"Method not supported: {method}"}}
            asyncio.get_running_loop().create_task(self._safe_send(reply))
        # notifications are ignored (tools/list_changed is picked up on restart)

    async def _safe_send(self, msg):
        try:
            await self.t.send(msg)
        except Exception:
            pass

    async def request(self, method: str, params: Optional[Dict[str, Any]] = None, timeout: float = CALL_TIMEOUT) -> Any:
        if self.closed:
            raise McpError(self.closed)
        self._id += 1
        mid = self._id
        msg: Dict[str, Any] = {"jsonrpc": "2.0", "id": mid, "method": method}
        if params is not None:
            msg["params"] = params
        fut = asyncio.get_running_loop().create_future()
        self._pending[mid] = fut

        async def go():
            await self.t.send(msg)
            return await fut
        try:
            resp = await asyncio.wait_for(go(), timeout)
        except asyncio.TimeoutError:
            raise McpError(f"MCP 请求超时 ({method}, {int(timeout)}s)")
        finally:
            self._pending.pop(mid, None)
        if "error" in resp and resp["error"] is not None:
            err = resp["error"] if isinstance(resp["error"], dict) else {"message": str(resp["error"])}
            raise McpError(f"{err.get('message', 'error')} (code {err.get('code')})")
        return resp.get("result")

    async def notify(self, method: str, params: Optional[Dict[str, Any]] = None) -> None:
        msg: Dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        await self.t.send(msg)

    async def initialize(self, timeout: float = CONNECT_TIMEOUT) -> None:
        last: Optional[Exception] = None
        for ver in PROTOCOL_VERSIONS:
            try:
                res = await self.request("initialize", {"protocolVersion": ver, "capabilities": {},
                                                        "clientInfo": CLIENT_INFO}, timeout=timeout)
            except McpError as e:
                last = e
                if self.closed or "超时" in str(e) or "HTTP" in str(e) or "连接" in str(e):
                    raise
                continue  # e.g. "unsupported protocol version" -> try an older one
            res = res or {}
            self.protocol_version = str(res.get("protocolVersion") or ver)
            self.server_info = res.get("serverInfo") or {}
            self.capabilities = res.get("capabilities") or {}
            if isinstance(self.t, HttpTransport):
                self.t.protocol_version = self.protocol_version
            await self.notify("notifications/initialized")
            return
        raise last or McpError("initialize 失败")

    async def list_tools(self, timeout: float = CONNECT_TIMEOUT) -> List[Dict[str, Any]]:
        tools: List[Dict[str, Any]] = []
        cursor = None
        for _ in range(MAX_LIST_PAGES):
            res = await self.request("tools/list", {"cursor": cursor} if cursor else {}, timeout=timeout) or {}
            for t in res.get("tools") or []:
                if isinstance(t, dict) and t.get("name"):
                    tools.append(t)
            cursor = res.get("nextCursor")
            if not cursor:
                break
        return tools

    async def call_tool(self, name: str, arguments: Dict[str, Any], timeout: float = CALL_TIMEOUT) -> Dict[str, Any]:
        return await self.request("tools/call", {"name": name, "arguments": arguments or {}}, timeout=timeout) or {}

    async def close(self) -> None:
        self._on_close("连接已关闭")
        await self.t.close()


def result_to_text(res: Dict[str, Any]) -> Tuple[bool, str]:
    parts: List[str] = []
    for c in res.get("content") or []:
        if not isinstance(c, dict):
            continue
        t = c.get("type")
        if t == "text":
            parts.append(str(c.get("text", "")))
        elif t in ("image", "audio"):
            parts.append(f"[{t}: {c.get('mimeType', '')}，{len(str(c.get('data', '')))} 个 base64 字符，未展示]")
        elif t == "resource":
            r = c.get("resource") or {}
            parts.append(str(r.get("text")) if r.get("text") is not None else f"[资源: {r.get('uri', '')}]")
        elif t == "resource_link":
            parts.append(f"[资源链接: {c.get('uri', '')} {c.get('name', '')}]".strip())
        else:
            parts.append(json.dumps(c, ensure_ascii=False)[:2000])
    if not parts and res.get("structuredContent") is not None:
        parts.append(json.dumps(res["structuredContent"], ensure_ascii=False))
    return not bool(res.get("isError")), "\n".join(parts)


# ============================================================================ server config
_SECRET_OK = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def make_server_id(name: str, existing: List[str]) -> str:
    base = re.sub(r"[^a-z0-9]+", "_", str(name or "").lower()).strip("_")[:32].strip("_")
    if not base:
        base = "server_" + hashlib.sha1(str(name).encode("utf-8")).hexdigest()[:6]
    sid, n = base, 1
    while sid in existing:
        n += 1
        sid = f"{base}_{n}"
    return sid


def _str_map(v: Any, what: str) -> Dict[str, str]:
    if v is None:
        return {}
    if not isinstance(v, dict):
        raise McpError(f"{what} 必须是对象 {{key: value}}")
    out = {}
    for k, val in v.items():
        k = str(k).strip()
        if not k:
            continue
        out[k] = "" if val is None else str(val)
    return out


def normalize_server(data: Dict[str, Any], existing: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Validate/merge a server definition. ``***`` values in env/headers keep the stored secret."""
    if not isinstance(data, dict):
        raise McpError("请求体必须是对象")
    cur = dict(existing or {})
    out: Dict[str, Any] = {
        "id": cur.get("id", ""),
        "name": str(data.get("name", cur.get("name", "")) or "").strip(),
        "transport": str(data.get("transport", cur.get("transport", "")) or "").strip().lower(),
        "command": str(data.get("command", cur.get("command", "")) or "").strip(),
        "args": data.get("args", cur.get("args", [])),
        "env": data.get("env", cur.get("env", {})),
        "url": str(data.get("url", cur.get("url", "")) or "").strip(),
        "headers": data.get("headers", cur.get("headers", {})),
        "enabled": bool(data.get("enabled", cur.get("enabled", True))),
    }
    if not out["transport"]:
        out["transport"] = "stdio" if out["command"] else ("sse" if out["url"].rstrip("/").endswith("/sse") else "http")
    if out["transport"] == "streamable-http" or out["transport"] == "streamable_http":
        out["transport"] = "http"
    if out["transport"] not in TRANSPORTS:
        raise McpError("transport 必须是 stdio / http / sse")
    if isinstance(out["args"], str):
        import shlex
        try:
            out["args"] = shlex.split(out["args"], posix=not IS_WIN)
        except ValueError as e:
            raise McpError(f"args 解析失败: {e}")
    if not isinstance(out["args"], list):
        raise McpError("args 必须是字符串数组")
    out["args"] = [str(a) for a in out["args"]]
    env = _str_map(out["env"], "env")
    headers = _str_map(out["headers"], "headers")
    old_env, old_headers = cur.get("env") or {}, cur.get("headers") or {}
    out["env"] = {k: (old_env.get(k, "") if v == MASK else v) for k, v in env.items()}
    out["headers"] = {k: (old_headers.get(k, "") if v == MASK else v) for k, v in headers.items()}
    for k in out["env"]:
        if not _SECRET_OK.match(k):
            raise McpError(f"非法环境变量名: {k}")
    if out["transport"] == "stdio":
        if not out["command"]:
            raise McpError("stdio 类型必须填写 command")
        if any(c in out["command"] for c in "\x00\r\n"):
            raise McpError("command 含非法字符")
    else:
        parts = urlsplit(out["url"])
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise McpError("url 必须是 http:// 或 https:// 地址")
    if not out["name"]:
        out["name"] = out["command"].split("/")[-1].split("\\")[-1] or parts_host(out["url"]) or "mcp"
    return out


def parts_host(url: str) -> str:
    try:
        return urlsplit(url).hostname or ""
    except Exception:
        return ""


def mask_server(cfg: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(cfg)
    out["env"] = {k: (MASK if v else "") for k, v in (cfg.get("env") or {}).items()}
    out["headers"] = {k: (MASK if v else "") for k, v in (cfg.get("headers") or {}).items()}
    return out


def parse_import(payload: Any) -> List[Dict[str, Any]]:
    """Claude Desktop / Cursor / VS Code style -> list of raw server dicts (with ``name``)."""
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError as e:
            raise McpError(f"JSON 解析失败: {e}")
    if not isinstance(payload, dict):
        raise McpError("导入内容必须是 JSON 对象")
    servers = payload.get("mcpServers")
    if servers is None:
        servers = payload.get("servers")
    if servers is None and isinstance(payload.get("mcp"), dict):
        servers = payload["mcp"].get("servers")
    if servers is None:
        servers = payload if all(isinstance(v, dict) for v in payload.values()) else None
    if not isinstance(servers, dict) or not servers:
        raise McpError("未找到 mcpServers 配置")
    out = []
    for name, spec in servers.items():
        if not isinstance(spec, dict):
            raise McpError(f"服务器 {name} 的配置必须是对象")
        typ = str(spec.get("type") or spec.get("transport") or "").lower()
        item: Dict[str, Any] = {"name": str(name), "enabled": not bool(spec.get("disabled", False))}
        if spec.get("command"):
            item.update(transport="stdio", command=spec.get("command"), args=spec.get("args") or [],
                        env=spec.get("env") or {})
        elif spec.get("url") or spec.get("serverUrl"):
            url = spec.get("url") or spec.get("serverUrl")
            if typ in ("sse",):
                tr = "sse"
            elif typ in ("http", "streamable-http", "streamable_http", "streamablehttp"):
                tr = "http"
            else:
                tr = "sse" if str(url).rstrip("/").endswith("/sse") else "http"
            item.update(transport=tr, url=url, headers=spec.get("headers") or {})
        else:
            raise McpError(f"服务器 {name} 缺少 command 或 url")
        out.append(item)
    return out


def _fingerprint(cfg: Dict[str, Any]) -> str:
    keys = ("transport", "command", "args", "env", "url", "headers")
    return hashlib.sha1(json.dumps({k: cfg.get(k) for k in keys}, sort_keys=True).encode("utf-8")).hexdigest()


# ============================================================================ manager
class _Runtime:
    def __init__(self, sid: str):
        self.id = sid
        self.status = "connecting"
        self.error = ""
        self.tools: List[Dict[str, Any]] = []
        self.session: Optional[McpSession] = None
        self.fingerprint = ""
        self.lock: Optional[asyncio.Lock] = None
        self.connected_at = 0.0
        self.attempted_at = time.time()


class McpManager:
    """Owns MCP connections on a private event loop thread."""

    def __init__(self):
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._start_lock = threading.Lock()
        self._rt: Dict[str, _Runtime] = {}
        self._rt_lock = threading.Lock()

    # ---------------------------------------------------------------- loop plumbing
    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        with self._start_lock:
            if self._loop is not None and self._thread is not None and self._thread.is_alive():
                return self._loop
            loop = asyncio.new_event_loop()
            ready = threading.Event()

            def run():
                asyncio.set_event_loop(loop)
                loop.call_soon(ready.set)
                loop.run_forever()
                try:
                    loop.close()
                except Exception:
                    pass
            t = threading.Thread(target=run, name="agent-mcp-loop", daemon=True)
            t.start()
            ready.wait(5)
            self._loop, self._thread = loop, t
            return loop

    def _submit(self, coro: Awaitable) -> concurrent.futures.Future:
        return asyncio.run_coroutine_threadsafe(coro, self._ensure_loop())

    async def _run(self, coro: Awaitable, timeout: Optional[float] = None):
        fut = self._submit(coro)
        try:
            return await asyncio.wait_for(asyncio.wrap_future(fut), timeout)
        except asyncio.CancelledError:
            fut.cancel()
            raise

    # ---------------------------------------------------------------- status
    def runtime(self, sid: str) -> Optional[_Runtime]:
        with self._rt_lock:
            return self._rt.get(sid)

    def status(self, cfg: Dict[str, Any]) -> Dict[str, Any]:
        if not cfg.get("enabled", True):
            return {"status": "disabled", "error": "", "tools": []}
        rt = self.runtime(cfg["id"])
        if rt is None:
            return {"status": "connecting", "error": "", "tools": []}
        if rt.status == "connected" and rt.fingerprint != _fingerprint(cfg):
            return {"status": "connecting", "error": "", "tools": []}
        return {"status": rt.status, "error": rt.error, "tools": list(rt.tools)}

    def needs_connect(self, cfg: Dict[str, Any]) -> bool:
        if not cfg.get("enabled", True):
            return False
        rt = self.runtime(cfg["id"])
        if rt is None:
            return True
        if rt.status == "connecting":
            return False
        if rt.status == "error" and time.time() - rt.attempted_at > ERROR_RETRY_SEC:
            return True
        return rt.fingerprint != _fingerprint(cfg) or (rt.status == "connected" and rt.session is not None and bool(rt.session.closed))

    def kick(self, cfg: Dict[str, Any]) -> None:
        """Start connecting in the background (no waiting)."""
        if self.needs_connect(cfg):
            self._mark_connecting(cfg["id"])
            self._submit(self._connect(dict(cfg), force=False))

    def _mark_connecting(self, sid: str) -> None:
        with self._rt_lock:
            rt = self._rt.get(sid)
            if rt is None:
                self._rt[sid] = _Runtime(sid)
            elif rt.status != "connected":
                rt.status = "connecting"

    # ---------------------------------------------------------------- public async API (any loop)
    async def ensure(self, cfg: Dict[str, Any], force: bool = False) -> Dict[str, Any]:
        if not cfg.get("enabled", True):
            await self.stop(cfg["id"])
            return self.status(cfg)
        try:
            await self._run(self._connect(dict(cfg), force=force), CONNECT_TIMEOUT + 15)
        except (asyncio.TimeoutError, Exception):
            pass
        return self.status(cfg)

    async def ensure_many(self, cfgs: List[Dict[str, Any]]) -> None:
        todo = [c for c in cfgs if self.needs_connect(c) or (self.runtime(c["id"]) and self.runtime(c["id"]).status == "connecting")]
        if todo:
            await asyncio.gather(*(self.ensure(c) for c in todo), return_exceptions=True)

    async def call_tool(self, cfg: Dict[str, Any], tool: str, args: Dict[str, Any]) -> Tuple[bool, str]:
        return await self._run(self._call(dict(cfg), tool, args), CALL_TIMEOUT + CONNECT_TIMEOUT + 15)

    async def stop(self, sid: str) -> None:
        if self._loop is None:
            with self._rt_lock:
                self._rt.pop(sid, None)
            return
        try:
            await self._run(self._stop(sid), 15)
        except Exception:
            pass

    def shutdown(self, timeout: float = 10.0) -> None:
        """Close every connection and kill every stdio process (sync; lifespan shutdown / atexit)."""
        loop = self._loop
        if loop is None or not loop.is_running():
            self._kill_all_sync()
            return
        try:
            asyncio.run_coroutine_threadsafe(self._stop_all(), loop).result(timeout)
        except Exception:
            self._kill_all_sync()
        try:
            loop.call_soon_threadsafe(loop.stop)
        except RuntimeError:
            pass
        if self._thread is not None:
            self._thread.join(timeout=3)
        self._loop, self._thread = None, None

    async def ashutdown(self) -> None:
        await asyncio.to_thread(self.shutdown)

    def _kill_all_sync(self) -> None:
        with self._rt_lock:
            rts = list(self._rt.values())
            self._rt.clear()
        for rt in rts:
            if rt.session is not None:
                try:
                    rt.session.t.close_sync()
                except Exception:
                    pass

    # ---------------------------------------------------------------- MCP-loop coroutines
    def _get_rt(self, sid: str) -> _Runtime:
        with self._rt_lock:
            rt = self._rt.get(sid)
            if rt is None:
                rt = self._rt[sid] = _Runtime(sid)
        if rt.lock is None:
            rt.lock = asyncio.Lock()
        return rt

    def _make_transport(self, cfg: Dict[str, Any]) -> _Transport:
        tr = cfg.get("transport")
        if tr == "stdio":
            return StdioTransport(cfg["command"], cfg.get("args") or [], cfg.get("env") or {})
        if tr == "sse":
            return SseTransport(cfg["url"], cfg.get("headers") or {})
        return HttpTransport(cfg["url"], cfg.get("headers") or {})

    async def _connect(self, cfg: Dict[str, Any], force: bool = False) -> None:
        rt = self._get_rt(cfg["id"])
        fp = _fingerprint(cfg)
        async with rt.lock:
            alive = rt.session is not None and not rt.session.closed
            if not force and alive and rt.status == "connected" and rt.fingerprint == fp:
                return
            await self._close_rt(rt)
            rt.status, rt.error, rt.tools, rt.fingerprint = "connecting", "", [], fp
            rt.attempted_at = time.time()
            transport = self._make_transport(cfg)
            session = McpSession(transport)
            try:
                async def go():
                    await transport.start()
                    await session.initialize(CONNECT_TIMEOUT)
                    return await session.list_tools(CONNECT_TIMEOUT)
                tools = await asyncio.wait_for(go(), CONNECT_TIMEOUT)
            except BaseException as e:
                msg = "连接超时（30 秒）" if isinstance(e, asyncio.TimeoutError) else (str(e) or type(e).__name__)
                detail = transport.detail()
                if detail and detail not in msg:
                    msg += "\n" + detail[-1500:]
                try:
                    await session.close()
                except Exception:
                    pass
                rt.session, rt.status, rt.error = None, "error", msg
                if isinstance(e, asyncio.CancelledError):
                    raise
                return
            rt.session, rt.tools, rt.status, rt.error = session, tools, "connected", ""
            rt.connected_at = time.time()

    async def _call(self, cfg: Dict[str, Any], tool: str, args: Dict[str, Any]) -> Tuple[bool, str]:
        rt = self._get_rt(cfg["id"])
        if rt.session is None or rt.session.closed or rt.status != "connected" or rt.fingerprint != _fingerprint(cfg):
            await self._connect(cfg)
        if rt.session is None:
            raise McpError(rt.error or "MCP 服务器未连接")
        try:
            res = await rt.session.call_tool(tool, args, CALL_TIMEOUT)
        except McpError as e:
            if rt.session is not None and rt.session.closed:
                rt.status, rt.error = "error", str(e)
            raise
        return result_to_text(res)

    async def _close_rt(self, rt: _Runtime) -> None:
        s, rt.session = rt.session, None
        if s is not None:
            try:
                await asyncio.wait_for(s.close(), 10)
            except BaseException:
                try:
                    s.t.close_sync()
                except Exception:
                    pass

    async def _stop(self, sid: str) -> None:
        with self._rt_lock:
            rt = self._rt.pop(sid, None)
        if rt is not None:
            if rt.lock is None:
                rt.lock = asyncio.Lock()
            async with rt.lock:
                await self._close_rt(rt)

    async def _stop_all(self) -> None:
        with self._rt_lock:
            ids = list(self._rt.keys())
        await asyncio.gather(*(self._stop(i) for i in ids), return_exceptions=True)
