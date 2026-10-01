"""REST API ``/api/agent/*`` (docs/AGENT_API.md §2)."""
from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from pydantic import BaseModel

from . import prompts as prompts_mod
from .mcp_client import McpError, make_server_id, mask_server, normalize_server, parse_import
from .service import AgentService
from .skills import MAX_IMPORT_BYTES, SkillError


class ToolUpdateBody(BaseModel):
    enabled: Optional[bool] = None
    always_allow: Optional[bool] = None


class SkillBody(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    content: Optional[str] = None
    enabled: Optional[bool] = None


class PromptBody(BaseModel):
    name: Optional[str] = None
    content: Optional[str] = None


class ActiveBody(BaseModel):
    id: Optional[str] = None
    mode: Optional[str] = None


class ApproveBody(BaseModel):
    call_id: str
    decision: str
    remember: bool = False


def create_agent_router(service: AgentService) -> APIRouter:
    check_security = service.deps.check_security

    def _guard(request: Request) -> None:
        if check_security is None:
            return
        auth = request.headers.get("authorization", "")
        token = auth[7:].strip() if auth.lower().startswith("bearer ") else request.headers.get("x-tv-token", "").strip()
        reason = check_security(request.method, request.client.host if request.client else "",
                                request.headers.get("host", ""), request.headers.get("origin"), token)
        if reason:
            raise HTTPException(403, reason)

    router = APIRouter(prefix="/api/agent", tags=["agent"], dependencies=[Depends(_guard)])
    store = service.store

    # ======================================================================= tools
    @router.get("/tools")
    async def list_tools():
        service.kick_mcp()
        return {"tools": [e.public() for e in service.collect_tools()]}

    @router.post("/tools/{name}")
    async def update_tool(name: str, body: ToolUpdateBody):
        names = {e.name for e in service.collect_tools()}
        if name not in names:
            raise HTTPException(404, f"工具不存在: {name}")

        def _upd(cfg):
            if body.enabled is not None:
                cfg.setdefault("tools", {}).setdefault(name, {})["enabled"] = bool(body.enabled)
            if body.always_allow is not None:
                appr = cfg.setdefault("approvals", {})
                if body.always_allow:
                    appr[name] = "always"
                else:
                    appr.pop(name, None)
        store.update_config(_upd)
        return {"ok": True}

    # ======================================================================= MCP
    def _server_out(cfg: Dict[str, Any]) -> Dict[str, Any]:
        st = service.mcp.status(cfg)
        out = mask_server(cfg)
        out.update(status=st["status"], error=st["error"] or "",
                   tools=[{"name": t.get("name"), "description": t.get("description") or "",
                           "read_only": (t.get("annotations") or {}).get("readOnlyHint") is True
                           if isinstance(t.get("annotations"), dict) else False} for t in st["tools"]])
        return out

    def _bad(e: Exception) -> HTTPException:
        return HTTPException(400, str(e))

    @router.get("/mcp")
    async def list_mcp():
        servers = service.servers()
        for s in servers:
            service.mcp.kick(s)
        return {"servers": [_server_out(s) for s in servers]}

    @router.post("/mcp/import")
    async def import_mcp(body: Dict[str, Any]):
        # {"json": "<text>" | {...}}; a bare {"mcpServers": {...}} body is accepted too
        payload = body.get("json", body) if isinstance(body, dict) else body
        try:
            items = parse_import(payload)
        except McpError as e:
            raise _bad(e)
        added: List[str] = []
        with store.lock:
            cfg = store.load_config()
            existing = [s["id"] for s in cfg["mcp_servers"]]
            new = []
            for item in items:
                try:
                    srv = normalize_server(item)
                except McpError as e:
                    raise HTTPException(400, f"{item.get('name')}: {e}")
                srv["id"] = make_server_id(srv["name"], existing + [s["id"] for s in new])
                new.append(srv)
            cfg["mcp_servers"].extend(new)
            store.save_config(cfg)
            added = [s["id"] for s in new]
        for s in new:
            service.mcp.kick(s)
        return {"added": added}

    @router.post("/mcp")
    async def create_mcp(body: Dict[str, Any]):
        try:
            srv = normalize_server(body)
        except McpError as e:
            raise _bad(e)
        with store.lock:
            cfg = store.load_config()
            srv["id"] = make_server_id(srv["name"], [s["id"] for s in cfg["mcp_servers"]])
            cfg["mcp_servers"].append(srv)
            store.save_config(cfg)
        if srv["enabled"]:
            await service.mcp.ensure(srv)
        return _server_out(srv)

    @router.put("/mcp/{sid}")
    async def update_mcp(sid: str, body: Dict[str, Any]):
        with store.lock:
            cfg = store.load_config()
            idx = next((i for i, s in enumerate(cfg["mcp_servers"]) if s["id"] == sid), None)
            if idx is None:
                raise HTTPException(404, "MCP 服务器不存在")
            try:
                srv = normalize_server(body, cfg["mcp_servers"][idx])
            except McpError as e:
                raise _bad(e)
            srv["id"] = sid
            cfg["mcp_servers"][idx] = srv
            store.save_config(cfg)
        if srv["enabled"]:
            await service.mcp.ensure(srv)
        else:
            await service.mcp.stop(sid)
        return _server_out(srv)

    @router.delete("/mcp/{sid}")
    async def delete_mcp(sid: str):
        with store.lock:
            cfg = store.load_config()
            kept = [s for s in cfg["mcp_servers"] if s["id"] != sid]
            if len(kept) == len(cfg["mcp_servers"]):
                raise HTTPException(404, "MCP 服务器不存在")
            cfg["mcp_servers"] = kept
            prefix = f"mcp__{sid}__"
            cfg["tools"] = {k: v for k, v in cfg["tools"].items() if not k.startswith(prefix)}
            cfg["approvals"] = {k: v for k, v in cfg["approvals"].items() if not k.startswith(prefix)}
            store.save_config(cfg)
        await service.mcp.stop(sid)
        return {"ok": True}

    @router.post("/mcp/{sid}/restart")
    async def restart_mcp(sid: str):
        srv = service.server(sid)
        if srv is None:
            raise HTTPException(404, "MCP 服务器不存在")
        if not srv.get("enabled", True):
            await service.mcp.stop(sid)
        else:
            await service.mcp.ensure(srv, force=True)
        return _server_out(srv)

    # ======================================================================= skills
    def _skill_err(e: SkillError) -> HTTPException:
        return HTTPException(e.status, e.detail)

    @router.get("/skills")
    async def list_skills():
        return {"skills": await asyncio.to_thread(service.skills.list)}

    @router.post("/skills/import")
    async def import_skill(file: UploadFile = File(...)):
        data = await file.read(MAX_IMPORT_BYTES + 1)
        try:
            s = await asyncio.to_thread(service.skills.import_file, file.filename or "", data)
        except SkillError as e:
            raise _skill_err(e)
        return s

    @router.post("/skills")
    async def create_skill(body: SkillBody):
        try:
            return await asyncio.to_thread(service.skills.create, body.name, body.description, body.content, body.enabled)
        except SkillError as e:
            raise _skill_err(e)

    @router.get("/skills/{slug}")
    async def get_skill(slug: str):
        try:
            s = await asyncio.to_thread(service.skills.get, slug)
        except SkillError as e:
            raise _skill_err(e)
        return s

    @router.put("/skills/{slug}")
    async def update_skill(slug: str, body: SkillBody):
        try:
            return await asyncio.to_thread(service.skills.update, slug, body.name, body.description, body.content,
                                           body.enabled)
        except SkillError as e:
            raise _skill_err(e)

    @router.delete("/skills/{slug}")
    async def delete_skill(slug: str):
        try:
            dest = await asyncio.to_thread(service.skills.delete, slug)
        except SkillError as e:
            raise _skill_err(e)
        return {"ok": True, "trash": dest}

    # ======================================================================= prompts
    def _prompt_err(e: prompts_mod.PromptError) -> HTTPException:
        return HTTPException(e.status, e.detail)

    @router.get("/prompts")
    async def list_prompts():
        return prompts_mod.list_prompts(store)

    @router.post("/prompts/active")
    async def set_active(body: ActiveBody):
        try:
            return prompts_mod.set_active(store, body.id, body.mode)
        except prompts_mod.PromptError as e:
            raise _prompt_err(e)

    @router.post("/prompts")
    async def create_prompt(body: PromptBody):
        try:
            return prompts_mod.create_prompt(store, body.name, body.content)
        except prompts_mod.PromptError as e:
            raise _prompt_err(e)

    @router.put("/prompts/{pid}")
    async def update_prompt(pid: str, body: PromptBody):
        try:
            return prompts_mod.update_prompt(store, pid, body.name, body.content)
        except prompts_mod.PromptError as e:
            raise _prompt_err(e)

    @router.delete("/prompts/{pid}")
    async def delete_prompt(pid: str):
        try:
            prompts_mod.delete_prompt(store, pid)
        except prompts_mod.PromptError as e:
            raise _prompt_err(e)
        return {"ok": True}

    # ======================================================================= approvals
    @router.post("/approve")
    async def approve(body: ApproveBody):
        if body.decision not in ("allow", "deny"):
            raise HTTPException(400, "decision 只能是 allow 或 deny")
        name = service.approvals.resolve(body.call_id, body.decision, body.remember)
        if name is None:
            raise HTTPException(404, "该调用不存在或已过期")
        return {"ok": True}

    return router
