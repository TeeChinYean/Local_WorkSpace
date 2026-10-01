"""User-activity tracking for idle-time background work.

``ActivityTracker`` counts in-flight user LLM requests (/api/chat, /v1/chat/completions, IDE AI edit, the
agent loop which runs inside /api/chat) and remembers when the last one ended. Listeners (the KG worker's
``preempt``) fire synchronously at the start of every user request so a background llama stream is
cancelled before the user's request reaches llama-server (single slot).

``ActivityMiddleware`` is a pure ASGI middleware: it wraps the whole response including streaming bodies,
so a long SSE answer keeps the gateway "busy" until the last byte is sent or the client disconnects.
"""
from __future__ import annotations

import threading
import time
from typing import Callable, List


class ActivityTracker:
    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._lock = threading.Lock()
        self._active = 0
        self._last = clock()          # startup counts as activity: never extract in the first idle window
        self._listeners: List[Callable[[], None]] = []

    @property
    def active(self) -> int:
        return self._active

    @property
    def last_activity(self) -> float:
        return self._last

    def add_listener(self, fn: Callable[[], None]) -> None:
        self._listeners.append(fn)

    def begin(self) -> None:
        with self._lock:
            self._active += 1
            self._last = self._clock()
        for fn in list(self._listeners):
            try:
                fn()
            except Exception:
                pass

    def end(self) -> None:
        with self._lock:
            self._active = max(0, self._active - 1)
            self._last = self._clock()

    def touch(self) -> None:
        with self._lock:
            self._last = self._clock()

    def idle_for(self) -> float:
        with self._lock:
            if self._active > 0:
                return 0.0
            return max(0.0, self._clock() - self._last)


class ActivityMiddleware:
    def __init__(self, app, tracker: ActivityTracker, match: Callable[[str, str], bool]):
        self.app = app
        self.tracker = tracker
        self.match = match

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http" or not self.match(scope.get("method", ""), scope.get("path", "")):
            await self.app(scope, receive, send)
            return
        self.tracker.begin()
        try:
            await self.app(scope, receive, send)
        finally:
            self.tracker.end()


USER_LLM_PATHS = ("/api/chat", "/v1/chat/completions", "/v1/completions")
USER_LLM_PREFIXES = ("/api/ide/ai/",)


def is_user_llm_request(method: str, path: str) -> bool:
    if (method or "").upper() != "POST":
        return False
    p = (path or "").rstrip("/") or "/"
    return p in USER_LLM_PATHS or any((path or "").startswith(x) for x in USER_LLM_PREFIXES)


__all__ = ["ActivityTracker", "ActivityMiddleware", "is_user_llm_request"]
