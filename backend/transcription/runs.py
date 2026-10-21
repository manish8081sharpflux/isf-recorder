"""
ISF Recorder — Transcription runs
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Each transcription of a recording is stored as a run (model, transcript, summary, cost), so the
same audio can be compared across models. The recording's own fields hold the active run.
"""
import json
import logging
import re
from typing import Any, Dict, List, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

logger = logging.getLogger("isf_recorder.runs")


def _json(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value if value is not None else [])


def model_display_name(model_id: Optional[str]) -> str:
    from backend.settings.routes import AVAILABLE_MODELS
    for m in AVAILABLE_MODELS:
        if m["id"] == model_id:
            return m["name"]
    legacy = {
        "assemblyai-universal-1": "AssemblyAI Universal-2",
        "assemblyai-nano": "AssemblyAI Universal-2",
        "gemini-2.5-pro": "Gemini 2.5 Pro",
        "gemini-3.0-flash": "Gemini 3.0 Flash",
        "gemini-3.5-pro": "Gemini 3.5 Pro",
    }
    return legacy.get(model_id or "", model_id or "Unknown model")


def snapshot_original_run(db: Session, recording_id: str) -> Optional[str]:
    """
    Before the first tracked run overwrites a recording, keep its existing transcript as the
    'original' run and attach the usage-log rows that produced it, so its cost stays visible.
    """
    if db.execute(text("SELECT 1 FROM isf_recorder.transcription_run WHERE recording_id = :id LIMIT 1"),
                  {"id": recording_id}).fetchone():
        return None

    rec = db.execute(text("""
        SELECT transcript, title, overview, key_points, decisions, action_items, detected_language,
               stt_engine, created_at
        FROM isf_recorder.recording WHERE id = :id
    """), {"id": recording_id}).fetchone()
    if not rec or not (rec.transcript or "").strip():
        return None

    usage = db.execute(text("""
        SELECT MIN(request_timestamp) AS first_call, MAX(response_timestamp) AS last_call,
               (SELECT gemini_model FROM isf_recorder.gemini_usage_log
                WHERE recording_id = :id AND run_id IS NULL AND operation_type = 'TRANSCRIPTION'
                GROUP BY gemini_model ORDER BY COUNT(*) DESC LIMIT 1) AS stt_model
        FROM isf_recorder.gemini_usage_log
        WHERE recording_id = :id AND run_id IS NULL AND operation_type IN ('TRANSCRIPTION', 'SUMMARY')
    """), {"id": recording_id}).fetchone()

    model = (usage.stt_model if usage else None) or (
        "assemblyai-universal-2" if rec.stt_engine == "assemblyai" else "gemini-2.5-flash")

    segments = [
        {"speaker": s.speaker, "text": s.text, "language": s.language,
         "start_time": float(s.start_time or 0), "end_time": float(s.end_time or 0)}
        for s in db.execute(text("""
            SELECT speaker, text, language, start_time, end_time FROM isf_recorder.transcript_segment
            WHERE recording_id = :id ORDER BY start_time ASC
        """), {"id": recording_id}).fetchall()
    ]

    run_id = db.execute(text("""
        INSERT INTO isf_recorder.transcription_run
            (recording_id, model, stt_engine, status, is_original, started_at, finished_at, detected_language,
             title, transcript, segments, overview, key_points, decisions, action_items)
        VALUES
            (:rid, :model, :engine, 'completed', TRUE, :started, :finished, :lang,
             :title, :transcript, CAST(:segments AS jsonb), :overview, CAST(:kp AS jsonb), CAST(:dec AS jsonb),
             CAST(:ai AS jsonb))
        RETURNING id
    """), {
        "rid": recording_id,
        "model": model,
        "engine": rec.stt_engine or "gemini",
        "started": (usage.first_call if usage else None) or rec.created_at,
        "finished": (usage.last_call if usage else None) or rec.created_at,
        "lang": rec.detected_language,
        "title": rec.title,
        "transcript": rec.transcript,
        "segments": json.dumps(segments),
        "overview": rec.overview,
        "kp": _json(rec.key_points),
        "dec": _json(rec.decisions),
        "ai": _json(rec.action_items),
    }).scalar()

    db.execute(text("""
        UPDATE isf_recorder.gemini_usage_log SET run_id = :run
        WHERE recording_id = :id AND run_id IS NULL AND operation_type IN ('TRANSCRIPTION', 'SUMMARY')
    """), {"run": str(run_id), "id": recording_id})
    db.execute(text("UPDATE isf_recorder.recording SET active_run_id = :run WHERE id = :id"),
               {"run": str(run_id), "id": recording_id})
    db.commit()
    logger.info(f"Saved existing transcript of {recording_id} as original run {run_id} ({model})")
    return str(run_id)


def start_run(db: Session, recording_id: str, model: str, stt_engine: str, requested_by: Optional[str]) -> str:
    run_id = db.execute(text("""
        INSERT INTO isf_recorder.transcription_run (recording_id, model, stt_engine, status, requested_by)
        VALUES (:rid, :model, :engine, 'processing', :by)
        RETURNING id
    """), {"rid": recording_id, "model": model, "engine": stt_engine, "by": requested_by}).scalar()
    db.commit()
    return str(run_id)


def complete_run(db: Session, run_id: str, result: Dict[str, Any]) -> None:
    """Saves a finished run. Caller commits (together with the recording update)."""
    db.execute(text("""
        UPDATE isf_recorder.transcription_run
        SET status = 'completed', finished_at = NOW(), error_message = NULL,
            detected_language = :lang, title = :title, transcript = :transcript,
            segments = CAST(:segments AS jsonb), overview = :overview,
            key_points = CAST(:kp AS jsonb), decisions = CAST(:dec AS jsonb), action_items = CAST(:ai AS jsonb)
        WHERE id = :run
    """), {
        "run": run_id,
        "lang": result.get("detected_language"),
        "title": result.get("title"),
        "transcript": result.get("transcript"),
        "segments": json.dumps([
            {"speaker": s.get("speaker", "SPEAKER_00"), "text": s.get("text", ""), "language": s.get("language"),
             "start_time": float(s.get("start_time", 0.0)), "end_time": float(s.get("end_time", 0.0))}
            for s in result.get("segments", [])
        ]),
        "overview": result.get("overview"),
        "kp": _json(result.get("key_points")),
        "dec": _json(result.get("decisions")),
        "ai": _json(result.get("action_items")),
    })


def fail_run(db: Session, run_id: str, error: str) -> None:
    try:
        db.execute(text("""
            UPDATE isf_recorder.transcription_run
            SET status = 'error', finished_at = NOW(), error_message = :err
            WHERE id = :run
        """), {"run": run_id, "err": error[:500]})
        db.commit()
    except Exception as e:
        logger.warning(f"Could not mark run {run_id} as failed: {e}")


def _speaker_id(label: str) -> int:
    match = re.search(r"SPEAKER_(\d+)", label or "")
    return int(match.group(1)) if match else 0


def activate_run(db: Session, recording_id: str, run_id: str) -> bool:
    """Makes a stored run the recording's main transcript (what the app, chat and sharing use)."""
    run = db.execute(text("""
        SELECT * FROM isf_recorder.transcription_run
        WHERE id = :run AND recording_id = :rid AND status = 'completed'
    """), {"run": run_id, "rid": recording_id}).fetchone()
    if not run:
        return False

    rec = db.execute(text("SELECT org_id, user_id FROM isf_recorder.recording WHERE id = :id"),
                     {"id": recording_id}).fetchone()
    segments = run.segments if isinstance(run.segments, list) else json.loads(run.segments or "[]")
    action_items = run.action_items if isinstance(run.action_items, list) else json.loads(run.action_items or "[]")

    db.execute(text("DELETE FROM isf_recorder.transcript_segment WHERE recording_id = :id"), {"id": recording_id})
    for s in segments:
        db.execute(text("""
            INSERT INTO isf_recorder.transcript_segment
            (recording_id, speaker, speaker_id, text, language, start_time, end_time, confidence)
            VALUES (:rid, :speaker, :sid, :text, :lang, :st, :et, :conf)
        """), {
            "rid": recording_id, "speaker": s.get("speaker", "SPEAKER_00"), "sid": _speaker_id(s.get("speaker", "")),
            "text": s.get("text", ""), "lang": s.get("language") or run.detected_language or "mixed",
            "st": float(s.get("start_time", 0.0)), "et": float(s.get("end_time", 0.0)), "conf": 0.9,
        })

    db.execute(text("DELETE FROM isf_recorder.action_item WHERE recording_id = :id"), {"id": recording_id})
    for item in action_items:
        if not isinstance(item, dict):
            continue
        db.execute(text("""
            INSERT INTO isf_recorder.action_item (recording_id, org_id, user_id, assigned_to, description, urgency)
            VALUES (:rid, :org, :uid, :assigned, :desc, :urgency)
        """), {
            "rid": recording_id, "org": str(rec.org_id), "uid": str(rec.user_id) if rec.user_id else None,
            "assigned": item.get("assigned_to"), "desc": item.get("description", ""),
            "urgency": item.get("urgency", "normal"),
        })

    db.execute(text("""
        UPDATE isf_recorder.recording
        SET transcript = :transcript, overview = :overview, detected_language = :lang,
            stt_engine = :engine, key_points = CAST(:kp AS jsonb), decisions = CAST(:dec AS jsonb),
            action_items = CAST(:ai AS jsonb), active_run_id = :run, status = 'completed', error_message = NULL,
            pipeline_stage = 'done'
        WHERE id = :id
    """), {
        "id": recording_id, "run": run_id, "transcript": run.transcript, "overview": run.overview,
        "lang": run.detected_language, "engine": run.stt_engine,
        "kp": _json(run.key_points), "dec": _json(run.decisions), "ai": _json(action_items),
    })
    db.commit()
    return True


def _segment_stats(segments: List[Dict[str, Any]]) -> Dict[str, Any]:
    texts = [str(s.get("text", "")).strip().lower() for s in segments]
    return {
        "lines": len(segments),
        "unique_lines": len(set(t for t in texts if t)),
        "words": sum(len(t.split()) for t in texts),
        "last_timestamp": max((float(s.get("end_time") or 0) for s in segments), default=0.0),
    }


def list_runs(db: Session, recording_id: str) -> Dict[str, Any]:
    rec = db.execute(text("SELECT active_run_id, duration_seconds FROM isf_recorder.recording WHERE id = :id"),
                     {"id": recording_id}).fetchone()

    costs = {
        str(r.run_id): r for r in db.execute(text("""
            SELECT run_id,
                   COUNT(*) AS calls,
                   COUNT(*) FILTER (WHERE api_status <> 'SUCCESS') AS failed_calls,
                   COALESCE(SUM(total_cost_inr), 0) AS cost_inr,
                   COALESCE(SUM(total_cost_inr) FILTER (WHERE operation_type = 'TRANSCRIPTION'), 0) AS transcript_cost_inr,
                   COALESCE(SUM(total_cost_inr) FILTER (WHERE operation_type = 'SUMMARY'), 0) AS summary_cost_inr,
                   COALESCE(SUM(output_tokens), 0) AS output_tokens,
                   ARRAY_AGG(DISTINCT gemini_model) FILTER (WHERE operation_type = 'TRANSCRIPTION') AS stt_models
            FROM isf_recorder.gemini_usage_log
            WHERE recording_id = :id AND run_id IS NOT NULL
            GROUP BY run_id
        """), {"id": recording_id}).fetchall()
    }
    chat = db.execute(text("""
        SELECT COUNT(*) AS calls, COALESCE(SUM(total_cost_inr), 0) AS cost_inr
        FROM isf_recorder.gemini_usage_log
        WHERE recording_id = :id AND operation_type = 'CHAT' AND api_status = 'SUCCESS'
    """), {"id": recording_id}).fetchone()

    runs = []
    for r in db.execute(text("""
        SELECT id, model, stt_engine, status, is_original, started_at, finished_at, error_message, segments
        FROM isf_recorder.transcription_run WHERE recording_id = :id ORDER BY started_at DESC
    """), {"id": recording_id}).fetchall():
        c = costs.get(str(r.id))
        segments = r.segments if isinstance(r.segments, list) else json.loads(r.segments or "[]")
        processing_seconds = None
        if r.started_at and r.finished_at:
            processing_seconds = int((r.finished_at - r.started_at).total_seconds())
        runs.append({
            "id": str(r.id),
            "model": r.model,
            "model_name": model_display_name(r.model),
            "stt_engine": r.stt_engine,
            "status": r.status,
            "is_original": r.is_original,
            "is_active": rec is not None and str(rec.active_run_id) == str(r.id),
            "started_at": r.started_at.isoformat() if r.started_at else None,
            "finished_at": r.finished_at.isoformat() if r.finished_at else None,
            "processing_seconds": processing_seconds,
            "error_message": r.error_message,
            "cost_inr": round(float(c.cost_inr), 2) if c else 0.0,
            "transcript_cost_inr": round(float(c.transcript_cost_inr), 2) if c else 0.0,
            "summary_cost_inr": round(float(c.summary_cost_inr), 2) if c else 0.0,
            "calls": int(c.calls) if c else 0,
            "failed_calls": int(c.failed_calls) if c else 0,
            "output_tokens": int(c.output_tokens) if c else 0,
            "models_used": [m for m in (c.stt_models or []) if m] if c else [],
            **_segment_stats(segments),
        })

    return {
        "recording_id": recording_id,
        "duration_seconds": rec.duration_seconds if rec else None,
        "active_run_id": str(rec.active_run_id) if rec and rec.active_run_id else None,
        "chat_calls": int(chat.calls or 0) if chat else 0,
        "chat_cost_inr": round(float(chat.cost_inr or 0), 2) if chat else 0.0,
        "runs": runs,
    }


def get_run(db: Session, recording_id: str, run_id: str) -> Optional[Dict[str, Any]]:
    r = db.execute(text("""
        SELECT * FROM isf_recorder.transcription_run WHERE id = :run AND recording_id = :rid
    """), {"run": run_id, "rid": recording_id}).fetchone()
    if not r:
        return None
    summary = next((x for x in list_runs(db, recording_id)["runs"] if x["id"] == run_id), {})

    def as_list(v):
        if v is None:
            return []
        return v if isinstance(v, list) else json.loads(v)

    return {
        **summary,
        "title": r.title,
        "detected_language": r.detected_language,
        "transcript": r.transcript,
        "overview": r.overview,
        "key_points": as_list(r.key_points),
        "decisions": as_list(r.decisions),
        "action_items": as_list(r.action_items),
        "segments": as_list(r.segments),
    }
