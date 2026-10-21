"""
ISF Recorder — Gemini API Usage Tracker & Cost Engine
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Captures actual token usage from Google Gemini API responses,
calculates USD and live-synced INR costs, and logs every individual
call against recording_id.

Key Responsibilities:
1. Enforces recording_id association for all recording-related calls.
2. Extracts audio modality tokens vs text tokens vs output tokens from usage_metadata.
3. Calculates exact costs based on active model pricing and live internet exchange rate.
4. Preserves full auditability (raw usage JSON, exchange rate, USD cost, INR cost).
5. Tracks both successful and failed attempts (for retries).
6. Non-blocking & fault-tolerant: usage logging failures never crash audio processing.
"""
import uuid
import json
import logging
from datetime import datetime, timezone
from typing import Optional, Dict, Any, Callable
from sqlalchemy import text
from sqlalchemy.orm import Session

from backend.database import SessionLocal
from backend.transcription.forex_sync import get_live_usd_to_inr_rate

logger = logging.getLogger("isf_recorder.usage_tracker")

# In-memory pricing cache: {model_name: {"id": uuid, "rates": {...}, "cached_at": float}}
_pricing_cache: Dict[str, Dict[str, Any]] = {}
_pricing_cache_time: float = 0.0


def get_model_pricing(db: Session, model_name: str, at: Optional[datetime] = None) -> Dict[str, Any]:
    """
    Fetches the active price of a model from isf_recorder.gemini_model_pricing.
    Prices live only in that table (admins update them via POST /admin/gemini-usage/pricing).
    A model with no active row is costed at 0 and a warning is logged, never at a guessed price.
    """
    # Normalize model name e.g. "models/gemini-2.5-flash" -> "gemini-2.5-flash"
    clean_model = model_name.replace("models/", "").strip().lower()

    try:
        row = db.execute(
            text("""
                SELECT id, input_price_per_million, output_price_per_million,
                       audio_price_per_million, cached_input_price_per_million, price_per_audio_hour
                FROM isf_recorder.gemini_model_pricing
                WHERE gemini_model = :model AND is_active = TRUE
                  AND COALESCE(effective_from, '-infinity'::timestamptz) <= :at
                  AND (effective_to IS NULL OR effective_to > :at)
                ORDER BY effective_from DESC NULLS LAST, created_at DESC
                LIMIT 1
            """),
            {"model": clean_model, "at": at or datetime.now(timezone.utc)}
        ).fetchone()

        if row:
            return {
                "id": str(row.id),
                "input_price": float(row.input_price_per_million),
                "output_price": float(row.output_price_per_million),
                "audio_price": float(row.audio_price_per_million),
                "cached_price": float(row.cached_input_price_per_million),
                "audio_hour_price": float(row.price_per_audio_hour or 0),
            }
    except Exception as e:
        logger.warning(f"Error reading model pricing from DB for {clean_model}: {e}")

    logger.warning(f"No active price for model '{clean_model}' in gemini_model_pricing: cost recorded as 0. "
                   f"Add it with POST /admin/gemini-usage/pricing.")
    return {
        "id": None,
        "input_price": 0.0,
        "output_price": 0.0,
        "audio_price": 0.0,
        "cached_price": 0.0,
        "audio_hour_price": 0.0,
    }


def extract_usage_tokens(usage_metadata: Any) -> Dict[str, Any]:
    """
    Extracts granular token metrics from Google GenAI usage_metadata.
    Accurately identifies audio modality tokens from prompt_tokens_details.
    """
    if not usage_metadata:
        return {
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "input_audio_tokens": 0,
            "cached_input_tokens": 0,
            "thinking_tokens": 0,
            "raw_json": {}
        }

    # Extract raw dictionary for auditing
    raw_dict = {}
    if hasattr(usage_metadata, "model_dump"):
        try:
            raw_dict = usage_metadata.model_dump()
        except Exception:
            pass
    elif hasattr(usage_metadata, "to_json_dict"):
        try:
            raw_dict = usage_metadata.to_json_dict()
        except Exception:
            pass
    elif isinstance(usage_metadata, dict):
        raw_dict = usage_metadata

    prompt_tokens = int(getattr(usage_metadata, "prompt_token_count", 0) or 0)
    candidates_tokens = int(getattr(usage_metadata, "candidates_token_count", 0) or 0)
    total_tokens = int(getattr(usage_metadata, "total_token_count", 0) or 0)
    cached_tokens = int(getattr(usage_metadata, "cached_content_token_count", 0) or 0)
    thinking_tokens = int(getattr(usage_metadata, "thoughts_token_count", 0) or 0)

    # Extract audio tokens from modality details
    audio_tokens = 0
    prompt_details = getattr(usage_metadata, "prompt_tokens_details", None)
    if prompt_details and isinstance(prompt_details, (list, tuple)):
        for item in prompt_details:
            modality = getattr(item, "modality", None) or (item.get("modality") if isinstance(item, dict) else None)
            token_count = getattr(item, "token_count", 0) or (item.get("token_count", 0) if isinstance(item, dict) else 0)
            if modality and "AUDIO" in str(modality).upper():
                audio_tokens += int(token_count)

    # If audio tokens were detected, calculate non-audio text tokens
    text_input_tokens = max(0, prompt_tokens - audio_tokens - cached_tokens)

    return {
        "input_tokens": text_input_tokens,
        "output_tokens": candidates_tokens,
        "total_tokens": total_tokens if total_tokens > 0 else (prompt_tokens + candidates_tokens),
        "input_audio_tokens": audio_tokens,
        "cached_input_tokens": cached_tokens,
        "thinking_tokens": thinking_tokens,
        "raw_json": raw_dict
    }


def calculate_cost(
    tokens: Dict[str, Any],
    pricing: Dict[str, Any],
    exchange_rate: float
) -> Dict[str, float]:
    """
    Calculates estimated USD and INR costs from token counts and pricing.
    """
    input_text = tokens.get("input_tokens", 0)
    input_audio = tokens.get("input_audio_tokens", 0)
    cached_input = tokens.get("cached_input_tokens", 0)
    # Thinking tokens are billed as output (Gemini 2.5/3.x); candidates_token_count excludes them
    output_tokens = tokens.get("output_tokens", 0) + (tokens.get("thinking_tokens", 0) or 0)

    input_cost_usd = (input_text / 1_000_000.0) * pricing["input_price"]
    audio_cost_usd = (input_audio / 1_000_000.0) * pricing["audio_price"]
    cached_cost_usd = (cached_input / 1_000_000.0) * pricing["cached_price"]
    output_cost_usd = (output_tokens / 1_000_000.0) * pricing["output_price"]

    total_usd = input_cost_usd + audio_cost_usd + cached_cost_usd + output_cost_usd
    total_inr = total_usd * exchange_rate

    return {
        "input_cost_usd": round(input_cost_usd, 6),
        "audio_cost_usd": round(audio_cost_usd, 6),
        "cached_cost_usd": round(cached_cost_usd, 6),
        "output_cost_usd": round(output_cost_usd, 6),
        "total_cost_usd": round(total_usd, 6),
        "total_cost_inr": round(total_inr, 4),
    }


def log_gemini_call(
    recording_id: Optional[str],
    operation_type: str,
    gemini_model: str,
    gemini_request_id: str,
    request_timestamp: datetime,
    response_timestamp: datetime,
    duration_ms: int,
    tokens: Dict[str, Any],
    costs: Dict[str, float],
    pricing_id: Optional[str],
    exchange_rate: float,
    api_status: str = "SUCCESS",
    error_message: Optional[str] = None,
    audio_duration_seconds: float = 0.0,
    db: Optional[Session] = None,
    org_id: Optional[str] = None
) -> None:
    """
    Persists an individual Gemini usage log record into the database.
    Fault-tolerant: Catches and logs any DB write error without raising.
    """
    from backend.transcription.run_context import current_run_id

    close_db = False
    sess = db
    if sess is None:
        sess = SessionLocal()
        close_db = True

    try:
        sess.execute(
            text("""
                INSERT INTO isf_recorder.gemini_usage_log
                (
                    run_id, recording_id, org_id, operation_type, gemini_model, gemini_request_id,
                    request_timestamp, response_timestamp, duration_ms,
                    input_tokens, output_tokens, total_tokens, input_audio_tokens,
                    cached_input_tokens, thinking_tokens, audio_duration_seconds,
                    api_status, error_message, pricing_model_id, exchange_rate,
                    input_cost_usd, output_cost_usd, audio_cost_usd, cached_cost_usd,
                    total_cost_usd, total_cost_inr, currency, raw_usage_json
                )
                VALUES
                (
                    :run_id, :recording_id,
                    COALESCE(CAST(:org_id AS uuid),
                             (SELECT r.org_id FROM isf_recorder.recording r WHERE r.id = CAST(:recording_id AS uuid))),
                    :operation_type, :gemini_model, :gemini_request_id,
                    :request_timestamp, :response_timestamp, :duration_ms,
                    :input_tokens, :output_tokens, :total_tokens, :input_audio_tokens,
                    :cached_input_tokens, :thinking_tokens, :audio_duration_seconds,
                    :api_status, :error_message, :pricing_model_id, :exchange_rate,
                    :input_cost_usd, :output_cost_usd, :audio_cost_usd, :cached_cost_usd,
                    :total_cost_usd, :total_cost_inr, 'INR', CAST(:raw_usage_json AS jsonb)
                )
            """),
            {
                "run_id": current_run_id.get(),
                "recording_id": recording_id,
                "org_id": org_id,
                "operation_type": operation_type,
                "gemini_model": gemini_model,
                "gemini_request_id": gemini_request_id,
                "request_timestamp": request_timestamp,
                "response_timestamp": response_timestamp,
                "duration_ms": duration_ms,
                "input_tokens": tokens.get("input_tokens", 0),
                "output_tokens": tokens.get("output_tokens", 0),
                "total_tokens": tokens.get("total_tokens", 0),
                "input_audio_tokens": tokens.get("input_audio_tokens", 0),
                "cached_input_tokens": tokens.get("cached_input_tokens", 0),
                "thinking_tokens": tokens.get("thinking_tokens", 0),
                "audio_duration_seconds": audio_duration_seconds,
                "api_status": api_status,
                "error_message": error_message,
                "pricing_model_id": pricing_id,
                "exchange_rate": exchange_rate,
                "input_cost_usd": costs.get("input_cost_usd", 0.0),
                "output_cost_usd": costs.get("output_cost_usd", 0.0),
                "audio_cost_usd": costs.get("audio_cost_usd", 0.0),
                "cached_cost_usd": costs.get("cached_cost_usd", 0.0),
                "total_cost_usd": costs.get("total_cost_usd", 0.0),
                "total_cost_inr": costs.get("total_cost_inr", 0.0),
                "raw_usage_json": json.dumps(tokens.get("raw_json", {})),
            }
        )
        sess.commit()
        logger.info(
            f"Logged Gemini call: rec={recording_id}, op={operation_type}, model={gemini_model}, "
            f"in={tokens.get('input_tokens')}, audio={tokens.get('input_audio_tokens')}, out={tokens.get('output_tokens')}, "
            f"cost=₹{costs.get('total_cost_inr', 0.0):.2f} (USD ${costs.get('total_cost_usd', 0.0):.4f} @ {exchange_rate:.2f})"
        )
    except Exception as e:
        logger.error(f"Failed to save Gemini usage log: {e}")
        try:
            sess.rollback()
        except Exception:
            pass
    finally:
        if close_db:
            sess.close()


def track_gemini_call(
    recording_id: Optional[str],
    operation_type: str,
    gemini_model: str,
    call_fn: Callable[[], Any],
    audio_duration_seconds: float = 0.0,
    db: Optional[Session] = None,
    org_id: Optional[str] = None
) -> Any:
    """
    Execution wrapper for Gemini API calls.
    - Generates correlation ID
    - Measures duration
    - Executes call_fn()
    - On success: extracts tokens, computes USD and INR costs, and logs record
    - On failure: logs FAILED status with error details, then re-raises exception
    """
    req_prefix = operation_type[:3].lower()
    gemini_request_id = f"gemini_{req_prefix}_{uuid.uuid4().hex[:12]}"
    req_time = datetime.now(timezone.utc)

    # Get live forex rate and pricing
    own_db = False
    sess = db
    if sess is None:
        sess = SessionLocal()
        own_db = True

    try:
        pricing = get_model_pricing(sess, gemini_model)
        exchange_rate = get_live_usd_to_inr_rate(sess)
    finally:
        if own_db:
            sess.close()

    try:
        response = call_fn()
        resp_time = datetime.now(timezone.utc)
        duration_ms = int((resp_time - req_time).total_seconds() * 1000)

        # Extract usage metadata
        usage_metadata = getattr(response, "usage_metadata", None)
        tokens = extract_usage_tokens(usage_metadata)
        costs = calculate_cost(tokens, pricing, exchange_rate)

        log_gemini_call(
            recording_id=recording_id,
            operation_type=operation_type,
            gemini_model=gemini_model,
            gemini_request_id=gemini_request_id,
            request_timestamp=req_time,
            response_timestamp=resp_time,
            duration_ms=duration_ms,
            tokens=tokens,
            costs=costs,
            pricing_id=pricing.get("id"),
            exchange_rate=exchange_rate,
            api_status="SUCCESS",
            error_message=None,
            audio_duration_seconds=audio_duration_seconds,
            db=db,
            org_id=org_id
        )
        return response

    except Exception as exc:
        resp_time = datetime.now(timezone.utc)
        duration_ms = int((resp_time - req_time).total_seconds() * 1000)

        # Log failed attempt
        tokens = {
            "input_tokens": 0, "output_tokens": 0, "total_tokens": 0,
            "input_audio_tokens": 0, "cached_input_tokens": 0, "thinking_tokens": 0,
            "raw_json": {"error": str(exc)}
        }
        costs = {
            "input_cost_usd": 0.0, "audio_cost_usd": 0.0, "cached_cost_usd": 0.0,
            "output_cost_usd": 0.0, "total_cost_usd": 0.0, "total_cost_inr": 0.0
        }

        log_gemini_call(
            recording_id=recording_id,
            operation_type=operation_type,
            gemini_model=gemini_model,
            gemini_request_id=gemini_request_id,
            request_timestamp=req_time,
            response_timestamp=resp_time,
            duration_ms=duration_ms,
            tokens=tokens,
            costs=costs,
            pricing_id=pricing.get("id"),
            exchange_rate=exchange_rate,
            api_status="FAILED",
            error_message=str(exc)[:500],
            audio_duration_seconds=audio_duration_seconds,
            db=db,
            org_id=org_id
        )
        raise exc
