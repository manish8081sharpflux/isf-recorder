"""
ISF Recorder — Organization Settings Routes (Admin Only)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
GET  /settings/ai-models       — Get current AI model configuration
PUT  /settings/ai-models       — Update AI model selection (admin only)
GET  /settings/ai-models/available — List available AI models
"""
import json
import logging
from typing import Optional
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from backend.database import get_db
from backend.auth.routes import get_current_user

logger = logging.getLogger("isf_recorder.settings")
router = APIRouter(prefix="/settings", tags=["Organization Settings"])

# ─── Available Models Registry ────────────────────────────────────────────────
# This list can be extended as new models become available.
# The backend validates admin selections against this list.
AVAILABLE_MODELS = [
    # Only models verified to work on our API keys (2026-10-05). Gemini 2.5 Pro is retired for our key,
    # "3.0 Flash" / "3.5 Pro" do not exist, and AssemblyAI Nano is no longer offered.
    {
        "id": "gemini-2.5-flash",
        "short_name": "Gemini 2.5 Flash",
        "name": "Gemini 2.5 Flash",
        "provider": "Google",
        "description": "Current default. Can get stuck repeating on long meetings.",
        "category": "fast",
        "supports": ["transcription", "intelligence", "chat"],
    },
    {
        "id": "gemini-3.5-flash",
        "short_name": "Gemini 3.5 Flash",
        "name": "Gemini 3.5 Flash",
        "provider": "Google",
        "description": "Newer Gemini model.",
        "category": "fast",
        "supports": ["transcription", "intelligence", "chat"],
    },
    {
        "id": "gemini-3.8-flash",
        "short_name": "Gemini 3.8 Flash",
        "name": "Gemini 3.8 Flash",
        "provider": "Google",
        "description": "Newest Flash model (introductory price until 31 Dec 2026).",
        "category": "fast",
        "supports": ["transcription", "intelligence", "chat"],
    },
    {
        "id": "openai-gpt-4o-transcribe-diarize",
        "short_name": "GPT-4o Diarize",
        "chip": "Speakers",
        "name": "OpenAI GPT-4o Transcribe (speakers)",
        "provider": "OpenAI",
        "description": "Transcript with speaker labels and times. Reports stay on Gemini.",
        "category": "pro",
        "supports": ["transcription"],
    },
    {
        "id": "openai-gpt-transcribe",
        "short_name": "GPT Transcribe",
        "chip": "No speakers",
        "name": "OpenAI GPT Transcribe",
        "provider": "OpenAI",
        "description": "Accurate text, but no speakers or exact times (line times are estimated).",
        "category": "fast",
        "supports": ["transcription"],
    },
    {
        "id": "assemblyai-universal-3-5-pro",
        "short_name": "AssemblyAI U3.5 Pro",
        "name": "AssemblyAI Universal-3.5 Pro",
        "provider": "AssemblyAI",
        "description": "Best AssemblyAI model, supports Hindi/Marathi.",
        "category": "pro",
        "supports": ["transcription"],
    },
    {
        "id": "assemblyai-universal-2",
        "short_name": "AssemblyAI U2",
        "name": "AssemblyAI Universal-2",
        "provider": "AssemblyAI",
        "description": "Cheaper AssemblyAI model.",
        "category": "fast",
        "supports": ["transcription"],
    },
]

VALID_MODEL_IDS = {m["id"] for m in AVAILABLE_MODELS}


def require_model_price(db: Session, model_id: str) -> None:
    """Refuses a model that has no active price, so its usage is never costed at ₹0."""
    priced = db.execute(
        text("SELECT 1 FROM isf_recorder.gemini_model_pricing WHERE gemini_model = :m AND is_active = TRUE LIMIT 1"),
        {"m": model_id},
    ).fetchone()
    if not priced:
        raise HTTPException(
            status_code=400,
            detail=f"'{model_id}' has no price set. Add its price first (POST /admin/gemini-usage/pricing).",
        )


class AIModelUpdateRequest(BaseModel):
    transcription_model: Optional[str] = None
    intelligence_model: Optional[str] = None


def get_org_ai_models(db: Session, org_id: str) -> dict:
    """
    Reads the active AI model configuration from organization.settings.
    Falls back to gemini-2.5-flash if not configured.
    """
    row = db.execute(
        text("SELECT settings FROM isf_recorder.organization WHERE id = :org_id"),
        {"org_id": org_id},
    ).fetchone()

    settings = {}
    if row and row[0]:
        settings = row[0] if isinstance(row[0], dict) else json.loads(row[0])

    ai_models = settings.get("ai_models", {})
    return {
        "transcription_model": ai_models.get("transcription_model", "gemini-2.5-flash"),
        "intelligence_model": ai_models.get("intelligence_model", "gemini-2.5-flash"),
        "updated_at": ai_models.get("updated_at"),
        "updated_by": ai_models.get("updated_by"),
    }


@router.get("/ai-models")
def get_ai_model_config(
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Get the current AI model configuration for the organization.
    Available to all users (staff sees which model is active).
    """
    org_id = current_user["org_id"]
    config = get_org_ai_models(db, org_id)

    # Enrich with model display names
    for m in AVAILABLE_MODELS:
        if m["id"] == config["transcription_model"]:
            config["transcription_model_name"] = m["name"]
        if m["id"] == config["intelligence_model"]:
            config["intelligence_model_name"] = m["name"]

    config.setdefault("transcription_model_name", config["transcription_model"])
    config.setdefault("intelligence_model_name", config["intelligence_model"])

    return config


@router.put("/ai-models")
def update_ai_model_config(
    body: AIModelUpdateRequest,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Update the AI model selection (admin only).
    Changes take effect immediately for all users — no logout/login needed.
    """
    if current_user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Only admins can update AI model settings")

    org_id = current_user["org_id"]
    admin_id = current_user["user_id"]
    admin_name = current_user.get("name", "Admin")

    # Validate model IDs
    if body.transcription_model and body.transcription_model not in VALID_MODEL_IDS:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid transcription model '{body.transcription_model}'. "
                   f"Available: {', '.join(sorted(VALID_MODEL_IDS))}"
        )
    if body.intelligence_model and body.intelligence_model not in VALID_MODEL_IDS:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid intelligence model '{body.intelligence_model}'. "
                   f"Available: {', '.join(sorted(VALID_MODEL_IDS))}"
        )
    for chosen in (body.transcription_model, body.intelligence_model):
        if chosen:
            require_model_price(db, chosen)

    # Read current settings
    row = db.execute(
        text("SELECT settings FROM isf_recorder.organization WHERE id = :org_id"),
        {"org_id": org_id},
    ).fetchone()

    settings = {}
    if row and row[0]:
        settings = row[0] if isinstance(row[0], dict) else json.loads(row[0])

    ai_models = settings.get("ai_models", {})

    old_transcription = ai_models.get("transcription_model", "gemini-2.5-flash")
    old_intelligence = ai_models.get("intelligence_model", "gemini-2.5-flash")

    # If AssemblyAI is selected for intelligence, fallback to Gemini 2.5 Flash
    # since AssemblyAI is specialized for STT transcription
    if body.intelligence_model and body.intelligence_model.startswith(("assemblyai", "openai-")):
        body.intelligence_model = "gemini-2.5-flash"

    if body.transcription_model:
        ai_models["transcription_model"] = body.transcription_model
    if body.intelligence_model:
        ai_models["intelligence_model"] = body.intelligence_model

    ai_models["updated_at"] = datetime.now(timezone.utc).isoformat()
    ai_models["updated_by"] = admin_id

    # Write only the ai_models key, atomically (other keys such as 'recorder' are never overwritten)
    db.execute(
        text("""UPDATE isf_recorder.organization
                SET settings = COALESCE(settings, '{}'::jsonb) || jsonb_build_object('ai_models', CAST(:ai AS jsonb))
                WHERE id = :org_id"""),
        {"org_id": org_id, "ai": json.dumps(ai_models)},
    )
    db.commit()

    # Immediately invalidate the in-memory model cache so changes take effect instantly
    from backend.settings.model_resolver import invalidate_cache
    invalidate_cache(org_id)

    new_transcription = ai_models.get("transcription_model", "gemini-2.5-flash")
    new_intelligence = ai_models.get("intelligence_model", "gemini-2.5-flash")

    logger.info(
        f"✅ AI model config updated by {admin_name}: "
        f"transcription={old_transcription}→{new_transcription}, "
        f"intelligence={old_intelligence}→{new_intelligence}"
    )

    return {
        "status": "ok",
        "message": "AI model configuration updated. Changes are live for all users.",
        "transcription_model": new_transcription,
        "intelligence_model": new_intelligence,
        "updated_at": ai_models["updated_at"],
    }


@router.get("/ai-models/available")
def list_available_models(
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    List all available AI models that admin can choose from.
    Also returns which models have pricing configured in DB.
    """
    # Prices come only from the pricing table; the label is shown with each model in the app
    prices = {
        r.gemini_model: r for r in db.execute(
            text("""
                SELECT DISTINCT ON (gemini_model) gemini_model, input_price_per_million,
                       output_price_per_million, audio_price_per_million, price_per_audio_hour
                FROM isf_recorder.gemini_model_pricing
                WHERE is_active = TRUE
                ORDER BY gemini_model, created_at DESC
            """)
        ).fetchall()
    }

    def price_label(p) -> str:
        if p is None:
            return "No price set"
        if float(p.price_per_audio_hour or 0) > 0:
            return f"${float(p.price_per_audio_hour):g} per audio hour"
        return (f"${float(p.audio_price_per_million):g} audio / ${float(p.input_price_per_million):g} text in, "
                f"${float(p.output_price_per_million):g} out per 1M tokens")

    def price_short(p, model_id: str) -> str:
        """Compact per-hour price for the model picker, from the DB prices only."""
        if p is None:
            return "no price set"
        if float(p.price_per_audio_hour or 0) > 0:
            return f"${float(p.price_per_audio_hour):.2f}/h"
        if model_id.startswith("gemini") and float(p.audio_price_per_million or 0) > 0:
            # Gemini counts 32 audio tokens per second (audio input only; output tokens extra)
            return f"≈ ${32 * 3600 * float(p.audio_price_per_million) / 1e6:.2f}/h + output"
        return f"${float(p.input_price_per_million):g}/${float(p.output_price_per_million):g} per 1M tok"

    result = []
    for m in AVAILABLE_MODELS:
        p = prices.get(m["id"])
        label = price_label(p)
        result.append({
            **m,
            "description": f"{m['description']} {label}.",
            "price_label": label,
            "price_short": price_short(p, m["id"]),
            "has_pricing": p is not None,
        })

    return {"models": result}
