"""Subprocess helpers shared by run_command and the MCP stdio transport.

Everything here is thread/Popen based (no asyncio subprocesses) so it works on any event loop,
including uvicorn's SelectorEventLoop on Windows, which cannot spawn asyncio subprocesses.
"""
from __future__ import annotations

import codecs
import os
import signal
import subprocess
import time
from typing import Dict, Optional

IS_WIN = os.name == "nt"
CREATE_NO_WINDOW = 0x08000000
CREATE_NEW_PROCESS_GROUP = 0x00000200


def popen_kwargs(new_group: bool = True) -> Dict:
    """Hide console windows on Windows and put the child in its own process group/session."""
    if IS_WIN:
        flags = CREATE_NO_WINDOW | (CREATE_NEW_PROCESS_GROUP if new_group else 0)
        return {"creationflags": flags}
    return {"start_new_session": True} if new_group else {}


def kill_tree(proc: subprocess.Popen, grace: float = 0.0) -> None:
    """Kill ``proc`` and everything it spawned. Best effort, never raises."""
    if proc is None:
        return
    try:
        if proc.poll() is not None and not IS_WIN:
            # leader gone, but children in its session may still live
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                pass
            return
    except Exception:
        pass
    if IS_WIN:
        try:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], stdin=subprocess.DEVNULL,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15,
                           creationflags=CREATE_NO_WINDOW)
        except Exception:
            pass
        try:
            proc.kill()
        except Exception:
            pass
    else:
        if grace > 0:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except OSError:
                pass
            deadline = time.monotonic() + grace
            while time.monotonic() < deadline and proc.poll() is None:
                time.sleep(0.02)
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            try:
                proc.kill()
            except Exception:
                pass
    try:
        proc.wait(timeout=5)
    except Exception:
        pass


def oem_encoding() -> str:
    if IS_WIN:
        try:
            import ctypes
            cp = ctypes.windll.kernel32.GetOEMCP()  # type: ignore[attr-defined]
            codecs.lookup(f"cp{cp}")
            return f"cp{cp}"
        except Exception:
            pass
    import locale
    return locale.getpreferredencoding(False) or "utf-8"


def decode_output(data: bytes, fallback: Optional[str] = None) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        pass
    for enc in (fallback or oem_encoding(), "gb18030"):
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", errors="replace")
