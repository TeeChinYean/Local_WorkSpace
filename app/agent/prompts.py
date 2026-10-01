"""System prompt presets (docs/AGENT_API.md §2 "System prompts")."""
from __future__ import annotations

import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

from .config import AgentStore

BUILTIN_ID = "default"
BUILTIN_PRESET = {"id": BUILTIN_ID, "name": "默认", "content": "", "builtin": True, "created_at": 0}
MAX_NAME = 100
MAX_CONTENT = 50_000
PROMPT_HEADER = "【用户自定义系统提示词 (Custom System Prompt)】"


class PromptError(ValueError):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


def _clean(name: Any, content: Any, partial: bool = False) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    if name is not None or not partial:
        n = str(name or "").strip()
        if not n:
            raise PromptError(400, "提示词名称不能为空")
        if len(n) > MAX_NAME:
            raise PromptError(400, f"提示词名称过长（最多 {MAX_NAME} 字符）")
        out["name"] = n
    if content is not None or not partial:
        c = str(content or "")
        if len(c) > MAX_CONTENT:
            raise PromptError(400, f"提示词内容过长（最多 {MAX_CONTENT} 字符）")
        out["content"] = c
    return out


def list_prompts(store: AgentStore) -> Dict[str, Any]:
    cfg = store.load_config()
    prompts = [dict(BUILTIN_PRESET)] + store.load_prompts()
    active = cfg.get("active_prompt_id") or BUILTIN_ID
    if not any(p["id"] == active for p in prompts):
        active = BUILTIN_ID
    return {"prompts": prompts, "active_prompt_id": active, "prompt_mode": cfg.get("prompt_mode", "append")}


def create_prompt(store: AgentStore, name: Any, content: Any) -> Dict[str, Any]:
    fields = _clean(name, content)
    with store.lock:
        prompts = store.load_prompts()
        p = {"id": uuid.uuid4().hex[:12], "name": fields["name"], "content": fields["content"],
             "builtin": False, "created_at": int(time.time())}
        prompts.append(p)
        store.save_prompts(prompts)
    return p


def update_prompt(store: AgentStore, pid: str, name: Any = None, content: Any = None) -> Dict[str, Any]:
    if pid == BUILTIN_ID:
        raise PromptError(403, "内置提示词为只读，不能修改")
    fields = _clean(name, content, partial=True)
    with store.lock:
        prompts = store.load_prompts()
        for p in prompts:
            if p["id"] == pid:
                p.update(fields)
                store.save_prompts(prompts)
                return p
    raise PromptError(404, "提示词不存在")


def delete_prompt(store: AgentStore, pid: str) -> None:
    if pid == BUILTIN_ID:
        raise PromptError(403, "内置提示词不能删除")
    with store.lock:
        prompts = store.load_prompts()
        kept = [p for p in prompts if p["id"] != pid]
        if len(kept) == len(prompts):
            raise PromptError(404, "提示词不存在")
        store.save_prompts(kept)

        def _reset(cfg):
            if cfg.get("active_prompt_id") == pid:
                cfg["active_prompt_id"] = BUILTIN_ID
        store.update_config(_reset)


def set_active(store: AgentStore, pid: Optional[str], mode: Optional[str]) -> Dict[str, Any]:
    pid = pid or BUILTIN_ID
    if mode is not None and mode not in ("append", "replace"):
        raise PromptError(400, "mode 只能是 append 或 replace")
    with store.lock:
        if pid != BUILTIN_ID and not any(p["id"] == pid for p in store.load_prompts()):
            raise PromptError(404, "提示词不存在")

        def _set(cfg):
            cfg["active_prompt_id"] = pid
            if mode:
                cfg["prompt_mode"] = mode
        store.update_config(_set)
    return list_prompts(store)


def resolve_prompt(store: AgentStore, prompt_id: Optional[str] = None) -> Tuple[str, str]:
    """-> (preset content, mode). ``prompt_id`` overrides the active preset for one request.
    Unknown ids fall back to the built-in (empty) preset. Never raises."""
    try:
        cfg = store.load_config()
        mode = cfg.get("prompt_mode", "append")
        pid = prompt_id or cfg.get("active_prompt_id") or BUILTIN_ID
        if pid == BUILTIN_ID:
            return "", mode
        for p in store.load_prompts():
            if p["id"] == pid:
                return str(p.get("content") or ""), mode
    except Exception:
        pass
    return "", "append"


def apply_prompt(base_system: str, content: str, mode: str) -> str:
    """Combine the gateway's built-in capability prompt with a preset.

    append  -> base + header + preset;  replace -> preset only (context blocks are added by the caller)."""
    content = (content or "").strip()
    if not content:
        return base_system
    if mode == "replace":
        return content
    return f"{base_system}\n\n{PROMPT_HEADER}\n{content}" if base_system else content


def append_suffix(content: str) -> str:
    """Suffix used where only append mode is supported (IDE AI edit)."""
    content = (content or "").strip()
    return f"\n\n{PROMPT_HEADER}\n{content}" if content else ""
