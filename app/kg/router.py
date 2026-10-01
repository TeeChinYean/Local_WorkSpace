"""REST API ``/api/kg/*`` (docs/KG_API.md §3)."""
from __future__ import annotations

import asyncio
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from .service import IDLE_MAX, IDLE_MIN, KGService


class SettingsBody(BaseModel):
    enabled: Optional[bool] = None
    paused: Optional[bool] = None
    idle_seconds: Optional[int] = None


class MergeBody(BaseModel):
    keep_id: int
    merge_ids: List[int]


class RebuildBody(BaseModel):
    mode: str = "missing"


def create_kg_router(service: KGService) -> APIRouter:
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

    router = APIRouter(prefix="/api/kg", tags=["kg"], dependencies=[Depends(_guard)])

    @router.get("/status")
    async def status():
        return await asyncio.to_thread(service.status)

    @router.post("/settings")
    async def update_settings(body: SettingsBody):
        if body.idle_seconds is not None and not (IDLE_MIN <= body.idle_seconds <= IDLE_MAX):
            raise HTTPException(400, f"idle_seconds 需在 {IDLE_MIN}-{IDLE_MAX} 秒之间")
        await asyncio.to_thread(service.update_settings, body.enabled, body.paused, body.idle_seconds)
        return await asyncio.to_thread(service.status)

    @router.get("/entities")
    async def list_entities(q: str = "", limit: int = 50, offset: int = 0):
        items, total = await asyncio.to_thread(service.store.list_entities, q, limit, offset)
        return {"items": items, "total": total, "limit": max(1, min(200, limit)), "offset": max(0, offset)}

    @router.get("/entities/{entity_id}")
    async def get_entity(entity_id: int):
        def _get():
            ent = service.store.get_entity(entity_id)
            if ent is None:
                return None
            return {"entity": ent, "relations": service.store.entity_relations(entity_id)}
        out = await asyncio.to_thread(_get)
        if out is None:
            raise HTTPException(404, "实体不存在")
        return out

    @router.delete("/relations/{relation_id}")
    async def delete_relation(relation_id: int):
        if not await asyncio.to_thread(service.store.delete_relation, relation_id):
            raise HTTPException(404, "关系不存在")
        return {"ok": True}

    @router.delete("/entities/{entity_id}")
    async def delete_entity(entity_id: int):
        n = await asyncio.to_thread(service.store.delete_entity, entity_id)
        if n is None:
            raise HTTPException(404, "实体不存在")
        return {"ok": True, "deleted_relations": n}

    @router.post("/entities/merge")
    async def merge_entities(body: MergeBody):
        ids = [i for i in dict.fromkeys(body.merge_ids) if i != body.keep_id]
        if not ids:
            raise HTTPException(400, "请选择要合并的实体")
        try:
            ent = await asyncio.to_thread(service.store.merge_entities, body.keep_id, ids)
        except KeyError as e:
            raise HTTPException(404, f"实体不存在: {e.args[0] if e.args else ''}")
        return {"ok": True, "entity": ent}

    @router.post("/rebuild")
    async def rebuild(body: RebuildBody):
        if body.mode not in ("missing", "all"):
            raise HTTPException(400, "mode 只能是 missing 或 all")
        n = await asyncio.to_thread(service.rebuild, body.mode)
        return {"ok": True, "mode": body.mode, "enqueued": n}

    @router.post("/queue/clear_errors")
    async def clear_errors():
        n = await asyncio.to_thread(service.store.clear_errors)
        return {"ok": True, "cleared": n}

    return router


__all__ = ["create_kg_router"]
