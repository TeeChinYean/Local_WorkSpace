"""Built-in tools (docs/AGENT_API.md §2 "Built-in tools").

Workspace tools use the IDE router's own path guards (``ide_api.create_ide_router(...).workspace``):
relative paths only, no ``..``, no ``.git`` writes, realpath must stay inside the workspace root.
"""
from __future__ import annotations

import asyncio
import collections
import json
import os
import re
import shutil
import signal
import subprocess
import threading
import time
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from .base import AgentDeps, ToolError, ToolSpec
from .proc import IS_WIN, decode_output, kill_tree, popen_kwargs
from .skills import SkillError, SkillStore

FETCH_MAX_CHARS = 8000
READ_MAX_LINES = 2000
READ_MAX_BYTES = 200 * 1024
READ_FILE_MAX_SIZE = 20 * 1024 * 1024
LIST_MAX_ENTRIES = 500
GREP_MAX_RESULTS = 200
WRITE_MAX_CHARS = 5_000_000
RUN_TIMEOUT = 60
RUN_OUTPUT_MAX = 16 * 1024
MEMORY_ITEM_MAX = 1500

WEEKDAYS = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]


def _obj(props: Dict[str, Any], required: Optional[List[str]] = None) -> Dict[str, Any]:
    s: Dict[str, Any] = {"type": "object", "properties": props}
    if required:
        s["required"] = required
    return s


def _http_detail(e: Exception) -> str:
    detail = getattr(e, "detail", None)
    return str(detail) if detail else str(e)


def _decode_text(data: bytes) -> Tuple[str, str]:
    if data.startswith(b"\xef\xbb\xbf"):
        try:
            return data[3:].decode("utf-8"), "utf-8-sig"
        except UnicodeDecodeError:
            pass
    for enc in ("utf-8", "gb18030"):
        try:
            return data.decode(enc), enc
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1"), "latin-1"


def _atomic_write(target: str, data: bytes) -> None:
    d = os.path.dirname(target) or "."
    tmp = os.path.join(d, f".{os.path.basename(target)}.{uuid.uuid4().hex[:8]}.tvtmp")
    try:
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        if os.path.exists(target):
            try:
                shutil.copymode(target, tmp)
            except OSError:
                pass
        last: Optional[OSError] = None
        for _ in range(4 if IS_WIN else 1):
            try:
                os.replace(tmp, target)
                return
            except PermissionError as e:
                last = e
                time.sleep(0.05)
        raise last if last else OSError("replace failed")
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


class BuiltinTools:
    """Factory for the built-in ToolSpecs bound to the injected gateway dependencies."""

    def __init__(self, deps: AgentDeps, skills: SkillStore):
        self.deps = deps
        self.skills = skills

    # ------------------------------------------------------------------ helpers
    def _ws(self):
        ws = self.deps.get_workspace() if self.deps.get_workspace else None
        if ws is None:
            raise ToolError("工作区不可用（IDE 工作区未初始化）")
        return ws

    def _resolve(self, rel: Any, *, write: bool = False, allow_root: bool = True) -> Tuple[str, str, str]:
        ws = self._ws()
        try:
            return ws.resolve(str(rel or ""), write=write, allow_root=allow_root)
        except Exception as e:  # HTTPException from ide_api path guards
            raise ToolError(f"路径被拒绝: {_http_detail(e)}")

    def _backup(self, root: str, full: str, rel: str) -> Optional[str]:
        """Copy an existing file to <storage>/trash/<stamp>/agent/<rel> before overwriting it."""
        if not os.path.isfile(full):
            return None
        ws = self._ws()
        trash = getattr(ws, "trash_root", None) or os.path.join(self.deps.get_storage_dir(), "trash")
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        dest = os.path.join(trash, stamp, "agent", *rel.split("/"))
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.copy2(full, dest)
        return dest

    # ------------------------------------------------------------------ specs
    def specs(self) -> List[ToolSpec]:
        return [
            ToolSpec("web_search", "联网搜索", "搜索互联网，返回 [{title, url, snippet}]。用于时效性信息、新闻或不确定的事实。", True,
                     _obj({"query": {"type": "string", "description": "搜索关键词"},
                           "num_results": {"type": "integer", "description": "结果数量 1-8，默认 4"}}, ["query"]),
                     self.web_search),
            ToolSpec("fetch_url", "抓取网页", "抓取一个公网网页并返回清洗后的正文（最多 8000 字符）。", True,
                     _obj({"url": {"type": "string", "description": "http(s) 网址"}}, ["url"]), self.fetch_url),
            ToolSpec("memory_search", "记忆检索", "在 Turbovec 长期记忆库中做语义检索，返回最相关的记忆文本。", True,
                     _obj({"query": {"type": "string"}, "k": {"type": "integer", "description": "条数 1-10，默认 5"}}, ["query"]),
                     self.memory_search),
            ToolSpec("list_dir", "列出目录", "列出工作区中某个目录的内容（相对工作区根目录的路径，空字符串表示根目录）。", True,
                     _obj({"path": {"type": "string", "description": "相对路径，默认根目录"}}), self.list_dir),
            ToolSpec("read_file", "读取文件", "读取工作区文件内容，可指定起止行（从 1 开始）。单次最多 2000 行 / 200KB。", True,
                     _obj({"path": {"type": "string", "description": "相对工作区根目录的文件路径"},
                           "start_line": {"type": "integer"}, "end_line": {"type": "integer"}}, ["path"]),
                     self.read_file),
            ToolSpec("grep", "搜索代码", "在工作区所有文本文件中搜索字符串或正则，返回 path:line: text。", True,
                     _obj({"pattern": {"type": "string", "description": "要搜索的文本或正则"},
                           "regex": {"type": "boolean", "description": "是否为正则，默认 false"},
                           "case_sensitive": {"type": "boolean"},
                           "glob": {"type": "string", "description": "文件过滤，如 *.py,src/*.js"}}, ["pattern"]),
                     self.grep),
            ToolSpec("write_file", "写入文件", "创建或覆盖工作区文件（原文件会先备份到回收站）。需要用户批准。", False,
                     _obj({"path": {"type": "string"}, "content": {"type": "string", "description": "完整文件内容"}},
                          ["path", "content"]), self.write_file),
            ToolSpec("edit_file", "编辑文件", "用 SEARCH/REPLACE 修改工作区文件：search 必须逐字复制文件中的原文（唯一匹配），替换为 replace。需要用户批准。", False,
                     _obj({"path": {"type": "string"}, "search": {"type": "string", "description": "要替换的原文"},
                           "replace": {"type": "string", "description": "替换后的内容"},
                           "replace_all": {"type": "boolean", "description": "替换所有匹配，默认 false"}},
                          ["path", "search", "replace"]), self.edit_file),
            ToolSpec("run_command", "执行命令",
                     ("在工作区根目录执行一条 shell 命令（Windows: PowerShell；其他: /bin/sh），超时 60 秒，输出最多 16KB。需要用户批准。"),
                     False, _obj({"command": {"type": "string"},
                                  "timeout": {"type": "integer", "description": "超时秒数，最大 60"}}, ["command"]),
                     self.run_command),
            ToolSpec("get_time", "当前时间", "获取本地当前日期、时间与时区。", True, _obj({}), self.get_time),
        ]

    def skill_specs(self) -> List[ToolSpec]:
        return [
            ToolSpec("load_skill", "加载技能", "读取某个已启用技能的完整说明 (SKILL.md)。", True,
                     _obj({"name": {"type": "string", "description": "技能名称"}}, ["name"]), self.load_skill, source="skill"),
            ToolSpec("read_skill_file", "读取技能文件", "读取技能目录中的附加文件（路径相对技能目录）。", True,
                     _obj({"name": {"type": "string"}, "path": {"type": "string"}}, ["name", "path"]),
                     self.read_skill_file, source="skill"),
        ]

    # ------------------------------------------------------------------ web / memory
    async def web_search(self, args: Dict[str, Any]) -> str:
        if not self.deps.web_search:
            raise ToolError("联网搜索不可用")
        q = str(args.get("query") or "").strip()
        if not q:
            raise ToolError("query 不能为空")
        n = max(1, min(8, int(args.get("num_results") or 4)))
        items = await asyncio.to_thread(self.deps.web_search, q, n)
        out = [{"title": str(i.get("title") or ""), "url": str(i.get("url") or ""),
                "snippet": str(i.get("snippet") or "")[:500]} for i in (items or [])[:n]]
        if not out:
            return "没有找到搜索结果。"
        return json.dumps(out, ensure_ascii=False, indent=1)

    async def fetch_url(self, args: Dict[str, Any]) -> str:
        if not self.deps.fetch_url:
            raise ToolError("网页抓取不可用")
        url = str(args.get("url") or "").strip()
        if not re.match(r"^https?://", url, re.I):
            raise ToolError("仅支持 http:// 或 https:// 网址")
        try:
            title, text = await asyncio.to_thread(self.deps.fetch_url, url)
        except ToolError:
            raise
        except Exception as e:
            raise ToolError(f"抓取失败: {e}")
        text = text or ""
        more = ""
        if len(text) > FETCH_MAX_CHARS:
            more = f"\n…(正文共 {len(text)} 字符，已截断为前 {FETCH_MAX_CHARS} 字符)"
            text = text[:FETCH_MAX_CHARS]
        return f"标题: {title}\nURL: {url}\n\n{text}{more}"

    async def memory_search(self, args: Dict[str, Any]) -> str:
        if not self.deps.memory_search:
            raise ToolError("记忆检索不可用")
        q = str(args.get("query") or "").strip()
        if not q:
            raise ToolError("query 不能为空")
        k = max(1, min(10, int(args.get("k") or 5)))
        texts = await asyncio.to_thread(self.deps.memory_search, q, k)
        if not texts:
            return "记忆库中没有相关内容。"
        return "\n\n".join(f"[{i}] {t[:MEMORY_ITEM_MAX]}" for i, t in enumerate(texts, 1))

    # ------------------------------------------------------------------ workspace (read)
    async def list_dir(self, args: Dict[str, Any]) -> str:
        return await asyncio.to_thread(self._list_dir, args)

    def _list_dir(self, args: Dict[str, Any]) -> str:
        root, full, rel = self._resolve(args.get("path") or "")
        if not os.path.isdir(full):
            raise ToolError(f"目录不存在: {rel or '.'}")
        entries = []
        try:
            with os.scandir(full) as it:
                for e in it:
                    if e.name.lower() == ".git":
                        continue
                    try:
                        is_dir = e.is_dir()
                        size = 0 if is_dir else e.stat().st_size
                    except OSError:
                        is_dir, size = False, 0
                    entries.append((not is_dir, e.name.lower(), e.name, is_dir, size))
        except OSError as e:
            raise ToolError(f"无法读取目录: {e}")
        entries.sort()
        lines = [f"{name}/" if is_dir else f"{name}  ({size} B)" for _a, _b, name, is_dir, size in entries[:LIST_MAX_ENTRIES]]
        ws_name = os.path.basename(root.rstrip("/\\")) or root
        head = f"目录 {rel or '.'} (工作区: {ws_name}) 共 {len(entries)} 项"
        if len(entries) > LIST_MAX_ENTRIES:
            lines.append(f"…(仅显示前 {LIST_MAX_ENTRIES} 项)")
        return head + ":\n" + ("\n".join(lines) if lines else "(空目录)")

    async def read_file(self, args: Dict[str, Any]) -> str:
        return await asyncio.to_thread(self._read_file, args)

    def _read_file(self, args: Dict[str, Any]) -> str:
        _root, full, rel = self._resolve(args.get("path"), allow_root=False)
        if os.path.isdir(full):
            raise ToolError(f"{rel} 是目录，请使用 list_dir")
        if not os.path.isfile(full):
            raise ToolError(f"文件不存在: {rel}")
        size = os.path.getsize(full)
        if size > READ_FILE_MAX_SIZE:
            raise ToolError(f"文件过大 ({size} 字节)，无法读取")
        with open(full, "rb") as f:
            data = f.read()
        if b"\x00" in data[:8192]:
            raise ToolError(f"{rel} 是二进制文件")
        text, _enc = _decode_text(data)
        lines = text.replace("\r\n", "\n").split("\n")
        if lines and lines[-1] == "" and text.endswith("\n"):
            lines.pop()
        total = len(lines)
        start = max(1, int(args.get("start_line") or 1))
        end = int(args.get("end_line") or 0) or (start + READ_MAX_LINES - 1)
        end = min(total, end, start + READ_MAX_LINES - 1)
        if total == 0:
            return f"文件 {rel} 为空。"
        if start > total:
            raise ToolError(f"start_line {start} 超出文件行数 ({total})")
        out: List[str] = []
        used = 0
        last = start - 1
        for i in range(start - 1, end):
            ln = lines[i]
            if used + len(ln) + 1 > READ_MAX_BYTES:
                break
            out.append(ln)
            used += len(ln) + 1
            last = i + 1
        head = f"文件 {rel}（第 {start}-{last} 行，共 {total} 行）"
        tail = ""
        if last < total:
            tail = f"\n…(还有后续内容，可用 start_line={last + 1} 继续读取)"
        return head + ":\n" + "\n".join(out) + tail

    async def grep(self, args: Dict[str, Any]) -> str:
        return await asyncio.to_thread(self._grep, args)

    def _grep(self, args: Dict[str, Any]) -> str:
        ws = self._ws()
        pattern = str(args.get("pattern") or "")
        if not pattern:
            raise ToolError("pattern 不能为空")
        try:
            res = ws.search(q=pattern, regex="1" if args.get("regex") else "0",
                            case="1" if args.get("case_sensitive") else "0", glob=str(args.get("glob") or ""))
        except Exception as e:
            raise ToolError(f"搜索失败: {_http_detail(e)}")
        results = res.get("results") or []
        if not results:
            return f"没有找到匹配 {pattern!r} 的内容（扫描了 {res.get('files_scanned', 0)} 个文件）。"
        lines = [f"{r['path']}:{r['line']}: {r['text']}" for r in results[:GREP_MAX_RESULTS]]
        if len(results) > GREP_MAX_RESULTS or res.get("truncated"):
            lines.append(f"…(结果过多，仅显示前 {min(len(results), GREP_MAX_RESULTS)} 条)")
        return "\n".join(lines)

    # ------------------------------------------------------------------ workspace (write)
    async def write_file(self, args: Dict[str, Any]) -> str:
        return await asyncio.to_thread(self._write_file, args)

    def _write_file(self, args: Dict[str, Any]) -> str:
        content = args.get("content")
        if not isinstance(content, str):
            raise ToolError("content 必须是字符串")
        if len(content) > WRITE_MAX_CHARS:
            raise ToolError("文件内容过大")
        root, full, rel = self._resolve(args.get("path"), write=True, allow_root=False)
        target = os.path.realpath(full)
        if os.path.isdir(target):
            raise ToolError(f"{rel} 是目录")
        existed = os.path.exists(target)
        enc = "utf-8"
        if existed:
            try:
                with open(target, "rb") as f:
                    head = f.read(READ_FILE_MAX_SIZE)
                if b"\x00" not in head[:8192]:
                    enc = _decode_text(head)[1]
            except OSError:
                pass
            if enc == "latin-1":
                enc = "utf-8"
        try:
            data = content.encode(enc)
        except UnicodeEncodeError:
            data = content.encode("utf-8")
        try:
            backup = self._backup(root, target, rel) if existed else None
            os.makedirs(os.path.dirname(target), exist_ok=True)
            _atomic_write(target, data)
        except OSError as e:
            raise ToolError(f"写入失败: {e}")
        msg = f"{'已覆盖' if existed else '已创建'} {rel}（{len(data)} 字节）"
        if backup:
            msg += "，原文件已备份到回收站"
        return msg

    async def edit_file(self, args: Dict[str, Any]) -> str:
        return await asyncio.to_thread(self._edit_file, args)

    def _edit_file(self, args: Dict[str, Any]) -> str:
        search = args.get("search", args.get("old_string"))
        replace = args.get("replace", args.get("new_string"))
        if not isinstance(search, str) or not isinstance(replace, str):
            raise ToolError("search 与 replace 必须是字符串")
        if not search.strip():
            raise ToolError("search 不能为空（新建文件请使用 write_file）")
        root, full, rel = self._resolve(args.get("path"), write=True, allow_root=False)
        target = os.path.realpath(full)
        if not os.path.isfile(target):
            raise ToolError(f"文件不存在: {rel}（新建文件请使用 write_file）")
        with open(target, "rb") as f:
            data = f.read(READ_FILE_MAX_SIZE + 1)
        if len(data) > READ_FILE_MAX_SIZE or b"\x00" in data[:8192]:
            raise ToolError(f"{rel} 过大或是二进制文件")
        text, enc = _decode_text(data)
        crlf = "\r\n" in text and text.count("\r\n") >= text.count("\n") - text.count("\r\n")
        norm = text.replace("\r\n", "\n")
        new_text, how = apply_search_replace(norm, search.replace("\r\n", "\n"), replace.replace("\r\n", "\n"),
                                             bool(args.get("replace_all")))
        if crlf:
            new_text = new_text.replace("\n", "\r\n")
        if enc == "latin-1":
            enc = "utf-8"
        try:
            out = new_text.encode(enc)
        except UnicodeEncodeError:
            out = new_text.encode("utf-8")
        try:
            self._backup(root, target, rel)
            _atomic_write(target, out)
        except OSError as e:
            raise ToolError(f"写入失败: {e}")
        return f"已修改 {rel}（{how}）"

    # ------------------------------------------------------------------ run_command
    async def run_command(self, args: Dict[str, Any]) -> str:
        cmd = str(args.get("command") or "").strip()
        if not cmd:
            raise ToolError("command 不能为空")
        try:
            timeout = float(args.get("timeout") or RUN_TIMEOUT)
        except (TypeError, ValueError):
            timeout = RUN_TIMEOUT
        timeout = max(1.0, min(float(RUN_TIMEOUT), timeout))
        ws = self._ws()
        try:
            cwd = ws.root()
        except Exception as e:
            raise ToolError(f"工作区不可用: {_http_detail(e)}")
        code, out, timed_out = await asyncio.to_thread(run_shell, cmd, cwd, timeout)
        if timed_out:
            return f"命令超时（{int(timeout)} 秒），已终止进程树。\n输出:\n{out}"
        return f"退出码: {code}\n输出:\n{out if out.strip() else '(无输出)'}"

    # ------------------------------------------------------------------ misc
    async def get_time(self, args: Dict[str, Any]) -> str:
        now = datetime.now().astimezone()
        off = now.strftime("%z")
        off = f"UTC{off[:3]}:{off[3:]}" if off else "UTC"
        return f"{now.strftime('%Y-%m-%d %H:%M:%S')} {WEEKDAYS[now.weekday()]} ({off}{', ' + now.tzname() if now.tzname() else ''})"

    async def load_skill(self, args: Dict[str, Any]) -> str:
        try:
            return await asyncio.to_thread(self.skills.load_skill_text, str(args.get("name") or ""))
        except SkillError as e:
            raise ToolError(e.detail)

    async def read_skill_file(self, args: Dict[str, Any]) -> str:
        try:
            return await asyncio.to_thread(self.skills.read_skill_file, str(args.get("name") or ""),
                                           str(args.get("path") or ""))
        except SkillError as e:
            raise ToolError(e.detail)


# ----------------------------------------------------------------------------- SEARCH/REPLACE
def _indent(s: str) -> str:
    return s[:len(s) - len(s.lstrip(" \t"))]


def _reindent(r_lines: List[str], s_lines: List[str], f_lines: List[str]) -> List[str]:
    """Map the model's indentation (as used in SEARCH) onto the indentation actually in the file.
    Line j of REPLACE whose indent equals SEARCH line j's indent gets file line j's indent; other
    lines are shifted by the first line's delta."""
    first_s, first_f = _indent(s_lines[0]), _indent(f_lines[0])
    out: List[str] = []
    for j, ln in enumerate(r_lines):
        if not ln.strip():
            out.append(ln)
            continue
        ind = _indent(ln)
        if j < len(s_lines) and s_lines[j].strip() and ind == _indent(s_lines[j]):
            out.append(_indent(f_lines[j]) + ln[len(ind):])
        elif first_s != first_f and first_s and ln.startswith(first_s):
            out.append(first_f + ln[len(first_s):])
        elif first_s != first_f and not first_s:
            out.append(first_f + ln)
        else:
            out.append(ln)
    return out


def apply_search_replace(text: str, search: str, replace: str, replace_all: bool = False) -> Tuple[str, str]:
    """Exact match first, then a whitespace-tolerant line match. Returns (new_text, description)."""
    n = text.count(search)
    if n == 1 or (n > 1 and replace_all):
        return text.replace(search, replace), f"精确匹配 {n} 处"
    if n > 1:
        raise ToolError(f"search 内容在文件中出现了 {n} 次，请提供更多上下文使其唯一，或设置 replace_all=true")

    lines = text.split("\n")
    s_lines = search.split("\n")
    r_lines = replace.split("\n")
    while s_lines and not s_lines[0].strip():
        s_lines.pop(0)
        if r_lines and not r_lines[0].strip():
            r_lines.pop(0)
    while s_lines and not s_lines[-1].strip():
        s_lines.pop()
        if r_lines and not r_lines[-1].strip():
            r_lines.pop()
    if not s_lines:
        raise ToolError("search 不能为空")

    for norm in (lambda s: s.strip(), lambda s: re.sub(r"\s+", " ", s.strip())):
        target = [norm(x) for x in s_lines]
        k = len(target)
        hits = [i for i in range(len(lines) - k + 1) if [norm(x) for x in lines[i:i + k]] == target]
        if len(hits) > 1 and not replace_all:
            raise ToolError(f"search 内容（忽略空白后）匹配到 {len(hits)} 处，请提供更多上下文使其唯一")
        if hits:
            for i in reversed(hits):
                lines[i:i + k] = _reindent(r_lines, s_lines, lines[i:i + k])
            return "\n".join(lines), f"忽略空白差异匹配 {len(hits)} 处"
    raise ToolError("未在文件中找到 search 内容。请先用 read_file 查看文件当前内容，并逐字复制要替换的原文")


# ----------------------------------------------------------------------------- shell
def run_shell(cmd: str, cwd: str, timeout: float) -> Tuple[Optional[int], str, bool]:
    """Run ``cmd`` via PowerShell (Windows) or /bin/sh. -> (exit code, output ≤16KB, timed_out)."""
    if IS_WIN:
        argv = ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; $OutputEncoding=[System.Text.Encoding]::UTF8; " + cmd]
    else:
        argv = ["/bin/sh", "-c", cmd]
    env = dict(os.environ)
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env["GIT_TERMINAL_PROMPT"] = "0"
    try:
        proc = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, **popen_kwargs(True))
    except OSError as e:
        raise ToolError(f"无法启动命令: {e}")
    half = RUN_OUTPUT_MAX // 2
    head = bytearray()
    tail: collections.deque = collections.deque()
    tail_len = [0]
    total = [0]

    def reader():
        try:
            while True:
                chunk = proc.stdout.read1(65536) if hasattr(proc.stdout, "read1") else proc.stdout.read(4096)
                if not chunk:
                    break
                total[0] += len(chunk)
                if len(head) < half:
                    take = half - len(head)
                    head.extend(chunk[:take])
                    chunk = chunk[take:]
                if chunk:
                    tail.append(chunk)
                    tail_len[0] += len(chunk)
                    while tail and tail_len[0] - len(tail[0]) >= half:
                        tail_len[0] -= len(tail.popleft())
        except Exception:
            pass

    t = threading.Thread(target=reader, name="agent-run-command", daemon=True)
    t.start()
    timed_out = False
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        kill_tree(proc)
    except BaseException:
        kill_tree(proc)
        raise
    finally:
        if not timed_out and not IS_WIN:
            # the shell exited; kill leftover background children holding the pipe
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                pass
    t.join(timeout=5)
    try:
        proc.stdout.close()
    except Exception:
        pass
    tail_bytes = b"".join(tail)
    if len(tail_bytes) > half:
        tail_bytes = tail_bytes[-half:]
    omitted = total[0] - len(head) - len(tail_bytes)
    text = decode_output(bytes(head))
    if omitted > 0:
        text += f"\n…(输出过长，已省略 {omitted} 字节)…\n"
    text += decode_output(tail_bytes)
    return (None if timed_out else proc.returncode), text, timed_out
