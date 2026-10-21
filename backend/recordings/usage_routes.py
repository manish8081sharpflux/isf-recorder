"""
ISF Recorder — Gemini Usage & Cost Reporting Endpoints
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Provides APIs for:
1. Recording-level usage & estimated cost breakdown (GET /recordings/{recording_id}/gemini-usage)
2. Monthly administrative cost dashboard (GET /admin/gemini-usage/monthly)
3. Pricing & live exchange rate configuration (GET/POST /admin/gemini-usage/pricing, POST /admin/gemini-usage/exchange-rate)
"""
import logging
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from backend.database import get_db
from backend.auth.routes import get_current_user
from backend.transcription.forex_sync import get_live_usd_to_inr_rate
from backend.calls.access import call_access, CALL_VISIBILITY_SQL, visibility_params

logger = logging.getLogger("isf_recorder.usage_routes")

# Two routers: one for recording-level usage (/recordings/{id}/gemini-usage),
# one for admin-level usage & pricing (/admin/gemini-usage)
recording_usage_router = APIRouter(prefix="/recordings", tags=["Recordings — Gemini Usage"])
admin_usage_router = APIRouter(prefix="/admin/gemini-usage", tags=["Admin — Gemini Usage & Pricing"])


def _format_seconds(sec: float) -> str:
    """Formats seconds into MM:SS or HH:MM:SS."""
    s = max(0, int(sec))
    h = s // 3600
    m = (s % 3600) // 60
    sec_rem = s % 60
    if h > 0:
        return f"{h:02d}:{m:02d}:{sec_rem:02d}"
    return f"{m:02d}:{sec_rem:02d}"


# ─── 1. Recording-Level Usage ────────────────────────────────────────────────

@recording_usage_router.get("/{recording_id}/gemini-usage")
def get_recording_gemini_usage(
    recording_id: str,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Returns aggregated Gemini API usage and cost summary for a specific recording,
    along with individual call records for detailed auditing.
    """
    call_access(db, recording_id, current_user, write=False)   # calls are private to their owner
    # Verify recording exists and user has access
    rec = db.execute(
        text("""
            SELECT id, user_id, org_id, duration_seconds, title
            FROM isf_recorder.recording
            WHERE id = :id AND is_deleted = FALSE
        """),
        {"id": recording_id}
    ).fetchone()

    if not rec:
        raise HTTPException(status_code=404, detail="Recording not found")

    if str(rec.org_id) != current_user["org_id"]:
        raise HTTPException(status_code=403, detail="Permission denied")

    # Fetch individual Gemini usage logs
    logs = db.execute(
        text("""
            SELECT id, operation_type, gemini_model, request_timestamp, response_timestamp,
                   duration_ms, input_tokens, output_tokens, total_tokens,
                   input_audio_tokens, cached_input_tokens, thinking_tokens,
                   audio_duration_seconds, api_status, error_message,
                   exchange_rate, input_cost_usd, output_cost_usd, audio_cost_usd,
                   cached_cost_usd, total_cost_usd, total_cost_inr, currency
            FROM isf_recorder.gemini_usage_log
            WHERE recording_id = :rec_id
            ORDER BY request_timestamp ASC
        """),
        {"rec_id": recording_id}
    ).fetchall()

    calls = []
    total_in = 0
    total_out = 0
    total_audio = 0
    total_cached = 0
    total_tokens_all = 0
    total_cost_inr_all = 0.0
    total_cost_usd_all = 0.0
    transcription_calls = 0
    summary_calls = 0
    chat_calls = 0
    successful_calls = 0
    failed_calls = 0
    primary_model = "gemini-2.5-flash"
    transcription_model = None
    last_rate = 96.0

    for row in logs:
        op = row.operation_type
        if op == "TRANSCRIPTION":
            transcription_calls += 1
            transcription_model = row.gemini_model
        elif op == "SUMMARY":
            summary_calls += 1
        elif op == "CHAT":
            chat_calls += 1

        if row.api_status == "SUCCESS":
            successful_calls += 1
        else:
            failed_calls += 1

        in_tok = row.input_tokens or 0
        out_tok = row.output_tokens or 0
        aud_tok = row.input_audio_tokens or 0
        cac_tok = row.cached_input_tokens or 0
        tot_tok = row.total_tokens or (in_tok + out_tok + aud_tok)
        cost_inr = float(row.total_cost_inr or 0.0)
        cost_usd = float(row.total_cost_usd or 0.0)
        rate = float(row.exchange_rate or 96.0)

        total_in += in_tok
        total_out += out_tok
        total_audio += aud_tok
        total_cached += cac_tok
        total_tokens_all += tot_tok
        total_cost_inr_all += cost_inr
        total_cost_usd_all += cost_usd
        if not transcription_model:
            primary_model = row.gemini_model or primary_model
        last_rate = rate

        calls.append({
            "id": str(row.id),
            "operation_type": op,
            "model": row.gemini_model,
            "request_timestamp": row.request_timestamp.isoformat() if row.request_timestamp else None,
            "duration_ms": row.duration_ms or 0,
            "input_tokens": in_tok,
            "output_tokens": out_tok,
            "audio_tokens": aud_tok,
            "cached_tokens": cac_tok,
            "thinking_tokens": row.thinking_tokens or 0,
            "total_tokens": tot_tok,
            "api_status": row.api_status,
            "error_message": row.error_message,
            "estimated_cost_usd": round(cost_usd, 6),
            "estimated_cost": round(cost_inr, 2),
            "estimated_cost_formatted": f"₹{cost_inr:.2f}",
        })

    if transcription_model:
        primary_model = transcription_model
    elif getattr(rec, "stt_engine", None) == "assemblyai":
        primary_model = "assemblyai-universal-2"

    duration_sec = rec.duration_seconds or 0

    return {
        "recording_id": str(rec.id),
        "title": rec.title,
        "audio_duration_seconds": duration_sec,
        "audio_duration_formatted": _format_seconds(duration_sec),
        "primary_model": primary_model,
        "total_input_tokens": total_in,
        "total_output_tokens": total_out,
        "total_audio_tokens": total_audio,
        "total_cached_tokens": total_cached,
        "total_tokens": total_tokens_all,
        "transcription_calls": transcription_calls,
        "summary_calls": summary_calls,
        "chat_calls": chat_calls,
        "total_gemini_calls": len(logs),
        "successful_calls": successful_calls,
        "failed_calls": failed_calls,
        "exchange_rate": round(last_rate, 2),
        "estimated_cost_usd": round(total_cost_usd_all, 4),
        "estimated_cost": round(total_cost_inr_all, 2),
        "estimated_cost_formatted": f"₹{total_cost_inr_all:.2f}",
        "currency": "INR",
        "disclaimer": "Estimated application-level cost based on actual Gemini token usage and active pricing. Not Google Cloud's final invoice amount.",
        "calls": calls
    }


# ─── 2. Monthly Dashboard (Admin) ────────────────────────────────────────────

@admin_usage_router.get("/monthly")
def get_monthly_usage_dashboard(
    year: Optional[int] = Query(None),
    month: Optional[int] = Query(None),
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Returns monthly aggregated Gemini API usage and estimated INR cost across all recordings.
    Allows comparing JOMI estimated application-level usage with Google Cloud's final invoice.
    """
    if current_user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")

    org_id = current_user["org_id"]
    now = datetime.now(timezone.utc)
    target_year = year or now.year
    target_month = month or now.month

    # Aggregate monthly stats
    stats_query = text("""
        SELECT
            COUNT(DISTINCT l.recording_id) as total_recordings,
            COUNT(l.id) as total_calls,
            COUNT(CASE WHEN l.operation_type = 'TRANSCRIPTION' THEN 1 END) as transcription_calls,
            COUNT(CASE WHEN l.operation_type = 'SUMMARY' THEN 1 END) as summary_calls,
            COUNT(CASE WHEN l.operation_type = 'CHAT' THEN 1 END) as chat_calls,
            COUNT(CASE WHEN l.operation_type = 'REPORT' THEN 1 END) as report_calls,
            COUNT(CASE WHEN l.api_status = 'SUCCESS' THEN 1 END) as successful_calls,
            COUNT(CASE WHEN l.api_status = 'FAILED' THEN 1 END) as failed_calls,
            COALESCE(SUM(l.input_tokens), 0) as total_input_tokens,
            COALESCE(SUM(l.input_audio_tokens), 0) as total_audio_tokens,
            COALESCE(SUM(l.output_tokens), 0) as total_output_tokens,
            COALESCE(SUM(l.cached_input_tokens), 0) as total_cached_tokens,
            COALESCE(SUM(l.total_tokens), 0) as total_tokens,
            COALESCE(SUM(l.total_cost_usd), 0) as total_cost_usd,
            COALESCE(SUM(l.total_cost_inr), 0) as total_cost_inr
        FROM isf_recorder.gemini_usage_log l
        LEFT JOIN isf_recorder.recording r ON l.recording_id = r.id
        WHERE EXTRACT(YEAR FROM l.request_timestamp) = :yr
          AND EXTRACT(MONTH FROM l.request_timestamp) = :mo
          AND (COALESCE(l.org_id, r.org_id) = :org_id OR (l.org_id IS NULL AND l.recording_id IS NULL))
    """)

    row = db.execute(stats_query, {
        "yr": target_year,
        "mo": target_month,
        "org_id": org_id
    }).fetchone()

    month_name = datetime(target_year, target_month, 1).strftime("%B %Y")
    cost_inr = float(row.total_cost_inr or 0.0)
    cost_usd = float(row.total_cost_usd or 0.0)

    # Breakdown by model
    model_breakdown_query = text("""
        SELECT
            l.gemini_model,
            COUNT(l.id) as calls,
            COALESCE(SUM(l.total_tokens), 0) as tokens,
            COALESCE(SUM(l.total_cost_inr), 0) as cost_inr
        FROM isf_recorder.gemini_usage_log l
        LEFT JOIN isf_recorder.recording r ON l.recording_id = r.id
        WHERE EXTRACT(YEAR FROM l.request_timestamp) = :yr
          AND EXTRACT(MONTH FROM l.request_timestamp) = :mo
          AND (COALESCE(l.org_id, r.org_id) = :org_id OR (l.org_id IS NULL AND l.recording_id IS NULL))
        GROUP BY l.gemini_model
        ORDER BY cost_inr DESC
    """)
    models = db.execute(model_breakdown_query, {
        "yr": target_year,
        "mo": target_month,
        "org_id": org_id
    }).fetchall()

    return {
        "month": month_name,
        "year": target_year,
        "month_number": target_month,
        "total_recordings": row.total_recordings or 0,
        "total_gemini_calls": row.total_calls or 0,
        "transcription_calls": row.transcription_calls or 0,
        "summary_calls": row.summary_calls or 0,
        "chat_calls": row.chat_calls or 0,
        "report_calls": row.report_calls or 0,
        "successful_calls": row.successful_calls or 0,
        "failed_calls": row.failed_calls or 0,
        "total_input_tokens": int(row.total_input_tokens or 0),
        "total_audio_tokens": int(row.total_audio_tokens or 0),
        "total_output_tokens": int(row.total_output_tokens or 0),
        "total_cached_tokens": int(row.total_cached_tokens or 0),
        "total_tokens": int(row.total_tokens or 0),
        "estimated_cost_usd": round(cost_usd, 4),
        "estimated_gemini_cost": round(cost_inr, 2),
        "estimated_gemini_cost_formatted": f"₹{cost_inr:,.2f}",
        "currency": "INR",
        "models": [
            {
                "model": m.gemini_model,
                "calls": m.calls,
                "tokens": int(m.tokens),
                "estimated_cost_inr": round(float(m.cost_inr), 2),
                "estimated_cost_formatted": f"₹{float(m.cost_inr):,.2f}",
            }
            for m in models
        ],
        "disclaimer": "Estimated application-level cost, not Google Cloud's final invoice amount."
    }


# ─── 3. Pricing Configuration (Admin) ─────────────────────────────────────────

class ModelPricingUpdateRequest(BaseModel):
    gemini_model: str
    input_price_per_million: float = 0.0
    output_price_per_million: float = 0.0
    audio_price_per_million: float = 0.0
    cached_input_price_per_million: float = 0.0
    price_per_audio_hour: float = 0.0  # for models billed per audio hour (AssemblyAI)


class ExchangeRateUpdateRequest(BaseModel):
    exchange_rate: Optional[float] = None
    force_sync_from_internet: bool = False


@admin_usage_router.get("/pricing")
def get_pricing_configuration(
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Returns active model pricing table and current USD to INR exchange rate."""
    if current_user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")

    rates = db.execute(
        text("""
            SELECT id, gemini_model, input_price_per_million, output_price_per_million,
                   audio_price_per_million, cached_input_price_per_million, price_per_audio_hour,
                   effective_from, is_active
            FROM isf_recorder.gemini_model_pricing
            WHERE is_active = TRUE
            ORDER BY gemini_model ASC
        """)
    ).fetchall()

    current_forex = get_live_usd_to_inr_rate(db)

    row = db.execute(
        text("SELECT updated_at FROM isf_recorder.app_config WHERE key = 'usd_to_inr_exchange_rate'")
    ).fetchone()
    last_synced = row[0].isoformat() if row and row[0] else None

    return {
        "exchange_rate": round(current_forex, 4),
        "exchange_rate_formatted": f"1 USD = ₹{current_forex:.2f}",
        "currency": "INR",
        "last_synced_at": last_synced,
        "models": [
            {
                "id": str(r.id),
                "gemini_model": r.gemini_model,
                "input_price_usd": float(r.input_price_per_million),
                "output_price_usd": float(r.output_price_per_million),
                "audio_price_usd": float(r.audio_price_per_million),
                "cached_price_usd": float(r.cached_input_price_per_million),
                "price_per_audio_hour_usd": float(r.price_per_audio_hour or 0),
                "is_active": r.is_active
            }
            for r in rates
        ]
    }


@admin_usage_router.post("/pricing")
def set_model_pricing(
    req: ModelPricingUpdateRequest,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Adds or updates pricing rates for a Gemini model."""
    if current_user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")

    clean_model = req.gemini_model.replace("models/", "").strip().lower()

    # Deactivate existing rows for this model
    db.execute(
        text("""
            UPDATE isf_recorder.gemini_model_pricing
            SET is_active = FALSE, effective_to = NOW()
            WHERE gemini_model = :model AND is_active = TRUE
        """),
        {"model": clean_model}
    )

    # Insert new active pricing row
    db.execute(
        text("""
            INSERT INTO isf_recorder.gemini_model_pricing
            (gemini_model, input_price_per_million, output_price_per_million,
             audio_price_per_million, cached_input_price_per_million, price_per_audio_hour, effective_from, is_active)
            VALUES
            (:model, :input_p, :output_p, :audio_p, :cached_p, :hour_p, NOW(), TRUE)
        """),
        {
            "model": clean_model,
            "input_p": req.input_price_per_million,
            "output_p": req.output_price_per_million,
            "audio_p": req.audio_price_per_million,
            "cached_p": req.cached_input_price_per_million,
            "hour_p": req.price_per_audio_hour,
        }
    )
    db.commit()
    logger.info(f"Admin updated pricing for model {clean_model}: in={req.input_price_per_million}, audio={req.audio_price_per_million}, out={req.output_price_per_million}, per_hour={req.price_per_audio_hour}")

    return {"status": "success", "message": f"Pricing updated for {clean_model}"}


@admin_usage_router.post("/exchange-rate")
def update_exchange_rate(
    req: ExchangeRateUpdateRequest,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Updates USD to INR exchange rate manually or triggers an immediate live sync from the internet."""
    if current_user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")

    if req.force_sync_from_internet:
        rate = get_live_usd_to_inr_rate(db, force_refresh=True)
        return {
            "status": "success",
            "source": "live_internet_sync",
            "exchange_rate": round(rate, 4),
            "exchange_rate_formatted": f"1 USD = ₹{rate:.2f}"
        }

    if req.exchange_rate and req.exchange_rate > 0:
        db.execute(
            text("""
                INSERT INTO isf_recorder.app_config (key, value, description, updated_at)
                VALUES ('usd_to_inr_exchange_rate', :val, 'Admin manual USD to INR exchange rate', NOW())
                ON CONFLICT (key) DO UPDATE
                SET value = :val, updated_at = NOW()
            """),
            {"val": f"{req.exchange_rate:.4f}"}
        )
        db.commit()
        return {
            "status": "success",
            "source": "manual_override",
            "exchange_rate": round(req.exchange_rate, 4),
            "exchange_rate_formatted": f"1 USD = ₹{req.exchange_rate:.2f}"
        }

    raise HTTPException(status_code=400, detail="Provide an exchange_rate or set force_sync_from_internet=true")
