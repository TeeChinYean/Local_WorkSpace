"""Cursor-like web IDE backend (`/api/ide/*`).

Contract: docs/IDE_API.md.  Wire it into the gateway with::

    from ide_api import create_ide_router
    app.include_router(create_ide_router(
        project_root=PROJECT_ROOT, storage_dir=STORAGE_DIR,
        is_allowed_path=_is_allowed_path, check_security=check_request_security))

Design notes
- Stdlib + fastapi/pydantic only.  ``winpty`` (pywinpty) is imported lazily on Windows.
- Blocking HTTP handlers are plain ``def`` (FastAPI runs them in its threadpool).
- No file-content caching; search streams line by line; every listing is capped.
- git is always invoked as an argv list (never ``shell=True``) with
  ``GIT_TERMINAL_PROMPT=0`` / ``GIT_LITERAL_PATHSPECS=1`` and a timeout; on Windows
  with ``CREATE_NO_WINDOW`` so no console windows flash.
"""
import asyncio
import atexit
import codecs
import collections
import fnmatch
import heapq
import importlib
import json
import ntpath
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime
from typing import Callable, Dict, Iterator, List, Optional, Tuple
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket
from pydantic import BaseModel

IS_WIN = os.name == "nt"
CREATE_NO_WINDOW = 0x08000000
CREATE_NEW_PROCESS_GROUP = 0x00000200

IGNORED_DIRS = {"node_modules", "__pycache__", ".venv", "venv", ".pytest_cache", "dist", "build"}
TREE_MAX_ENTRIES = 2000
FILES_SCAN_MAX = 20000
FILES_LIMIT_MAX = 500
READ_MAX_BYTES = 2 * 1024 * 1024
BINARY_SNIFF_BYTES = 8192
WRITE_MAX_CHARS = 20 * 1024 * 1024
SEARCH_FILE_MAX_BYTES = 1024 * 1024
SEARCH_MAX_RESULTS = 500
SEARCH_TEXT_MAX = 300
SEARCH_LINE_SCAN_MAX = 10000  # longer (minified) lines are only matched on their first N chars
SEARCH_TIME_BUDGET = 20.0
GIT_TIMEOUT = 30
GIT_NET_TIMEOUT = 120
GIT_STATUS_MAX_FILES = 5000
SHOW_DIFF_MAX_BYTES = 400 * 1024
DIFF_MAX_BYTES = 1024 * 1024
LOG_LIMIT_MAX = 500
MTIME_TOLERANCE = 0.001



def _env_int(name: str, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(os.environ.get(name, "").strip() or default)))
    except ValueError:
        return default


MAX_TERMINALS = _env_int("TV_IDE_MAX_TERMINALS", 3, 1, 20)  # concurrent shells (4429 beyond)
TERM_MAX_SESSIONS = MAX_TERMINALS  # backwards-compatible alias
TERM_PROGRAM_VERSION = "1.0.0"
PIPE_HINT_NO_WINPTY = "未检测到 pywinpty：在终端运行 pip install pywinpty 后，新开一个终端即可 (无需重启服务)"
TERM_BUFFER_MAX = 256 * 1024  # chars kept per session while the socket is slow (drop oldest)
TERM_BATCH_SEC = 0.016
TERM_INPUT_MAX = 64 * 1024

REF_RE = re.compile(r"^[A-Za-z0-9._/\-~^]{1,100}$")
REMOTE_RE = re.compile(r"^[A-Za-z0-9._/\-]{1,100}$")
_WIN_DEVICE_NAMES = {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"} | {f"COM{i}" for i in range(1, 10)} | {f"LPT{i}" for i in range(1, 10)}

# module-level alias so tests can simulate a failing atomic replace
_replace = os.replace


# ==============================================================================
# request models
# ==============================================================================
class WorkspaceBody(BaseModel):
    root: str


class WriteBody(BaseModel):
    path: str
    content: str
    expected_mtime: Optional[float] = None
    encoding: Optional[str] = None


class FsBody(BaseModel):
    op: str
    path: str
    new_path: Optional[str] = None


class PathsBody(BaseModel):
    paths: List[str] = []


class CommitBody(BaseModel):
    message: str = ""
    amend: bool = False


class HashBody(BaseModel):
    hash: str


class RestoreBody(BaseModel):
    path: str
    ref: str = "HEAD"


class BranchBody(BaseModel):
    name: str
    checkout: bool = True


class CheckoutBody(BaseModel):
    branch: str


# ==============================================================================
# generic helpers
# ==============================================================================
def _popen_flags() -> Dict:
    return {"creationflags": CREATE_NO_WINDOW} if IS_WIN else {}


def _norm(p: str) -> str:
    return os.path.normcase(os.path.realpath(os.path.abspath(p)))


def _within(real_path: str, real_root: str) -> bool:
    """Both arguments must already be _norm()'ed."""
    try:
        return os.path.commonpath([real_path, real_root]) == real_root
    except ValueError:  # different drives
        return False


def _name_ok(name: str) -> bool:
    try:
        name.encode("utf-8")
        return True
    except UnicodeEncodeError:  # undecodable file name (surrogate escapes): cannot round-trip through JSON
        return False


def _is_git_component(part: str) -> bool:
    p = part.lower()
    return p == ".git" or p.startswith("git~")  # .git and its Windows 8.3 short name GIT~1


def _is_reparse_dir(entry: "os.DirEntry") -> bool:
    """Symlinked dir or (Windows) junction: never descended into by walkers."""
    try:
        if entry.is_symlink():
            return True
        if IS_WIN:
            attrs = getattr(entry.stat(follow_symlinks=False), "st_file_attributes", 0)
            return bool(attrs & 0x400)  # FILE_ATTRIBUTE_REPARSE_POINT
    except OSError:
        return True
    return False


def _decode_text(data: bytes) -> Tuple[str, str]:
    if data.startswith(codecs.BOM_UTF8):
        try:
            return data[len(codecs.BOM_UTF8):].decode("utf-8"), "utf-8-sig"
        except UnicodeDecodeError:
            pass
    for enc in ("utf-8", "gb18030"):
        try:
            return data.decode(enc), enc
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1"), "latin-1"


def _decode_line(raw: bytes) -> str:
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        try:
            return raw.decode("gb18030")
        except UnicodeDecodeError:
            return raw.decode("latin-1")


def _detect_eol(text: str) -> str:
    crlf = text.count("\r\n")
    if crlf and crlf >= text.count("\n") - crlf:
        return "\r\n"
    return "\n"


def _oserror(e: OSError) -> HTTPException:
    if isinstance(e, FileNotFoundError):
        return HTTPException(404, "文件或目录不存在")
    if isinstance(e, PermissionError):
        return HTTPException(403, f"权限不足或文件被占用: {e.strerror or e}")
    if isinstance(e, (IsADirectoryError, NotADirectoryError)):
        return HTTPException(400, f"路径类型不正确: {e.strerror or e}")
    return HTTPException(500, f"文件系统错误: {e.strerror or e}")


def _atomic_write_bytes(target: str, data: bytes) -> None:
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
        last_err: Optional[OSError] = None
        for _ in range(4 if IS_WIN else 1):
            try:
                _replace(tmp, target)
                return
            except PermissionError as e:  # Windows: target briefly locked (AV / indexer / other editor)
                last_err = e
                time.sleep(0.05)
        if IS_WIN and last_err is not None:
            # last resort: in-place overwrite (non-atomic, but keeps the user's edit)
            with open(target, "wb") as f:
                f.write(data)
            return
        raise last_err if last_err else OSError("replace failed")
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def _validate_ref(value: Optional[str], what: str = "引用") -> str:
    v = (value or "").strip()
    if not v or not REF_RE.match(v) or v.startswith("-"):
        raise HTTPException(400, f"非法的{what}: {value!r}")
    return v


def _flag_true(v) -> bool:
    return str(v).strip().lower() in {"1", "true", "yes", "on"}


# ==============================================================================
# git helpers
# ==============================================================================
def _git_env(lang_c: bool = False) -> Dict[str, str]:
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_LITERAL_PATHSPECS"] = "1"  # file names are never interpreted as globs / :(magic)
    env["GIT_MERGE_AUTOEDIT"] = "no"
    env["GIT_OPTIONAL_LOCKS"] = "0"
    if lang_c:  # machine-parsed output
        env["LC_ALL"] = "C"
        env["LANGUAGE"] = "C"
    return env


def _git_cmd(args: List[str]) -> List[str]:
    return ["git", "-c", "core.quotepath=false", "-c", "color.ui=false"] + list(args)


def _git(root: str, args: List[str], *, timeout: float = GIT_TIMEOUT, input: Optional[bytes] = None,
         lang_c: bool = False) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            _git_cmd(args), cwd=root, env=_git_env(lang_c),
            input=input, stdin=None if input is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=timeout, **_popen_flags())
    except FileNotFoundError:
        raise HTTPException(500, "未找到 git 命令，请先安装 Git 并加入 PATH")
    except subprocess.TimeoutExpired:
        raise HTTPException(504, f"git 操作超时 ({int(timeout)} 秒)")


def _git_capped(root: str, args: List[str], cap: int, timeout: float = GIT_TIMEOUT) -> Tuple[bytes, bool, int]:
    """Run git and read at most ``cap`` bytes of stdout (kills git beyond that). -> (data, truncated, rc)"""
    try:
        p = subprocess.Popen(_git_cmd(args), cwd=root, env=_git_env(), stdin=subprocess.DEVNULL,
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, **_popen_flags())
    except FileNotFoundError:
        raise HTTPException(500, "未找到 git 命令，请先安装 Git 并加入 PATH")
    timer = threading.Timer(timeout, p.kill)
    timer.daemon = True
    timer.start()
    chunks: List[bytes] = []
    n = 0
    truncated = False
    try:
        while True:
            b = p.stdout.read(65536)
            if not b:
                break
            if n + len(b) > cap:
                chunks.append(b[: cap - n])
                truncated = True
                p.kill()
                break
            chunks.append(b)
            n += len(b)
    finally:
        try:
            p.stdout.close()
        except OSError:
            pass
        rc = p.wait()
        timer.cancel()
    return b"".join(chunks), truncated, (0 if truncated else rc)


def _out(cp: subprocess.CompletedProcess) -> str:
    return (cp.stdout or b"").decode("utf-8", "replace")


def _err(cp: subprocess.CompletedProcess) -> str:
    return ((cp.stderr or b"").decode("utf-8", "replace").strip()
            or (cp.stdout or b"").decode("utf-8", "replace").strip()
            or f"git 退出码 {cp.returncode}")


def _combined(cp: subprocess.CompletedProcess) -> str:
    parts = [(cp.stdout or b"").decode("utf-8", "replace").strip(), (cp.stderr or b"").decode("utf-8", "replace").strip()]
    return "\n".join(p for p in parts if p)


def _ok_or_400(cp: subprocess.CompletedProcess) -> Dict:
    """Success -> {ok: true, output}; git failure -> HTTP 400 whose detail is git's own output."""
    if cp.returncode != 0:
        raise HTTPException(400, _combined(cp) or _err(cp))
    return {"ok": True, "output": _combined(cp)}


def _git_info(root: str) -> Optional[str]:
    """Returns the repo prefix of ``root`` ("" or "sub/dir/") or None if not inside a work tree."""
    try:
        cp = _git(root, ["rev-parse", "--is-inside-work-tree", "--show-prefix"], timeout=10, lang_c=True)
    except HTTPException:
        return None
    if cp.returncode != 0:
        return None
    lines = _out(cp).splitlines()
    if not lines or lines[0].strip() != "true":
        return None
    return lines[1].strip() if len(lines) > 1 else ""


def _require_repo(root: str) -> str:
    prefix = _git_info(root)
    if prefix is None:
        raise HTTPException(400, "当前工作区不是 git 仓库")
    return prefix


def _current_branch(root: str) -> Tuple[Optional[str], bool]:
    """-> (branch or short hash, detached)"""
    cp = _git(root, ["symbolic-ref", "--short", "-q", "HEAD"], timeout=10)
    if cp.returncode == 0 and _out(cp).strip():
        return _out(cp).strip(), False
    cp = _git(root, ["rev-parse", "--short", "HEAD"], timeout=10)
    if cp.returncode == 0:
        return _out(cp).strip(), True
    return None, True


def _strip_prefix(path: str, prefix: str) -> Optional[str]:
    if not prefix:
        return path
    if path.startswith(prefix):
        return path[len(prefix):] or None
    return None


def _parse_branch_header(h: str) -> Dict:
    info = {"branch": None, "upstream": None, "ahead": 0, "behind": 0, "detached": False}
    h = h[3:] if h.startswith("## ") else h
    for lead in ("No commits yet on ", "Initial commit on "):
        if h.startswith(lead):
            info["branch"] = h[len(lead):].strip()
            return info
    if h.startswith("HEAD (no branch)"):
        info["detached"] = True
        return info
    track = ""
    if " [" in h and h.endswith("]"):
        h, track = h.split(" [", 1)
        track = track[:-1]
    if "..." in h:
        b, up = h.split("...", 1)
        info["branch"], info["upstream"] = b, up or None
    else:
        info["branch"] = h.strip() or None
    m = re.search(r"ahead (\d+)", track)
    if m:
        info["ahead"] = int(m.group(1))
    m = re.search(r"behind (\d+)", track)
    if m:
        info["behind"] = int(m.group(1))
    return info


def _parse_porcelain_z(data: bytes) -> Tuple[Optional[str], List[Tuple[str, str, str, Optional[str]]]]:
    """Parse ``git status --porcelain=v1 -z [-b]``.  -> (branch header, [(X, Y, path, orig_path)])

    With -z a rename/copy record is ``XY <new>\\0<orig>\\0`` (new path first)."""
    header = None
    entries = []
    toks = data.split(b"\0")
    i = 0
    while i < len(toks):
        tok = toks[i]
        i += 1
        if not tok:
            continue
        s = tok.decode("utf-8", "replace")
        if s.startswith("## "):
            header = s
            continue
        if len(s) < 4:
            continue
        x, y, path = s[0], s[1], s[3:]
        orig = None
        if x in "RC" or y in "RC":
            if i < len(toks):
                orig = toks[i].decode("utf-8", "replace")
                i += 1
        entries.append((x, y, path, orig))
    return header, entries


def _parse_name_status_z(data: bytes) -> List[Dict]:
    toks = [t.decode("utf-8", "replace") for t in data.split(b"\0")]
    files = []
    i = 0
    while i < len(toks):
        st = toks[i].strip()
        i += 1
        if not st:
            continue
        if st[0] in "RC":
            orig = toks[i] if i < len(toks) else ""
            new = toks[i + 1] if i + 1 < len(toks) else ""
            i += 2
            files.append({"path": new, "status": st[0], "orig_path": orig})
        else:
            p = toks[i] if i < len(toks) else ""
            i += 1
            files.append({"path": p, "status": st[0]})
    return files


def _remotes(root: str) -> List[str]:
    cp = _git(root, ["remote"], timeout=10)
    return [r.strip() for r in _out(cp).splitlines() if r.strip()] if cp.returncode == 0 else []


def _chunks(items: List[str], n: int = 100) -> Iterator[List[str]]:
    for i in range(0, len(items), n):
        yield items[i:i + n]


# ==============================================================================
# terminal backends
# ==============================================================================
def _oem_encoding() -> str:
    if IS_WIN:
        try:
            import ctypes
            cp = ctypes.windll.kernel32.GetOEMCP()
            codecs.lookup(f"cp{cp}")
            return f"cp{cp}"
        except Exception:
            pass
    import locale
    return locale.getpreferredencoding(False) or "utf-8"


def _import_winpty():
    """Import pywinpty *fresh* on every call so that ``pip install pywinpty`` run inside a (pipe-mode)
    terminal takes effect for the next new terminal without restarting the server.

    - ``importlib.invalidate_caches()`` drops the path finders' cached directory listings (and, on
      3.10+, negative ``sys.path_importer_cache`` entries for dirs that did not exist at startup).
    - A failed import can leave half-initialised ``winpty.*`` submodules (or a ``None`` blocker) in
      ``sys.modules``; they are purged so the next attempt really re-imports.
    - A per-user site dir created by ``pip install --user`` after startup is added to ``sys.path``.
    Returns the module or None.  Never raises."""
    mod = sys.modules.get("winpty")
    if mod is not None and getattr(mod, "PtyProcess", None) is not None:
        return mod
    try:
        importlib.invalidate_caches()
        for k, v in list(sys.path_importer_cache.items()):
            if v is None:
                sys.path_importer_cache.pop(k, None)
    except Exception:
        pass
    try:
        import site
        usp = site.getusersitepackages() if getattr(site, "ENABLE_USER_SITE", False) else ""
        if usp and os.path.isdir(usp) and usp not in sys.path:
            site.addsitedir(usp)
    except Exception:
        pass
    for k in [k for k in list(sys.modules) if k == "winpty" or k.startswith("winpty.")]:
        sys.modules.pop(k, None)
    try:
        mod = importlib.import_module("winpty")
        if getattr(mod, "PtyProcess", None) is None:
            raise ImportError("winpty.PtyProcess missing")
        return mod
    except Exception:
        for k in [k for k in list(sys.modules) if k == "winpty" or k.startswith("winpty.")]:
            sys.modules.pop(k, None)
        return None


def _term_mode() -> str:
    forced = os.environ.get("TV_IDE_TERM_MODE", "").strip().lower()
    if forced == "pipe":
        return "pipe"
    if IS_WIN:
        return "pty" if _import_winpty() is not None else "pipe"
    try:
        import fcntl  # noqa: F401
        import termios  # noqa: F401
        return "pty" if hasattr(os, "openpty") else "pipe"
    except Exception:
        return "pipe"


# ------------------------------------------------------------------ shell profiles (VS Code-like)
_POSIX_SHELLS = {"bash", "sh", "zsh", "dash", "ksh", "fish"}


def _split_cmdline(cmd: str, is_win: bool) -> List[str]:
    if is_win:
        return [t[1:-1] if len(t) >= 2 and t[0] == t[-1] == '"' else t for t in shlex.split(cmd, posix=False)]
    return shlex.split(cmd)


def _profile(pid: str, name: str, path: str, args: List[str], icon: str, kind: str, **extra) -> Dict:
    p = {"id": pid, "name": name, "path": path, "args": list(args), "icon": icon, "kind": kind}
    p.update(extra)
    return p


def _detect_profiles(*, is_win: Optional[bool] = None, environ=None, which=None, isfile=None) -> Tuple[str, List[Dict]]:
    """Shell profiles like VS Code's ``terminal.integrated.profiles.*``.  Returns (default_id, profiles).
    Internal keys (``kind``, ``env``, ``pipe_encoding``) are stripped by ``_public_profile``.
    The keyword arguments exist for tests (detect the Windows set on Linux)."""
    is_win = IS_WIN if is_win is None else is_win
    env = os.environ if environ is None else environ
    which = which or shutil.which
    isfile = isfile or os.path.isfile
    profiles: List[Dict] = []
    seen = set()

    def add(p: Dict) -> None:
        if p["id"] not in seen:
            seen.add(p["id"])
            profiles.append(p)

    if is_win:
        j = ntpath.join
        sysroot = env.get("SystemRoot") or env.get("windir") or "C:\\Windows"
        pf_dirs = [d for d in (env.get("ProgramW6432"), env.get("ProgramFiles"), "C:\\Program Files",
                               env.get("ProgramFiles(x86)")) if d]
        pwsh = which("pwsh.exe") or next((c for c in (j(d, "PowerShell", "7", "pwsh.exe") for d in pf_dirs) if isfile(c)), None)
        if pwsh:
            add(_profile("pwsh", "pwsh", pwsh, ["-NoLogo"], "terminal-powershell", "powershell"))
        winps = j(sysroot, "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
        winps = winps if isfile(winps) else which("powershell.exe")
        if winps:
            add(_profile("powershell", "powershell", winps, ["-NoLogo"], "terminal-powershell", "powershell"))
        cmd = env.get("ComSpec") or j(sysroot, "System32", "cmd.exe")
        cmd = cmd if isfile(cmd) else which("cmd.exe")
        if cmd:
            # UTF-8 code page so Chinese output of UTF-8 tools is not garbled
            add(_profile("cmd", "cmd", cmd, ["/K", "chcp 65001>nul"], "terminal-cmd", "cmd", pipe_encoding="utf-8"))
        bash_cands = [j(d, "Git", "bin", "bash.exe") for d in pf_dirs]
        if env.get("LOCALAPPDATA"):
            bash_cands.append(j(env["LOCALAPPDATA"], "Programs", "Git", "bin", "bash.exe"))
        git = which("git.exe") or which("git")
        if git:  # <Git>\cmd\git.exe or <Git>\mingw64\bin\git.exe
            gd = ntpath.dirname(git)
            bash_cands += [ntpath.normpath(j(gd, "..", "bin", "bash.exe")), ntpath.normpath(j(gd, "..", "..", "bin", "bash.exe"))]
        gb = next((c for c in bash_cands if isfile(c)), None)
        if gb:
            add(_profile("gitbash", "Git Bash", gb, ["--login", "-i"], "terminal-git-bash", "posix",
                         env={"CHERE_INVOKING": "1"}, pipe_encoding="utf-8"))
        wsl = j(sysroot, "System32", "wsl.exe")
        wsl = wsl if isfile(wsl) else which("wsl.exe")
        if wsl:
            add(_profile("wsl", "WSL", wsl, [], "terminal-linux", "wsl", pipe_encoding="utf-8"))
        default = "pwsh" if "pwsh" in seen else "powershell"  # VS Code's default
        if not profiles:  # nothing found at all: let CreateProcess search PATH
            add(_profile("powershell", "powershell", "powershell.exe", ["-NoLogo"], "terminal-powershell", "powershell"))
        elif default not in seen:
            default = profiles[0]["id"]
    else:
        cands = []
        login = env.get("SHELL") or ""
        if login and isfile(login):
            cands.append(login)
        for n in ("bash", "zsh", "sh"):
            for d in ("/bin", "/usr/bin", "/usr/local/bin"):
                cands.append(f"{d}/{n}")
        for c in cands:
            n = os.path.basename(c)
            if n in seen or not isfile(c):
                continue
            add(_profile(n, n, c, [], "terminal-bash" if n in ("bash", "zsh") else "terminal", "posix"))
        lb = os.path.basename(login) if login else ""
        default = lb if lb in seen else ("bash" if "bash" in seen else (profiles[0]["id"] if profiles else "sh"))
        if not profiles:
            add(_profile("sh", "sh", "/bin/sh", [], "terminal", "posix"))
    override = (env.get("TV_IDE_SHELL") or "").strip()
    if override:
        argv = [override.strip('"')] if isfile(override.strip('"')) else _split_cmdline(override, is_win)
        if argv:
            n = ntpath.basename(argv[0]) if is_win else os.path.basename(argv[0])
            profiles.insert(0, _profile("custom", n, argv[0], argv[1:], "terminal", "custom"))
            default = "custom"
    return default, profiles


def _public_profile(p: Dict) -> Dict:
    return {k: p[k] for k in ("id", "name", "path", "args", "icon")}


def _find_profile(pid: Optional[str]) -> Optional[Dict]:
    default, profiles = _detect_profiles()
    want = (pid or "").strip() or default
    return next((p for p in profiles if p["id"] == want), None)


def _profile_argv(p: Dict, mode: str) -> List[str]:
    argv = [p["path"]] + list(p["args"])
    kind = p.get("kind")
    if mode == "pipe":
        if kind == "powershell":
            argv += ["-Command", "-"]  # read commands from the redirected stdin (no PSReadLine without a PTY)
        elif kind == "posix" and os.path.basename(p["path"]).lower().replace(".exe", "") in _POSIX_SHELLS and "-i" not in argv:
            argv.append("-i")
    return argv


def _shell_argv(mode: str) -> List[str]:
    """argv of the default profile (kept for callers of the old API)."""
    p = _find_profile(None)
    return _profile_argv(p, mode) if p else []


def _shell_display(argv: List[str]) -> str:
    return os.path.basename(argv[0]) if argv else ""


def _term_env(profile: Optional[Dict] = None) -> Dict[str, str]:
    """Child env like VS Code's integrated terminal."""
    env = dict(os.environ)
    env.update({"TERM": "xterm-256color", "COLORTERM": "truecolor", "TERM_PROGRAM": "vscode",
                "TERM_PROGRAM_VERSION": TERM_PROGRAM_VERSION})
    if not IS_WIN and "utf" not in (env.get("LC_ALL") or env.get("LANG") or "").lower():
        env["LANG"] = "en_US.UTF-8"
    for k, v in ((profile or {}).get("env") or {}).items():
        env[k] = v
    return env


def _posix_session_pids(root_pid: int) -> List[int]:
    """All processes in the shell's session plus its descendants (Linux /proc; pgrep elsewhere)."""
    me = os.getpid()
    found = set()
    if os.path.isdir("/proc"):
        table: Dict[int, Tuple[int, int]] = {}
        try:
            names = os.listdir("/proc")
        except OSError:
            names = []
        for name in names:
            if not name.isdigit():
                continue
            try:
                with open(f"/proc/{name}/stat", "rb") as f:
                    raw = f.read().decode("latin-1")
                rest = raw[raw.rindex(")") + 2:].split()
                table[int(name)] = (int(rest[1]), int(rest[3]))  # ppid, session
            except (OSError, ValueError, IndexError):
                continue
        children: Dict[int, List[int]] = collections.defaultdict(list)
        for pid, (ppid, sid) in table.items():
            children[ppid].append(pid)
            if sid == root_pid:
                found.add(pid)
        stack = [root_pid]
        while stack:
            for c in children.get(stack.pop(), []):
                if c not in found:
                    found.add(c)
                    stack.append(c)
    else:
        try:
            cp = subprocess.run(["pgrep", "-s", str(root_pid)], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=5)
            found.update(int(x) for x in cp.stdout.split() if x.strip().isdigit())
        except Exception:
            pass
    found.discard(me)
    return [p for p in found if p > 1]


def _posix_kill_tree(proc: subprocess.Popen) -> None:
    pid = proc.pid
    pids = _posix_session_pids(pid)
    for sig in (signal.SIGHUP, signal.SIGKILL):
        try:
            os.killpg(pid, sig)
        except OSError:
            pass
        for p in pids:
            try:
                os.kill(p, sig)
            except OSError:
                pass
        if sig == signal.SIGHUP:
            deadline = time.monotonic() + 0.3
            while time.monotonic() < deadline and proc.poll() is None:
                time.sleep(0.02)
    try:
        proc.wait(timeout=3)
    except Exception:
        pass


def _win_taskkill(pid: int) -> None:
    if not pid:
        return
    try:
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)], stdin=subprocess.DEVNULL,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15, **_popen_flags())
    except Exception:
        pass


class _WinJob:
    """Windows Job Object with KILL_ON_JOB_CLOSE: kills every process the shell spawned, even
    orphans that taskkill /T can no longer find by parent pid.  Best effort; all failures ignored."""

    def __init__(self, pid: int):
        self.handle = None
        if not IS_WIN or not pid:
            return
        try:
            import ctypes
            from ctypes import wintypes

            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
            k32.CreateJobObjectW.restype = wintypes.HANDLE
            k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
            k32.SetInformationJobObject.restype = wintypes.BOOL
            k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            k32.OpenProcess.restype = wintypes.HANDLE
            k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
            k32.AssignProcessToJobObject.restype = wintypes.BOOL
            k32.CloseHandle.argtypes = [wintypes.HANDLE]
            k32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]

            class BASIC(ctypes.Structure):
                _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                            ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                            ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                            ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                            ("SchedulingClass", wintypes.DWORD)]

            class IOC(ctypes.Structure):
                _fields_ = [(n, ctypes.c_uint64) for n in ("R", "W", "O", "RT", "WT", "OT")]

            class EXT(ctypes.Structure):
                _fields_ = [("BasicLimitInformation", BASIC), ("IoInfo", IOC),
                            ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                            ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

            job = k32.CreateJobObjectW(None, None)
            if not job:
                return
            info = EXT()
            info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not k32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):
                k32.CloseHandle(job)
                return
            hproc = k32.OpenProcess(0x0100 | 0x0001, False, pid)  # PROCESS_SET_QUOTA | PROCESS_TERMINATE
            if not hproc:
                k32.CloseHandle(job)
                return
            ok = k32.AssignProcessToJobObject(job, hproc)
            k32.CloseHandle(hproc)
            if not ok:
                k32.CloseHandle(job)
                return
            self._k32 = k32
            self.handle = job
        except Exception:
            self.handle = None

    def kill(self) -> None:
        if not self.handle:
            return
        try:
            self._k32.TerminateJobObject(self.handle, 1)
            self._k32.CloseHandle(self.handle)
        except Exception:
            pass
        self.handle = None


class _PosixPty:
    mode = "pty"

    def __init__(self, argv: List[str], cwd: str, env: Dict[str, str], rows: int, cols: int):
        import fcntl
        import termios

        master, slave = os.openpty()
        try:
            self._winsize(slave, rows, cols)

            def _preexec():  # runs in the child after setsid(): make the pty our controlling terminal
                try:
                    fcntl.ioctl(0, termios.TIOCSCTTY, 0)
                except Exception:
                    pass

            self.proc = subprocess.Popen(argv, stdin=slave, stdout=slave, stderr=slave, cwd=cwd, env=env,
                                         start_new_session=True, preexec_fn=_preexec, close_fds=True)
        except Exception:
            os.close(master)
            os.close(slave)
            raise
        os.close(slave)
        self.fd = master
        self.pid = self.proc.pid
        self._dec = codecs.getincrementaldecoder("utf-8")("replace")
        self._closed = False
        self._lock = threading.Lock()

    @staticmethod
    def _winsize(fd: int, rows: int, cols: int) -> None:
        import fcntl
        import struct
        import termios
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))

    def read(self) -> Optional[str]:
        import select
        while not self._closed:
            try:
                r, _, _ = select.select([self.fd], [], [], 0.5)
            except (OSError, ValueError):
                return None
            if not r:
                continue
            try:
                data = os.read(self.fd, 65536)
            except OSError:  # EIO once every slave fd is closed
                return None
            if not data:
                return None
            return self._dec.decode(data)
        return None

    def write(self, data: str) -> str:
        b = data.encode("utf-8", "replace")
        while b and not self._closed:
            n = os.write(self.fd, b)
            b = b[n:]
        return ""

    def resize(self, rows: int, cols: int) -> None:
        if not self._closed:
            self._winsize(self.fd, rows, cols)

    def isalive(self) -> bool:
        return self.proc.poll() is None

    def exit_code(self, timeout: float = 2.0) -> Optional[int]:
        try:
            return self.proc.wait(timeout=timeout)
        except Exception:
            return None

    def kill(self) -> None:
        with self._lock:
            if self._closed:
                return
            _posix_kill_tree(self.proc)
            self._closed = True
            try:
                os.close(self.fd)
            except OSError:
                pass


class _WinPty:
    """pywinpty (ConPTY / winpty) backend.  Every pywinpty call is guarded."""
    mode = "pty"

    def __init__(self, argv: List[str], cwd: str, env: Dict[str, str], rows: int, cols: int):
        wp = _import_winpty()
        if wp is None:
            raise RuntimeError("pywinpty 不可用")
        PtyProcess = wp.PtyProcess
        # pywinpty >= 2: prefer ConPTY explicitly (winpty-agent is slower and mangles some VT output)
        attempts: List[Tuple[str, Dict]] = []
        try:
            be = getattr(wp, "Backend", None)
            if be is not None and getattr(be, "ConPTY", None) is not None:
                attempts.append(("conpty", {"backend": be.ConPTY}))
        except Exception:
            pass
        attempts.append(("default", {}))
        err: Optional[BaseException] = None
        self.p = None
        for name, kw in attempts:
            try:
                self.p = PtyProcess.spawn(argv, cwd=cwd, env=env, dimensions=(rows, cols), **kw)
                self.pty_backend = name
                break
            except Exception as e:  # e.g. ConPTY missing (Windows < 10 1809) or old pywinpty without backend=
                err = e
        if self.p is None:
            raise err or RuntimeError("pywinpty spawn failed")
        try:
            self.pid = int(getattr(self.p, "pid", 0) or 0)
        except Exception:
            self.pid = 0
        self._job = _WinJob(self.pid)
        self._dec = codecs.getincrementaldecoder("utf-8")("replace")
        self._closed = False
        self._lock = threading.Lock()

    def read(self) -> Optional[str]:
        while not self._closed:
            try:
                data = self.p.read(65536)
            except EOFError:
                return None
            except Exception:
                return None
            if isinstance(data, bytes):
                data = self._dec.decode(data)
            if data:
                return data
            if not self.isalive():  # read() may return "" (keep-alive marker)
                return None
            time.sleep(0.01)
        return None

    def write(self, data: str) -> str:
        try:
            self.p.write(data)
        except Exception:
            pass
        return ""

    def resize(self, rows: int, cols: int) -> None:
        try:
            self.p.setwinsize(rows, cols)
        except Exception:
            pass

    def isalive(self) -> bool:
        try:
            return bool(self.p.isalive())
        except Exception:
            return False

    def exit_code(self, timeout: float = 2.0) -> Optional[int]:
        deadline = time.monotonic() + timeout
        while self.isalive() and time.monotonic() < deadline:
            time.sleep(0.05)
        try:
            code = getattr(self.p, "exitstatus", None)
            return int(code) if code is not None else None
        except Exception:
            return None

    def kill(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            _win_taskkill(self.pid)
            self._job.kill()
            try:
                self.p.close(force=True)
            except Exception:
                try:
                    self.p.terminate(force=True)
                except Exception:
                    pass


class _PipeProc:
    """Fallback: plain pipes (no PTY).  Implements a tiny line discipline (local echo, backspace,
    Enter, Ctrl+C) because nothing else echoes keystrokes; resize is ignored."""
    mode = "pipe"

    def __init__(self, argv: List[str], cwd: str, env: Dict[str, str], rows: int, cols: int,
                 encoding: Optional[str] = None):
        kw: Dict = dict(stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                        bufsize=0, cwd=cwd, env=env)
        if IS_WIN:
            kw["creationflags"] = CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP
            self.encoding = encoding or _oem_encoding()
        else:
            kw["start_new_session"] = True
            self.encoding = encoding or "utf-8"
        self.proc = subprocess.Popen(argv, **kw)
        self.pid = self.proc.pid
        self._job = _WinJob(self.pid) if IS_WIN else None
        self._dec = codecs.getincrementaldecoder(self.encoding)("replace")
        self._line: List[str] = []
        self._esc = ""
        self._last_in_cr = False
        self._last_out_cr = False
        self._closed = False
        self._lock = threading.Lock()
        self.echo_cb: Optional[Callable[[str], None]] = None

    def read(self) -> Optional[str]:
        fd = self.proc.stdout.fileno()
        if not IS_WIN:
            import select
            while not self._closed:
                try:
                    r, _, _ = select.select([fd], [], [], 0.5)
                except (OSError, ValueError):
                    return None
                if r:
                    break
            if self._closed:
                return None
        try:
            data = os.read(fd, 65536)
        except OSError:
            return None
        if not data:
            return None
        text = self._dec.decode(data)
        return self._crlf(text)

    def _crlf(self, text: str) -> str:
        """xterm needs CR LF; pipes give bare LF."""
        if not text:
            return text
        out = []
        prev_cr = self._last_out_cr
        for ch in text:
            if ch == "\n" and not prev_cr:
                out.append("\r")
            out.append(ch)
            prev_cr = ch == "\r"
        self._last_out_cr = prev_cr
        return "".join(out)

    def _send(self, s: str) -> None:
        try:
            self.proc.stdin.write(s.encode(self.encoding, "replace"))
        except (OSError, ValueError):
            pass

    def _interrupt(self) -> None:
        if IS_WIN:
            return  # CTRL_BREAK cannot reach a console-less child; the user can close the tab instead
        try:
            os.killpg(self.proc.pid, signal.SIGINT)
        except OSError:
            pass

    def write(self, data: str) -> str:
        echo: List[str] = []
        for ch in data:
            if self._esc:  # swallow escape sequences (arrow keys, ...)
                self._esc += ch
                if (len(self._esc) == 2 and ch not in "[O") or (len(self._esc) > 2 and (ch.isalpha() or ch == "~")) or len(self._esc) > 16:
                    self._esc = ""
                continue
            if ch == "\x1b":
                self._esc = ch
                continue
            if ch == "\n" and self._last_in_cr:
                self._last_in_cr = False
                continue
            self._last_in_cr = ch == "\r"
            if ch in "\r\n":
                echo.append("\r\n")
                if self.echo_cb is not None:  # echo must reach the terminal before the command's output
                    self.echo_cb("".join(echo))
                    echo = []
                self._send("".join(self._line) + "\n")
                self._line = []
            elif ch in "\x7f\b":
                if self._line:
                    self._line.pop()
                    echo.append("\b \b")
            elif ch == "\x03":
                echo.append("^C\r\n")
                self._line = []
                self._interrupt()
            elif ch == "\t" or ord(ch) >= 32:
                self._line.append(ch)
                echo.append(ch)
        return "".join(echo)

    def resize(self, rows: int, cols: int) -> None:
        return None

    def isalive(self) -> bool:
        return self.proc.poll() is None

    def exit_code(self, timeout: float = 2.0) -> Optional[int]:
        try:
            return self.proc.wait(timeout=timeout)
        except Exception:
            return None

    def kill(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            if IS_WIN:
                _win_taskkill(self.pid)
                if self._job:
                    self._job.kill()
                try:
                    self.proc.kill()
                except OSError:
                    pass
                try:
                    self.proc.wait(timeout=3)
                except Exception:
                    pass
            else:
                _posix_kill_tree(self.proc)
            for s in (self.proc.stdin, self.proc.stdout):
                try:
                    s.close()
                except Exception:
                    pass


def _pipe_hint(reason: str, detail: str = "") -> str:
    if reason == "forced":
        return "已通过环境变量 TV_IDE_TERM_MODE=pipe 强制使用管道模式（无 PTY：没有提示符编辑/颜色/全屏程序）"
    if reason == "no_winpty":
        return PIPE_HINT_NO_WINPTY
    if reason == "pty_failed":
        return "PTY 启动失败，已回退到管道模式" + (f"：{detail}" if detail else "")
    return "当前系统不支持 PTY，已使用管道模式"


def _spawn_terminal(cwd: str, rows: int, cols: int, profile: Optional[Dict] = None):
    """Spawn *profile* (default profile when None).  PTY first (ConPTY on Windows), pipes as fallback.
    The backend gets ``shell``, ``profile`` (id), ``profile_name`` and, in pipe mode, ``hint``."""
    if profile is None:
        profile = _find_profile(None)
        if profile is None:
            raise RuntimeError("没有可用的 shell")
    env = _term_env(profile)
    forced = os.environ.get("TV_IDE_TERM_MODE", "").strip().lower() == "pipe"
    mode = _term_mode()
    reason, detail = ("forced" if forced else ("no_winpty" if IS_WIN else "no_pty")), ""
    if mode == "pty":
        argv = _profile_argv(profile, "pty")
        try:
            b = _WinPty(argv, cwd, env, rows, cols) if IS_WIN else _PosixPty(argv, cwd, env, rows, cols)
            b.shell = _shell_display(argv)
            b.profile, b.profile_name, b.hint = profile["id"], profile["name"], ""
            return b
        except FileNotFoundError:
            raise
        except Exception as e:  # e.g. pywinpty DLL problem -> fall back to pipes
            reason, detail = "pty_failed", str(e)[:200]
    argv = _profile_argv(profile, "pipe")
    b = _PipeProc(argv, cwd, env, rows, cols, encoding=profile.get("pipe_encoding"))
    b.shell = _shell_display(argv)
    b.profile, b.profile_name, b.hint = profile["id"], profile["name"], _pipe_hint(reason, detail)
    return b


class _TermSession:
    def __init__(self, backend, loop: asyncio.AbstractEventLoop):
        self.backend = backend
        self.loop = loop
        self.event = asyncio.Event()
        self._chunks: "collections.deque[str]" = collections.deque()
        self._size = 0
        self._lock = threading.Lock()
        self.eof = False
        self.thread = threading.Thread(target=self._reader, name="tv-ide-term-reader", daemon=True)

    def start(self) -> None:
        self.thread.start()

    def _notify(self) -> None:
        try:
            self.loop.call_soon_threadsafe(self.event.set)
        except RuntimeError:  # loop closed
            pass

    def push(self, text: str) -> None:
        if not text:
            return
        with self._lock:
            self._chunks.append(text)
            self._size += len(text)
            while self._size > TERM_BUFFER_MAX and self._chunks:  # drop oldest
                extra = self._size - TERM_BUFFER_MAX
                head = self._chunks[0]
                if len(head) <= extra:
                    self._chunks.popleft()
                    self._size -= len(head)
                else:
                    self._chunks[0] = head[extra:]
                    self._size -= extra
        self._notify()

    def has_data(self) -> bool:
        with self._lock:
            return self._size > 0

    def take(self) -> str:
        with self._lock:
            s = "".join(self._chunks)
            self._chunks.clear()
            self._size = 0
        return s

    def _reader(self) -> None:
        while True:
            try:
                data = self.backend.read()
            except Exception:
                data = None
            if data is None:
                break
            self.push(data)
        self.eof = True
        self._notify()


def _clamp_int(v, lo: int, hi: int, default: int) -> int:
    try:
        return max(lo, min(hi, int(v)))
    except (TypeError, ValueError):
        return default


def _is_local_origin(origin: Optional[str]) -> bool:
    if not origin:
        return False
    try:
        u = urlsplit(origin.strip())
        return u.scheme in ("http", "https") and (u.hostname or "") in {"localhost", "127.0.0.1", "::1"}
    except ValueError:
        return False


async def _ws_close(ws: WebSocket, code: int, reason: str = "") -> None:
    r = reason.encode("utf-8")[:120].decode("utf-8", "ignore")
    try:
        await ws.close(code=code, reason=r)
    except Exception:
        pass


# ==============================================================================
# router factory
# ==============================================================================
def create_ide_router(*, project_root: str, storage_dir: str,
                      is_allowed_path: Callable[[str], bool],
                      check_security: Callable[[str, str, str, Optional[str], str], Optional[str]]) -> APIRouter:
    project_root = os.path.abspath(str(project_root))
    storage_dir = os.path.abspath(str(storage_dir))
    settings_file = os.path.join(storage_dir, "ide_settings.json")
    trash_root = os.path.join(storage_dir, "trash")

    state_lock = threading.Lock()
    state = {"root": project_root}
    term_lock = threading.Lock()
    term_sessions: Dict[int, object] = {}

    def _root_ok(p: str) -> bool:
        try:
            return os.path.isdir(p) and bool(is_allowed_path(p))
        except Exception:
            return False

    # -------- settings --------
    try:
        with open(settings_file, "r", encoding="utf-8") as f:
            saved = json.load(f).get("root")
        if isinstance(saved, str) and os.path.isabs(saved) and _root_ok(saved):
            state["root"] = os.path.abspath(saved)
    except (OSError, ValueError, AttributeError):
        pass

    def _save_settings() -> None:
        os.makedirs(storage_dir, exist_ok=True)
        data = {}
        try:
            with open(settings_file, "r", encoding="utf-8") as f:
                data = json.load(f) or {}
                if not isinstance(data, dict):
                    data = {}
        except (OSError, ValueError):
            pass
        data["root"] = state["root"]
        _atomic_write_bytes(settings_file, json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8"))

    def _root() -> str:
        with state_lock:
            r = state["root"]
        if not _root_ok(r):
            raise HTTPException(400, f"工作区目录不可用或不在允许范围内: {r}")
        return r

    # -------- path safety --------
    def _split_rel(rel: Optional[str]) -> List[str]:
        raw = (rel or "").strip()
        if "\x00" in raw:
            raise HTTPException(400, "非法路径")
        raw = raw.replace("\\", "/")
        if raw.startswith("/") or re.match(r"^[A-Za-z]:", raw) or os.path.isabs(raw):
            raise HTTPException(400, "路径必须是相对于工作区根目录的相对路径")
        parts = [p for p in raw.split("/") if p not in ("", ".")]
        for p in parts:
            if p == "..":
                raise HTTPException(400, "路径中不允许包含 ..")
            # Windows tricks: ADS (a:b), trailing dot/space, device names
            if ":" in p or p.endswith(" ") or p.endswith(".") or p.split(".")[0].upper() in _WIN_DEVICE_NAMES:
                raise HTTPException(400, f"非法文件名: {p}")
        return parts

    def _resolve(rel: Optional[str], *, write: bool = False, allow_root: bool = True) -> Tuple[str, str, str]:
        """-> (root, lexical absolute path, normalized relative path with '/').

        The lexical path is used for rename/delete (so a symlink itself is moved, not its target);
        its realpath is guaranteed to be inside the workspace."""
        root = _root()
        parts = _split_rel(rel)
        if not parts and not allow_root:
            raise HTTPException(400, "不能对工作区根目录执行此操作")
        full = os.path.join(root, *parts) if parts else root
        rroot = _norm(root)
        rfull = _norm(full)
        if not _within(rfull, rroot):
            raise HTTPException(403, "路径超出工作区范围")
        if parts:
            rparent = _norm(os.path.dirname(full))
            if not _within(rparent, rroot):
                raise HTTPException(403, "路径超出工作区范围")
        if write:
            if not parts:
                raise HTTPException(400, "不能写入工作区根目录")
            real_rel = os.path.relpath(rfull, rroot)
            real_parts = [] if real_rel == "." else re.split(r"[\\/]+", real_rel)
            if any(_is_git_component(p) for p in parts + real_parts):
                raise HTTPException(403, "禁止修改 .git 目录")
        return root, full, "/".join(parts)

    def _to_rel(root: str, abspath: str) -> str:
        return os.path.relpath(abspath, root).replace(os.sep, "/")

    def _walk_files(root: str, max_files: int, stats: Optional[Dict] = None) -> Iterator[Tuple[str, str, int]]:
        """Yield (rel, abs, size) for regular files, skipping .git / ignored / trash / reparse dirs.
        Sets stats["limit_hit"] when more than ``max_files`` files exist."""
        trash_norm = _norm(trash_root)
        stack = [root]
        seen = 0
        while stack:
            d = stack.pop()
            try:
                it = os.scandir(d)
            except OSError:
                continue
            subdirs = []
            with it:
                for e in it:
                    if not _name_ok(e.name):
                        continue
                    try:
                        if e.is_dir(follow_symlinks=False):
                            if (e.name.lower() == ".git" or e.name in IGNORED_DIRS or _is_reparse_dir(e)):
                                continue
                            if _norm(e.path) == trash_norm:
                                continue
                            subdirs.append(e.path)
                        elif e.is_file(follow_symlinks=False):
                            seen += 1
                            if seen > max_files:
                                if stats is not None:
                                    stats["limit_hit"] = True
                                return
                            try:
                                size = e.stat(follow_symlinks=False).st_size
                            except OSError:
                                size = 0
                            yield _to_rel(root, e.path), e.path, size
                    except OSError:
                        continue
            subdirs.sort(key=lambda p: os.path.basename(p).lower(), reverse=True)
            stack.extend(subdirs)

    def _move_to_trash(root: str, full: str, rel: str) -> str:
        rt = _norm(trash_root)
        rf = _norm(full)
        if _within(rt, rf) or _within(rf, rt):
            raise HTTPException(400, "不能删除回收站本身或包含回收站的目录")
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        base = os.path.join(trash_root, stamp)
        n = 0
        while os.path.exists(base):
            n += 1
            base = os.path.join(trash_root, f"{stamp}_{n}")
        dest = os.path.join(base, *rel.split("/"))
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.move(full, dest)
        return dest

    def _http_guard(request: Request) -> None:
        auth = request.headers.get("authorization", "")
        token = auth[7:].strip() if auth.lower().startswith("bearer ") else request.headers.get("x-tv-token", "").strip()
        reason = check_security(request.method, request.client.host if request.client else "",
                                request.headers.get("host", ""), request.headers.get("origin"), token)
        if reason:
            raise HTTPException(403, reason)

    router = APIRouter(prefix="/api/ide", tags=["ide"])
    deps = [Depends(_http_guard)]

    # ==========================================================================
    # workspace
    # ==========================================================================
    def _workspace_info() -> Dict:
        with state_lock:
            root = state["root"]
        if not _root_ok(root):
            root = project_root
        prefix = _git_info(root) if os.path.isdir(root) else None
        branch = _current_branch(root)[0] if prefix is not None else None
        mode = _term_mode()
        default_id, profiles = _detect_profiles()
        dp = next((p for p in profiles if p["id"] == default_id), None)
        return {"root": root, "name": os.path.basename(root.rstrip("\\/")) or root, "is_git": prefix is not None,
                "branch": branch, "terminal_mode": mode,
                "shell": _shell_display(_profile_argv(dp, mode)) if dp else "", "default_profile": default_id}

    @router.get("/workspace", dependencies=deps)
    def get_workspace():
        return _workspace_info()

    @router.post("/workspace", dependencies=deps)
    def set_workspace(body: WorkspaceBody):
        raw = (body.root or "").strip().strip('"')
        if not raw or not os.path.isabs(raw):
            raise HTTPException(400, "工作区路径必须是绝对路径")
        root = os.path.abspath(raw)
        if not os.path.isdir(root):
            raise HTTPException(404, f"目录不存在: {root}")
        if not is_allowed_path(root):
            raise HTTPException(403, f"该目录不在允许访问的白名单内: {root}")
        with state_lock:
            state["root"] = root
            try:
                _save_settings()
            except OSError as e:
                raise HTTPException(500, f"保存 IDE 设置失败: {e}")
        return _workspace_info()

    # ==========================================================================
    # files
    # ==========================================================================
    @router.get("/tree", dependencies=deps)
    def tree(path: str = ""):
        root, full, rel = _resolve(path)
        if not os.path.isdir(full):
            raise HTTPException(404 if not os.path.exists(full) else 400, "目录不存在")
        entries = []
        try:
            with os.scandir(full) as it:
                for e in it:
                    if e.name.lower() == ".git" or not _name_ok(e.name):
                        continue
                    try:
                        is_dir = e.is_dir()
                    except OSError:
                        is_dir = False
                    size = 0
                    if not is_dir:
                        try:
                            size = e.stat().st_size
                        except OSError:
                            size = 0
                    entries.append({"name": e.name, "path": f"{rel}/{e.name}" if rel else e.name,
                                    "type": "dir" if is_dir else "file", "size": size,
                                    "ignored": is_dir and e.name in IGNORED_DIRS})
        except OSError as e:
            raise _oserror(e)
        entries.sort(key=lambda x: (x["type"] != "dir", x["name"].lower(), x["name"]))
        truncated = len(entries) > TREE_MAX_ENTRIES
        return {"path": rel, "entries": entries[:TREE_MAX_ENTRIES], "truncated": truncated}

    @router.get("/files", dependencies=deps)
    def quick_open(q: str = "", limit: int = 50):
        root = _root()
        limit = _clamp_int(limit, 1, FILES_LIMIT_MAX, 50)
        ql = q.strip().lower().replace("\\", "/")
        compact = ql.replace(" ", "")

        def _subseq(needle: str, hay: str) -> bool:
            it = iter(hay)
            return all(c in it for c in needle)

        def scored():
            for rel, _abs, _size in _walk_files(root, FILES_SCAN_MAX):
                rl = rel.lower()
                if not ql:
                    yield (0, len(rel), rel)
                    continue
                base = rl.rsplit("/", 1)[-1]
                if base.startswith(ql):
                    s = 0
                elif ql in base:
                    s = 1
                elif ql in rl:
                    s = 2
                elif _subseq(compact, base):
                    s = 3
                elif _subseq(compact, rl):
                    s = 4
                else:
                    continue
                yield (s, len(rel), rel)

        best = heapq.nsmallest(limit, scored())
        return {"files": [r for _s, _l, r in best]}

    @router.get("/file", dependencies=deps)
    def read_file(path: str):
        root, full, rel = _resolve(path, allow_root=False)
        try:
            st = os.stat(full)
        except OSError as e:
            raise _oserror(e)
        if os.path.isdir(full):
            raise HTTPException(400, "该路径是目录")
        res = {"path": rel, "content": None, "size": st.st_size, "mtime": st.st_mtime,
               "binary": False, "too_large": False, "eol": "\n", "encoding": None}
        if st.st_size > READ_MAX_BYTES:
            res["too_large"] = True
            return res
        try:
            with open(full, "rb") as f:
                data = f.read(READ_MAX_BYTES + 1)
        except OSError as e:
            raise _oserror(e)
        if len(data) > READ_MAX_BYTES:
            res["too_large"] = True
            return res
        if b"\x00" in data[:BINARY_SNIFF_BYTES]:
            res["binary"] = True
            return res
        text, enc = _decode_text(data)
        res.update(content=text, eol=_detect_eol(text), encoding=enc)
        return res

    def _existing_encoding(full: str) -> Optional[str]:
        try:
            if os.path.getsize(full) > READ_MAX_BYTES:
                return None
            with open(full, "rb") as f:
                data = f.read(READ_MAX_BYTES + 1)
        except OSError:
            return None
        if b"\x00" in data[:BINARY_SNIFF_BYTES]:
            return None
        return _decode_text(data)[1]

    @router.put("/file", dependencies=deps)
    def write_file(body: WriteBody):
        root, full, rel = _resolve(body.path, write=True, allow_root=False)
        if len(body.content) > WRITE_MAX_CHARS:
            raise HTTPException(413, "文件内容过大")
        target = os.path.realpath(full)  # write through symlinks (target verified inside workspace)
        if os.path.isdir(target):
            raise HTTPException(400, "该路径是目录")
        exists = os.path.exists(target)
        if body.expected_mtime is not None:
            try:
                cur = os.stat(target).st_mtime
            except OSError:
                cur = None
            if cur is None or abs(cur - float(body.expected_mtime)) > MTIME_TOLERANCE:
                raise HTTPException(409, "文件已在磁盘上被修改")
        enc = (body.encoding or "").strip().lower() or (_existing_encoding(target) if exists else None) or "utf-8"
        if enc not in {"utf-8", "utf-8-sig", "gb18030", "gbk", "latin-1"}:
            enc = "utf-8"
        try:
            data = body.content.encode(enc)
        except UnicodeEncodeError:
            data = body.content.encode("utf-8")  # content no longer fits the old encoding
        try:
            os.makedirs(os.path.dirname(target), exist_ok=True)
            _atomic_write_bytes(target, data)
            st = os.stat(target)
        except OSError as e:
            raise _oserror(e)
        return {"path": rel, "size": st.st_size, "mtime": st.st_mtime}

    @router.post("/fs", dependencies=deps)
    def fs_op(body: FsBody):
        op = (body.op or "").strip()
        if op not in {"create_file", "create_dir", "rename", "delete"}:
            raise HTTPException(400, f"不支持的操作: {op}")
        root, full, rel = _resolve(body.path, write=True, allow_root=False)
        try:
            if op in ("create_file", "create_dir"):
                if os.path.lexists(full):
                    raise HTTPException(409, "目标已存在")
                if op == "create_dir":
                    os.makedirs(full)
                else:
                    os.makedirs(os.path.dirname(full), exist_ok=True)
                    with open(full, "xb"):
                        pass
                return {"ok": True, "path": rel}
            if op == "rename":
                if not body.new_path:
                    raise HTTPException(400, "重命名需要 new_path")
                _r, new_full, new_rel = _resolve(body.new_path, write=True, allow_root=False)
                if not os.path.lexists(full):
                    raise HTTPException(404, "源文件不存在")
                case_only = new_full != full and os.path.normcase(new_full) == os.path.normcase(full)
                if os.path.lexists(new_full) and not case_only:
                    raise HTTPException(409, "目标已存在")
                if _within(_norm(new_full), _norm(full)) and not case_only:
                    raise HTTPException(400, "不能把目录移动到它自身内部")
                os.makedirs(os.path.dirname(new_full), exist_ok=True)
                os.rename(full, new_full)
                return {"ok": True, "path": new_rel}
            # delete -> trash
            if not os.path.lexists(full):
                raise HTTPException(404, "文件或目录不存在")
            dest = _move_to_trash(root, full, rel)
            return {"ok": True, "path": rel, "trash": dest}
        except OSError as e:
            raise _oserror(e)

    @router.get("/search", dependencies=deps)
    def search(q: str = "", regex: str = "0", case: str = "0", glob: str = ""):
        root = _root()
        if not q:
            return {"results": [], "truncated": False, "files_scanned": 0}
        flags = 0 if _flag_true(case) else re.IGNORECASE
        try:
            rx = re.compile(q if _flag_true(regex) else re.escape(q), flags)
        except re.error as e:
            raise HTTPException(400, f"正则表达式错误: {e}")
        globs = [g.strip().replace("\\", "/").lower() for g in (glob or "").split(",") if g.strip()]
        results: List[Dict] = []
        truncated = False
        scanned = 0
        deadline = time.monotonic() + SEARCH_TIME_BUDGET
        stats: Dict = {}
        for rel, abspath, size in _walk_files(root, FILES_SCAN_MAX, stats):
            if globs:
                rl = rel.lower()
                base = rl.rsplit("/", 1)[-1]
                if not any(fnmatch.fnmatchcase(rl if "/" in g else base, g) for g in globs):
                    continue
            if size > SEARCH_FILE_MAX_BYTES:
                continue
            if time.monotonic() > deadline:
                truncated = True
                break
            try:
                with open(abspath, "rb") as f:
                    if b"\x00" in f.read(BINARY_SNIFF_BYTES):
                        continue
                    f.seek(0)
                    scanned += 1
                    for lineno, raw in enumerate(f, 1):
                        line = _decode_line(raw).rstrip("\r\n")
                        m = rx.search(line if len(line) <= SEARCH_LINE_SCAN_MAX else line[:SEARCH_LINE_SCAN_MAX])
                        if m:
                            results.append({"path": rel, "line": lineno, "col": m.start() + 1,
                                            "text": line[:SEARCH_TEXT_MAX]})
                            if len(results) >= SEARCH_MAX_RESULTS:
                                truncated = True
                                break
            except OSError:
                continue
            if truncated:
                break
        if stats.get("limit_hit"):
            truncated = True
        return {"results": results, "truncated": truncated, "files_scanned": scanned}

    # ==========================================================================
    # git
    # ==========================================================================
    def _status_entries(root: str, prefix: str, pathspecs: Optional[List[str]] = None) -> Tuple[Optional[str], List]:
        args = ["status", "--porcelain=v1", "-z", "-b", "--untracked-files=all"]
        if pathspecs:
            args += ["--"] + pathspecs
        cp = _git(root, args, lang_c=True)
        if cp.returncode != 0:
            raise HTTPException(400, _err(cp))
        header, entries = _parse_porcelain_z(cp.stdout or b"")
        out = []
        for x, y, p, orig in entries:
            sp = _strip_prefix(p, prefix)
            if sp is None:
                continue
            out.append((x, y, sp, _strip_prefix(orig, prefix) if orig else None))
        return header, out

    @router.get("/git/status", dependencies=deps)
    def git_status():
        root = _root()
        prefix = _git_info(root)
        if prefix is None:
            return {"is_repo": False, "branch": None, "upstream": None, "ahead": 0, "behind": 0, "files": []}
        header, entries = _status_entries(root, prefix)
        info = _parse_branch_header(header or "")
        if info["detached"] or not info["branch"]:
            info["branch"] = _current_branch(root)[0]
        files = [{"path": p, "index": x, "worktree": y, "orig_path": o} for x, y, p, o in entries[:GIT_STATUS_MAX_FILES]]
        return {"is_repo": True, "branch": info["branch"], "upstream": info["upstream"], "ahead": info["ahead"],
                "behind": info["behind"], "detached": info["detached"], "files": files,
                "truncated": len(entries) > GIT_STATUS_MAX_FILES}

    def _is_tracked(root: str, rel: str) -> bool:
        cp = _git(root, ["ls-files", "--error-unmatch", "--", rel], timeout=15)
        return cp.returncode == 0

    @router.get("/git/diff", dependencies=deps)
    def git_diff(path: str = "", staged: str = "0"):
        root = _root()
        _require_repo(root)
        is_staged = _flag_true(staged)
        rel = ""
        if path:
            _r, full, rel = _resolve(path)
        if rel and not is_staged and os.path.isfile(full) and not _is_tracked(root, rel):
            data, trunc, rc = _git_capped(root, ["diff", "--no-index", "--no-color", "--no-ext-diff", "--", "/dev/null", rel], DIFF_MAX_BYTES)
            return {"diff": data.decode("utf-8", "replace"), "truncated": trunc}
        args = ["diff", "--no-color", "--no-ext-diff", "--relative"]
        if is_staged:
            args.append("--cached")
        args.append("--")
        if rel:
            args.append(rel)
        data, trunc, rc = _git_capped(root, args, DIFF_MAX_BYTES)
        if rc not in (0, 1):
            cp = _git(root, args)
            raise HTTPException(400, _err(cp))
        return {"diff": data.decode("utf-8", "replace"), "truncated": trunc}

    @router.get("/git/file_at", dependencies=deps)
    def git_file_at(path: str, ref: str = "HEAD"):
        root = _root()
        _require_repo(root)
        ref = _validate_ref(ref)
        _r, _full, rel = _resolve(path, allow_root=False)
        spec = f"{ref}:./{rel}"
        chk = _git(root, ["cat-file", "-e", spec], timeout=15)
        if chk.returncode != 0:
            raise HTTPException(404, f"该文件不存在于 {ref}")
        data, trunc, rc = _git_capped(root, ["cat-file", "blob", spec], READ_MAX_BYTES)
        if rc != 0:
            raise HTTPException(404, f"该文件不存在于 {ref}")
        if trunc:
            return {"content": None, "too_large": True, "binary": False}
        if b"\x00" in data[:BINARY_SNIFF_BYTES]:
            return {"content": None, "too_large": False, "binary": True}
        return {"content": _decode_text(data)[0], "too_large": False, "binary": False}

    def _rels(paths: List[str]) -> List[str]:
        out = []
        for p in paths or []:
            _r, _f, rel = _resolve(p, allow_root=False)
            out.append(rel)
        return out

    @router.post("/git/stage", dependencies=deps)
    def git_stage(body: PathsBody):
        root = _root()
        _require_repo(root)
        rels = _rels(body.paths)
        for chunk in (_chunks(rels) if rels else [["."]]):
            cp = _git(root, ["add", "-A", "--"] + chunk)
            if cp.returncode != 0:
                raise HTTPException(400, _err(cp))
        return {"ok": True}

    @router.post("/git/unstage", dependencies=deps)
    def git_unstage(body: PathsBody):
        root = _root()
        _require_repo(root)
        rels = _rels(body.paths)
        for chunk in (_chunks(rels) if rels else [["."]]):
            cp = _git(root, ["reset", "-q", "--"] + chunk)
            if cp.returncode not in (0, 1):
                raise HTTPException(400, _err(cp))
        return {"ok": True}

    @router.post("/git/discard", dependencies=deps)
    def git_discard(body: PathsBody):
        root = _root()
        prefix = _require_repo(root)
        rels = _rels(body.paths)
        entries = []
        for chunk in (_chunks(rels) if rels else [None]):
            entries += _status_entries(root, prefix, chunk)[1]
        tracked = sorted({p for x, y, p, _o in entries if not (x == "?" and y == "?") and y not in (" ", "?")})
        untracked = sorted({p for x, y, p, _o in entries if x == "?" and y == "?"})
        for chunk in _chunks(tracked):
            cp = _git(root, ["checkout", "-q", "--"] + chunk)  # restore worktree from the index
            if cp.returncode != 0:
                raise HTTPException(400, _err(cp))
        for rel in untracked:
            _r, full, rel2 = _resolve(rel, write=True, allow_root=False)
            if os.path.lexists(full):
                try:
                    _move_to_trash(root, full, rel2)
                except OSError as e:
                    raise _oserror(e)
        return {"ok": True}

    @router.post("/git/commit", dependencies=deps)
    def git_commit(body: CommitBody):
        root = _root()
        _require_repo(root)
        msg = (body.message or "").strip()
        if not msg:
            raise HTTPException(400, "提交说明不能为空")
        args = ["commit", "-q", "-F", "-"] + (["--amend"] if body.amend else [])
        cp = _git(root, args, input=(msg + "\n").encode("utf-8"), timeout=GIT_NET_TIMEOUT)
        if cp.returncode != 0:
            raise HTTPException(400, _err(cp))
        h = _out(_git(root, ["rev-parse", "HEAD"])).strip()
        return {"ok": True, "hash": h, "short": _out(_git(root, ["rev-parse", "--short", "HEAD"])).strip()}

    @router.get("/git/log", dependencies=deps)
    def git_log(limit: int = 50, path: str = ""):
        root = _root()
        _require_repo(root)
        limit = _clamp_int(limit, 1, LOG_LIMIT_MAX, 50)
        args = ["log", f"-n{limit}", "--no-color", "--format=%H%x1f%h%x1f%an%x1f%aI%x1f%s%x1e"]
        if path:
            _r, full, rel = _resolve(path, allow_root=False)
            if os.path.isfile(full):
                args.append("--follow")
            args += ["--", rel]
        cp = _git(root, args)
        if cp.returncode != 0:
            if _git(root, ["rev-parse", "-q", "--verify", "HEAD"], timeout=10).returncode != 0:
                return {"commits": []}  # unborn branch
            raise HTTPException(400, _err(cp))
        commits = []
        for rec in _out(cp).split("\x1e"):
            f = rec.strip("\n").split("\x1f")
            if len(f) >= 5:
                commits.append({"hash": f[0], "short": f[1], "author": f[2], "date": f[3], "subject": f[4]})
        return {"commits": commits}

    @router.get("/git/show", dependencies=deps)
    def git_show(hash: str):
        root = _root()
        _require_repo(root)
        h = _validate_ref(hash, "提交哈希")
        cp = _git(root, ["rev-parse", "-q", "--verify", f"{h}^{{commit}}"], timeout=10)
        if cp.returncode != 0:
            raise HTTPException(404, f"提交不存在: {h}")
        full_hash = _out(cp).strip()
        cp = _git(root, ["show", "-s", "--no-color", "--format=%H%x1f%an%x1f%aI%x1f%s", full_hash])
        if cp.returncode != 0:
            raise HTTPException(400, _err(cp))
        meta = _out(cp).strip("\n").split("\x1f")
        while len(meta) < 4:
            meta.append("")
        ns = _git(root, ["show", "--no-color", "--format=", "--name-status", "-z", "--relative", full_hash])
        files = _parse_name_status_z(ns.stdout or b"") if ns.returncode == 0 else []
        data, trunc, _rc = _git_capped(root, ["show", "--no-color", "--no-ext-diff", "--format=", "--patch", "--relative", full_hash], SHOW_DIFF_MAX_BYTES)
        return {"hash": meta[0], "subject": meta[3], "author": meta[1], "date": meta[2], "files": files,
                "diff": data.decode("utf-8", "replace"), "truncated": trunc}

    @router.post("/git/revert", dependencies=deps)
    def git_revert(body: HashBody):
        root = _root()
        _require_repo(root)
        h = _validate_ref(body.hash, "提交哈希")
        cp = _git(root, ["revert", "--no-edit", h], timeout=GIT_NET_TIMEOUT)
        return _ok_or_400(cp)

    @router.post("/git/restore_file", dependencies=deps)
    def git_restore_file(body: RestoreBody):
        root = _root()
        _require_repo(root)
        ref = _validate_ref(body.ref or "HEAD")
        _r, _full, rel = _resolve(body.path, write=True, allow_root=False)
        cp = _git(root, ["checkout", ref, "--", rel])
        if cp.returncode != 0:
            raise HTTPException(400, _err(cp))
        return {"ok": True}

    @router.get("/git/branches", dependencies=deps)
    def git_branches():
        root = _root()
        _require_repo(root)
        current, _detached = _current_branch(root)
        cp = _git(root, ["for-each-ref", "--format=%(refname:short)%1f%(HEAD)%1f%(upstream:short)", "refs/heads"], lang_c=True)
        branches = []
        for line in _out(cp).splitlines():
            f = line.split("\x1f")
            if len(f) >= 3 and f[0]:
                branches.append({"name": f[0], "current": f[1].strip() == "*", "upstream": f[2] or None})
        if current and not _detached and not any(b["name"] == current for b in branches):
            branches.insert(0, {"name": current, "current": True, "upstream": None})  # unborn branch
        remotes = []
        cp = _git(root, ["config", "--get-regexp", r"^remote\..*\.url$"], lang_c=True)
        for line in _out(cp).splitlines():
            key, _, url = line.partition(" ")
            if key.startswith("remote.") and key.endswith(".url"):
                remotes.append({"name": key[len("remote."):-len(".url")], "url": url.strip()})
        return {"current": current, "branches": branches, "remotes": remotes}

    def _validate_branch_name(root: str, name: str) -> str:
        n = _validate_ref(name, "分支名")
        if n == "HEAD" or n.startswith("/"):
            raise HTTPException(400, f"非法的分支名: {name!r}")
        cp = _git(root, ["check-ref-format", "--branch", n], timeout=10)
        if cp.returncode != 0:
            raise HTTPException(400, f"非法的分支名: {name!r}")
        return n

    @router.post("/git/branch", dependencies=deps)
    def git_branch(body: BranchBody):
        root = _root()
        _require_repo(root)
        name = _validate_branch_name(root, body.name)
        cp = _git(root, ["checkout", "-q", "-b", name] if body.checkout else ["branch", name])
        if cp.returncode != 0:
            raise HTTPException(400, _err(cp))
        return {"ok": True, "branch": name}

    @router.post("/git/checkout", dependencies=deps)
    def git_checkout(body: CheckoutBody):
        root = _root()
        _require_repo(root)
        name = _validate_ref(body.branch, "分支名")
        cp = _git(root, ["checkout", "-q", name, "--"])
        if cp.returncode != 0:
            raise HTTPException(400, _err(cp))
        return {"ok": True, "branch": _current_branch(root)[0]}

    def _net_target(root: str) -> Tuple[str, str, bool]:
        remotes = _remotes(root)
        if not remotes:
            raise HTTPException(400, "未配置远程仓库")
        branch, detached = _current_branch(root)
        if detached or not branch:
            raise HTTPException(400, "当前处于分离 HEAD 状态，无法推送/拉取")
        has_up = _git(root, ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"], timeout=10).returncode == 0
        remote = "origin" if "origin" in remotes else remotes[0]
        if not REMOTE_RE.match(remote) or remote.startswith("-"):
            raise HTTPException(400, f"非法的远程仓库名: {remote!r}")
        return remote, branch, has_up

    @router.post("/git/push", dependencies=deps)
    def git_push():
        root = _root()
        _require_repo(root)
        remote, branch, has_up = _net_target(root)
        args = ["push"] if has_up else ["push", "-u", remote, branch]
        cp = _git(root, args, timeout=GIT_NET_TIMEOUT)
        return _ok_or_400(cp)

    @router.post("/git/pull", dependencies=deps)
    def git_pull():
        root = _root()
        _require_repo(root)
        remote, branch, has_up = _net_target(root)
        args = ["pull", "--no-edit"] if has_up else ["pull", "--no-edit", remote, branch]
        cp = _git(root, args, timeout=GIT_NET_TIMEOUT)
        return _ok_or_400(cp)

    @router.post("/git/init", dependencies=deps)
    def git_init():
        root = _root()
        cp = _git(root, ["init"])
        if cp.returncode != 0:
            raise HTTPException(400, _err(cp))
        return {"ok": True}

    # ==========================================================================
    # terminal
    # ==========================================================================
    def _kill_all_sessions() -> None:
        with term_lock:
            items = list(term_sessions.values())
        for b in items:
            try:
                b.kill()
            except Exception:
                pass

    atexit.register(_kill_all_sessions)

    def _ws_reason(ws: WebSocket) -> Optional[str]:
        auth = ws.headers.get("authorization", "")
        token = (ws.query_params.get("token")
                 or (auth[7:].strip() if auth.lower().startswith("bearer ") else "")
                 or ws.headers.get("x-tv-token", "").strip())
        origin = ws.headers.get("origin")
        try:
            reason = check_security("GET", ws.client.host if ws.client else "", ws.headers.get("host", ""), origin, token or "")
        except Exception:
            reason = "安全校验失败"
        if reason:
            return reason
        if not _is_local_origin(origin):
            return f"终端 WebSocket 仅允许本机页面连接 (Origin: {origin})"
        return None

    @router.get("/term/profiles", dependencies=deps)
    def term_profiles():
        default_id, profiles = _detect_profiles()
        return {"default": default_id, "profiles": [_public_profile(p) for p in profiles],
                "mode": _term_mode(), "max_terminals": MAX_TERMINALS}

    @router.websocket("/term")
    async def terminal(websocket: WebSocket):
        reason = _ws_reason(websocket)
        await websocket.accept()  # accept first so the browser sees our close code
        if reason:
            await _ws_close(websocket, 4403, "forbidden")
            return
        qp = websocket.query_params
        cols = _clamp_int(qp.get("cols"), 2, 500, 120)
        rows = _clamp_int(qp.get("rows"), 2, 300, 30)
        try:
            _r, cwd, _rel = _resolve(qp.get("cwd") or "")
            if not os.path.isdir(cwd):
                raise HTTPException(400, "cwd 不是目录")
        except HTTPException:
            await _ws_close(websocket, 4400, "bad cwd")
            return
        profile = await asyncio.to_thread(_find_profile, qp.get("profile"))
        if profile is None:
            await _ws_close(websocket, 4400, "unknown profile")
            return
        slot = object()
        with term_lock:
            full = len(term_sessions) >= MAX_TERMINALS
            if not full:
                term_sessions[id(slot)] = slot
        if full:
            await _ws_close(websocket, 4429, "too many terminals")
            return
        backend = None
        try:
            try:
                backend = await asyncio.to_thread(_spawn_terminal, cwd, rows, cols, profile)
            except Exception as e:
                try:
                    await websocket.send_json({"type": "output", "data": f"\r\n启动终端失败: {e}\r\n"})
                except Exception:
                    pass
                await _ws_close(websocket, 1011, "spawn failed")
                return
            with term_lock:
                term_sessions[id(slot)] = backend
            session = _TermSession(backend, asyncio.get_running_loop())
            if hasattr(backend, "echo_cb"):
                backend.echo_cb = session.push
            session.start()
            info = {"type": "info", "mode": backend.mode, "shell": backend.shell, "pid": backend.pid,
                    "profile": getattr(backend, "profile", ""), "name": getattr(backend, "profile_name", ""),
                    "cwd": _rel or "", "max_terminals": MAX_TERMINALS}
            if backend.mode == "pipe":
                info["hint"] = getattr(backend, "hint", "") or _pipe_hint("")
            if getattr(backend, "pty_backend", None):
                info["backend"] = backend.pty_backend
            await websocket.send_json(info)

            async def pump_out():
                dead_since = None
                while True:
                    try:
                        await asyncio.wait_for(session.event.wait(), timeout=0.5)
                    except asyncio.TimeoutError:
                        pass
                    session.event.clear()
                    if session.has_data():
                        await asyncio.sleep(TERM_BATCH_SEC)  # batch output (~60 msgs/s max)
                        data = session.take()
                        if data:
                            await websocket.send_json({"type": "output", "data": data})
                    if session.eof and not session.has_data():
                        break
                    if not backend.isalive():
                        # shell exited but a background child may still hold the pty open
                        dead_since = dead_since or time.monotonic()
                        if time.monotonic() - dead_since > 1.0 and not session.has_data():
                            break
                code = await asyncio.to_thread(backend.exit_code, 2.0)
                await websocket.send_json({"type": "exit", "code": code})

            async def pump_in():
                while True:
                    msg = await websocket.receive()
                    if msg.get("type") == "websocket.disconnect":
                        return
                    raw = msg.get("text")
                    if raw is None and msg.get("bytes") is not None:
                        raw = msg["bytes"].decode("utf-8", "replace")
                    try:
                        obj = json.loads(raw or "")
                    except ValueError:
                        continue
                    if not isinstance(obj, dict):
                        continue
                    t = obj.get("type")
                    if t == "input":
                        data = obj.get("data")
                        if isinstance(data, str) and data:
                            echo = await asyncio.to_thread(backend.write, data[:TERM_INPUT_MAX])
                            if echo:
                                session.push(echo)
                    elif t == "resize":
                        c = _clamp_int(obj.get("cols"), 2, 500, cols)
                        r = _clamp_int(obj.get("rows"), 2, 300, rows)
                        await asyncio.to_thread(backend.resize, r, c)

            tasks = {asyncio.create_task(pump_out()), asyncio.create_task(pump_in())}
            try:
                done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for t in tasks:
                    t.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            if backend is not None:
                try:
                    await asyncio.to_thread(backend.kill)
                except BaseException:
                    backend.kill()  # cancelled (server shutdown): kill synchronously
                    raise
                finally:
                    with term_lock:
                        term_sessions.pop(id(slot), None)
            else:
                with term_lock:
                    term_sessions.pop(id(slot), None)
            await _ws_close(websocket, 1000)

    # Workspace helpers for other in-process consumers (app/agent built-in tools). They are the very
    # same closures the HTTP handlers use, so path guards / walk rules / search limits stay identical.
    # All of them raise fastapi.HTTPException on invalid input, exactly like the endpoints.
    router.workspace = WorkspaceHelpers(
        root=_root, resolve=_resolve, to_rel=_to_rel, walk_files=_walk_files, search=search,
        read_file=read_file, move_to_trash=_move_to_trash, trash_root=trash_root,
    )
    return router


class WorkspaceHelpers:
    """Plain attribute bag exposed as ``create_ide_router(...).workspace``."""

    def __init__(self, **kw):
        self.__dict__.update(kw)


def get_workspace_helpers(router: APIRouter) -> "WorkspaceHelpers":
    """Return the workspace helpers of a router made by :func:`create_ide_router`."""
    return router.workspace  # type: ignore[attr-defined]
