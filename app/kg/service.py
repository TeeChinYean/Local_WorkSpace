"""KGService: one per gateway process — settings, store, queueing, retrieval cache and the idle worker."""
from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np

from . import retrieval
from .activity import ActivityTracker
from .extract import KGWorker
from .store import KGStore

MIN_ENTRY_CHARS = 40
IDLE_MIN, IDLE_MAX = 5, 3600
DEFAULT_SETTINGS: Dict[str, Any] = {"enabled": True, "paused": False, "idle_seconds": 20}
VEC_BATCH = 16

_FILE_RE = re.compile(r"^【文件[^】\-]*-\s*(.+?)(?:\s\(\d+/\d+\))?】")
_WEB_FILE_PREFIXES = ("🌐", "web_", "url_")


@dataclass
class KGDeps:
    """Callables injected by app/main.py (the package never imports main)."""
    get_storage_dir: Callable[[], str]
    get_llm_base: Callable[[], str] = lambda: "http://127.0.0.1:18089/v1"
    get_client: Optional[Callable[[], Any]] = None                  # -> httpx.AsyncClient (llama auth headers)
    get_model: Callable[[], str] = lambda: ""
    get_n_ctx: Callable[[], Optional[int]] = lambda: None           # live n_ctx, None when llama is unreachable
    is_switching: Callable[[], bool] = lambda: False
    embed: Optional[Callable[[List[str]], np.ndarray]] = None       # sync, L2-normalised float32 rows
    count_tokens: Callable[[str], int] = lambda s: len(s or "")
    get_memory_history: Callable[[], List[str]] = lambda: []
    check_security: Optional[Callable[[str, str, str, Optional[str], str], Optional[str]]] = None


def classify_entry(text: str, index: int) -> Tuple[str, str]:
    """Memory entry -> (source_kind, source_ref). chat/web refs are memory_history indices, file refs are filenames."""
    t = (text or "").lstrip()
    if t.startswith("【联网知识记忆"):
        return "web", str(index)
    if t.startswith("【文件"):
        m = _FILE_RE.match(t)
        fn = m.group(1).strip() if m else ""
        kind = "web" if fn.lower().startswith(_WEB_FILE_PREFIXES) else "file"
        return kind, fn or str(index)
    return "chat", str(index)


def _atomic_write_json(path: str, obj: Any) -> None:
    d = os.path.dirname(path) or "."
    os.makedirs(d, exist_ok=True)
    tmp = os.path.join(d, f".{os.path.basename(path)}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        for i in range(4 if os.name == "nt" else 1):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                if i == 3 or os.name != "nt":
                    raise
                time.sleep(0.05)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def normalize_settings(raw: Any) -> Dict[str, Any]:
    s = dict(DEFAULT_SETTINGS)
    if isinstance(raw, dict):
        if isinstance(raw.get("enabled"), bool):
            s["enabled"] = raw["enabled"]
        if isinstance(raw.get("paused"), bool):
            s["paused"] = raw["paused"]
        try:
            s["idle_seconds"] = max(IDLE_MIN, min(IDLE_MAX, int(raw.get("idle_seconds", s["idle_seconds"]))))
        except (TypeError, ValueError):
            pass
    return s


class KGService:
    def __init__(self, deps: KGDeps, activity: Optional[ActivityTracker] = None):
        self.deps = deps
        self.activity = activity or ActivityTracker()
        self._lock = threading.RLock()
        self._store: Optional[KGStore] = None
        self._settings_cache: Optional[Tuple[str, float, Dict[str, Any]]] = None
        self._cache: Dict[str, Any] = {}
        self.worker = KGWorker(self)

    # ------------------------------------------------------------------ paths / store
    @property
    def kg_dir(self) -> str:
        return os.path.join(os.path.abspath(str(self.deps.get_storage_dir())), "kg")

    @property
    def db_path(self) -> str:
        return os.path.join(self.kg_dir, "graph.db")

    @property
    def settings_path(self) -> str:
        return os.path.join(self.kg_dir, "settings.json")

    @property
    def store(self) -> KGStore:
        path = os.path.abspath(self.db_path)
        with self._lock:
            if self._store is None or self._store.path != path:
                if self._store is not None:
                    self._store.close()
                self._store = KGStore(path)
                self._cache = {}
            return self._store

    def close(self) -> None:
        with self._lock:
            if self._store is not None:
                self._store.close()
                self._store = None

    # ------------------------------------------------------------------ settings
    def settings(self) -> Dict[str, Any]:
        path = self.settings_path
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            mtime = -1.0
        c = self._settings_cache
        if c and c[0] == path and c[1] == mtime:
            return dict(c[2])
        raw: Any = {}
        if mtime >= 0:
            try:
                with open(path, "r", encoding="utf-8") as f:
                    raw = json.load(f)
            except (OSError, ValueError):
                raw = {}
        s = normalize_settings(raw)
        self._settings_cache = (path, mtime, s)
        return dict(s)

    def update_settings(self, enabled: Optional[bool] = None, paused: Optional[bool] = None,
                        idle_seconds: Optional[int] = None) -> Dict[str, Any]:
        with self._lock:
            s = self.settings()
            if enabled is not None:
                s["enabled"] = bool(enabled)
            if paused is not None:
                s["paused"] = bool(paused)
            if idle_seconds is not None:
                s["idle_seconds"] = int(idle_seconds)
            s = normalize_settings(s)
            _atomic_write_json(self.settings_path, s)
            self._settings_cache = None
        if not s["enabled"] or s["paused"]:
            self.worker.preempt()
        return s

    # ------------------------------------------------------------------ queueing
    def entries_to_jobs(self, entries: Sequence[str], start_index: int) -> List[Tuple[str, str, str]]:
        jobs = []
        for i, text in enumerate(entries):
            if not text or len(text.strip()) < MIN_ENTRY_CHARS:
                continue
            kind, ref = classify_entry(text, start_index + i)
            jobs.append((kind, ref, text))
        return jobs

    def enqueue_memory(self, entries: Sequence[str], start_index: int) -> int:
        """Called after new memory entries were appended at memory_history[start_index:]."""
        if not self.settings()["enabled"]:
            return 0
        jobs = self.entries_to_jobs(entries, start_index)
        return self.store.enqueue_many(jobs) if jobs else 0

    def rebuild(self, mode: str = "missing") -> int:
        history = list(self.deps.get_memory_history() or [])
        store = self.store
        if mode == "all":
            self.worker.preempt()
            store.clear_graph()
            store.clear_queue()
            store.set_meta("last_error", "")
        return store.enqueue_many(self.entries_to_jobs(history, 0))

    # ------------------------------------------------------------------ status
    def status(self) -> Dict[str, Any]:
        s = self.settings()
        store = self.store
        counts = store.counts()
        ok, reason = self.worker.can_run()
        running = self.worker.running_job is not None
        last_run = store.get_meta("last_run_at")
        return {
            "enabled": s["enabled"], "paused": s["paused"], "idle_seconds": s["idle_seconds"],
            "running": running,
            "waiting": "" if running else (reason if not ok else "ready"),
            "entities": counts["entities"], "relations": counts["relations"],
            "queue": store.queue_counts(),
            "last_error": store.get_meta("last_error") or "",
            "last_error_at": _float_or_none(store.get_meta("last_error_at")),
            "last_run_at": _float_or_none(last_run),
            "idle_for": round(self.activity.idle_for(), 1),
        }

    def summary(self) -> Dict[str, Any]:
        """Tiny, cheap subset for /api/status (sidebar status line)."""
        try:
            s = self.settings()
            c = self.store.counts()
            q = self.store.queue_counts()
            return {"enabled": s["enabled"], "paused": s["paused"], "running": self.worker.running_job is not None,
                    "entities": c["entities"], "relations": c["relations"], "pending": q["pending"] + q["running"]}
        except Exception:
            return {"enabled": False, "paused": False, "running": False, "entities": 0, "relations": 0, "pending": 0}

    # ------------------------------------------------------------------ embeddings
    def fill_name_vectors(self, limit: int = 64) -> int:
        embed = self.deps.embed
        if embed is None:
            return 0
        store = self.store
        todo = store.entities_missing_vectors(limit)
        done = 0
        for i in range(0, len(todo), VEC_BATCH):
            part = todo[i:i + VEC_BATCH]
            vecs = np.asarray(embed([name for _, name in part]), dtype=np.float32)
            if vecs.ndim != 2 or vecs.shape[0] != len(part):
                break
            ids, mat = self._vectors()
            if mat is not None and mat.shape[1] != vecs.shape[1]:
                store.clear_vectors()       # embedding backend / dimension changed
            store.set_vectors([(eid, vecs[j]) for j, (eid, _) in enumerate(part)])
            done += len(part)
        return done

    def _names(self) -> List[Tuple[str, int]]:
        store = self.store
        c = self._cache
        if c.get("names_v") != store.version or "names" not in c:
            c["names"] = store.name_index()
            c["names_v"] = store.version
        return c["names"]

    def _vectors(self) -> Tuple[List[int], Optional[np.ndarray]]:
        store = self.store
        c = self._cache
        if c.get("vec_v") != store.version or "vec" not in c:
            c["vec"] = store.vector_matrix()
            c["vec_v"] = store.version
        return c["vec"]

    # ------------------------------------------------------------------ retrieval
    def retrieval_enabled(self) -> bool:
        try:
            return bool(self.settings()["enabled"]) and self.store.counts()["relations"] > 0
        except Exception:
            return False

    def build_block(self, query: str, max_tokens: int = 500) -> str:
        if not self.retrieval_enabled():
            return ""
        embed = self.deps.embed
        embed_query: Optional[Callable[[str], np.ndarray]] = None
        if embed is not None:
            try:
                self.fill_name_vectors(16)   # cheap catch-up (normally done by the worker)
            except Exception:
                pass
            embed_query = lambda q: np.asarray(embed([q]), dtype=np.float32)[0]  # noqa: E731
        with self._lock:
            names = self._names()
            vectors = self._vectors() if embed_query is not None else ([], None)
        return retrieval.build_block(self.store, query, self.deps.count_tokens, max_tokens,
                                     embed_query=embed_query, name_index=names, vectors=vectors)

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        self.worker.start()

    async def stop(self) -> None:
        await self.worker.stop()


def _float_or_none(v: Optional[str]) -> Optional[float]:
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


__all__ = ["KGService", "KGDeps", "classify_entry", "normalize_settings", "DEFAULT_SETTINGS", "MIN_ENTRY_CHARS"]
