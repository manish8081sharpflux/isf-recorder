"""
ISF Recorder — Director's POV (21 Oct 2026), ADMINS ONLY
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
One tap in Previous recordings instead of Report → AI chat → / Templates → Director's POV.

GET  /recordings/{id}/director-pov                  → {status: none|generating|ready|error, content, …}
POST /recordings/{id}/director-pov?regenerate=false → makes it (in the background) unless already saved

- The template is the org's active AI chat template whose title contains "Director" (admins edit it in
  Settings → AI chat templates). It runs exactly like picking that template in the recording's AI chat.
- The result is saved; the next tap shows it instantly. Regenerate makes a fresh one.
- Coaches: 403 here, the template is hidden from their template list and refused in AI chat (chat/routes.py).
- Calls never get one (404).
"""
import logging
import threading
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.orm import Session

from backend.auth.routes import require_admin
from backend.database import SessionLocal, get_db

logger = logging.getLogger("isf_recorder.reports.director_pov")
router = APIRouter(prefix="/recordings", tags=["Director's POV"])

STALE_AFTER = timedelta(minutes=10)     # a 'generating' row older than this was cut off (restart): start again


def is_director_template(title: Optional[str]) -> bool:
    return "director" in (title or "").lower()


def find_template(db: Session, org_id: str):
    return db.execute(text("""
        SELECT id, title, prompt, example_output FROM isf_recorder.chat_template
        WHERE org_id = :o AND COALESCE(is_active, TRUE) = TRUE AND title ILIKE '%director%'
        ORDER BY sort_order ASC, created_at ASC LIMIT 1
    """), {"o": org_id}).fetchone()


def _recording(db: Session, recording_id: str, admin: dict):
    try:
        rec = db.execute(text("""
            SELECT id, org_id, source, COALESCE(LENGTH(transcript), 0) > 0
                   OR EXISTS (SELECT 1 FROM isf_recorder.transcript_segment s WHERE s.recording_id = r.id) AS has_transcript
            FROM isf_recorder.recording r WHERE id = CAST(:id AS uuid) AND is_deleted = FALSE
        """), {"id": recording_id}).fetchone()
    except Exception:
        db.rollback()
        rec = None
    if not rec or str(rec.org_id) != admin["org_id"] or rec.source == "call":
        raise HTTPException(status_code=404, detail="Recording not found")
    return rec


def _json(row) -> dict:
    if not row:
        return {"status": "none", "content": ""}
    return {
        "status": row.status,
        "content": row.content or "",
        "template_title": row.template_title,
        "error_message": row.error_message,
        "generated_at": row.generated_at.isoformat() if row.generated_at else None,
        "generated_by_name": row.generated_by_name,
    }


def _saved(db: Session, recording_id: str):
    return db.execute(text("""
        SELECT p.*, u.name AS generated_by_name FROM isf_recorder.recording_director_pov p
        LEFT JOIN isf_recorder."user" u ON u.id = p.generated_by
        WHERE p.recording_id = CAST(:id AS uuid)
    """), {"id": recording_id}).fetchone()


@router.get("/{recording_id}/director-pov")
def get_director_pov(recording_id: str, admin: dict = Depends(require_admin), db: Session = Depends(get_db)):
    rec = _recording(db, recording_id, admin)
    return _json(_saved(db, str(rec.id)))


@router.post("/{recording_id}/director-pov")
def make_director_pov(recording_id: str, regenerate: bool = Query(False), admin: dict = Depends(require_admin),
                      db: Session = Depends(get_db)):
    rec = _recording(db, recording_id, admin)
    rid = str(rec.id)
    row = _saved(db, rid)
    if row and row.status == "ready" and not regenerate:
        return _json(row)
    if row and row.status == "generating" and datetime.now(timezone.utc) - row.started_at < STALE_AFTER:
        return _json(row)
    tpl = find_template(db, admin["org_id"])
    if not tpl:
        raise HTTPException(status_code=404, detail="There is no Director's POV template. Add one in Settings → "
                                                    "AI chat templates with 'Director' in its title.")
    if not rec.has_transcript:
        raise HTTPException(status_code=409, detail="This recording has no transcript yet")
    db.execute(text("""
        INSERT INTO isf_recorder.recording_director_pov (recording_id, template_id, template_title, status, started_at, generated_by)
        VALUES (CAST(:id AS uuid), :t, :title, 'generating', NOW(), CAST(:u AS uuid))
        ON CONFLICT (recording_id) DO UPDATE SET template_id = EXCLUDED.template_id, template_title = EXCLUDED.template_title,
            status = 'generating', error_message = NULL, started_at = NOW(), generated_by = EXCLUDED.generated_by
    """), {"id": rid, "t": str(tpl.id), "title": tpl.title, "u": admin["user_id"]})
    db.commit()
    start_generation(rid, str(tpl.id), dict(admin))
    return _json(_saved(db, rid))


def start_generation(recording_id: str, template_id: str, admin: dict) -> None:
    threading.Thread(target=generate, args=(recording_id, template_id, admin), daemon=True,
                     name=f"director-pov-{recording_id[:8]}").start()


def generate(recording_id: str, template_id: str, admin: dict) -> None:
    """Runs the template exactly as the recording's AI chat does (same context, same model call)."""
    from backend.chat.gemini_chat import chat_with_gemini, clean_chat_response
    from backend.chat.routes import _accessible_recording, _session_context
    db = SessionLocal()
    try:
        tpl = db.execute(text("SELECT id, title, prompt, example_output FROM isf_recorder.chat_template WHERE id = CAST(:t AS uuid)"),
                         {"t": template_id}).fetchone()
        rec_row = _accessible_recording(db, recording_id, admin)
        context = _session_context(db, rec_row, admin["user_id"])
        template = {"id": str(tpl.id), "title": tpl.title, "prompt": tpl.prompt, "example_output": tpl.example_output}
        content = clean_chat_response(chat_with_gemini(context, [], "", template, recording_id=recording_id,
                                                       org_id=admin["org_id"]))
        if not content.strip():
            raise RuntimeError("The AI returned an empty answer")
        db.execute(text("""
            UPDATE isf_recorder.recording_director_pov
            SET content = :c, status = 'ready', error_message = NULL, generated_at = NOW()
            WHERE recording_id = CAST(:id AS uuid)
        """), {"id": recording_id, "c": content})
        db.commit()
    except Exception as e:
        logger.warning(f"Director's POV failed for {recording_id}: {e}")
        db.rollback()
        db.execute(text("""
            UPDATE isf_recorder.recording_director_pov SET status = 'error', error_message = :e
            WHERE recording_id = CAST(:id AS uuid)
        """), {"id": recording_id, "e": "The AI could not make the Director's POV. Tap Regenerate to try again."})
        db.commit()
    finally:
        db.close()
