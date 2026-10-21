"""
ISF Recorder — Recording Management & Audio Ingestion Routes
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Handles complete recording lifecycle:
- Start session (optional Schedule/Meeting metadata)
- Idempotent chunk upload (atomic writes, tombstones, stop cap)
- Heartbeat, pause / resume tracking
- Finish (fast + idempotent; stitching and transcription run in the background, see lifecycle.py)
- Star / unstar, re-transcribe (single-flight), audio streaming
- Role-based list & detail access (staff: own only, admin: all org)
"""
import os
import json
import re
import uuid
import hashlib
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Any
from pathlib import Path

IST_TZ = timezone(timedelta(hours=5, minutes=30))

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form, Query, Header, Request
from fastapi.responses import FileResponse, Response, StreamingResponse
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

import threading
from backend.database import get_db, SessionLocal
from backend.auth.routes import get_current_user
from backend.config import AUDIO_STORAGE_ROOT
from backend.recordings.chunk_assembler import concat_audio_files_to_m4a
from backend.recordings.retention import toggle_star_recording
from backend.recordings import lifecycle
from backend.transcription.engine import schedule_transcription_job, queue_for_transcription, is_inflight
from backend.calls.access import call_access, CALL_VISIBILITY_SQL, visibility_params

logger = logging.getLogger("isf_recorder.recordings")
router = APIRouter(prefix="/recordings", tags=["Recordings"])


# ─── Pydantic Request Models ──────────────────────────────────────────────────

class Participant(BaseModel):
    user_id: Optional[str] = None
    name: str


class StartRecordingRequest(BaseModel):
    session_id: str
    mic_source: str = "built_in"      # built_in | wired | usb | bluetooth
    sample_rate: int = 16000
    device_info: Optional[dict] = {}
    # Jomi v2 (all optional; mode absent = v1.2.0 app)
    mode: Optional[str] = None        # schedule | meeting
    balgruh_name: Optional[str] = None
    assistant_coach: Optional[str] = None            # legacy single coach (still accepted)
    assistant_coaches: Optional[List[Participant]] = None
    participants: Optional[List[Participant]] = None
    title: Optional[str] = None


class PauseResumeRequest(BaseModel):
    reason: str = "user"             # user | mic_conflict | camera


class HeartbeatRequest(BaseModel):
    state: Optional[str] = "recording"   # recording | listening | paused
    voice_seconds: Optional[int] = None
    session_seconds: Optional[int] = None


class FinishRecordingRequest(BaseModel):
    total_chunks: int
    duration_seconds: Optional[int] = 0
    voice_seconds: Optional[int] = None
    silence_skipped_seconds: Optional[int] = None
    # v2.1: [{at_voice_sec, wall_start_ms, seconds}] skipped silence (>= 3 s), position in the SAVED audio
    silence_gaps: Optional[List[dict]] = None


class MergeRecordingsRequest(BaseModel):
    recording_ids: List[str]
    title: Optional[str] = None


class UpdateTitleRequest(BaseModel):
    title: str


# ─── Helpers ──────────────────────────────────────────────────────────────────

def get_org_slug(db: Session, org_id: str) -> str:
    row = db.execute(
        text("SELECT slug FROM isf_recorder.organization WHERE id = :id"),
        {"id": org_id}
    ).fetchone()
    return row.slug if row else "default"


def _check_session_access(rec, current_user: dict) -> None:
    """Owner or an admin of the recording's organization."""
    if str(rec.org_id) != str(current_user["org_id"]):
        raise HTTPException(status_code=403, detail="Permission denied: Recording belongs to another organization")
    if current_user.get("role") != "admin" and str(rec.user_id) != str(current_user["user_id"]):
        raise HTTPException(status_code=403, detail="Permission denied")


def _auto_create_session(db: Session, session_id: str, current_user: dict) -> None:
    """Offline phones may upload chunks / finish before /start reached the server."""
    db.execute(
        text("""
            INSERT INTO isf_recorder.recording
            (user_id, org_id, session_id, status, mic_source, sample_rate, started_at, recorded_by_name)
            VALUES (:user_id, :org_id, :sid, 'recording', 'built_in', 16000, NOW(), :name)
            ON CONFLICT (session_id) DO NOTHING
        """),
        {"user_id": current_user["user_id"], "org_id": current_user["org_id"], "sid": session_id,
         "name": current_user.get("name")},
    )
    db.commit()


MAX_SILENCE_GAPS = 500
MIN_GAP_SECONDS = 3.0


def _clean_gaps(raw) -> Optional[list]:
    """Validates the app's skipped-silence list; None = the app sent nothing (old app)."""
    if raw is None:
        return None
    out = []
    for g in raw[:MAX_SILENCE_GAPS * 2]:
        try:
            at = float(g.get("at_voice_sec"))
            secs = float(g.get("seconds"))
            wall = int(g.get("wall_start_ms") or 0)
        except (TypeError, ValueError, AttributeError):
            continue
        if at < 0 or secs < MIN_GAP_SECONDS or secs > 24 * 3600:
            continue
        out.append({"at_voice_sec": round(at, 2), "wall_start_ms": wall, "seconds": round(secs, 1)})
        if len(out) >= MAX_SILENCE_GAPS:
            break
    out.sort(key=lambda g: g["at_voice_sec"])
    return out


def _gaps_json(value) -> list:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return []
    return value if isinstance(value, list) else []


# ─── 1. Start Recording Session ───────────────────────────────────────────────

@router.post("/start")
def start_recording(
    body: StartRecordingRequest,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Creates a new recording session or returns existing if re-started (idempotent).
    Optional Schedule/Meeting metadata: mode="schedule" needs balgruh_name and assistant_coach.
    """
    meta = _start_metadata(body)
    existing = db.execute(
        text("SELECT id, session_id, status, mode, org_id FROM isf_recorder.recording WHERE session_id = :sid"),
        {"sid": body.session_id}
    ).fetchone()

    if existing:
        if meta["mode"] and existing.mode is None and str(existing.org_id) == current_user["org_id"]:
            # e.g. an offline chunk created the row before /start arrived: attach the metadata now
            db.execute(text("""
                UPDATE isf_recorder.recording
                SET mode = :mode, balgruh_name = :balgruh, assistant_coach = :coach,
                    assistant_coaches = CAST(:coaches AS jsonb),
                    participants = CAST(:participants AS jsonb), title = COALESCE(:title, title)
                WHERE id = :id AND mode IS NULL
            """), {**meta, "id": str(existing.id)})
            db.commit()
        return {
            "status": "exists",
            "recording_id": str(existing.id),
            "session_id": existing.session_id,
            "current_status": existing.status
        }

    result = db.execute(
        text("""
            INSERT INTO isf_recorder.recording
            (user_id, org_id, session_id, status, mic_source, sample_rate, device_info, started_at, recorded_by_name,
             mode, balgruh_name, assistant_coach, assistant_coaches, participants, title)
            VALUES (:user_id, :org_id, :sid, 'recording', :mic, :sample_rate, CAST(:dev_info AS jsonb), NOW(), :name,
                    :mode, :balgruh, :coach, CAST(:coaches AS jsonb), CAST(:participants AS jsonb), :title)
            ON CONFLICT (session_id) DO NOTHING
            RETURNING id, session_id, status, started_at
        """),
        {
            "user_id": current_user["user_id"],
            "org_id": current_user["org_id"],
            "sid": body.session_id,
            "mic": body.mic_source,
            "sample_rate": body.sample_rate,
            "dev_info": json.dumps(body.device_info or {}),
            "name": current_user.get("name"),
            **meta,
        }
    ).fetchone()
    db.commit()
    if result is None:  # a concurrent /start or chunk created it first
        row = db.execute(text("SELECT id, session_id, status FROM isf_recorder.recording WHERE session_id = :sid"),
                         {"sid": body.session_id}).fetchone()
        return {"status": "exists", "recording_id": str(row.id), "session_id": row.session_id,
                "current_status": row.status}

    return {
        "status": "created",
        "recording_id": str(result.id),
        "session_id": result.session_id,
        "current_status": result.status
    }


def _people(items) -> list:
    out, seen = [], set()
    for p in items or []:
        name = " ".join((p.name or "").split())
        if name and name.lower() not in seen:
            seen.add(name.lower())
            out.append({"user_id": p.user_id, "name": name})
    return out


def _start_metadata(body: StartRecordingRequest) -> dict:
    mode = (body.mode or "").strip().lower() or None
    if mode is not None and mode not in ("schedule", "meeting"):
        raise HTTPException(status_code=400, detail="mode must be 'schedule' or 'meeting'")
    balgruh = (body.balgruh_name or "").strip() or None
    title = (body.title or "").strip() or None
    coaches = _people(body.assistant_coaches)
    if not coaches and (body.assistant_coach or "").strip():
        coaches = _people([Participant(name=body.assistant_coach)])  # legacy single field
    participants = _people(body.participants)
    if mode == "schedule":
        if not balgruh or not coaches:
            raise HTTPException(status_code=400, detail="Schedule recordings need a Balgruha and at least one assistant coach")
        title = title or f"{balgruh} · {', '.join(c['name'] for c in coaches)}"  # e.g. "Mohor Boys · Rizwana, Archana"
    if mode == "meeting" and not participants:
        raise HTTPException(status_code=400, detail="Meeting recordings need at least one participant")
    return {"mode": mode, "balgruh": balgruh if mode else None,
            "coach": ", ".join(c["name"] for c in coaches) if (mode and coaches) else None,
            "coaches": json.dumps(coaches if mode else []),
            "participants": json.dumps(participants), "title": title}


# ─── 2. Upload Audio Chunk ────────────────────────────────────────────────────

@router.post("/{session_id}/chunk")
async def upload_chunk(
    session_id: str,
    chunk_index: int = Form(...),
    duration_seconds: float = Form(0.0),
    checksum: Optional[str] = Form(None),
    chunk_file: UploadFile = File(...),
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Upload an audio chunk. Idempotent: safe to retry if the network dropped.
    - Erased session -> 200 "discarded" (nothing stored; old apps retry non-2xx forever)
    - Stopped by the user and chunk_index >= expected_chunks -> 200 "ignored_after_stop"
    - Written to *.tmp then os.replace, so a stitch never reads a half-written WAV
    """
    if lifecycle.is_tombstoned(db, session_id):
        return {"status": "discarded", "session_id": session_id, "chunk_index": chunk_index}

    query = text("""
        SELECT id, org_id, user_id, status, finish_source, expected_chunks, pipeline_stage,
               finish_requested_at, audio_file_path, chunks_cleaned_at
        FROM isf_recorder.recording WHERE session_id = :sid
    """)
    rec = db.execute(query, {"sid": session_id}).fetchone()
    if not rec:
        logger.info(f"Auto-creating offline session {session_id} on chunk {chunk_index} upload for user {current_user['user_id']}")
        _auto_create_session(db, session_id, current_user)
        rec = db.execute(query, {"sid": session_id}).fetchone()
    if not rec:
        raise HTTPException(status_code=500, detail="Failed to initialize recording session")
    _check_session_access(rec, current_user)

    if (rec.finish_source in ("client", "sync_worker") and rec.expected_chunks is not None
            and chunk_index >= rec.expected_chunks):
        return {"status": "ignored_after_stop", "session_id": session_id, "chunk_index": chunk_index}

    file_bytes = await chunk_file.read()
    if checksum:
        actual = hashlib.sha256(file_bytes).hexdigest()
        if actual.lower() != checksum.strip().lower():
            logger.warning(f"Checksum mismatch for {session_id} chunk {chunk_index}: got {len(file_bytes)} bytes")
            raise HTTPException(status_code=400, detail="Checksum mismatch, please retry the upload")
    calc_sum = checksum or hashlib.sha256(file_bytes).hexdigest()

    rec_id = str(rec.id)
    org_slug = get_org_slug(db, str(rec.org_id))
    existing_chunk = db.execute(text("""
        SELECT file_path FROM isf_recorder.audio_chunk WHERE recording_id = :rec_id AND chunk_index = :idx
    """), {"rec_id": rec_id, "idx": chunk_index}).fetchone()
    if existing_chunk and existing_chunk.file_path:
        chunk_path = Path(existing_chunk.file_path)  # a retry overwrites the same file
        chunk_path.parent.mkdir(parents=True, exist_ok=True)
    else:
        chunk_dir = Path(AUDIO_STORAGE_ROOT) / org_slug / datetime.utcnow().strftime("%Y-%m-%d") / session_id / "chunks"
        chunk_dir.mkdir(parents=True, exist_ok=True)
        chunk_path = chunk_dir / f"chunk_{chunk_index:04d}.wav"

    tmp_path = chunk_path.with_name(f"{chunk_path.name}.{uuid.uuid4().hex[:8]}.tmp")
    with open(tmp_path, "wb") as f:
        f.write(file_bytes)
    try:
        os.replace(tmp_path, chunk_path)
    except PermissionError:
        # Windows: the target is open (e.g. being stitched). Same bytes as before for a retry.
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        if not chunk_path.exists():
            raise HTTPException(status_code=503, detail="Chunk could not be saved, please retry")

    db.execute(
        text("""
            INSERT INTO isf_recorder.audio_chunk
            (recording_id, chunk_index, file_path, duration_seconds, size_bytes, checksum, status)
            VALUES (:rec_id, :idx, :fpath, :dur, :size, :csum, 'received')
            ON CONFLICT (recording_id, chunk_index) DO UPDATE
            SET file_path = EXCLUDED.file_path,
                size_bytes = EXCLUDED.size_bytes,
                checksum = EXCLUDED.checksum,
                status = 'received'
        """),
        {"rec_id": rec_id, "idx": chunk_index, "fpath": str(chunk_path), "dur": duration_seconds,
         "size": len(file_bytes), "csum": calc_sum},
    )
    counts = db.execute(
        text("""
            UPDATE isf_recorder.recording
            SET synced_chunks = (SELECT count(*) FROM isf_recorder.audio_chunk WHERE recording_id = :rec_id),
                last_chunk_at = NOW()
            WHERE id = :rec_id
            RETURNING synced_chunks, expected_chunks, pipeline_stage, finish_requested_at, audio_file_path,
                      chunks_cleaned_at
        """),
        {"rec_id": rec_id}
    ).fetchone()
    db.commit()

    if counts.pipeline_stage == "awaiting_chunks":
        if counts.synced_chunks >= (counts.expected_chunks or 0):
            lifecycle.start_finalize_thread(rec_id)
    elif (counts.finish_requested_at is not None and counts.audio_file_path and counts.chunks_cleaned_at is None
          and counts.pipeline_stage != "stitching"):
        # Late chunk after the recording was finalized: debounced re-stitch in the background
        lifecycle.schedule_late_restitch(rec_id)

    return {
        "status": "ok",
        "session_id": session_id,
        "chunk_index": chunk_index,
        "bytes_received": len(file_bytes)
    }


# ─── 3. Heartbeat, Pause & Resume ─────────────────────────────────────────────

@router.post("/{session_id}/heartbeat")
def heartbeat(
    session_id: str,
    body: Optional[HeartbeatRequest] = None,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """The app calls this every ~60 s while a session is open. Tells the app if the server finalized it."""
    if lifecycle.is_tombstoned(db, session_id):
        return {"status": "discarded", "recording_status": None}
    rec = db.execute(
        text("SELECT id, org_id, user_id, status FROM isf_recorder.recording WHERE session_id = :sid"),
        {"sid": session_id}
    ).fetchone()
    if not rec:
        raise HTTPException(status_code=404, detail="Recording not found")
    _check_session_access(rec, current_user)
    if rec.status != "recording":
        return {"status": "finalized", "recording_status": rec.status}

    body = body or HeartbeatRequest()
    row = db.execute(
        text("""
            UPDATE isf_recorder.recording
            SET last_heartbeat_at = NOW(),
                voice_seconds = COALESCE(:voice, voice_seconds)
            WHERE id = :id AND status = 'recording'
            RETURNING status
        """),
        {"id": str(rec.id), "voice": body.voice_seconds}
    ).fetchone()
    db.commit()
    if not row:
        status_now = db.execute(text("SELECT status FROM isf_recorder.recording WHERE id = :id"),
                                {"id": str(rec.id)}).scalar()
        return {"status": "finalized", "recording_status": status_now}
    return {"status": "ok", "recording_status": "recording"}


def _session_for_update(db: Session, session_id: str, current_user: dict):
    rec = db.execute(
        text("SELECT id, org_id, user_id FROM isf_recorder.recording WHERE session_id = :sid"),
        {"sid": session_id}
    ).fetchone()
    if rec:
        _check_session_access(rec, current_user)
    return rec


@router.post("/{session_id}/pause")
def pause_recording(
    session_id: str,
    body: PauseResumeRequest,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Track pause event (e.g., mic conflict from camera, incoming call, or manual)."""
    now_str = datetime.utcnow().isoformat()
    _session_for_update(db, session_id, current_user)
    pause_entry = json.dumps([{"reason": body.reason, "paused_at": now_str, "resumed_at": None}])

    db.execute(
        text("""
            UPDATE isf_recorder.recording
            SET pauses = COALESCE(pauses, '[]'::jsonb) || CAST(:entry AS jsonb)
            WHERE session_id = :sid
        """),
        {"sid": session_id, "entry": pause_entry}
    )
    db.commit()
    return {"status": "paused", "reason": body.reason, "timestamp": now_str}


@router.post("/{session_id}/resume")
def resume_recording(
    session_id: str,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Track session resume: sets resumed_at inside the last pause entry."""
    now_str = datetime.utcnow().isoformat()
    _session_for_update(db, session_id, current_user)
    db.execute(
        text("""
            UPDATE isf_recorder.recording
            SET pauses = jsonb_set(
                    pauses,
                    ARRAY[CAST(jsonb_array_length(pauses) - 1 AS text), 'resumed_at'],
                    to_jsonb(CAST(:now AS text)),
                    true)
            WHERE session_id = :sid
              AND jsonb_typeof(pauses) = 'array'
              AND jsonb_array_length(pauses) > 0
              AND jsonb_typeof(pauses -> (jsonb_array_length(pauses) - 1)) = 'object'
        """),
        {"sid": session_id, "now": now_str}
    )
    db.commit()
    return {"status": "resumed", "timestamp": now_str}


# ─── 4. Finish Session ────────────────────────────────────────────────────────

@router.post("/{session_id}/finish")
def finish_recording(
    session_id: str,
    body: FinishRecordingRequest,
    x_finish_source: Optional[str] = Header(None),
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Idempotent and fast. Records the stop and returns; lifecycle.maybe_finalize stitches once all
    expected chunks are in (the sweeper finalizes with what arrived after 10 min) and queues ONE
    transcription. A repeated call only raises expected_chunks and returns already_finished=true.
    """
    if lifecycle.is_tombstoned(db, session_id):
        return {"status": "discarded", "session_id": session_id}

    rec = db.execute(text("SELECT id FROM isf_recorder.recording WHERE session_id = :sid"), {"sid": session_id}).fetchone()
    if not rec:
        logger.info(f"Auto-creating offline session {session_id} on finish call for user {current_user['user_id']}")
        _auto_create_session(db, session_id, current_user)

    rec = db.execute(text("""
        SELECT id, org_id, user_id, status, finish_requested_at, expected_chunks, duration_seconds
        FROM isf_recorder.recording WHERE session_id = :sid FOR UPDATE
    """), {"sid": session_id}).fetchone()
    if not rec:
        raise HTTPException(status_code=500, detail="Failed to initialize recording session")
    try:
        _check_session_access(rec, current_user)
    except HTTPException:
        db.rollback()
        raise
    rec_id = str(rec.id)
    total_chunks = max(0, int(body.total_chunks or 0))
    gaps = _clean_gaps(body.silence_gaps)

    if rec.finish_requested_at is not None:
        row = db.execute(text("""
            UPDATE isf_recorder.recording
            SET expected_chunks = GREATEST(COALESCE(expected_chunks, 0), :n),
                silence_gaps = CASE WHEN CAST(:gaps AS jsonb) IS NOT NULL
                                     AND jsonb_array_length(COALESCE(silence_gaps, '[]'::jsonb)) = 0
                                    THEN CAST(:gaps AS jsonb) ELSE silence_gaps END,
                silence_skipped_count = CASE WHEN CAST(:gaps AS jsonb) IS NOT NULL AND silence_skipped_count IS NULL
                                             THEN :gap_count ELSE silence_skipped_count END
            WHERE id = :id
            RETURNING expected_chunks, pipeline_stage, synced_chunks, duration_seconds
        """), {"id": rec_id, "n": total_chunks, "gaps": json.dumps(gaps) if gaps is not None else None,
               "gap_count": len(gaps or [])}).fetchone()
        db.commit()
        if row.pipeline_stage == "awaiting_chunks" and (row.synced_chunks or 0) >= (row.expected_chunks or 0):
            lifecycle.start_finalize_thread(rec_id)
        return {
            "status": "processing",
            "recording_id": rec_id,
            "session_id": session_id,
            "duration_seconds": int(row.duration_seconds or body.duration_seconds or 0),
            "chunks_assembled": int(row.synced_chunks or 0),
            "already_finished": True,
        }

    source = "sync_worker" if (x_finish_source or "").strip().lower() == "sync_worker" else "client"
    row = db.execute(text("""
        UPDATE isf_recorder.recording
        SET finish_requested_at = NOW(), finish_source = :source, expected_chunks = :n,
            status = 'uploading', pipeline_stage = 'awaiting_chunks',
            voice_seconds = COALESCE(:voice, voice_seconds),
            silence_skipped_seconds = COALESCE(:silence, silence_skipped_seconds),
            silence_gaps = COALESCE(CAST(:gaps AS jsonb), silence_gaps),
            silence_skipped_count = COALESCE(:gap_count, silence_skipped_count),
            finished_at = COALESCE(finished_at, NOW())
        WHERE id = :id
        RETURNING synced_chunks
    """), {"id": rec_id, "source": source, "n": total_chunks, "voice": body.voice_seconds,
           "silence": body.silence_skipped_seconds if body.silence_skipped_seconds is not None
                      else (int(round(sum(g["seconds"] for g in gaps))) if gaps else None),
           "gaps": json.dumps(gaps) if gaps is not None else None,
           "gap_count": len(gaps) if gaps is not None else None}).fetchone()
    db.commit()
    lifecycle.start_finalize_thread(rec_id)

    return {
        "status": "processing",
        "recording_id": rec_id,
        "session_id": session_id,
        "duration_seconds": int(body.duration_seconds or rec.duration_seconds or 0),
        "chunks_assembled": int(row.synced_chunks or 0),
    }


# ─── 5. List Recordings (Role-Based Access) ───────────────────────────────────

def _clean_overview(text: Optional[str]) -> str:
    if not text:
        return ""
    from backend.chat.gemini_chat import clean_chat_response
    cleaned = clean_chat_response(text)
    # Clean up python list representation if present, e.g. "Key Outcomes: ['...']"
    def _format_outcomes(m):
        raw = m.group(1)
        items = re.findall(r"['\"]([^'\"]+)['\"]", raw)
        lines = ["• " + it.strip(" '\"") for it in items]
        return "Key Outcomes:\n" + "\n".join(lines)

    cleaned = re.sub(r"Key Outcomes:\s*\[(['\"][^\]]+['\"])\]", _format_outcomes, cleaned)
    return cleaned


def _clean_list_field(val: Any) -> List[str]:
    if not val:
        return []
    if isinstance(val, str):
        val = val.strip()
        if val.startswith("[") and val.endswith("]"):
            try:
                parsed = json.loads(val)
                if isinstance(parsed, list):
                    return [str(x).replace("\\r\\n", "\n").replace("\\n", "\n").strip() for x in parsed if x]
            except Exception:
                pass
        return [p.strip() for p in val.replace("\\r\\n", "\n").replace("\\n", "\n").split("\n") if p.strip()]
    if isinstance(val, list):
        cleaned = []
        for x in val:
            if isinstance(x, str):
                cleaned.append(x.replace("\\r\\n", "\n").replace("\\n", "\n").strip())
            else:
                cleaned.append(str(x))
        return cleaned
    return []


@router.get("/")
def list_recordings(
    starred_only: bool = Query(False),
    status: Optional[str] = Query(None),
    search: Optional[str] = Query(None),
    user_id: Optional[str] = Query(None),
    user_name: Optional[str] = Query(None),
    date_from: Optional[str] = Query(None, description="YYYY-MM-DD, IST, inclusive"),
    date_to: Optional[str] = Query(None, description="YYYY-MM-DD, IST, inclusive"),
    mode: Optional[str] = Query(None, description="schedule | meeting"),
    balgruh: Optional[str] = Query(None, description="exact Balgruha name"),
    assistant_coach: Optional[str] = Query(None, description="name of one of the assistant coaches"),
    source: str = Query("recording", description="recording (classes/meetings) | call (phone calls, private)"),
    contact: Optional[str] = Query(None, description="calls: exact contact name"),
    shared_only: bool = Query(False, description="calls (admin): only calls other people shared with admins"),
    page: int = Query(1, ge=1),
    limit: int = Query(25, ge=1, le=100),
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Role-based recording listing:
    - Staff sees ONLY their own recordings.
    - Admin sees ALL recordings across the entire organization, with optional staff filter.
    """
    is_admin = current_user.get("role") == "admin"
    current_user_id = current_user["user_id"]
    org_id = current_user["org_id"]
    offset = (page - 1) * limit

    # Build WHERE conditions
    where_clauses = ["r.is_deleted = FALSE", "r.org_id = :org_id"]
    params = {"org_id": org_id, "limit": limit, "offset": offset}

    # STAFF: only see their own recordings
    if not is_admin:
        where_clauses.append("r.user_id = :current_user_id")
        params["current_user_id"] = current_user_id
    else:
        # Admin can filter by specific user
        if user_id:
            where_clauses.append("r.user_id = :filter_user_id")
            params["filter_user_id"] = user_id
        elif user_name:
            where_clauses.append("COALESCE(u.name, r.recorded_by_name) ILIKE :filter_user_name")
            params["filter_user_name"] = f"%{user_name}%"

    if starred_only:
        where_clauses.append("r.is_starred = TRUE")

    if status:
        where_clauses.append("r.status = :status")
        params["status"] = status

    if search:
        where_clauses.append("(r.title ILIKE :search OR r.overview ILIKE :search OR r.transcript ILIKE :search)")
        params["search"] = f"%{search}%"

    if mode:
        if mode not in ("schedule", "meeting"):
            raise HTTPException(status_code=400, detail="mode must be 'schedule' or 'meeting'")
        # old-app recordings (mode NULL) count as Schedule, like their report template
        where_clauses.append("r.mode = :mode" if mode == "meeting" else "(r.mode = :mode OR r.mode IS NULL)")
        params["mode"] = mode
    if source not in ("recording", "call"):
        raise HTTPException(status_code=400, detail="source must be 'recording' or 'call'")
    where_clauses.append("r.source = :source")
    params["source"] = source
    if source == "call":
        # Calls are private: own calls, plus (admins) calls their owner shared. Never the org-wide view.
        params["viewer"] = current_user_id
        if is_admin and shared_only:
            where_clauses.append("r.shared_with_admin AND r.user_id <> CAST(:viewer AS uuid)")
        elif is_admin:
            where_clauses.append("(r.user_id = CAST(:viewer AS uuid) OR r.shared_with_admin)")
        else:
            where_clauses.append("r.user_id = CAST(:viewer AS uuid)")
        if contact:
            where_clauses.append("LOWER(TRIM(r.contact_name)) = LOWER(TRIM(:contact))")
            params["contact"] = contact
    if balgruh:
        where_clauses.append("r.balgruh_name = :balgruh")
        params["balgruh"] = balgruh.strip()
    if assistant_coach:
        # new app: assistant_coaches [{user_id?, name}]; old rows: assistant_coach text "A, B"
        where_clauses.append("""(
            (jsonb_typeof(r.assistant_coaches) = 'array' AND EXISTS (
                SELECT 1 FROM jsonb_array_elements(r.assistant_coaches) e WHERE LOWER(TRIM(e->>'name')) = LOWER(:ac)))
            OR (',' || LOWER(REPLACE(COALESCE(r.assistant_coach, ''), ', ', ',')) || ',') LIKE ('%,' || LOWER(:ac) || ',%')
        )""")
        params["ac"] = assistant_coach.strip()
    for key, value, op in (("date_from", date_from, ">="), ("date_to", date_to, "<=")):
        if value:
            try:
                params[key] = datetime.strptime(value, "%Y-%m-%d").date()
            except ValueError:
                raise HTTPException(status_code=400, detail=f"{key} must be YYYY-MM-DD")
            where_clauses.append(f"(COALESCE(r.started_at, r.created_at) AT TIME ZONE 'Asia/Kolkata')::date {op} :{key}")

    where_sql = " AND ".join(where_clauses)

    query = f"""
        WITH page_recs AS (
            SELECT r.id, r.user_id, COALESCE(u.name, r.recorded_by_name) as user_name, r.session_id, r.title, r.status,
                   r.duration_seconds, r.is_starred, r.starred_at, r.audio_deleted_at,
                   r.detected_language, LEFT(r.overview, 300) as overview, r.created_at, r.finished_at,
                   r.started_at, r.error_message, r.stt_engine,
                   r.audio_file_path IS NOT NULL AND r.audio_deleted_at IS NULL as has_audio,
                   r.mode, r.balgruh_name, r.assistant_coach, r.assistant_coaches, r.participants, r.voice_seconds, r.pipeline_stage,
                   r.source, r.contact_name, r.contact_phone, r.call_type, r.shared_with_admin,
                   (SELECT COUNT(*) FROM isf_recorder.call_task t
                     WHERE t.recording_id = r.id AND NOT t.done AND t.deleted_at IS NULL) AS open_tasks,
                   r.silence_skipped_count, r.silence_skipped_seconds,
                   rr.status AS report_status
            FROM isf_recorder.recording r
            LEFT JOIN isf_recorder.user u ON r.user_id = u.id
            LEFT JOIN isf_recorder.recording_report rr ON rr.recording_id = r.id
            WHERE {where_sql}
            ORDER BY r.created_at DESC
            LIMIT :limit OFFSET :offset
        )
        SELECT pr.*,
               COALESCE(SUM(ul.total_cost_inr), 0) as estimated_cost,
               COALESCE(
                   MAX(CASE WHEN ul.operation_type = 'TRANSCRIPTION' THEN ul.gemini_model END),
                   CASE WHEN pr.stt_engine = 'assemblyai' THEN 'assemblyai-universal-2'
                        WHEN pr.stt_engine = 'whisper' THEN 'whisper'
                        ELSE MAX(ul.gemini_model)
                   END,
                   'gemini-2.5-flash'
               ) as primary_model,
               COUNT(ul.id) as total_calls
        FROM page_recs pr
        LEFT JOIN isf_recorder.gemini_usage_log ul ON ul.recording_id = pr.id
        GROUP BY pr.id, pr.user_id, pr.user_name, pr.session_id, pr.title, pr.status,
                 pr.duration_seconds, pr.is_starred, pr.starred_at, pr.audio_deleted_at,
                 pr.detected_language, pr.overview, pr.created_at, pr.finished_at,
                 pr.started_at, pr.error_message, pr.stt_engine, pr.has_audio,
                 pr.mode, pr.balgruh_name, pr.assistant_coach, pr.assistant_coaches, pr.participants, pr.voice_seconds,
                 pr.pipeline_stage, pr.report_status, pr.silence_skipped_count, pr.silence_skipped_seconds,
                        pr.source, pr.contact_name, pr.contact_phone, pr.call_type, pr.shared_with_admin, pr.open_tasks
        ORDER BY pr.created_at DESC
    """

    rows = db.execute(text(query), params).fetchall()

    count_query = f"""
        SELECT count(*) as total
        FROM isf_recorder.recording r
        LEFT JOIN isf_recorder.user u ON r.user_id = u.id
        WHERE {where_sql}
    """
    total = db.execute(text(count_query), params).fetchone().total

    items = []
    for row in rows:
        st_dt = row.started_at or row.created_at
        st_fmt = None
        if st_dt:
            try:
                st_fmt = st_dt.astimezone(IST_TZ).strftime("%d %b %Y, %I:%M %p")
            except Exception:
                st_fmt = st_dt.strftime("%d %b %Y, %I:%M %p")

        est_cost = round(float(row.estimated_cost or 0.0), 2)
        stt_eng = getattr(row, "stt_engine", None)
        model_name = row.primary_model
        if stt_eng == "assemblyai" and (not model_name or "gemini" in model_name):
            model_name = "assemblyai-universal-2"

        items.append({
            "id": str(row.id),
            "user_id": str(row.user_id) if row.user_id else None,
            "user_name": row.user_name,
            "session_id": row.session_id,
            "title": row.title if (row.title and not row.title.lower().startswith("untitled")) else (
                f"Recording {st_dt.strftime('%d %b, %I:%M %p')}" if st_dt else (row.title or "Recording Session")
            ),
            "status": row.status,
            "error_message": getattr(row, "error_message", None),
            "duration_seconds": row.duration_seconds,
            "is_starred": row.is_starred,
            "has_audio": row.has_audio,
            "audio_deleted_at": row.audio_deleted_at.isoformat() if row.audio_deleted_at else None,
            "detected_language": row.detected_language,
            "overview": _clean_overview(row.overview),
            "started_at": row.started_at.isoformat() if row.started_at else None,
            "started_at_formatted": st_fmt,
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "finished_at": row.finished_at.isoformat() if row.finished_at else None,
            "estimated_cost": est_cost,
            "estimated_cost_formatted": f"₹{est_cost:.2f}",
            "stt_engine": stt_eng or "gemini",
            "primary_model": model_name or "gemini-2.5-flash",
            "total_calls": int(row.total_calls or 0),
            **_v2_fields(row, st_dt),
        })

    return {
        "items": items,
        "total": total,
        "page": page,
        "limit": limit,
        "is_admin": is_admin
    }


def format_duration(seconds: Optional[int]) -> str:
    total = int(seconds or 0)
    if total < 60:
        return f"{total} sec"
    h, m = divmod(total // 60, 60)
    return f"{h} h {m:02d} min" if h else f"{m} min"


def _v2_fields(row, st_dt) -> dict:
    """Jomi v2 fields shared by list and detail (contract §4.6)."""
    date_fmt = None
    if st_dt:
        d = st_dt.astimezone(IST_TZ) if st_dt.tzinfo else st_dt
        date_fmt = f"{d.day} {d.strftime('%b %Y')}"
    def _as_list(value):
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except ValueError:
                value = []
        return value or []
    participants = _as_list(row.participants)
    coaches = _as_list(getattr(row, "assistant_coaches", None))
    if not coaches and row.assistant_coach:
        coaches = [{"user_id": None, "name": n.strip()} for n in row.assistant_coach.split(",") if n.strip()]
    return {
        "mode": row.mode,
        "balgruh_name": row.balgruh_name,
        "assistant_coach": row.assistant_coach,
        "assistant_coaches": coaches,
        "participants": participants,
        "report_status": getattr(row, "report_status", None),
        "voice_seconds": row.voice_seconds,
        "pipeline_stage": row.pipeline_stage,
        "silence_skipped_count": getattr(row, "silence_skipped_count", None) or 0,
        "silence_skipped_seconds": getattr(row, "silence_skipped_seconds", None) or 0,
        "date_formatted": date_fmt,
        "duration_formatted": format_duration(row.duration_seconds),
        # Calls (14 Oct): a recording synced from the phone's call recorder, private to its owner
        "source": getattr(row, "source", None) or "recording",
        "contact_name": getattr(row, "contact_name", None),
        "contact_phone": getattr(row, "contact_phone", None),
        "call_type": getattr(row, "call_type", None),
        "shared_with_admin": bool(getattr(row, "shared_with_admin", False)),
        "open_tasks": getattr(row, "open_tasks", None),
    }


# ─── 5b. Status of several recordings (Home polling, ReportStatusWorker) ──────
# Declared before GET /{recording_id} so FastAPI does not route "status" to the detail handler.

@router.get("/status")
def recordings_status(
    ids: str = Query(..., description="Comma-separated recording ids (max 50)"),
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    wanted = []
    for raw in [x.strip() for x in ids.split(",") if x.strip()]:
        try:
            wanted.append(str(uuid.UUID(raw)))
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid recording id: {raw}")
    if len(wanted) > 50:
        raise HTTPException(status_code=400, detail="At most 50 ids per request")
    if not wanted:
        return {"items": []}
    params = {"ids": wanted, "org": current_user["org_id"]}
    owner_sql = ""
    if current_user.get("role") != "admin":
        owner_sql = "AND r.user_id = :uid"
        params["uid"] = current_user["user_id"]
    rows = db.execute(text(f"""
        SELECT r.id, r.status, r.pipeline_stage, rr.status AS report_status, r.error_message, r.title,
               r.duration_seconds, r.silence_skipped_count, r.silence_skipped_seconds
        FROM isf_recorder.recording r
        LEFT JOIN isf_recorder.recording_report rr ON rr.recording_id = r.id
        WHERE r.id = ANY(CAST(:ids AS uuid[])) AND r.org_id = :org AND r.is_deleted = FALSE {owner_sql}
          AND {CALL_VISIBILITY_SQL}
    """), {**params, **visibility_params(current_user)}).fetchall()
    return {"items": [{
        "id": str(r.id), "status": r.status, "pipeline_stage": r.pipeline_stage, "report_status": r.report_status,
        "error_message": r.error_message, "title": r.title, "duration_seconds": r.duration_seconds,
        "silence_skipped_count": r.silence_skipped_count or 0, "silence_skipped_seconds": r.silence_skipped_seconds or 0,
    } for r in rows]}


# ─── 5c. Unfinished sessions (admin) ───────────────────────────────────────────
# Sessions still 'recording' that nobody finished (app killed, phone lost, pre-deploy orphans).
# Admin can force-finish (POST /{id}/force-finish) or discard (DELETE /{id}). Declared above the detail route.

LIVE_HEARTBEAT_SQL = "(r.last_heartbeat_at IS NOT NULL AND r.last_heartbeat_at > NOW() - INTERVAL '3 minutes')"


@router.get("/unfinished")
def list_unfinished_sessions(current_user: dict = Depends(get_current_user), db: Session = Depends(get_db)):
    if current_user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    rows = db.execute(text(f"""
        SELECT r.id, r.session_id, COALESCE(u.name, r.recorded_by_name) AS recorded_by, r.started_at,
               r.mode, r.balgruh_name, r.title,
               COUNT(c.id) AS chunks, COALESCE(SUM(c.duration_seconds), 0) AS audio_seconds,
               GREATEST(r.last_heartbeat_at, r.last_chunk_at, r.started_at) AS last_activity
        FROM isf_recorder.recording r
        LEFT JOIN isf_recorder.user u ON u.id = r.user_id
        LEFT JOIN isf_recorder.audio_chunk c ON c.recording_id = r.id
        WHERE r.org_id = :org AND r.status = 'recording' AND r.finish_requested_at IS NULL
          AND NOT {LIVE_HEARTBEAT_SQL}
        GROUP BY r.id, u.name
        ORDER BY r.started_at DESC
    """), {"org": current_user["org_id"]}).fetchall()

    def ist(dt):
        return dt.astimezone(IST_TZ).strftime("%d %b %Y, %I:%M %p") if dt else None
    return {"items": [{
        "id": str(r.id), "session_id": r.session_id, "recorded_by": r.recorded_by,
        "started_at": r.started_at.isoformat() if r.started_at else None, "started_at_formatted": ist(r.started_at),
        "chunks": int(r.chunks or 0), "audio_seconds": round(float(r.audio_seconds or 0), 1),
        "last_activity": r.last_activity.isoformat() if r.last_activity else None,
        "last_activity_formatted": ist(r.last_activity),
        "mode": r.mode, "balgruh_name": r.balgruh_name, "title": r.title,
    } for r in rows]}


@router.post("/{recording_id}/force-finish")
def force_finish_session(recording_id: str, current_user: dict = Depends(get_current_user),
                         db: Session = Depends(get_db)):
    """Admin: finish a stuck session with the audio that arrived (no audio -> marked abandoned)."""
    call_access(db, recording_id, current_user, write=True)   # calls are private to their owner
    if current_user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    try:
        rec = db.execute(text(f"""
            SELECT r.id, r.org_id, r.status, r.finish_requested_at, {LIVE_HEARTBEAT_SQL} AS live
            FROM isf_recorder.recording r WHERE r.id = CAST(:id AS uuid)
        """), {"id": recording_id}).fetchone()
    except Exception:
        db.rollback()
        rec = None
    if not rec or str(rec.org_id) != current_user["org_id"]:
        raise HTTPException(status_code=404, detail="Recording not found")
    if rec.live:
        raise HTTPException(status_code=409, detail="This session is still live (the app sent a heartbeat in the last 3 minutes)")
    if rec.status != "recording" or rec.finish_requested_at is not None:
        raise HTTPException(status_code=409, detail="This session is already finished")
    outcome = lifecycle.finish_unfinished_session(db, str(rec.id), "admin", background=True)
    if outcome is None:
        raise HTTPException(status_code=409, detail="This session was finished in the meantime")
    row = db.execute(text("SELECT status, pipeline_stage, error_message FROM isf_recorder.recording WHERE id = :id"),
                     {"id": str(rec.id)}).fetchone()
    return {"status": row.status, "pipeline_stage": row.pipeline_stage, "error_message": row.error_message,
            "recording_id": str(rec.id), "outcome": outcome}


# ─── 6. Recording Detail ──────────────────────────────────────────────────────

@router.get("/{recording_id}")
def get_recording_detail(
    recording_id: str,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Get comprehensive details of a single recording:
    Transcript, segments with speaker diarization & timestamps,
    summary, key points, decisions, and action items.
    """
    call_access(db, recording_id, current_user, write=False)   # calls are private to their owner
    rec = db.execute(
        text("""
            SELECT r.*, COALESCE(u.name, r.recorded_by_name) as user_name, u.phone as user_phone
            FROM isf_recorder.recording r
            LEFT JOIN isf_recorder.user u ON r.user_id = u.id
            WHERE r.id = :id AND r.is_deleted = FALSE
        """),
        {"id": recording_id}
    ).fetchone()

    if not rec:
        raise HTTPException(status_code=404, detail="Recording not found")

    # Verify recording belongs to the user or user is admin
    is_admin = current_user.get("role") == "admin"
    if not is_admin and str(rec.user_id) != current_user["user_id"]:
        raise HTTPException(status_code=403, detail="Permission denied: You can only view your own recordings")
    if str(rec.org_id) != current_user["org_id"]:
        raise HTTPException(status_code=403, detail="Permission denied: Recording belongs to another organization")

    # Fetch segments
    segments = db.execute(
        text("""
            SELECT id, speaker, speaker_id, text, language, start_time, end_time, confidence
            FROM isf_recorder.transcript_segment
            WHERE recording_id = :id
            ORDER BY start_time ASC
        """),
        {"id": recording_id}
    ).fetchall()

    # Fetch action items
    action_items = db.execute(
        text("""
            SELECT id, assigned_to, description, urgency, due_date, completed, completed_at
            FROM isf_recorder.action_item
            WHERE recording_id = :id
            ORDER BY created_at ASC
        """),
        {"id": recording_id}
    ).fetchall()

    has_audio = bool(rec.audio_file_path and os.path.exists(rec.audio_file_path) and not rec.audio_deleted_at)
    report_row = db.execute(text("""
        SELECT status, template_title, updated_at FROM isf_recorder.recording_report WHERE recording_id = :id
    """), {"id": recording_id}).fetchone()

    st_dt = rec.started_at or rec.created_at
    st_formatted = None
    if st_dt:
        try:
            st_formatted = st_dt.astimezone(IST_TZ).strftime("%A, %d %b %Y · %I:%M %p")
        except Exception:
            st_formatted = st_dt.strftime("%A, %d %b %Y · %I:%M %p")

    # Fetch Gemini usage summary
    usage_row = db.execute(
        text("""
            SELECT COUNT(id) as total_calls,
                   COUNT(CASE WHEN operation_type = 'TRANSCRIPTION' THEN 1 END) as transcription_calls,
                   COUNT(CASE WHEN operation_type = 'SUMMARY' THEN 1 END) as summary_calls,
                   COALESCE(SUM(input_tokens), 0) as total_input_tokens,
                   COALESCE(SUM(input_audio_tokens), 0) as total_audio_tokens,
                   COALESCE(SUM(output_tokens), 0) as total_output_tokens,
                   COALESCE(SUM(total_tokens), 0) as total_tokens,
                   COALESCE(SUM(total_cost_inr), 0) as estimated_cost,
                   COALESCE(
                       MAX(CASE WHEN operation_type = 'TRANSCRIPTION' THEN gemini_model END),
                       MAX(gemini_model)
                   ) as primary_model
            FROM isf_recorder.gemini_usage_log
            WHERE recording_id = :id
        """),
        {"id": recording_id}
    ).fetchone()

    detail_primary_model = usage_row.primary_model if (usage_row and usage_row.primary_model) else None
    if getattr(rec, "stt_engine", None) == "assemblyai" and (not detail_primary_model or "gemini" in detail_primary_model):
        detail_primary_model = "assemblyai-universal-2"
    elif not detail_primary_model:
        detail_primary_model = "gemini-2.5-flash"

    calls_rows = db.execute(
        text("""
            SELECT id, operation_type, gemini_model, duration_ms,
                   input_tokens, output_tokens, input_audio_tokens, cached_input_tokens,
                   total_tokens, total_cost_inr, api_status, error_message, request_timestamp
            FROM isf_recorder.gemini_usage_log
            WHERE recording_id = :id
            ORDER BY request_timestamp ASC
        """),
        {"id": recording_id}
    ).fetchall()

    calls_list = []
    for c in calls_rows:
        calls_list.append({
            "id": str(c.id),
            "operation_type": c.operation_type,
            "model": c.gemini_model,
            "duration_ms": c.duration_ms or 0,
            "input_tokens": c.input_tokens or 0,
            "output_tokens": c.output_tokens or 0,
            "audio_tokens": c.input_audio_tokens or 0,
            "cached_tokens": c.cached_input_tokens or 0,
            "total_tokens": c.total_tokens or 0,
            "estimated_cost_formatted": f"₹{float(c.total_cost_inr or 0.0):.2f}",
            "api_status": c.api_status or "SUCCESS",
            "error_message": c.error_message,
            "request_timestamp": c.request_timestamp.isoformat() if c.request_timestamp else None,
        })

    gemini_usage_summary = {
        "total_calls": usage_row.total_calls if usage_row else 0,
        "transcription_calls": usage_row.transcription_calls if usage_row else 0,
        "summary_calls": usage_row.summary_calls if usage_row else 0,
        "total_input_tokens": int(usage_row.total_input_tokens or 0) if usage_row else 0,
        "total_audio_tokens": int(usage_row.total_audio_tokens or 0) if usage_row else 0,
        "total_output_tokens": int(usage_row.total_output_tokens or 0) if usage_row else 0,
        "total_tokens": int(usage_row.total_tokens or 0) if usage_row else 0,
        "estimated_cost": round(float(usage_row.estimated_cost or 0.0), 2) if usage_row else 0.0,
        "estimated_cost_formatted": f"₹{float(usage_row.estimated_cost or 0.0):.2f}" if usage_row else "₹0.00",
        "primary_model": detail_primary_model,
        "stt_engine": getattr(rec, "stt_engine", "gemini"),
        "calls": calls_list,
        "currency": "INR",
    }

    return {
        "id": str(rec.id),
        "session_id": rec.session_id,
        "title": rec.title if (rec.title and not rec.title.lower().startswith("untitled")) else (
            f"Recording {st_dt.strftime('%d %b, %I:%M %p')}" if st_dt else (rec.title or "Recording Session")
        ),
        "status": rec.status,
        "error_message": getattr(rec, "error_message", None),
        "user_id": str(rec.user_id) if rec.user_id else None,
        "user_name": rec.user_name,
        "duration_seconds": rec.duration_seconds,
        "mic_source": rec.mic_source,
        "is_starred": rec.is_starred,
        "has_audio": has_audio,
        "audio_deleted_at": rec.audio_deleted_at.isoformat() if rec.audio_deleted_at else None,
        "detected_language": rec.detected_language,
        "transcript": rec.transcript,
        "overview": _clean_overview(rec.overview),
        "started_at": rec.started_at.isoformat() if rec.started_at else None,
        "started_at_formatted": st_formatted,
        "gemini_usage": gemini_usage_summary,
        "key_points": _clean_list_field(rec.key_points),
        "decisions": _clean_list_field(rec.decisions),
        "segments": [
            {
                "id": str(s.id),
                "speaker": s.speaker,
                "speaker_id": s.speaker_id,
                "text": s.text,
                "language": s.language,
                "start_time": s.start_time,
                "end_time": s.end_time,
            }
            for s in segments
        ],
        "action_items": [
            {
                "id": str(a.id),
                "assigned_to": a.assigned_to,
                "description": a.description,
                "urgency": a.urgency,
                "due_date": a.due_date.isoformat() if a.due_date else None,
                "completed": a.completed,
            }
            for a in action_items
        ],
        "created_at": rec.created_at.isoformat() if rec.created_at else None,
        "finished_at": rec.finished_at.isoformat() if rec.finished_at else None,
        "active_run_id": str(rec.active_run_id) if getattr(rec, "active_run_id", None) else None,
        **_v2_fields(rec, st_dt),
        "silence_gaps": _gaps_json(getattr(rec, "silence_gaps", None)),
        "report_status": report_row.status if report_row else None,
        "report": ({"status": report_row.status, "template_title": report_row.template_title,
                    "updated_at": report_row.updated_at.isoformat() if report_row.updated_at else None}
                   if report_row else None),
    }


# ─── 7. Star / Unstar Recording ───────────────────────────────────────────────

class StarRecordingRequest(BaseModel):
    otp_code: Optional[str] = None

@router.post("/{recording_id}/star")
def star_recording(
    recording_id: str,
    body: Optional[StarRecordingRequest] = None,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Staff can star/unstar their own recordings. Admin can star any recording.
    Starred recordings will NEVER have their audio deleted by the 7-day retention cleanup.
    Unstarring permanent recordings requires a WhatsApp OTP to prevent accidental loss.
    """
    call_access(db, recording_id, current_user, write=True)   # calls are private to their owner
    is_admin = current_user.get("role") == "admin"
    user_phone = current_user.get("phone")
    otp_code = body.otp_code if body else None

    res = toggle_star_recording(
        db,
        recording_id,
        current_user["user_id"],
        is_admin=is_admin,
        user_phone=user_phone,
        otp_code=otp_code
    )
    return res


# ─── 8. Re-transcribe Recording ───────────────────────────────────────────────

class ReTranscribeRequest(BaseModel):
    model: Optional[str] = None  # admin only: run this model instead of the org setting


def _transcription_model_ids() -> set:
    from backend.settings.routes import AVAILABLE_MODELS
    return {m["id"] for m in AVAILABLE_MODELS if "transcription" in m.get("supports", [])}


@router.post("/{recording_id}/re-transcribe")
def re_transcribe(
    recording_id: str,
    body: Optional[ReTranscribeRequest] = None,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Re-runs STT and intelligence extraction on the recording.
    Staff can re-transcribe their own; admin can re-transcribe any, and can pick the model
    ("Try another model"). The previous transcript is kept as its own version.
    Audio file must still be present on server.
    """
    call_access(db, recording_id, current_user, write=True)   # calls are private to their owner
    rec = db.execute(
        text("SELECT id, user_id, org_id, status, audio_file_path, audio_deleted_at FROM isf_recorder.recording WHERE id = :id"),
        {"id": recording_id}
    ).fetchone()

    if not rec:
        raise HTTPException(status_code=404, detail="Recording not found")

    is_admin = current_user.get("role") == "admin"
    if str(rec.org_id) != current_user["org_id"] or (not is_admin and str(rec.user_id) != current_user["user_id"]):
        raise HTTPException(status_code=403, detail="Permission denied")

    model = (body.model or "").strip() if body else ""
    if model:
        if not is_admin:
            raise HTTPException(status_code=403, detail="Only admins can choose the model")
        if model not in _transcription_model_ids():
            raise HTTPException(status_code=400, detail=f"Unknown transcription model '{model}'")
        from backend.settings.routes import require_model_price
        require_model_price(db, model)

    if rec.audio_deleted_at or not rec.audio_file_path or not os.path.exists(rec.audio_file_path):
        raise HTTPException(
            status_code=410,
            detail="The audio file is not available on the server, so it cannot be transcribed again. The transcript is preserved."
        )

    # Single-flight: one atomic UPDATE ... RETURNING claims the recording; a second tap, a second
    # phone or a running pipeline gets 409 instead of a second paid transcription.
    if not queue_for_transcription(db, recording_id, allowed_statuses=("completed", "error", "uploading")):
        raise HTTPException(status_code=409, detail="This recording is already being transcribed. Please wait for it to finish.")

    schedule_transcription_job(recording_id, model_override=model or None, requested_by=current_user["user_id"])

    return {
        "status": "processing",
        "recording_id": recording_id,
        "model": model or None,
        "message": f"Re-transcription started with {model}" if model else "Re-transcription started"
    }


# ─── 8b. Transcript versions (one per model run) ──────────────────────────────

def _get_accessible_recording(db: Session, recording_id: str, current_user: dict):
    rec = db.execute(
        text("SELECT id, user_id, org_id FROM isf_recorder.recording WHERE id = :id AND is_deleted = FALSE"),
        {"id": recording_id}
    ).fetchone()
    if not rec:
        raise HTTPException(status_code=404, detail="Recording not found")
    if str(rec.org_id) != current_user["org_id"]:
        raise HTTPException(status_code=403, detail="Permission denied")
    if current_user.get("role") != "admin" and str(rec.user_id) != current_user["user_id"]:
        raise HTTPException(status_code=403, detail="Permission denied")
    return rec


@router.get("/{recording_id}/runs")
def list_transcription_runs(
    recording_id: str,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Every transcription of this recording: model, status, time taken, cost, size of the transcript."""
    call_access(db, recording_id, current_user, write=False)   # calls are private to their owner
    _get_accessible_recording(db, recording_id, current_user)
    from backend.transcription.runs import list_runs
    return list_runs(db, recording_id)


@router.get("/{recording_id}/runs/{run_id}")
def get_transcription_run(
    recording_id: str,
    run_id: str,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """One version in full: its transcript, segments, summary and action items."""
    call_access(db, recording_id, current_user, write=False)   # calls are private to their owner
    _get_accessible_recording(db, recording_id, current_user)
    from backend.transcription.runs import get_run
    run = get_run(db, recording_id, run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Version not found")
    gaps = db.execute(text("SELECT silence_gaps FROM isf_recorder.recording WHERE id = :id"), {"id": recording_id}).scalar()
    return {**run, "silence_gaps": _gaps_json(gaps)}


@router.post("/{recording_id}/runs/{run_id}/activate")
def activate_transcription_run(
    recording_id: str,
    run_id: str,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Admin: make this version the recording's main transcript (used by the app, chat and sharing)."""
    call_access(db, recording_id, current_user, write=True)   # calls are private to their owner
    if current_user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Only admins can change the main transcript")
    _get_accessible_recording(db, recording_id, current_user)
    status = db.execute(text("SELECT status FROM isf_recorder.recording WHERE id = :id"), {"id": recording_id}).scalar()
    if status == "processing":
        raise HTTPException(status_code=409, detail="Wait until the current transcription finishes")
    from backend.transcription.runs import activate_run
    if not activate_run(db, recording_id, run_id):
        raise HTTPException(status_code=404, detail="Version not found or not completed")
    return {"status": "ok", "recording_id": recording_id, "active_run_id": run_id}


# ─── 8c. Import an audio file (admin model testing) ───────────────────────────

IMPORT_EXTENSIONS = {".m4a", ".mp3", ".wav", ".ogg", ".opus", ".aac", ".mp4", ".amr", ".flac", ".webm", ".3gp", ".mpeg", ".mpga"}
MAX_IMPORT_BYTES = 1024 * 1024 * 1024  # 1 GB


def _finish_import(recording_id: str, raw_path: str, final_path: str, model: str, requested_by: str):
    """Background: convert the uploaded file to the standard m4a, then transcribe it with the chosen model."""
    import subprocess
    from backend.recordings.chunk_assembler import find_binary
    from backend.transcription.gemini_stt import get_audio_duration_seconds

    db = SessionLocal()
    try:
        res = subprocess.run(
            [find_binary("ffmpeg"), "-y", "-i", raw_path, "-vn", "-c:a", "aac", "-b:a", "64k",
             "-ar", "16000", "-ac", "1", "-movflags", "+faststart", final_path],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=3600
        )
        if res.returncode != 0 or not os.path.exists(final_path):
            logger.error(f"Import {recording_id}: ffmpeg failed: {res.stderr.decode('utf-8', errors='ignore')[-500:]}")
            db.execute(text("""
                UPDATE isf_recorder.recording SET status = 'error', pipeline_stage = 'failed',
                       error_message = 'Could not read this audio file. Try m4a, mp3, wav or ogg.'
                WHERE id = :id
            """), {"id": recording_id})
            db.commit()
            return
        duration = get_audio_duration_seconds(final_path)
        db.execute(text("""
            UPDATE isf_recorder.recording SET duration_seconds = :dur, file_size_bytes = :size WHERE id = :id
        """), {"id": recording_id, "dur": int(duration), "size": os.path.getsize(final_path)})
        db.commit()
    except Exception as e:
        logger.exception(f"Import {recording_id} failed: {e}")
        db.execute(text("UPDATE isf_recorder.recording SET status = 'error', pipeline_stage = 'failed', error_message = :err WHERE id = :id"),
                   {"id": recording_id, "err": f"Import failed: {e}"[:500]})
        db.commit()
        return
    finally:
        db.close()
        try:
            if os.path.exists(raw_path) and raw_path != final_path:
                os.remove(raw_path)
        except Exception:
            pass

    # Through the normal queue: single-flight, concurrency limit
    schedule_transcription_job(recording_id, model_override=model, requested_by=requested_by)


@router.post("/import")
def import_audio(
    file: UploadFile = File(...),
    model: str = Form(...),
    title: Optional[str] = Form(None),
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Admin: upload any audio file and transcribe it with the chosen model. It becomes a normal
    recording (mic_source 'import') with transcript, summary, chat and cost tracking.
    """
    if current_user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Only admins can upload audio")
    model = (model or "").strip()
    if model not in _transcription_model_ids():
        raise HTTPException(status_code=400, detail=f"Unknown transcription model '{model}'")
    from backend.settings.routes import require_model_price
    require_model_price(db, model)

    original_name = os.path.basename(file.filename or "audio")
    ext = Path(original_name).suffix.lower()
    if ext not in IMPORT_EXTENSIONS:
        raise HTTPException(status_code=400, detail=f"Unsupported file type '{ext or '?'}'. Use m4a, mp3, wav, ogg, aac or mp4.")

    org_id = current_user["org_id"]
    user_id = current_user["user_id"]
    session_id = f"import_{uuid.uuid4().hex[:12]}"
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

    final_title = (title or "").strip() or Path(original_name).stem[:100]
    rec_id = db.execute(text("""
        INSERT INTO isf_recorder.recording
        (user_id, org_id, session_id, title, status, mic_source, sample_rate, duration_seconds,
         audio_file_path, file_size_bytes, started_at, finished_at, pipeline_stage, finish_requested_at,
         finish_source, recorded_by_name)
        VALUES (:user_id, :org_id, :sid, :title, 'processing', 'import', 16000, 0, :fpath, :size, NOW(), NOW(),
                'queued', NOW(), 'import', :name)
        RETURNING id
    """), {"user_id": user_id, "org_id": org_id, "sid": session_id, "title": final_title,
           "fpath": final_path, "size": size, "name": current_user.get("name")}).scalar()
    db.commit()
    rec_id = str(rec_id)

    threading.Thread(
        target=_finish_import, args=(rec_id, raw_path, final_path, model, user_id),
        daemon=True, name=f"import-{rec_id[:8]}"
    ).start()
    logger.info(f"Imported audio '{original_name}' ({size / 1048576:.1f} MB) as {rec_id}, transcribing with {model}")

    return {
        "status": "processing",
        "recording_id": rec_id,
        "title": final_title,
        "model": model,
        "size_bytes": size,
    }


# ─── 9. Stream / Download Audio ───────────────────────────────────────────────

@router.get("/{recording_id}/audio")
def stream_audio(
    recording_id: str,
    request: Request,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Stream or download the recorded audio file.
    Staff can only stream their own; admin can stream any.
    Returns 410 Gone if deleted by 7-day retention policy.
    """
    call_access(db, recording_id, current_user, write=False)   # calls are private to their owner
    rec = db.execute(
        text("SELECT id, user_id, audio_file_path, audio_deleted_at, session_id, status, pipeline_stage "
             "FROM isf_recorder.recording WHERE id = :id"),
        {"id": recording_id}
    ).fetchone()

    if not rec:
        raise HTTPException(status_code=404, detail="Recording not found")

    is_admin = current_user.get("role") == "admin"
    if not is_admin and str(rec.user_id) != current_user["user_id"]:
        raise HTTPException(status_code=403, detail="Permission denied")

    if (not rec.audio_deleted_at and not rec.audio_file_path
            and (rec.status in ("recording", "uploading") or rec.pipeline_stage in ("awaiting_chunks", "stitching"))):
        # Just stopped: the m4a is being stitched (a few seconds). 404 = "not ready yet", the app retries.
        raise HTTPException(status_code=404, detail="Audio is still being saved. Try again in a few seconds.")
    if rec.audio_deleted_at or not rec.audio_file_path or not os.path.exists(rec.audio_file_path):
        detail = "Audio file is not available for this recording."
        if rec.audio_deleted_at:
            detail += " Older recordings lost their audio to the old 7-day cleanup."
        raise HTTPException(status_code=410, detail=detail + " The transcript and report remain saved.")

    ext = Path(rec.audio_file_path).suffix.lower() if rec.audio_file_path else ".m4a"
    media_type = "audio/mp4" if ext in (".m4a", ".mp4") else "audio/wav"
    filename = f"{rec.session_id}{ext}"

    range_header = request.headers.get("range")
    if range_header:
        return _ranged_file_response(rec.audio_file_path, range_header, media_type, filename)
    resp = FileResponse(
        rec.audio_file_path,
        media_type=media_type,
        filename=filename
    )
    resp.headers["Accept-Ranges"] = "bytes"
    return resp


def _parse_range(range_header: str, size: int):
    """Single 'bytes=a-b' / 'bytes=a-' / 'bytes=-n' range -> (start, end) inclusive, or None if unsatisfiable."""
    m = re.fullmatch(r"\s*bytes\s*=\s*(\d*)\s*-\s*(\d*)\s*", range_header or "")
    if not m or (m.group(1) == "" and m.group(2) == ""):
        return None
    if m.group(1) == "":  # suffix range: last n bytes
        n = int(m.group(2))
        if n == 0:
            return None
        return max(0, size - n), size - 1
    start = int(m.group(1))
    end = int(m.group(2)) if m.group(2) else size - 1
    if start >= size or end < start:
        return None
    return start, min(end, size - 1)


def _ranged_file_response(path: str, range_header: str, media_type: str, filename: str):
    """206 Partial Content (Starlette 0.38's FileResponse ignores Range; ExoPlayer seeks with it)."""
    size = os.path.getsize(path)
    if "," in range_header:  # multi-range: just send the whole file
        resp = FileResponse(path, media_type=media_type, filename=filename)
        resp.headers["Accept-Ranges"] = "bytes"
        return resp
    rng = _parse_range(range_header, size)
    if rng is None:
        return Response(status_code=416, headers={"Content-Range": f"bytes */{size}", "Accept-Ranges": "bytes"})
    start, end = rng
    length = end - start + 1

    def _iter():
        with open(path, "rb") as f:
            f.seek(start)
            left = length
            while left > 0:
                data = f.read(min(256 * 1024, left))
                if not data:
                    break
                left -= len(data)
                yield data

    return StreamingResponse(_iter(), status_code=206, media_type=media_type, headers={
        "Content-Range": f"bytes {start}-{end}/{size}",
        "Content-Length": str(length),
        "Accept-Ranges": "bytes",
        "Content-Disposition": f'attachment; filename="{filename}"',
    })


# ─── 10. Soft Delete Recording ────────────────────────────────────────────────

@router.delete("/{recording_id}")
def delete_recording(
    recording_id: str,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Erase for real (owner or an admin of the org): audio, chunks, transcript, runs, report and chats.
    Cost rows stay (recording_id -> NULL). A tombstone stops late chunks / finish from re-creating it.
    """
    call_access(db, recording_id, current_user, write=True)   # calls are private to their owner
    try:
        rec_uuid = str(uuid.UUID(recording_id))
    except ValueError:
        raise HTTPException(status_code=404, detail="Recording not found")
    rec = db.execute(text("""
        SELECT id, user_id, org_id, session_id, title, status, pipeline_stage, audio_file_path, last_heartbeat_at,
               (last_heartbeat_at IS NOT NULL AND last_heartbeat_at > NOW() - INTERVAL '3 minutes') AS live
        FROM isf_recorder.recording WHERE id = :id FOR UPDATE
    """), {"id": rec_uuid}).fetchone()
    if not rec:
        db.rollback()
        raise HTTPException(status_code=404, detail="Recording not found")
    try:
        _check_session_access(rec, current_user)
    except HTTPException:
        db.rollback()
        raise
    if (rec.status == "processing" or (rec.status == "recording" and rec.live)
            or rec.pipeline_stage == "stitching" or is_inflight(rec_uuid)):
        db.rollback()
        raise HTTPException(status_code=409, detail="This recording is still being recorded or processed. Try again when it has finished.")

    chunk_paths = [r.file_path for r in db.execute(text(
        "SELECT file_path FROM isf_recorder.audio_chunk WHERE recording_id = :id"), {"id": rec_uuid}).fetchall()]
    db.execute(text("""
        INSERT INTO isf_recorder.deleted_session (session_id, recording_id, org_id, deleted_by)
        VALUES (:sid, :rid, :org, :by)
        ON CONFLICT (session_id) DO UPDATE SET recording_id = EXCLUDED.recording_id, deleted_by = EXCLUDED.deleted_by,
                                              deleted_at = NOW()
    """), {"sid": rec.session_id, "rid": rec_uuid, "org": str(rec.org_id), "by": current_user["user_id"]})
    # Keep the cost history: detach usage rows explicitly (org_id stays for the monthly totals)
    db.execute(text("""
        UPDATE isf_recorder.gemini_usage_log
        SET recording_id = NULL, run_id = NULL, org_id = COALESCE(org_id, :org)
        WHERE recording_id = :id
           OR run_id IN (SELECT id FROM isf_recorder.transcription_run WHERE recording_id = :id)
    """), {"id": rec_uuid, "org": str(rec.org_id)})
    db.execute(text("UPDATE isf_recorder.recording SET active_run_id = NULL WHERE id = :id"), {"id": rec_uuid})
    db.execute(text("UPDATE isf_recorder.recording_report SET run_id = NULL WHERE recording_id = :id"), {"id": rec_uuid})
    db.execute(text("DELETE FROM isf_recorder.recording WHERE id = :id"), {"id": rec_uuid})
    db.execute(text("""
        INSERT INTO isf_recorder.audit_log (user_id, org_id, action, target_type, target_id, metadata)
        VALUES (:u, :o, 'recording_erased', 'recording', :rid, CAST(:m AS jsonb))
    """), {"u": current_user["user_id"], "o": str(rec.org_id), "rid": rec_uuid,
           "m": json.dumps({"session_id": rec.session_id, "title": rec.title})})
    db.commit()

    targets = lifecycle.recording_file_targets(rec.audio_file_path, chunk_paths, rec.session_id)
    removed, pending = lifecycle.delete_paths(targets)
    if pending:
        db.execute(text("""
            UPDATE isf_recorder.deleted_session SET files_pending = CAST(:p AS jsonb) WHERE session_id = :sid
        """), {"sid": rec.session_id, "p": json.dumps(pending)})
        db.commit()
    logger.info(f"Recording {rec_uuid} ({rec.session_id}) erased by {current_user['user_id']}: "
                f"{removed} files removed, {len(pending)} pending")
    return {"status": "erased", "recording_id": rec_uuid, "files_removed": removed}


# ─── 11. Merge Multiple Sessions ──────────────────────────────────────────────

@router.post("/merge")
def merge_recordings(
    body: MergeRecordingsRequest,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Merge multiple recordings into a single session:
    1. Validates that all selected recordings exist and have audio.
    2. Stitches their audio files sequentially into one unified WAV.
    3. Creates a new merged recording session.
    4. Triggers Gemini transcription and diarization on the full merged audio.
    """
    if len(body.recording_ids) < 2:
        raise HTTPException(status_code=400, detail="At least 2 recordings are required to merge.")

    parsed_ids = []
    for rid in body.recording_ids:
        try:
            parsed_ids.append(str(uuid.UUID(rid)))
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid UUID: {rid}")

    is_admin = current_user.get("role") == "admin"
    user_id = current_user["user_id"]
    org_id = current_user["org_id"]

    recs = db.execute(
        text("""
            SELECT id, session_id, title, audio_file_path, audio_deleted_at, duration_seconds, started_at, user_id, source
            FROM isf_recorder.recording
            WHERE id = ANY(CAST(:ids AS uuid[])) AND org_id = :org_id AND is_deleted = FALSE
            ORDER BY started_at ASC
        """),
        {"ids": parsed_ids, "org_id": org_id}
    ).fetchall()

    if len(recs) < len(parsed_ids):
        raise HTTPException(status_code=404, detail="One or more selected recordings were not found.")
    if any(r.source == "call" for r in recs):
        raise HTTPException(status_code=400, detail="Calls cannot be merged.")

    if not is_admin:
        for r in recs:
            if str(r.user_id) != user_id:
                raise HTTPException(status_code=403, detail="Permission denied: you can only merge your own recordings.")

    # Check audio files
    audio_paths = []
    missing_audio = []
    for r in recs:
        if r.audio_deleted_at or not r.audio_file_path or not os.path.exists(r.audio_file_path):
            missing_audio.append(r.title or str(r.id))
        else:
            audio_paths.append(r.audio_file_path)

    if missing_audio:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot merge: audio files missing or expired for: {', '.join(missing_audio)}"
        )

    # Stitch audio files into one unified WAV
    merged_session_id = f"merged_{uuid.uuid4().hex[:12]}"
    org_slug = get_org_slug(db, org_id)
    date_str = datetime.utcnow().strftime("%Y-%m-%d")
    final_dir = Path(AUDIO_STORAGE_ROOT) / org_slug / date_str
    final_dir.mkdir(parents=True, exist_ok=True)
    final_file_path = str(final_dir / f"{merged_session_id}.m4a")

    success, calc_duration, file_size = concat_audio_files_to_m4a(audio_paths, final_file_path)
    if not success:
        raise HTTPException(status_code=500, detail="Failed to stitch audio files together.")

    # Determine merged title
    if body.title and body.title.strip():
        final_title = body.title.strip()
    else:
        titles = [r.title for r in recs if r.title and not r.title.startswith("Untitled")]
        if titles:
            combined = " + ".join(titles)
            final_title = f"Merged: {combined}"[:100]
        else:
            final_title = f"Merged Session ({len(recs)} parts)"

    # Insert merged recording record
    result = db.execute(
        text("""
            INSERT INTO isf_recorder.recording
            (user_id, org_id, session_id, title, status, mic_source, sample_rate, duration_seconds, audio_file_path, file_size_bytes, started_at, finished_at,
             pipeline_stage, finish_requested_at, finish_source, recorded_by_name)
            VALUES (:user_id, :org_id, :sid, :title, 'uploading', 'merged', 16000, :dur, :fpath, :size, :started_at, NOW(),
                    'queued', NOW(), 'merge', :name)
            RETURNING id, session_id, status
        """),
        {
            "user_id": user_id,
            "org_id": org_id,
            "sid": merged_session_id,
            "title": final_title,
            "dur": int(calc_duration),
            "fpath": final_file_path,
            "size": file_size,
            "started_at": recs[0].started_at or datetime.utcnow(),
            "name": current_user.get("name"),
        }
    ).fetchone()
    db.commit()

    merged_rec_id = str(result.id)

    # Dispatch Gemini transcription & diarization pipeline on full merged audio
    schedule_transcription_job(merged_rec_id)
    logger.info(f"Created merged recording {merged_rec_id} ({len(recs)} sessions, {calc_duration:.1f}s)")

    return {
        "status": "processing",
        "recording_id": merged_rec_id,
        "session_id": merged_session_id,
        "title": final_title,
        "duration_seconds": int(calc_duration),
        "merged_count": len(recs)
    }


# ─── 12. Update / Edit Recording Title ────────────────────────────────────────

@router.patch("/{recording_id}/title")
@router.put("/{recording_id}/title")
@router.patch("/{recording_id}")
def update_recording_title(
    recording_id: str,
    body: UpdateTitleRequest,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """
    Update or rename a recording's title.
    - Staff can edit titles of their own recordings.
    - Admins can edit any recording title in their organization.
    Accepts either UUID id or session_id.
    """
    call_access(db, recording_id, current_user, write=True)   # calls are private to their owner
    new_title = body.title.strip()
    if not new_title:
        raise HTTPException(status_code=400, detail="Title cannot be empty")

    rec = db.execute(
        text("SELECT id, user_id FROM isf_recorder.recording WHERE id::text = :id OR session_id = :id"),
        {"id": recording_id}
    ).fetchone()

    if not rec:
        raise HTTPException(status_code=404, detail="Recording not found")

    is_admin = current_user.get("role") == "admin"
    if not is_admin and str(rec.user_id) != current_user["user_id"]:
        raise HTTPException(status_code=403, detail="Permission denied: You can only edit your own recordings")

    db.execute(
        text("UPDATE isf_recorder.recording SET title = :title WHERE id = :id"),
        {"title": new_title, "id": str(rec.id)}
    )
    db.commit()

    logger.info(f"User {current_user['user_id']} updated title of recording {rec.id} to '{new_title}'")
    return {
        "success": True,
        "recording_id": str(rec.id),
        "title": new_title,
        "message": "Title updated successfully"
    }
