"""Knowledge-graph memory (Graph RAG) for the gateway — see docs/KG_API.md.

The package never imports ``main``; app/main.py injects its dependencies through ``KGDeps``.
"""
from .activity import ActivityMiddleware, ActivityTracker, is_user_llm_request
from .extract import ExtractionError, KGWorker, build_messages, parse_extraction
from .retrieval import BLOCK_HEADER, build_block, match_mentions
from .router import create_kg_router
from .service import KGDeps, KGService, classify_entry
from .store import KGStore, normalize_name

__all__ = [
    "ActivityMiddleware", "ActivityTracker", "is_user_llm_request",
    "ExtractionError", "KGWorker", "build_messages", "parse_extraction",
    "BLOCK_HEADER", "build_block", "match_mentions",
    "create_kg_router", "KGDeps", "KGService", "classify_entry",
    "KGStore", "normalize_name",
]
