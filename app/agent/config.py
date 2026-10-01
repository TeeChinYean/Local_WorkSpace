"""Atomic JSON persistence for the agent features (docs/AGENT_API.md §1).

Layout (under ``<storage>/agent/``)::

    config.json    {tools, approvals, mcp_servers, active_prompt_id, prompt_mode, max_tool_rounds}
    prompts.json   [{id, name, content, builtin, created_at}]
    skills/<slug>/SKILL.md

The storage directory is resolved through a callable on every access so tests (and the e2e
suite) can redirect ``main.STORAGE_DIR`` after import.
"""
from __future__ import annotations

import copy
import json
import os
import threading
import uuid
from typing import Any, Callable, Dict, List, Optional

DEFAULT_MAX_TOOL_ROUNDS = 6
MAX_TOOL_ROUNDS_CAP = 20

DEFAULT_CONFIG: Dict[str, Any] = {
    "tools": {},
    "approvals": {},
    "mcp_servers": [],
    "active_prompt_id": "default",
    "prompt_mode": "append",
    "max_tool_rounds": DEFAULT_MAX_TOOL_ROUNDS,
}


def atomic_write_json(path: str, obj: Any) -> None:
    """Write JSON via tmp file + fsync + os.replace (never leaves a half-written file)."""
    d = os.path.dirname(path) or "."
    os.makedirs(d, exist_ok=True)
    tmp = os.path.join(d, f".{os.path.basename(path)}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        last: Optional[OSError] = None
        for _ in range(4 if os.name == "nt" else 1):
            try:
                os.replace(tmp, path)
                return
            except PermissionError as e:  # Windows: AV / indexer briefly holds the file
                last = e
                import time
                time.sleep(0.05)
        raise last if last else OSError("replace failed")
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def read_json(path: str, default: Any) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return copy.deepcopy(default)


def normalize_config(raw: Any) -> Dict[str, Any]:
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    if not isinstance(raw, dict):
        return cfg
    if isinstance(raw.get("tools"), dict):
        cfg["tools"] = {str(k): (v if isinstance(v, dict) else {}) for k, v in raw["tools"].items()}
    if isinstance(raw.get("approvals"), dict):
        cfg["approvals"] = {str(k): v for k, v in raw["approvals"].items() if v == "always"}
    if isinstance(raw.get("mcp_servers"), list):
        cfg["mcp_servers"] = [s for s in raw["mcp_servers"] if isinstance(s, dict) and s.get("id")]
    if isinstance(raw.get("active_prompt_id"), str) and raw["active_prompt_id"]:
        cfg["active_prompt_id"] = raw["active_prompt_id"]
    if raw.get("prompt_mode") in ("append", "replace"):
        cfg["prompt_mode"] = raw["prompt_mode"]
    try:
        cfg["max_tool_rounds"] = max(1, min(MAX_TOOL_ROUNDS_CAP, int(raw.get("max_tool_rounds", DEFAULT_MAX_TOOL_ROUNDS))))
    except (TypeError, ValueError):
        pass
    for k, v in raw.items():  # keep unknown keys (forward compatibility)
        cfg.setdefault(k, v)
    return cfg


class AgentStore:
    """Thread-safe accessor for the agent JSON files."""

    def __init__(self, get_storage_dir: Callable[[], str]):
        self._get_storage_dir = get_storage_dir
        self.lock = threading.RLock()

    # ---------------------------------------------------------------- paths
    @property
    def storage_dir(self) -> str:
        return os.path.abspath(str(self._get_storage_dir()))

    @property
    def agent_dir(self) -> str:
        return os.path.join(self.storage_dir, "agent")

    @property
    def config_file(self) -> str:
        return os.path.join(self.agent_dir, "config.json")

    @property
    def prompts_file(self) -> str:
        return os.path.join(self.agent_dir, "prompts.json")

    @property
    def skills_dir(self) -> str:
        return os.path.join(self.agent_dir, "skills")

    @property
    def trash_dir(self) -> str:
        return os.path.join(self.storage_dir, "trash")

    # ---------------------------------------------------------------- config
    def load_config(self) -> Dict[str, Any]:
        with self.lock:
            return normalize_config(read_json(self.config_file, {}))

    def save_config(self, cfg: Dict[str, Any]) -> None:
        with self.lock:
            atomic_write_json(self.config_file, normalize_config(cfg))

    def update_config(self, fn: Callable[[Dict[str, Any]], Any]) -> Any:
        """Read-modify-write under the lock; ``fn`` mutates the dict in place and may return a value."""
        with self.lock:
            cfg = self.load_config()
            out = fn(cfg)
            self.save_config(cfg)
            return out

    # ---------------------------------------------------------------- prompts
    def load_prompts(self) -> List[Dict[str, Any]]:
        with self.lock:
            data = read_json(self.prompts_file, [])
            return [p for p in data if isinstance(p, dict) and p.get("id")] if isinstance(data, list) else []

    def save_prompts(self, prompts: List[Dict[str, Any]]) -> None:
        with self.lock:
            atomic_write_json(self.prompts_file, [p for p in prompts if not p.get("builtin")])
