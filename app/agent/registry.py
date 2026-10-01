"""Collects built-in, skill and MCP tools; OpenAI ``tools`` schema conversion; name sanitising."""
from __future__ import annotations

import copy
import hashlib
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

MAX_NAME_LEN = 64
MAX_DESC_CHARS = 300
MAX_TOOLS = 24


def sanitize_name(s: str, max_len: int = MAX_NAME_LEN) -> str:
    out = re.sub(r"[^a-zA-Z0-9_]", "_", str(s or ""))
    return out[:max_len] or "_"


def mcp_tool_name(server_id: str, tool: str, taken: Optional[set] = None) -> str:
    """``mcp__<server>__<tool>`` limited to [a-zA-Z0-9_] and 64 chars (hash suffix when shortened/colliding)."""
    srv = re.sub(r"_+", "_", sanitize_name(server_id)).strip("_") or "srv"
    t = sanitize_name(tool)
    name = f"mcp__{srv}__{t}"
    if len(name) > MAX_NAME_LEN or (taken is not None and name in taken):
        h = hashlib.sha1(f"{server_id}\x00{tool}".encode("utf-8")).hexdigest()[:6]
        name = f"mcp__{srv[:20]}__{t}"[:MAX_NAME_LEN - 7] + "_" + h
    return name


def clean_schema(schema: Any) -> Dict[str, Any]:
    """Make an MCP inputSchema safe for llama.cpp's OpenAI tools: object at top, no $schema noise."""
    if not isinstance(schema, dict):
        return {"type": "object", "properties": {}}
    s = copy.deepcopy(schema)
    for k in ("$schema", "$id", "title"):
        s.pop(k, None)
    if s.get("type") != "object":
        s["type"] = "object"
    if not isinstance(s.get("properties"), dict):
        s["properties"] = {}
    return s


@dataclass
class ToolEntry:
    name: str
    title: str
    description: str
    source: str               # "builtin" | "skill" | "mcp:<server_id>"
    read_only: bool
    enabled: bool
    always_allow: bool
    input_schema: Dict[str, Any]
    spec: Any = None          # ToolSpec for builtin / skill
    server_id: str = ""
    mcp_tool: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)

    def public(self) -> Dict[str, Any]:
        return {"name": self.name, "title": self.title, "description": self.description, "source": self.source,
                "read_only": self.read_only, "enabled": self.enabled, "always_allow": self.always_allow,
                "input_schema": self.input_schema}


def truncate_desc(d: str, n: int = MAX_DESC_CHARS) -> str:
    d = re.sub(r"\s+", " ", str(d or "")).strip()
    return d if len(d) <= n else d[: n - 1] + "…"


def to_openai_tools(entries: List[ToolEntry], max_tools: int = MAX_TOOLS) -> List[Dict[str, Any]]:
    """Enabled tools only, built-ins first, descriptions ≤ 300 chars, at most 24 tools."""
    order = {"builtin": 0, "skill": 1}
    picked = sorted((e for e in entries if e.enabled), key=lambda e: order.get(e.source, 2))[:max_tools]
    out = []
    for e in picked:
        desc = e.description or e.title or e.name
        if not e.read_only:
            desc = desc if "需要用户批准" in desc else desc + "（需要用户批准）"
        out.append({"type": "function", "function": {
            "name": e.name, "description": truncate_desc(desc), "parameters": clean_schema(e.input_schema)}})
    return out
