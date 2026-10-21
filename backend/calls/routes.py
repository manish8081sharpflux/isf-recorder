"""
Calls API (14 Oct 2026) — phone-call recordings synced from the phone's own call recorder, and the tasks
found in them. Everything here is private to the logged-in person; an admin only reads calls that were
shared with "Share with admin" (see backend/calls/access.py).

POST   /calls/upload                 one call file from the phone (dedupe by file_key)
GET    /calls/contacts               my contacts with call counts (search by contact)
POST   /calls/{recording_id}/share   {shared: bool} — owner only
GET    /tasks                        my tasks (?status=open|done|all, ?recording_id=)
POST   /tasks                        {text, due_text?, recording_id?}
PUT    /tasks/{task_id}              {text?, due_text?, done?, assigned_to?}
DELETE /tasks/{task_id}
GET    /task-reminder                {on, time, open_count}
PUT    /task-reminder                {on?, time?}
"""
import hashlib
import logging
import os
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from backend.auth.routes import get_current_user
from backend.config import AUDIO_STORAGE_ROOT
from backend.database import get_db
from backend.notifications.inbox import notify_safely, on_call_shared

logger = logging.getLogger("isf_recorder.calls")
router = APIRouter(tags=["Calls"])


def _parse_when(value: Optional[str]) -> Optional[datetime]:
    """ISO time or epoch milliseconds from the phone; None when missing / unreadable."""
    if not value:
        return None
    v = value.strip()
    try:
        if v.isdigit():
            return datetime.fromtimestamp(int(v) / 1000.0, tz=timezone.utc)
        dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, OverflowError, OSError):
        return None


@router.post("/calls/upload")
def upload_call(
    file: UploadFile = File(...),
    contact_name: Optional[str] = Form(None),
    contact_phone: Optional[str] = Form(None),
    call_type: Optional[str] = Form(None),
    call_app: Optional[str] = Form(None),
    call_started_at: Optional[str] = Form(None),
    file_key: Optional[str] = Form(None),
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """One call recording found on the phone. Saved in the logged-in person's account, then transcribed and
    summarised like any recording (Call summary + tasks). The same phone file is never stored twice."""
    from backend.recordings.routes import IMPORT_EXTENSIONS, MAX_IMPORT_BYTES, get_org_slug, _finish_import

    user_id, org_id = current_user["user_id"], current_user["org_id"]
    external_id = None
    if file_key and file_key.strip():
        digest = hashlib.sha1(file_key.strip().encode("utf-8")).hexdigest()[:24]
        external_id = f"phone:{user_id}:{digest}"
        dup = db.execute(text("SELECT id FROM isf_recorder.recording WHERE external_id = :e"),
                         {"e": external_id}).fetchone()
        if dup:
            return {"status": "duplicate", "recording_id": str(dup.id)}

    original_name = os.path.basename(file.filename or "call")
    ext = Path(original_name).suffix.lower()
    if ext not in IMPORT_EXTENSIONS:
        raise HTTPException(status_code=400, detail=f"Unsupported file type '{ext or '?'}'")

    session_id = f"call_{uuid.uuid4().hex[:12]}"
    final_dir = Path(AUDIO_STORAGE_ROOT) / get_org_slug(db, org_id) / datetime.utcnow().strftime("%Y-%m-%d")
    final_dir.mkdir(parents=True, exist_ok=True)
    raw_path = str(final_dir / f"{session_id}_upload{ext}")
    final_path = str(final_dir / f"{session_id}.m4a")
    size = 0
    with open(raw_path, "wb") as out:
        while True:
            block = file.file.read(1024 * 1024)
            if not block:
                break
            size += len(block)
            if size > MAX_IMPORT_BYTES:
                out.close()
                os.remove(raw_path)
                raise HTTPException(status_code=413, detail="File is larger than 1 GB")
            out.write(block)
    if size < 1000:
        os.remove(raw_path)
        raise HTTPException(status_code=400, detail="The file is empty")

    contact = (contact_name or "").strip()[:120] or None
    phone = re.sub(r"[^\d+]", "", contact_phone or "")[:20] or None
    kind = (call_type or "").strip().lower()
    kind = kind if kind in ("incoming", "outgoing") else "unknown"
    started = _parse_when(call_started_at)
    title = f"Call with {contact or phone or 'unknown'}"
    try:
        rec_id = db.execute(text("""
            INSERT INTO isf_recorder.recording
            (user_id, org_id, session_id, title, status, mic_source, sample_rate, duration_seconds,
             audio_file_path, file_size_bytes, started_at, finished_at, pipeline_stage, finish_requested_at,
             finish_source, recorded_by_name, source, contact_name, contact_phone, call_type, call_app, external_id)
            VALUES (:user_id, :org_id, :sid, :title, 'processing', 'call', 16000, 0, :fpath, :size,
                    COALESCE(:started, NOW()), NOW(), 'queued', NOW(), 'call_sync', :name,
                    'call', :contact, :phone, :kind, :app, :ext)
            RETURNING id
        """), {"user_id": user_id, "org_id": org_id, "sid": session_id, "title": title, "fpath": final_path,
               "size": size, "started": started, "name": current_user.get("name"), "contact": contact,
               "phone": phone, "kind": kind, "app": (call_app or "").strip()[:40] or None, "ext": external_id}).scalar()
        db.commit()
    except Exception as e:
        db.rollback()
        os.remove(raw_path)
        if external_id and "recording_external_id_key" in str(e):
            dup = db.execute(text("SELECT id FROM isf_recorder.recording WHERE external_id = :e"),
                             {"e": external_id}).fetchone()
            return {"status": "duplicate", "recording_id": str(dup.id) if dup else None}
        raise
    rec_id = str(rec_id)
    threading.Thread(target=_finish_import, args=(rec_id, raw_path, final_path, None, user_id),
                     daemon=True, name=f"call-{rec_id[:8]}").start()
    logger.info(f"Call from {current_user.get('name')} with {contact or phone or '?'} ({size / 1048576:.1f} MB) as {rec_id}")
    return {"status": "processing", "recording_id": rec_id, "title": title}


@router.get("/calls/contacts")
def my_call_contacts(current_user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    rows = db.execute(text("""
        SELECT COALESCE(NULLIF(TRIM(contact_name), ''), contact_phone, 'Unknown') AS name, COUNT(*) AS calls,
               MAX(started_at) AS last_call
        FROM isf_recorder.recording
        WHERE source = 'call' AND user_id = CAST(:u AS uuid) AND is_deleted = FALSE
        GROUP BY 1 ORDER BY MAX(started_at) DESC
    """), {"u": current_user["user_id"]}).fetchall()
    return {"items": [{"name": r.name, "calls": int(r.calls),
                       "last_call": r.last_call.isoformat() if r.last_call else None} for r in rows]}


class ShareBody(BaseModel):
    shared: bool


@router.post("/calls/{recording_id}/share")
def share_call(recording_id: str, body: ShareBody, current_user: dict = Depends(get_current_user),
               db: Session = Depends(get_db)):
    """Owner only: let admins read this one call (and its summary / tasks)."""
    try:
        row = db.execute(text("""
            UPDATE isf_recorder.recording SET shared_with_admin = :s
            WHERE id = CAST(:id AS uuid) AND source = 'call' AND user_id = CAST(:u AS uuid) AND is_deleted = FALSE
            RETURNING id
        """), {"s": body.shared, "id": recording_id, "u": current_user["user_id"]}).fetchone()
    except Exception:
        db.rollback()
        row = None
    if not row:
        raise HTTPException(status_code=404, detail="Call not found")
    db.commit()
    if body.shared:
        notify_safely(on_call_shared, db, recording_id, current_user)
    return {"recording_id": recording_id, "shared_with_admin": body.shared}


# ─── Tasks ───────────────────────────────────────────────────────────────────

def _task_json(r) -> dict:
    return {
        "id": str(r.id), "recording_id": str(r.recording_id) if r.recording_id else None,
        "text": r.text, "assigned_to": r.assigned_to, "due_text": r.due_text, "done": bool(r.done),
        "origin": r.origin, "created_at": r.created_at.isoformat() if r.created_at else None,
        "contact_name": getattr(r, "contact_name", None), "call_title": getattr(r, "call_title", None),
    }


TASK_SELECT = """
    SELECT t.id, t.recording_id, t.text, t.assigned_to, t.due_text, t.done, t.origin, t.created_at,
           r.contact_name, r.title AS call_title
    FROM isf_recorder.call_task t LEFT JOIN isf_recorder.recording r ON r.id = t.recording_id
"""


@router.get("/tasks")
def list_tasks(status: str = Query("open", description="open | done | all"),
               recording_id: Optional[str] = Query(None),
               current_user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    where = ["t.user_id = CAST(:u AS uuid)", "t.deleted_at IS NULL"]
    params = {"u": current_user["user_id"]}
    if status == "open":
        where.append("NOT t.done")
    elif status == "done":
        where.append("t.done")
    elif status != "all":
        raise HTTPException(status_code=400, detail="status must be open, done or all")
    if recording_id:
        where.append("t.recording_id = CAST(:r AS uuid)")
        params["r"] = recording_id
    rows = db.execute(text(TASK_SELECT + " WHERE " + " AND ".join(where) + " ORDER BY t.done, t.created_at DESC"),
                      params).fetchall()
    return {"items": [_task_json(r) for r in rows]}


class TaskCreate(BaseModel):
    text: str
    due_text: Optional[str] = None
    recording_id: Optional[str] = None


class TaskUpdate(BaseModel):
    text: Optional[str] = None
    due_text: Optional[str] = None
    assigned_to: Optional[str] = None
    done: Optional[bool] = None


@router.post("/tasks")
def add_task(body: TaskCreate, current_user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    task_text = (body.text or "").strip()
    if not task_text:
        raise HTTPException(status_code=400, detail="Write the task")
    if body.recording_id:
        own = db.execute(text("""
            SELECT 1 FROM isf_recorder.recording WHERE id = CAST(:r AS uuid) AND user_id = CAST(:u AS uuid)
        """), {"r": body.recording_id, "u": current_user["user_id"]}).fetchone()
        if not own:
            raise HTTPException(status_code=404, detail="Call not found")
    tid = db.execute(text("""
        INSERT INTO isf_recorder.call_task (recording_id, user_id, text, due_text, origin)
        VALUES (CAST(:r AS uuid), CAST(:u AS uuid), :t, :d, 'user') RETURNING id
    """), {"r": body.recording_id, "u": current_user["user_id"], "t": task_text[:500],
           "d": (body.due_text or "").strip()[:120] or None}).scalar()
    db.commit()
    row = db.execute(text(TASK_SELECT + " WHERE t.id = :id"), {"id": tid}).fetchone()
    return _task_json(row)


def _own_task(db: Session, task_id: str, user: dict):
    try:
        row = db.execute(text("""
            SELECT id FROM isf_recorder.call_task
            WHERE id = CAST(:id AS uuid) AND user_id = CAST(:u AS uuid) AND deleted_at IS NULL
        """), {"id": task_id, "u": user["user_id"]}).fetchone()
    except Exception:
        db.rollback()
        row = None
    if not row:
        raise HTTPException(status_code=404, detail="Task not found")


@router.put("/tasks/{task_id}")
def update_task(task_id: str, body: TaskUpdate, current_user: dict = Depends(get_current_user),
                db: Session = Depends(get_db)):
    _own_task(db, task_id, current_user)
    sets, params = ["updated_at = NOW()"], {"id": task_id}
    if body.text is not None:
        if not body.text.strip():
            raise HTTPException(status_code=400, detail="The task cannot be empty")
        sets.append("text = :t"); params["t"] = body.text.strip()[:500]
    if body.due_text is not None:
        sets.append("due_text = :d"); params["d"] = body.due_text.strip()[:120] or None
    if body.assigned_to is not None:
        sets.append("assigned_to = :a"); params["a"] = body.assigned_to.strip()[:120] or None
    if body.done is not None:
        sets.append("done = :done"); params["done"] = body.done
        sets.append("done_at = CASE WHEN :done THEN NOW() ELSE NULL END")
    db.execute(text(f"UPDATE isf_recorder.call_task SET {', '.join(sets)} WHERE id = CAST(:id AS uuid)"), params)
    db.commit()
    return _task_json(db.execute(text(TASK_SELECT + " WHERE t.id = CAST(:id AS uuid)"), {"id": task_id}).fetchone())


@router.delete("/tasks/{task_id}")
def delete_task(task_id: str, current_user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    _own_task(db, task_id, current_user)
    db.execute(text("UPDATE isf_recorder.call_task SET deleted_at = NOW() WHERE id = CAST(:id AS uuid)"), {"id": task_id})
    db.commit()
    return {"deleted": task_id}


class ReminderBody(BaseModel):
    on: Optional[bool] = None
    time: Optional[str] = None      # HH:MM


@router.get("/task-reminder")
def get_reminder(current_user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    row = db.execute(text("""
        SELECT u.task_reminder_on, u.task_reminder_time,
               (SELECT COUNT(*) FROM isf_recorder.call_task t
                 WHERE t.user_id = u.id AND NOT t.done AND t.deleted_at IS NULL) AS open_count
        FROM isf_recorder."user" u WHERE u.id = CAST(:u AS uuid)
    """), {"u": current_user["user_id"]}).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="User not found")
    return {"on": bool(row.task_reminder_on), "time": row.task_reminder_time, "open_count": int(row.open_count or 0)}


@router.put("/task-reminder")
def set_reminder(body: ReminderBody, current_user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    sets, params = [], {"u": current_user["user_id"]}
    if body.on is not None:
        sets.append("task_reminder_on = :on"); params["on"] = body.on
    if body.time is not None:
        if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", body.time.strip()):
            raise HTTPException(status_code=400, detail="Time must be HH:MM")
        sets.append("task_reminder_time = :t"); params["t"] = body.time.strip()
    if sets:
        db.execute(text(f'UPDATE isf_recorder."user" SET {", ".join(sets)} WHERE id = CAST(:u AS uuid)'), params)
        db.commit()
    return get_reminder(current_user, db)
