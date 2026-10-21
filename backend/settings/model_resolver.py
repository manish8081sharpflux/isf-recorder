"""
ISF Recorder — Dynamic AI Model Resolver
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Reads the admin-configured AI model from organization.settings in the database.
All Gemini-calling code uses this instead of static config values.

Features:
- In-memory cache with 60-second TTL to avoid hitting DB on every API call
- Thread-safe cache invalidation
- Falls back to config.py defaults if DB is unreachable
"""
import json
import time
import logging
import threading
from typing import Dict, Optional, Tuple

from backend.config import GEMINI_TRANSCRIBE_MODEL, GEMINI_INTELLIGENCE_MODEL

logger = logging.getLogger("isf_recorder.model_resolver")

# In-memory cache: {org_id: {"transcription": str, "intelligence": str, "cached_at": float}}
_model_cache: Dict[str, Dict] = {}
_cache_lock = threading.Lock()
_CACHE_TTL_SECONDS = 60  # Re-read from DB every 60 seconds


def get_active_models(org_id: Optional[str] = None) -> Tuple[str, str]:
    """
    Returns (transcription_model, intelligence_model) for the given organization.
    
    Uses a 60-second in-memory cache to avoid hitting the DB on every single
    transcription or chat call. When admin changes the model, it takes effect
    within 60 seconds for all users automatically.
    
    Falls back to config.py static values if:
    - org_id is not provided
    - DB is unreachable
    - No AI models configured in org settings
    """
    if not org_id:
        return GEMINI_TRANSCRIBE_MODEL, GEMINI_INTELLIGENCE_MODEL

    # Check cache first
    with _cache_lock:
        cached = _model_cache.get(org_id)
        if cached and (time.time() - cached["cached_at"]) < _CACHE_TTL_SECONDS:
            return cached["transcription"], cached["intelligence"]

    # Cache miss or expired — read from DB
    try:
        from backend.database import SessionLocal
        from sqlalchemy import text

        db = SessionLocal()
        try:
            row = db.execute(
                text("SELECT settings FROM isf_recorder.organization WHERE id = :org_id"),
                {"org_id": org_id},
            ).fetchone()

            if row and row[0]:
                settings = row[0] if isinstance(row[0], dict) else json.loads(row[0])
                ai_models = settings.get("ai_models", {})
                transcription = ai_models.get("transcription_model", GEMINI_TRANSCRIBE_MODEL)
                intelligence = ai_models.get("intelligence_model", GEMINI_INTELLIGENCE_MODEL)
            else:
                transcription = GEMINI_TRANSCRIBE_MODEL
                intelligence = GEMINI_INTELLIGENCE_MODEL
        finally:
            db.close()

        # Update cache
        with _cache_lock:
            _model_cache[org_id] = {
                "transcription": transcription,
                "intelligence": intelligence,
                "cached_at": time.time(),
            }

        return transcription, intelligence

    except Exception as e:
        logger.warning(f"Failed to read AI models from DB for org {org_id}, using defaults: {e}")
        return GEMINI_TRANSCRIBE_MODEL, GEMINI_INTELLIGENCE_MODEL


def get_transcription_model(org_id: Optional[str] = None) -> str:
    """Returns the active transcription model for the given org."""
    model, _ = get_active_models(org_id)
    return model


def get_intelligence_model(org_id: Optional[str] = None) -> str:
    """Returns the active intelligence/chat model for the given org."""
    _, model = get_active_models(org_id)
    return model


def invalidate_cache(org_id: Optional[str] = None):
    """
    Force-invalidates the model cache for an org (or all orgs).
    Called when admin updates model settings.
    """
    with _cache_lock:
        if org_id:
            _model_cache.pop(org_id, None)
        else:
            _model_cache.clear()
    logger.info(f"Model cache invalidated for org_id={org_id or 'ALL'}")
