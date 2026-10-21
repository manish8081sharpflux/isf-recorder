"""
ISF Recorder — AI Chat & Prompt Template Routes
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Provides endpoints for:
- Ask Jomi saved conversations (/chat/conversations ...): many per user, titled, never auto-deleted
- Legacy per-session / all-sessions chat (v1.2.0): mapped to the user's latest conversation of that scope
- Prompt template management (/chat/templates: list, create, update, delete)
"""
import re
import uuid
import json
import logging
import threading
from typing import Optional, List, Dict, Any
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from backend.database import get_db, SessionLocal
from backend.auth.routes import get_current_user
from backend.chat.gemini_chat import (
    build_session_context,
    build_all_sessions_context,
    chat_with_gemini,
    update_user_preferences,
    seed_default_templates,
    clean_chat_response,
)
from backend.calls.access import call_access, CALL_VISIBILITY_SQL, visibility_params

logger = logging.getLogger("isf_recorder.chat.routes")
router = APIRouter(prefix="/chat", tags=["AI Chat"])


# ─── Pydantic Models ─────────────────────────────────────────────────────────

class ChatMessageRequest(BaseModel):
    message: str = ""
    template_id: Optional[str] = None


class ChatTemplateCreate(BaseModel):
    title: str
    prompt: str
    example_output: Optional[str] = None
    icon: Optional[str] = "📋"
    sort_order: Optional[int] = 0


class ChatTemplateUpdate(BaseModel):
    title: Optional[str] = None
    prompt: Optional[str] = None
    example_output: Optional[str] = None
    icon: Optional[str] = None
    is_active: Optional[bool] = None
    sort_order: Optional[int] = None


# ─── Helper Functions ────────────────────────────────────────────────────────

def _norm_words(s: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", (s or "").lower()))


def _template_named_in_message(db: Session, org_id: str, message: str) -> Optional[Dict[str, Any]]:
    """Returns the org's active template whose title appears in the message (longest title wins), else None."""
    msg = f" {_norm_words(message)} "
    if not msg.strip():
        return None
    rows = db.execute(
        text("""SELECT id, title, prompt, example_output FROM isf_recorder.chat_template
                WHERE org_id = :org_id AND COALESCE(is_active, TRUE) = TRUE"""),
        {"org_id": org_id},
    ).fetchall()
    best = None
    for r in rows:
        title = _norm_words(r.title)
        if len(title.split()) >= 2 and f" {title} " in msg and (best is None or len(title) > len(_norm_words(best.title))):
            best = r
    if not best:
        return None
    return {"id": str(best.id), "title": best.title, "prompt": best.prompt, "example_output": best.example_output}


CONVERSATION_MODES = ("all_sessions", "session")
TITLE_LENGTH = 60


def _latest_conversation(db: Session, user_id: str, mode: str, recording_id: Optional[str]):
    """The user's most recently updated conversation for this scope (deterministic order)."""
    rec_sql = "AND recording_id = :recording_id" if mode == "session" else ""
    return db.execute(text(f"""
        SELECT id, context_snapshot, updated_at
        FROM isf_recorder.chat_conversation
        WHERE user_id = :user_id AND mode = :mode {rec_sql}
        ORDER BY updated_at DESC NULLS LAST, created_at DESC NULLS LAST, id DESC
        LIMIT 1
    """), {"user_id": user_id, "mode": mode, "recording_id": recording_id}).fetchone()


def _create_conversation(db: Session, user_id: str, org_id: str, mode: str, recording_id: Optional[str]) -> str:
    new_id = str(uuid.uuid4())
    db.execute(text("""
        INSERT INTO isf_recorder.chat_conversation (id, user_id, org_id, recording_id, mode, context_snapshot, updated_at)
        VALUES (:id, :user_id, :org_id, :recording_id, :mode, '{}'::jsonb, NOW())
    """), {"id": new_id, "user_id": user_id, "org_id": org_id, "recording_id": recording_id, "mode": mode})
    db.commit()
    return new_id


def _get_or_create_conversation(
    db: Session,
    user_id: str,
    org_id: str,
    mode: str,
    recording_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Legacy endpoints use the user's most recently updated conversation for the scope (created if none)."""
    row = _latest_conversation(db, user_id, mode, recording_id)
    if row:
        return {
            "id": str(row[0]),
            "context_snapshot": row[1] if isinstance(row[1], dict) else (json.loads(row[1]) if row[1] else {}),
            "updated_at": row[2],
        }
    return {"id": _create_conversation(db, user_id, org_id, mode, recording_id), "context_snapshot": {},
            "updated_at": datetime.now(timezone.utc)}


def _get_user_preferences(db: Session, user_id: str) -> Optional[Dict[str, Any]]:
    """Loads learned AI preferences from user settings JSONB."""
    row = db.execute(
        text("SELECT settings FROM isf_recorder.user WHERE id = :user_id"),
        {"user_id": user_id}
    ).fetchone()
    if row and row[0]:
        settings = row[0] if isinstance(row[0], dict) else json.loads(row[0])
        return settings.get("ai_preferences")
    return None


def _trigger_preference_learning_async(user_id: str):
    """Spawns a daemon thread to learn preferences in background."""
    def run_learning():
        with SessionLocal() as db:
            update_user_preferences(db, user_id)
    t = threading.Thread(target=run_learning, daemon=True)
    t.start()


def _accessible_recording(db: Session, recording_id: str, current_user: dict):
    call_access(db, recording_id, current_user, write=False)   # calls are private to their owner
    try:
        rec_row = db.execute(text("""
            SELECT r.id, r.user_id, r.title, r.started_at, r.created_at,
                   r.duration_seconds, r.detected_language,
                   r.overview, r.key_points, r.decisions, r.transcript,
                   COALESCE(u.name, r.recorded_by_name) AS user_name, u.phone AS user_phone
            FROM isf_recorder.recording r
            LEFT JOIN isf_recorder.user u ON r.user_id = u.id
            WHERE r.id = CAST(:rec_id AS uuid) AND r.org_id = :org_id AND r.is_deleted = FALSE
        """), {"rec_id": recording_id, "org_id": current_user["org_id"]}).fetchone()
    except Exception:
        db.rollback()
        rec_row = None
    if not rec_row:
        raise HTTPException(status_code=404, detail="Recording not found")
    if current_user["role"] != "admin" and str(rec_row.user_id) != str(current_user["user_id"]):
        raise HTTPException(status_code=403, detail="Access denied to this recording")
    return rec_row


def _session_context(db: Session, rec_row, user_id: str) -> str:
    # Always rebuilt from the current transcript: a cached snapshot kept answering from the OLD
    # transcript after a re-transcribe / "activate another model".
    seg_rows = db.execute(text("""
        SELECT speaker, text, start_time, end_time
        FROM isf_recorder.transcript_segment
        WHERE recording_id = :rec_id
        ORDER BY start_time ASC, created_at ASC
    """), {"rec_id": str(rec_row.id)}).fetchall()
    segments = [{"speaker": s.speaker, "text": s.text, "start_time": s.start_time, "end_time": s.end_time}
                for s in seg_rows]
    ai_rows = db.execute(text("""
        SELECT description, assigned_to, urgency, due_date
        FROM isf_recorder.action_item WHERE recording_id = :rec_id ORDER BY created_at ASC
    """), {"rec_id": str(rec_row.id)}).fetchall()
    action_items = [{"description": a.description, "assigned_to": a.assigned_to, "urgency": a.urgency,
                     "due_date": str(a.due_date) if a.due_date else None} for a in ai_rows]
    recording_dict = {
        "id": str(rec_row.id),
        "title": rec_row.title,
        "started_at": rec_row.started_at,
        "created_at": rec_row.created_at,
        "duration_seconds": rec_row.duration_seconds,
        "detected_language": rec_row.detected_language,
        "overview": clean_chat_response(rec_row.overview or ""),
        "key_points": rec_row.key_points,
        "decisions": rec_row.decisions,
        "transcript": (rec_row.transcript or "").replace("\\r\\n", "\n").replace("\\n", "\n"),
        "user_name": rec_row.user_name,
        "user_phone": rec_row.user_phone,
    }
    return build_session_context(recording_dict, segments, action_items, _get_user_preferences(db, user_id))


def _all_sessions_context(db: Session, current_user: dict) -> str:
    params = {"org_id": current_user["org_id"]}
    owner_sql = ""
    if current_user["role"] != "admin":
        owner_sql = "AND r.user_id = :user_id"
        params["user_id"] = current_user["user_id"]
    rows = db.execute(text(f"""
        SELECT r.id, r.title, r.started_at, r.created_at,
               r.duration_seconds, r.overview, r.key_points, r.decisions,
               COALESCE(u.name, r.recorded_by_name) AS user_name, u.phone AS user_phone
        FROM isf_recorder.recording r
        LEFT JOIN isf_recorder.user u ON r.user_id = u.id
        WHERE r.org_id = :org_id AND r.is_deleted = FALSE {owner_sql} AND {CALL_VISIBILITY_SQL}
        ORDER BY COALESCE(r.started_at, r.created_at) DESC
        LIMIT 40
    """), {**params, **visibility_params(current_user)}).fetchall()
    sessions = [{
        "id": str(r.id), "title": r.title, "started_at": r.started_at, "created_at": r.created_at,
        "duration_seconds": r.duration_seconds, "overview": clean_chat_response(r.overview or ""),
        "key_points": r.key_points, "decisions": r.decisions, "user_name": r.user_name, "user_phone": r.user_phone,
    } for r in rows]
    return build_all_sessions_context(sessions, _get_user_preferences(db, current_user["user_id"]))


def _chat_turn(db: Session, current_user: dict, conv_id: str, mode: str, recording_id: Optional[str],
               req: ChatMessageRequest) -> Dict[str, Any]:
    """One user message + assistant answer in a conversation. Same response shape as the v1.2.0 endpoints."""
    user_id = current_user["user_id"]
    org_id = current_user["org_id"]

    if mode == "session":
        rec_row = _accessible_recording(db, recording_id, current_user)
        system_context = _session_context(db, rec_row, user_id)
        db.execute(text("""
            UPDATE isf_recorder.chat_conversation SET context_snapshot = CAST(:snap AS jsonb) WHERE id = :conv_id
        """), {"conv_id": conv_id, "snap": json.dumps({"system_context": system_context})})
        db.commit()
    else:
        system_context = _all_sessions_context(db, current_user)

    template_dict = None
    if req.template_id:
        try:
            tmpl_row = db.execute(text("""
                SELECT id, title, prompt, example_output FROM isf_recorder.chat_template
                WHERE id = CAST(:tid AS uuid) AND org_id = :org_id
            """), {"tid": req.template_id, "org_id": org_id}).fetchone()
        except Exception:
            db.rollback()
            tmpl_row = None
        if tmpl_row:
            template_dict = {"id": str(tmpl_row.id), "title": tmpl_row.title, "prompt": tmpl_row.prompt,
                             "example_output": tmpl_row.example_output}
    if not template_dict and mode == "session":
        # Quick chips (e.g. "🎓 Coach Report") only send a sentence naming the template; attach the
        # full saved template so Gemini gets every section and rule, not just the template's name.
        template_dict = _template_named_in_message(db, org_id, req.message)
    template_id = template_dict["id"] if template_dict else None

    hist_rows = db.execute(text("""
        SELECT role, content FROM isf_recorder.chat_message
        WHERE conversation_id = :conv_id ORDER BY created_at ASC, id ASC
    """), {"conv_id": conv_id}).fetchall()
    history = [{"role": r[0], "content": r[1]} for r in hist_rows]

    display_user_msg = req.message.strip()
    if not display_user_msg and template_dict:
        display_user_msg = f"/{template_dict['title']}"
    if not display_user_msg:
        raise HTTPException(status_code=400, detail="Message is empty")
    db.execute(text("""
        INSERT INTO isf_recorder.chat_message (id, conversation_id, role, content, template_id, created_at)
        VALUES (:id, :conv_id, 'user', :content, :template_id, clock_timestamp())
    """), {"id": str(uuid.uuid4()), "conv_id": conv_id, "content": display_user_msg, "template_id": template_id})
    db.execute(text("""
        UPDATE isf_recorder.chat_conversation
        SET updated_at = NOW(), title = COALESCE(NULLIF(title, ''), :title)
        WHERE id = :conv_id
    """), {"conv_id": conv_id, "title": " ".join(display_user_msg.split())[:TITLE_LENGTH]})
    db.commit()

    ai_response = chat_with_gemini(system_context, history, req.message, template_dict,
                                   recording_id=recording_id if mode == "session" else None, org_id=org_id)
    ai_response = clean_chat_response(ai_response)

    asst_msg_id = str(uuid.uuid4())
    now_utc = datetime.now(timezone.utc)
    db.execute(text("""
        INSERT INTO isf_recorder.chat_message (id, conversation_id, role, content, template_id, created_at)
        VALUES (:id, :conv_id, 'assistant', :content, :template_id,
                GREATEST(clock_timestamp(), (SELECT MAX(created_at) FROM isf_recorder.chat_message
                                             WHERE conversation_id = :conv_id) + INTERVAL '1 millisecond'))
    """), {"id": asst_msg_id, "conv_id": conv_id, "content": ai_response, "template_id": template_id, "now": now_utc})
    db.execute(text("UPDATE isf_recorder.chat_conversation SET updated_at = :now WHERE id = :conv_id"),
               {"conv_id": conv_id, "now": now_utc})
    db.commit()

    total_user_msgs = db.execute(text("""
        SELECT COUNT(*) FROM isf_recorder.chat_message WHERE conversation_id = :conv_id AND role = 'user'
    """), {"conv_id": conv_id}).scalar()
    if total_user_msgs and total_user_msgs % 10 == 0:
        _trigger_preference_learning_async(user_id)

    return {
        "id": asst_msg_id,
        "conversation_id": conv_id,
        "role": "assistant",
        "content": ai_response,
        "template_id": template_id,
        "created_at": now_utc.isoformat(),
    }


def _messages(db: Session, conv_id: str) -> List[Dict[str, Any]]:
    rows = db.execute(text("""
        SELECT id, role, content, template_id, created_at
        FROM isf_recorder.chat_message WHERE conversation_id = :conv_id
        ORDER BY created_at ASC, id ASC
    """), {"conv_id": conv_id}).fetchall()
    return [{
        "id": str(r[0]),
        "role": r[1],
        "content": clean_chat_response(r[2] or ""),
        "template_id": str(r[3]) if r[3] else None,
        "created_at": r[4].isoformat() if r[4] else None,
    } for r in rows]


# ─── Legacy chat endpoints (v1.2.0): the latest conversation of the scope ────

@router.post("/session/{recording_id}/message")
def send_session_chat_message(
    recording_id: str,
    req: ChatMessageRequest,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Send a message in per-session chat (the user's most recent conversation about this recording)."""
    _accessible_recording(db, recording_id, current_user)
    conv = _get_or_create_conversation(db, current_user["user_id"], current_user["org_id"], "session", recording_id)
    return _chat_turn(db, current_user, conv["id"], "session", recording_id, req)


@router.post("/all-sessions/message")
def send_all_sessions_chat_message(
    req: ChatMessageRequest,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Send a message in organization-wide AI chat (the user's most recent all-sessions conversation)."""
    conv = _get_or_create_conversation(db, current_user["user_id"], current_user["org_id"], "all_sessions", None)
    return _chat_turn(db, current_user, conv["id"], "all_sessions", None, req)


@router.get("/session/{recording_id}/history")
def get_session_chat_history(
    recording_id: str,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Retrieve message history for a per-session chat."""
    conv = _get_or_create_conversation(db, current_user["user_id"], current_user["org_id"], "session", recording_id)
    return _messages(db, conv["id"])


@router.get("/all-sessions/history")
def get_all_sessions_chat_history(
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Retrieve message history for all-sessions chat."""
    conv = _get_or_create_conversation(db, current_user["user_id"], current_user["org_id"], "all_sessions", None)
    return _messages(db, conv["id"])


# ─── Ask Jomi: saved conversations (contract §6) ─────────────────────────────

class ConversationCreate(BaseModel):
    mode: str = "all_sessions"
    recording_id: Optional[str] = None


class ConversationUpdate(BaseModel):
    title: str


def _conversation_row(db: Session, conv_id: str, user_id: str):
    try:
        row = db.execute(text("""
            SELECT c.id, c.title, c.mode, c.recording_id, c.updated_at, c.created_at,
                   (SELECT content FROM isf_recorder.chat_message m WHERE m.conversation_id = c.id
                    ORDER BY m.created_at DESC, m.id DESC LIMIT 1) AS last_message,
                   (SELECT COUNT(*) FROM isf_recorder.chat_message m WHERE m.conversation_id = c.id) AS message_count,
                   (SELECT r.title FROM isf_recorder.recording r WHERE r.id = c.recording_id) AS recording_title
            FROM isf_recorder.chat_conversation c
            WHERE c.id = CAST(:id AS uuid) AND c.user_id = :uid
        """), {"id": conv_id, "uid": user_id}).fetchone()
    except Exception:
        db.rollback()
        row = None
    if not row:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return row


def _conversation_json(r) -> Dict[str, Any]:
    last = clean_chat_response(r.last_message or "") if r.last_message else None
    return {
        "id": str(r.id),
        "title": r.title,
        "mode": r.mode,
        "recording_id": str(r.recording_id) if r.recording_id else None,
        "last_message": last[:200] if last else None,
        "message_count": int(r.message_count or 0),
        "recording_title": getattr(r, "recording_title", None),  # the app shows it on session chats
        "updated_at": (r.updated_at or r.created_at).isoformat() if (r.updated_at or r.created_at) else None,
    }


@router.get("/conversations")
def list_conversations(
    mode: Optional[str] = Query(None),
    recording_id: Optional[str] = Query(None),
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """The caller's own conversations, newest first."""
    params: Dict[str, Any] = {"uid": current_user["user_id"]}
    where = ["c.user_id = :uid"]
    if mode:
        if mode not in CONVERSATION_MODES:
            raise HTTPException(status_code=400, detail="mode must be all_sessions or session")
        where.append("c.mode = :mode")
        params["mode"] = mode
    if recording_id:
        try:
            params["rid"] = str(uuid.UUID(recording_id))
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid recording_id")
        where.append("c.recording_id = :rid")
    rows = db.execute(text(f"""
        SELECT c.id, c.title, c.mode, c.recording_id, c.updated_at, c.created_at,
               lm.content AS last_message, COALESCE(mc.n, 0) AS message_count, rec.title AS recording_title
        FROM isf_recorder.chat_conversation c
        LEFT JOIN isf_recorder.recording rec ON rec.id = c.recording_id
        LEFT JOIN LATERAL (SELECT content FROM isf_recorder.chat_message m WHERE m.conversation_id = c.id
                           ORDER BY m.created_at DESC, m.id DESC LIMIT 1) lm ON TRUE
        LEFT JOIN LATERAL (SELECT COUNT(*) AS n FROM isf_recorder.chat_message m WHERE m.conversation_id = c.id) mc ON TRUE
        WHERE {' AND '.join(where)}
        ORDER BY c.updated_at DESC NULLS LAST, c.created_at DESC NULLS LAST, c.id DESC
        LIMIT 200
    """), params).fetchall()
    return {"items": [_conversation_json(r) for r in rows]}


@router.post("/conversations")
def create_conversation(
    body: ConversationCreate,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if body.mode not in CONVERSATION_MODES:
        raise HTTPException(status_code=400, detail="mode must be all_sessions or session")
    recording_id = None
    if body.mode == "session":
        if not body.recording_id:
            raise HTTPException(status_code=400, detail="recording_id is required for a session conversation")
        recording_id = str(_accessible_recording(db, body.recording_id, current_user).id)
    conv_id = _create_conversation(db, current_user["user_id"], current_user["org_id"], body.mode, recording_id)
    return _conversation_json(_conversation_row(db, conv_id, current_user["user_id"]))


@router.get("/conversations/{conversation_id}/messages")
def get_conversation_messages(
    conversation_id: str,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    row = _conversation_row(db, conversation_id, current_user["user_id"])
    return {"conversation": _conversation_json(row), "messages": _messages(db, str(row.id))}


@router.post("/conversations/{conversation_id}/message")
def send_conversation_message(
    conversation_id: str,
    req: ChatMessageRequest,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    row = _conversation_row(db, conversation_id, current_user["user_id"])
    return _chat_turn(db, current_user, str(row.id), row.mode,
                      str(row.recording_id) if row.recording_id else None, req)


@router.patch("/conversations/{conversation_id}")
def rename_conversation(
    conversation_id: str,
    body: ConversationUpdate,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    row = _conversation_row(db, conversation_id, current_user["user_id"])
    title = " ".join((body.title or "").split())[:120]
    if not title:
        raise HTTPException(status_code=400, detail="Title cannot be empty")
    db.execute(text("UPDATE isf_recorder.chat_conversation SET title = :t WHERE id = :id"),
               {"t": title, "id": str(row.id)})
    db.commit()
    return _conversation_json(_conversation_row(db, str(row.id), current_user["user_id"]))


@router.delete("/conversations/{conversation_id}")
def delete_conversation(
    conversation_id: str,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    row = _conversation_row(db, conversation_id, current_user["user_id"])
    db.execute(text("DELETE FROM isf_recorder.chat_conversation WHERE id = :id"), {"id": str(row.id)})
    db.commit()
    return {"status": "deleted", "id": str(row.id)}


# ─── Template Management Endpoints ───────────────────────────────────────────

@router.get("/templates")
def list_chat_templates(
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """List all active prompt templates for the organization."""
    org_id = current_user["org_id"]
    admin_id = current_user["user_id"]

    # Auto-seed defaults if organization has no templates yet
    seed_default_templates(db, org_id, admin_id)

    rows = db.execute(
        text("""
            SELECT id, title, prompt, example_output, icon, is_active, sort_order, created_at, updated_at
            FROM isf_recorder.chat_template
            WHERE org_id = :org_id AND is_active = TRUE
            ORDER BY sort_order ASC, created_at ASC
        """),
        {"org_id": org_id}
    ).fetchall()

    return [
        {
            "id": str(r[0]),
            "title": r[1],
            "prompt": r[2],
            "example_output": r[3],
            "icon": r[4] or "📋",
            "is_active": r[5],
            "sort_order": r[6],
            "created_at": r[7].isoformat() if r[7] else None,
            "updated_at": r[8].isoformat() if r[8] else None,
        }
        for r in rows
    ]


@router.post("/templates")
def create_chat_template(
    req: ChatTemplateCreate,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Create a new prompt template (Admin only)."""
    if current_user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Only admins can create prompt templates")

    tmpl_id = str(uuid.uuid4())
    org_id = current_user["org_id"]
    user_id = current_user["user_id"]

    db.execute(
        text("""
            INSERT INTO isf_recorder.chat_template
            (id, org_id, created_by, title, prompt, example_output, icon, sort_order)
            VALUES (:id, :org_id, :user_id, :title, :prompt, :example_output, :icon, :sort_order)
        """),
        {
            "id": tmpl_id,
            "org_id": org_id,
            "user_id": user_id,
            "title": req.title.strip(),
            "prompt": req.prompt.strip(),
            "example_output": req.example_output.strip() if req.example_output else None,
            "icon": req.icon or "📋",
            "sort_order": req.sort_order or 0,
        }
    )
    db.commit()

    return {
        "id": tmpl_id,
        "title": req.title.strip(),
        "prompt": req.prompt.strip(),
        "example_output": req.example_output,
        "icon": req.icon or "📋",
        "sort_order": req.sort_order or 0,
        "is_active": True,
    }


@router.put("/templates/{template_id}")
def update_chat_template(
    template_id: str,
    req: ChatTemplateUpdate,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Update an existing prompt template (Admin only)."""
    if current_user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Only admins can update prompt templates")

    org_id = current_user["org_id"]
    tmpl = db.execute(
        text("SELECT id FROM isf_recorder.chat_template WHERE id = :id AND org_id = :org_id"),
        {"id": template_id, "org_id": org_id}
    ).fetchone()
    if not tmpl:
        raise HTTPException(status_code=404, detail="Template not found")

    updates = []
    params: Dict[str, Any] = {"id": template_id, "org_id": org_id}

    if req.title is not None:
        updates.append("title = :title")
        params["title"] = req.title.strip()
    if req.prompt is not None:
        updates.append("prompt = :prompt")
        params["prompt"] = req.prompt.strip()
    if req.example_output is not None:
        updates.append("example_output = :example_output")
        params["example_output"] = req.example_output.strip()
    if req.icon is not None:
        updates.append("icon = :icon")
        params["icon"] = req.icon
    if req.is_active is not None:
        updates.append("is_active = :is_active")
        params["is_active"] = req.is_active
    if req.sort_order is not None:
        updates.append("sort_order = :sort_order")
        params["sort_order"] = req.sort_order

    if updates:
        updates.append("updated_at = NOW()")
        sql = f"UPDATE isf_recorder.chat_template SET {', '.join(updates)} WHERE id = :id AND org_id = :org_id"
        db.execute(text(sql), params)
        db.commit()

    row = db.execute(
        text("SELECT id, title, prompt, example_output, icon, is_active, sort_order FROM isf_recorder.chat_template WHERE id = :id"),
        {"id": template_id}
    ).fetchone()

    return {
        "id": str(row[0]),
        "title": row[1],
        "prompt": row[2],
        "example_output": row[3],
        "icon": row[4],
        "is_active": row[5],
        "sort_order": row[6],
    }


@router.delete("/templates/{template_id}")
def delete_chat_template(
    template_id: str,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Deletes/deactivates a prompt template (Admin only)."""
    if current_user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Only admins can delete prompt templates")

    org_id = current_user["org_id"]
    from backend.settings.recorder import template_mapped_modes
    mapped = template_mapped_modes(db, org_id, template_id)
    if mapped:
        names = " and ".join("Schedule" if m == "schedule" else "Meeting" for m in mapped)
        raise HTTPException(status_code=409, detail=f"This template writes the {names} report. "
                            f"Choose another template in Settings → Reports first.")
    result = db.execute(
        text("DELETE FROM isf_recorder.chat_template WHERE id = :id AND org_id = :org_id"),
        {"id": template_id, "org_id": org_id}
    )
    db.commit()
    if result.rowcount == 0:
        raise HTTPException(status_code=404, detail="Template not found")

    return {"status": "deleted", "id": template_id}
