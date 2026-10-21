"""
Jomi voice assistant (admins and super admins): the round button at the bottom right of the app.
The phone turns speech into text and reads the answer aloud; the server answers about the app only
and keeps every voice chat.

GET  /assistant/greeting                  "Hey, I am Jomi. How may I help you?"
POST /assistant/message                   {text, chat_id?} -> {chat_id, reply}   (no chat_id = new chat)
GET  /assistant/people                    people in the org with voice chats (Settings, below AI models)
GET  /assistant/chats?user_id=            that person's voice chats, newest first
GET  /assistant/chats/{chat_id}/messages  one voice chat, oldest first
"""
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from backend.assistant.knowledge import GREETING, SYSTEM_PROMPT
from backend.auth.routes import require_admin
from backend.database import get_db

logger = logging.getLogger("isf_recorder.assistant")
router = APIRouter(prefix="/assistant", tags=["Voice Assistant"])

MAX_TEXT = 1000          # one spoken question
HISTORY_TURNS = 12       # earlier messages of the same chat sent to the model


class VoiceMessage(BaseModel):
    text: str
    chat_id: Optional[str] = None


def ask_model(org_id: str, history: list, question: str) -> str:
    """The assistant's answer (history = [{"role": "user"|"assistant", "text": ...}], oldest first)."""
    from google.genai import types
    from backend.settings.model_resolver import get_intelligence_model
    from backend.transcription.gemini_stt import get_genai_client
    from backend.transcription.usage_tracker import track_gemini_call
    model = get_intelligence_model(org_id)
    contents = [{"role": "user" if m["role"] == "user" else "model", "parts": [{"text": m["text"]}]}
                for m in history]
    contents.append({"role": "user", "parts": [{"text": question}]})
    config = types.GenerateContentConfig(
        system_instruction=SYSTEM_PROMPT,
        temperature=0.2,
        max_output_tokens=400,
        thinking_config=types.ThinkingConfig(thinking_budget=0),
    )
    response = track_gemini_call(
        recording_id=None, operation_type="CHAT", gemini_model=model, org_id=org_id,
        call_fn=lambda: get_genai_client().models.generate_content(model=model, contents=contents, config=config),
    )
    return speakable(response.text or "")


def speakable(out: str) -> str:
    """Text that reads well aloud: no Markdown marks, bullets or line breaks."""
    out = (out or "").replace("**", "").replace("__", "").replace("`", "").replace("#", "")
    lines = [line.strip().lstrip("-*•").strip() for line in out.splitlines()]
    return " ".join(line for line in lines if line).strip()


def _own_chat(db: Session, chat_id: str, user: dict):
    row = db.execute(text("""
        SELECT id FROM isf_recorder.voice_chat
        WHERE id = CAST(:id AS uuid) AND user_id = CAST(:uid AS uuid) AND org_id = CAST(:org AS uuid)
    """), {"id": chat_id, "uid": user["user_id"], "org": user["org_id"]}).fetchone() if _is_uuid(chat_id) else None
    if not row:
        raise HTTPException(status_code=404, detail="Voice chat not found")
    return row


def _is_uuid(v: str) -> bool:
    import uuid
    try:
        uuid.UUID(str(v))
        return True
    except ValueError:
        return False


@router.get("/greeting")
def greeting(admin: dict = Depends(require_admin)):
    return {"text": GREETING}


@router.post("/message")
def message(body: VoiceMessage, admin: dict = Depends(require_admin), db: Session = Depends(get_db)):
    question = (body.text or "").strip()
    if not question:
        raise HTTPException(status_code=400, detail="Say something first")
    question = question[:MAX_TEXT]
    if body.chat_id:
        chat_id = str(_own_chat(db, body.chat_id, admin).id)
        rows = db.execute(text("""
            SELECT role, text FROM (
                SELECT role, text, created_at FROM isf_recorder.voice_chat_message
                WHERE chat_id = CAST(:id AS uuid) ORDER BY created_at DESC LIMIT :n) t
            ORDER BY created_at
        """), {"id": chat_id, "n": HISTORY_TURNS}).fetchall()
        history = [{"role": r.role, "text": r.text} for r in rows]
    else:
        chat_id = str(db.execute(text("""
            INSERT INTO isf_recorder.voice_chat (org_id, user_id)
            VALUES (CAST(:org AS uuid), CAST(:uid AS uuid)) RETURNING id
        """), {"org": admin["org_id"], "uid": admin["user_id"]}).scalar())
        db.execute(text("""
            INSERT INTO isf_recorder.voice_chat_message (chat_id, role, text)
            VALUES (CAST(:id AS uuid), 'assistant', :t)
        """), {"id": chat_id, "t": GREETING})
        history = [{"role": "assistant", "text": GREETING}]
    try:
        reply = ask_model(admin["org_id"], history, question)
    except Exception as e:
        logger.warning(f"Voice assistant failed for {admin['user_id']}: {e}")
        reply = ""
    if not reply:
        db.rollback()                                   # a new chat with no answer is not kept
        raise HTTPException(status_code=502, detail="Jomi could not answer just now. Please try again.")
    db.execute(text("""
        INSERT INTO isf_recorder.voice_chat_message (chat_id, role, text, created_at)
        VALUES (CAST(:id AS uuid), 'user', :q, clock_timestamp()),
               (CAST(:id AS uuid), 'assistant', :a, clock_timestamp() + INTERVAL '1 millisecond')
    """), {"id": chat_id, "q": question, "a": reply})
    db.execute(text("UPDATE isf_recorder.voice_chat SET last_at = NOW() WHERE id = CAST(:id AS uuid)"),
               {"id": chat_id})
    db.commit()
    return {"chat_id": chat_id, "reply": reply}


@router.get("/people")
def people(admin: dict = Depends(require_admin), db: Session = Depends(get_db)):
    rows = db.execute(text("""
        SELECT u.id, u.name, u.phone, COUNT(c.id) AS chats, MAX(c.last_at) AS last_at
        FROM isf_recorder.voice_chat c JOIN isf_recorder."user" u ON u.id = c.user_id
        WHERE c.org_id = CAST(:org AS uuid)
        GROUP BY u.id, u.name, u.phone
        ORDER BY MAX(c.last_at) DESC
    """), {"org": admin["org_id"]}).fetchall()
    return {"people": [{"user_id": str(r.id), "name": r.name, "phone": r.phone, "chats": int(r.chats),
                        "last_at": r.last_at.isoformat() if r.last_at else None} for r in rows]}


@router.get("/chats")
def chats(user_id: str = Query(...), admin: dict = Depends(require_admin), db: Session = Depends(get_db)):
    if not _is_uuid(user_id):
        raise HTTPException(status_code=400, detail="Invalid user_id")
    rows = db.execute(text("""
        SELECT c.id, c.started_at, c.last_at, COUNT(m.id) FILTER (WHERE m.role = 'user') AS questions,
               (SELECT m2.text FROM isf_recorder.voice_chat_message m2
                 WHERE m2.chat_id = c.id AND m2.role = 'user' ORDER BY m2.created_at LIMIT 1) AS first_question
        FROM isf_recorder.voice_chat c LEFT JOIN isf_recorder.voice_chat_message m ON m.chat_id = c.id
        WHERE c.org_id = CAST(:org AS uuid) AND c.user_id = CAST(:uid AS uuid)
        GROUP BY c.id ORDER BY c.last_at DESC
    """), {"org": admin["org_id"], "uid": user_id}).fetchall()
    return {"chats": [{"chat_id": str(r.id), "started_at": r.started_at.isoformat(), "last_at": r.last_at.isoformat(),
                       "questions": int(r.questions), "first_question": r.first_question or ""} for r in rows]}


@router.get("/chats/{chat_id}/messages")
def chat_messages(chat_id: str, admin: dict = Depends(require_admin), db: Session = Depends(get_db)):
    chat = db.execute(text("""
        SELECT c.id, c.user_id, u.name FROM isf_recorder.voice_chat c JOIN isf_recorder."user" u ON u.id = c.user_id
        WHERE c.id = CAST(:id AS uuid) AND c.org_id = CAST(:org AS uuid)
    """), {"id": chat_id, "org": admin["org_id"]}).fetchone() if _is_uuid(chat_id) else None
    if not chat:
        raise HTTPException(status_code=404, detail="Voice chat not found")
    rows = db.execute(text("""
        SELECT role, text, created_at FROM isf_recorder.voice_chat_message
        WHERE chat_id = CAST(:id AS uuid) ORDER BY created_at
    """), {"id": chat_id}).fetchall()
    return {"chat_id": chat_id, "user_id": str(chat.user_id), "name": chat.name,
            "messages": [{"role": r.role, "text": r.text, "created_at": r.created_at.isoformat()} for r in rows]}
