"""
ISF Recorder — Report endpoints (contract §5.1)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
GET  /recordings/{id}/report              PUT  /recordings/{id}/report {content}
POST /recordings/{id}/report/regenerate   POST /recordings/{id}/report/finalize
POST /recordings/{id}/report/reopen       POST /recordings/{id}/report/ai-edit {instruction, content?}
The recording's owner or an admin of its organization may read and edit.
"""
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from backend.auth.routes import get_current_user
from backend.database import get_db
from backend.reports import service
from backend.calls.access import call_access

logger = logging.getLogger("isf_recorder.reports.routes")
router = APIRouter(prefix="/recordings", tags=["Reports"])


class ReportUpdate(BaseModel):
    content: str


class RegenerateRequest(BaseModel):
    template_id: Optional[str] = None


class AiEditRequest(BaseModel):
    instruction: str
    content: Optional[str] = None


def _recording(db: Session, recording_id: str, user: dict):
    try:
        rec = db.execute(text("""
            SELECT id, org_id, user_id, status, mode, COALESCE(LENGTH(transcript), 0) > 0 AS has_transcript
            FROM isf_recorder.recording WHERE id = CAST(:id AS uuid) AND is_deleted = FALSE
        """), {"id": recording_id}).fetchone()
    except Exception:
        db.rollback()
        rec = None
    if not rec:
        raise HTTPException(status_code=404, detail="Recording not found")
    if str(rec.org_id) != user["org_id"] or (user.get("role") != "admin" and str(rec.user_id) != user["user_id"]):
        raise HTTPException(status_code=403, detail="Permission denied")
    return rec


def _report(db: Session, recording_id: str):
    return db.execute(text("""
        SELECT rr.*, u.name AS updated_by_name
        FROM isf_recorder.recording_report rr LEFT JOIN isf_recorder."user" u ON u.id = rr.updated_by
        WHERE rr.recording_id = :id
    """), {"id": recording_id}).fetchone()


def _iso(dt):
    return dt.isoformat() if dt else None


def _report_json(row, user: dict) -> dict:
    is_admin = user.get("role") == "admin"
    return {
        "status": row.status,
        "content": row.content or "",
        "template_id": str(row.template_id) if row.template_id else None,
        "template_title": row.template_title,
        "error_message": row.error_message,
        "generated_at": _iso(row.generated_at),
        "updated_at": _iso(row.updated_at),
        "updated_by_name": row.updated_by_name,
        "finalized_at": _iso(row.finalized_at),
        "can_edit": row.status in ("draft", "error") or (row.status == "final" and is_admin),
    }


@router.get("/{recording_id}/report")
def get_report(recording_id: str, user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    call_access(db, recording_id, user, write=False)   # calls are private to their owner
    rec = _recording(db, recording_id, user)
    row = _report(db, str(rec.id))
    if not row:
        raise HTTPException(status_code=404, detail="No report yet")
    return _report_json(row, user)


@router.put("/{recording_id}/report")
def save_report(recording_id: str, body: ReportUpdate, user: dict = Depends(get_current_user),
                db: Session = Depends(get_db)):
    call_access(db, recording_id, user, write=True)   # calls are private to their owner
    rec = _recording(db, recording_id, user)
    row = _report(db, str(rec.id))
    if row and row.status == "generating":
        raise HTTPException(status_code=409, detail="The report is being generated. Try again in a moment.")
    if row and row.status == "final" and user.get("role") != "admin":
        raise HTTPException(status_code=409, detail="This report is final. Ask an admin to reopen it.")
    if row:
        db.execute(text("""
            UPDATE isf_recorder.recording_report
            SET content = :c, updated_by = :u, updated_at = NOW(),
                status = CASE WHEN status = 'final' THEN 'final' ELSE 'draft' END, error_message = NULL
            WHERE recording_id = :id
        """), {"id": str(rec.id), "c": body.content, "u": user["user_id"]})
    else:
        db.execute(text("""
            INSERT INTO isf_recorder.recording_report (recording_id, content, status, updated_by, updated_at)
            VALUES (:id, :c, 'draft', :u, NOW())
        """), {"id": str(rec.id), "c": body.content, "u": user["user_id"]})
    db.commit()
    return _report_json(_report(db, str(rec.id)), user)


@router.post("/{recording_id}/report/regenerate")
def regenerate_report(recording_id: str, body: Optional[RegenerateRequest] = None,
                      user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    call_access(db, recording_id, user, write=True)   # calls are private to their owner
    rec = _recording(db, recording_id, user)
    if rec.status in ("processing", "recording", "uploading"):
        raise HTTPException(status_code=409, detail="Wait until the transcription has finished")
    if not rec.has_transcript:
        raise HTTPException(status_code=409, detail="There is no transcript to write a report from")
    template_id = (body.template_id if body else None) or None
    tpl = service._template_for(db, str(rec.org_id), rec.mode, template_id)
    if not tpl:
        kind = "Meeting" if rec.mode == "meeting" else "Schedule"
        raise HTTPException(status_code=400, detail=f"No report template for {kind} recordings. An admin can choose one "
                            f"in Settings → Reports, or tap 'Restore default report templates' there.")
    claimed = db.execute(text("""
        INSERT INTO isf_recorder.recording_report (recording_id, template_id, template_title, status, updated_at)
        VALUES (:id, :tid, :title, 'generating', NOW())
        ON CONFLICT (recording_id) DO UPDATE SET
            template_id = EXCLUDED.template_id, template_title = EXCLUDED.template_title,
            status = 'generating', error_message = NULL, updated_at = NOW()
        WHERE isf_recorder.recording_report.status <> 'generating'
          AND (isf_recorder.recording_report.status <> 'final' OR :is_admin)
        RETURNING id
    """), {"id": str(rec.id), "tid": str(tpl.id), "title": tpl.title,
           "is_admin": user.get("role") == "admin"}).fetchone()
    db.commit()
    if not claimed:
        raise HTTPException(status_code=409, detail="The report is already being generated or is final")
    service.generate_report_async(str(rec.id), template_id=str(tpl.id))
    return _report_json(_report(db, str(rec.id)), user)


@router.post("/{recording_id}/report/finalize")
def finalize_report(recording_id: str, user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    call_access(db, recording_id, user, write=True)   # calls are private to their owner
    rec = _recording(db, recording_id, user)
    row = db.execute(text("""
        UPDATE isf_recorder.recording_report
        SET status = 'final', finalized_at = NOW(), finalized_by = :u, updated_at = NOW()
        WHERE recording_id = :id AND status IN ('draft', 'final') AND content <> ''
        RETURNING id
    """), {"id": str(rec.id), "u": user["user_id"]}).fetchone()
    db.commit()
    if not row:
        raise HTTPException(status_code=409, detail="Only a written draft can be finalized")
    return _report_json(_report(db, str(rec.id)), user)


@router.post("/{recording_id}/report/reopen")
def reopen_report(recording_id: str, user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    call_access(db, recording_id, user, write=True)   # calls are private to their owner
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Only an admin can reopen a final report")
    rec = _recording(db, recording_id, user)
    row = db.execute(text("""
        UPDATE isf_recorder.recording_report
        SET status = 'draft', finalized_at = NULL, finalized_by = NULL, updated_at = NOW()
        WHERE recording_id = :id AND status = 'final' RETURNING id
    """), {"id": str(rec.id)}).fetchone()
    db.commit()
    if not row:
        raise HTTPException(status_code=409, detail="The report is not final")
    return _report_json(_report(db, str(rec.id)), user)


@router.post("/{recording_id}/report/ai-edit")
def ai_edit_report(recording_id: str, body: AiEditRequest, user: dict = Depends(get_current_user),
                   db: Session = Depends(get_db)):
    call_access(db, recording_id, user, write=True)   # calls are private to their owner
    rec = _recording(db, recording_id, user)
    if not (body.instruction or "").strip():
        raise HTTPException(status_code=400, detail="Tell the AI what to change")
    content = body.content
    if content is None:
        row = _report(db, str(rec.id))
        content = row.content if row else ""
    if not (content or "").strip():
        raise HTTPException(status_code=409, detail="There is no report to edit yet")
    try:
        proposed = service.ai_edit(str(rec.id), body.instruction, content)
    except Exception as e:
        logger.warning(f"AI edit failed for {recording_id}: {e}")
        raise HTTPException(status_code=502, detail="The AI could not edit the report. Please try again.")
    if not proposed.strip():
        raise HTTPException(status_code=502, detail="The AI returned an empty report. Please try again.")
    return {"proposed_content": proposed}


class TranslateRequest(BaseModel):
    lang: str


@router.post("/{recording_id}/report/translate")
def translate_report(recording_id: str, body: TranslateRequest, user: dict = Depends(get_current_user),
                     db: Session = Depends(get_db)):
    """Hindi ("hi") version of the saved report. Made once per version of the English
    text and saved; a later edit of the report makes the next request translate again."""
    call_access(db, recording_id, user, write=False)   # calls are private to their owner
    import json
    rec = _recording(db, recording_id, user)
    lang = (body.lang or "").strip().lower()
    if lang not in service.TRANSLATION_LANGUAGES:
        raise HTTPException(status_code=400, detail="Language must be 'hi' (Hindi)")
    row = db.execute(text("""
        SELECT content, status, COALESCE(translations, '{}'::jsonb) AS translations
        FROM isf_recorder.recording_report WHERE recording_id = :id
    """), {"id": str(rec.id)}).fetchone()
    if not row or not (row.content or "").strip() or row.status == "generating":
        raise HTTPException(status_code=409, detail="There is no report to translate yet")
    h = service.source_hash(row.content)
    cached = (row.translations or {}).get(lang) or {}
    if cached.get("source_hash") == h and (cached.get("text") or "").strip():
        return {"lang": lang, "content": cached["text"], "cached": True}
    try:
        translated = service.translate(str(rec.id), lang, row.content)
    except Exception as e:
        logger.warning(f"Translation ({lang}) failed for {recording_id}: {e}")
        raise HTTPException(status_code=502, detail="The translation could not be made. Please try again.")
    if not translated.strip():
        raise HTTPException(status_code=502, detail="The translation came back empty. Please try again.")
    db.execute(text("""
        UPDATE isf_recorder.recording_report
        SET translations = COALESCE(translations, '{}'::jsonb) || jsonb_build_object(CAST(:lang AS text), CAST(:val AS jsonb))
        WHERE recording_id = :id
    """), {"id": str(rec.id), "lang": lang, "val": json.dumps({"text": translated, "source_hash": h})})
    db.commit()
    return {"lang": lang, "content": translated, "cached": False}
