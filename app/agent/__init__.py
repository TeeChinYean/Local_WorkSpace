"""Agent features for the gateway: tools, MCP servers, skills and system-prompt presets.

Contract: docs/AGENT_API.md. Wiring (app/main.py)::

    from agent import AgentDeps, AgentService, create_agent_router
    agent_service = AgentService(AgentDeps(get_storage_dir=lambda: STORAGE_DIR, ...))
    app.include_router(create_agent_router(agent_service))

The package never imports ``main``; everything gateway-specific is injected through AgentDeps.
"""
from .base import AgentDeps, ToolError, ToolSpec
from .runner import ChatLoop, encode_event
from .service import AgentService, ApprovalManager
from .router import create_agent_router

__all__ = ["AgentDeps", "AgentService", "ApprovalManager", "ChatLoop", "ToolError", "ToolSpec",
           "create_agent_router", "encode_event"]
