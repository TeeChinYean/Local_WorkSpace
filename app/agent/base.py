"""Shared types: injected gateway dependencies, tool specs, argument validation."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional


class ToolError(Exception):
    """A tool failed in an expected way; the message is returned to the model as the tool result."""


@dataclass
class AgentDeps:
    """Callables injected by app/main.py (the package never imports main)."""
    get_storage_dir: Callable[[], str]
    check_security: Optional[Callable[[str, str, str, Optional[str], str], Optional[str]]] = None
    get_llm_base: Callable[[], str] = lambda: "http://127.0.0.1:18089/v1"
    get_client: Optional[Callable[[], Any]] = None                 # -> httpx.AsyncClient (llama auth headers)
    get_model: Callable[[], str] = lambda: ""
    get_n_ctx: Callable[[], int] = lambda: 8192
    count_tokens: Optional[Callable[[str], int]] = None
    web_search: Optional[Callable[[str, int], List[Dict[str, Any]]]] = None      # sync
    fetch_url: Optional[Callable[[str], Any]] = None                                # sync -> (title, text)
    memory_search: Optional[Callable[[str, int], List[str]]] = None                 # sync
    get_workspace: Optional[Callable[[], Any]] = None                               # -> ide_api.WorkspaceHelpers

    def tokens(self, text: str) -> int:
        if self.count_tokens is not None:
            try:
                return int(self.count_tokens(text))
            except Exception:
                pass
        if not text:
            return 0
        n_ascii = len(text.encode("ascii", "ignore"))
        return int(n_ascii / 3 + (len(text) - n_ascii)) + 1


@dataclass
class ToolSpec:
    name: str
    title: str
    description: str
    read_only: bool
    input_schema: Dict[str, Any]
    handler: Callable[[Dict[str, Any]], Awaitable[str]]
    source: str = "builtin"
    extra: Dict[str, Any] = field(default_factory=dict)


_TRUE = {"true", "1", "yes", "on"}
_FALSE = {"false", "0", "no", "off"}


def parse_arguments(raw: Any) -> Dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    s = (raw or "").strip() if isinstance(raw, str) else ""
    if not s:
        return {}
    try:
        val = json.loads(s)
    except ValueError as e:
        raise ToolError(f"工具参数不是合法的 JSON: {e}")
    if not isinstance(val, dict):
        raise ToolError("工具参数必须是 JSON 对象")
    return val


def validate_args(schema: Optional[Dict[str, Any]], args: Dict[str, Any]) -> Dict[str, Any]:
    """Minimal JSON-schema check (required + top-level primitive types, with lenient coercion)."""
    if not isinstance(args, dict):
        raise ToolError("工具参数必须是 JSON 对象")
    if not isinstance(schema, dict):
        return args
    props = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
    out = dict(args)
    for key in schema.get("required") or []:
        if key not in out or out[key] is None:
            raise ToolError(f"缺少必填参数: {key}")
    for key, val in list(out.items()):
        spec = props.get(key)
        if not isinstance(spec, dict) or val is None:
            continue
        typ = spec.get("type")
        types = typ if isinstance(typ, list) else [typ] if typ else []
        if not types:
            continue
        out[key] = _coerce(key, val, types)
    return out


def _coerce(key: str, val: Any, types: List[str]) -> Any:
    def ok(t: str) -> bool:
        if t == "string":
            return isinstance(val, str)
        if t == "integer":
            return isinstance(val, int) and not isinstance(val, bool)
        if t == "number":
            return isinstance(val, (int, float)) and not isinstance(val, bool)
        if t == "boolean":
            return isinstance(val, bool)
        if t == "array":
            return isinstance(val, list)
        if t == "object":
            return isinstance(val, dict)
        if t == "null":
            return val is None
        return True
    if any(ok(t) for t in types):
        return val
    for t in types:
        try:
            if t == "integer" and isinstance(val, (str, float)) and not isinstance(val, bool):
                f = float(val)
                if f.is_integer():
                    return int(f)
            if t == "number" and isinstance(val, str):
                return float(val)
            if t == "boolean" and isinstance(val, (str, int)):
                s = str(val).strip().lower()
                if s in _TRUE:
                    return True
                if s in _FALSE:
                    return False
            if t == "string" and isinstance(val, (int, float, bool)):
                return str(val).lower() if isinstance(val, bool) else str(val)
            if t == "array" and isinstance(val, str):
                v = json.loads(val)
                if isinstance(v, list):
                    return v
            if t == "object" and isinstance(val, str):
                v = json.loads(val)
                if isinstance(v, dict):
                    return v
        except (ValueError, TypeError):
            continue
    raise ToolError(f"参数 {key} 类型错误，应为 {'/'.join(types)}")
