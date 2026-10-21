"""
ISF Recorder — Recording lifecycle (finalize, late chunks, sweeper, raw-chunk cleanup)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
/finish only records the request (fast, idempotent). The work happens here:

- maybe_finalize(): claims pipeline_stage awaiting_chunks -> stitching atomically once every expected
  chunk has arrived (or when forced by the sweeper), stitches the m4a, then queues transcription.
- Late chunks (after finalize): debounced 60 s re-stitch; re-transcribe only when nothing is running
  and auto_retranscribe_count < 2. Never re-stitches a recording whose chunk WAVs were cleaned.
- run_sweeper() (every 60 s): finalizes sessions whose chunks never all arrived, finalizes abandoned
  sessions (no heartbeat/chunk for 30 min, or 3 h for apps that never send heartbeats), recovers
  stuck stages, retries file deletes that Windows refused.
- cleanup_chunk_wavs() (hourly, batch-limited): for recordings completed >= 24 h ago, verifies the m4a's
  content against the raw pieces (audio_verify), rebuilds it from the pieces if it fails, and deletes
  the pieces only when the m4a passed; sets chunks_cleaned_at. The m4a itself is kept forever.

The sweeper assumes one API process (one uvicorn worker), like production today; a Postgres
advisory lock stops two processes from sweeping at the same time.
"""
import json
import logging
import os
import shutil
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Set

from sqlalchemy import text

from backend.config import AUDIO_STORAGE_ROOT
from backend.database import SessionLocal
from backend.recordings.chunk_assembler import assemble_recording
from backend.transcription.engine import schedule_transcription_job, is_inflight, inflight_ids

logger = logging.getLogger("isf_recorder.lifecycle")

PROCESS_STARTED_AT = time.time()
STUCK_GRACE_SECONDS = 600          # leave restart recovery to the startup resume for the first 10 min
AWAIT_CHUNKS_TIMEOUT = "10 minutes"
AWAIT_CHUNKS_QUIET = "3 minutes"
IDLE_WITH_HEARTBEAT = "30 minutes"
IDLE_WITHOUT_HEARTBEAT = "3 hours"
LATE_STITCH_DELAY = 60.0
LATE_STITCH_RECHECK = 120.0
LATE_STITCH_GIVE_UP = 3 * 3600
MAX_AUTO_RETRANSCRIBE = 2
SWEEPER_LOCK_KEY = 0x15F5EE9  # pg advisory lock id
ABANDONED_MESSAGE = "Recording abandoned (no audio received)"

_finalizing: Set[str] = set()
_finalizing_lock = threading.Lock()
_late_timers: Dict[str, threading.Timer] = {}
_late_lock = threading.Lock()


# ─── Tombstones ──────────────────────────────────────────────────────────────

def is_tombstoned(db, session_id: str) -> bool:
    return db.execute(text("SELECT 1 FROM isf_recorder.deleted_session WHERE session_id = :sid"),
                      {"sid": session_id}).fetchone() is not None


# ─── Finalize ────────────────────────────────────────────────────────────────

def start_finalize_thread(recording_id: str, force: bool = False) -> None:
    threading.Thread(target=maybe_finalize, args=(str(recording_id), force), daemon=True,
                     name=f"finalize-{str(recording_id)[:8]}").start()


def _chunk_paths(db, recording_id: str) -> List[str]:
    rows = db.execute(text("""
        SELECT file_path FROM isf_recorder.audio_chunk WHERE recording_id = :id ORDER BY chunk_index ASC
    """), {"id": recording_id}).fetchall()
    return [r.file_path for r in rows if r.file_path and os.path.exists(r.file_path)]


def _org_slug(db, org_id) -> str:
    row = db.execute(text("SELECT slug FROM isf_recorder.organization WHERE id = :id"), {"id": org_id}).fetchone()
    return row.slug if row else "default"


def maybe_finalize(recording_id: str, force: bool = False) -> bool:
    """Stitch + queue transcription once all expected chunks are in (or force). Safe to call often."""
    with SessionLocal() as db:
        row = db.execute(text("""
            UPDATE isf_recorder.recording r
            SET pipeline_stage = 'stitching'
            WHERE r.id = :id AND r.pipeline_stage = 'awaiting_chunks'
              AND (:force OR (SELECT COUNT(*) FROM isf_recorder.audio_chunk c WHERE c.recording_id = r.id)
                              >= COALESCE(r.expected_chunks, 0))
            RETURNING r.session_id, r.org_id, r.expected_chunks, r.audio_file_path, r.duration_seconds,
                      r.total_chunks, r.finish_source
        """), {"id": recording_id, "force": bool(force)}).fetchone()
        db.commit()
        if not row:
            return False

        with _finalizing_lock:
            _finalizing.add(recording_id)
        try:
            paths = _chunk_paths(db, recording_id)
            if not paths and row.finish_source in ("client", "sync_worker") and int(row.expected_chunks or 0) == 0:
                # The app saved no audio at all (on-device VAD heard no voice): nothing to transcribe
                _complete_without_audio(db, recording_id)
                logger.info(f"Finalize {row.session_id}: no voice recorded; completed without AI calls")
                return False
            if not paths:
                msg = ABANDONED_MESSAGE if row.finish_source == "sweeper" else "No audio was received for this recording"
                db.execute(text("""
                    UPDATE isf_recorder.recording
                    SET status = 'error', pipeline_stage = 'failed', error_message = :msg
                    WHERE id = :id AND pipeline_stage = 'stitching'
                """), {"id": recording_id, "msg": msg})
                db.commit()
                logger.warning(f"Finalize {row.session_id}: no chunk files on disk")
                return False

            expected = int(row.expected_chunks or 0)
            if expected and len(paths) < expected:
                logger.warning(f"Finalizing {row.session_id} with {len(paths)}/{expected} chunks "
                               f"(late chunks will be re-stitched)")
            final_path = row.audio_file_path
            if not final_path:
                final_dir = Path(AUDIO_STORAGE_ROOT) / _org_slug(db, row.org_id) / datetime.utcnow().strftime("%Y-%m-%d")
                final_dir.mkdir(parents=True, exist_ok=True)
                final_path = str(final_dir / f"{row.session_id}.m4a")

            success, calc_duration, file_size = assemble_recording(paths, final_path)
            if not success or not os.path.exists(final_path) or os.path.getsize(final_path) == 0:
                raise RuntimeError("Audio assembly failed")

            duration = max(int(calc_duration) if calc_duration > 0 else len(paths) * 30, int(row.duration_seconds or 0))
            total = max(len(paths), expected, int(row.total_chunks or 0))
            db.execute(text("""
                UPDATE isf_recorder.recording
                SET status = 'uploading', pipeline_stage = 'queued', total_chunks = :total,
                    duration_seconds = :dur, audio_file_path = :path, file_size_bytes = :size,
                    finished_at = COALESCE(finished_at, NOW()), error_message = NULL
                WHERE id = :id AND pipeline_stage = 'stitching'
            """), {"id": recording_id, "total": total, "dur": duration, "path": final_path, "size": file_size})
            db.commit()
            logger.info(f"Finalized {row.session_id}: {len(paths)} chunks, {duration}s; queuing transcription")
            schedule_transcription_job(recording_id)
            return True
        except Exception as e:
            logger.exception(f"Finalize failed for {recording_id}: {e}")
            db.rollback()
            db.execute(text("""
                UPDATE isf_recorder.recording
                SET status = 'error', pipeline_stage = 'failed', error_message = :msg
                WHERE id = :id AND pipeline_stage = 'stitching'
            """), {"id": recording_id, "msg": f"Could not assemble the audio: {e}"[:500]})
            db.commit()
            return False
        finally:
            with _finalizing_lock:
                _finalizing.discard(recording_id)


def _complete_without_audio(db, recording_id: str) -> None:
    from backend.transcription.engine import NO_SPEECH_TITLE, _is_placeholder_title
    title = db.execute(text("SELECT title FROM isf_recorder.recording WHERE id = :id"), {"id": recording_id}).scalar()
    db.execute(text("""
        UPDATE isf_recorder.recording
        SET status = 'completed', pipeline_stage = 'done', error_message = NULL, title = :title,
            transcript = '', overview = 'No speech detected in this recording.', duration_seconds = 0,
            total_chunks = 0, finished_at = COALESCE(finished_at, NOW())
        WHERE id = :id AND pipeline_stage = 'stitching'
    """), {"id": recording_id, "title": NO_SPEECH_TITLE if _is_placeholder_title(title) else title})
    db.commit()


# ─── Late chunks ─────────────────────────────────────────────────────────────

def schedule_late_restitch(recording_id: str, delay: float = LATE_STITCH_DELAY, deadline: Optional[float] = None) -> None:
    """Debounced: every late chunk pushes the re-stitch back by `delay`."""
    rec_id = str(recording_id)
    deadline = deadline or (time.time() + LATE_STITCH_GIVE_UP)
    with _late_lock:
        old = _late_timers.pop(rec_id, None)
        if old:
            old.cancel()
        t = threading.Timer(delay, _late_restitch, args=(rec_id, deadline))
        t.daemon = True
        _late_timers[rec_id] = t
        t.start()


def _late_restitch(rec_id: str, deadline: float) -> None:
    with _late_lock:
        _late_timers.pop(rec_id, None)
    try:
        with SessionLocal() as db:
            rec = db.execute(text("""
                SELECT session_id, status, pipeline_stage, audio_file_path, chunks_cleaned_at,
                       duration_seconds, total_chunks
                FROM isf_recorder.recording WHERE id = :id
            """), {"id": rec_id}).fetchone()
            if not rec or rec.chunks_cleaned_at or not rec.audio_file_path:
                return
            if rec.pipeline_stage in ("awaiting_chunks", "stitching"):
                return  # the normal finalize will include the chunk
            busy = (rec.status == "processing" or is_inflight(rec_id) or rec_id in _finalizing
                    or rec.pipeline_stage in ("queued", "transcribing", "reporting"))
            if busy:
                if time.time() < deadline:
                    schedule_late_restitch(rec_id, LATE_STITCH_RECHECK, deadline)
                else:
                    logger.warning(f"Giving up late re-stitch of {rec.session_id}: still busy")
                return

            paths = _chunk_paths(db, rec_id)
            if not paths:
                return
            prev_duration = int(rec.duration_seconds or 0)
            success, calc, size = assemble_recording(paths, rec.audio_file_path)
            if not success:
                logger.warning(f"Late re-stitch of {rec.session_id} failed")
                return
            new_duration = max(int(calc), prev_duration)
            db.execute(text("""
                UPDATE isf_recorder.recording
                SET duration_seconds = :dur, file_size_bytes = :size, total_chunks = GREATEST(total_chunks, :tot)
                WHERE id = :id
            """), {"id": rec_id, "dur": new_duration, "size": size, "tot": len(paths)})
            db.commit()
            logger.info(f"Late re-stitch of {rec.session_id}: {calc:.1f}s from {len(paths)} chunks (was {prev_duration}s)")

            if int(calc) <= prev_duration:
                return
            claimed = db.execute(text("""
                UPDATE isf_recorder.recording
                SET status = 'processing', pipeline_stage = 'queued',
                    auto_retranscribe_count = auto_retranscribe_count + 1
                WHERE id = :id AND status IN ('completed', 'error')
                  AND COALESCE(pipeline_stage, '') NOT IN ('awaiting_chunks', 'stitching', 'queued', 'transcribing', 'reporting')
                  AND auto_retranscribe_count < :max
                RETURNING id
            """), {"id": rec_id, "max": MAX_AUTO_RETRANSCRIBE}).fetchone()
            db.commit()
            if claimed:
                logger.info(f"Late audio added to {rec.session_id}; re-transcribing")
                schedule_transcription_job(rec_id)
            else:
                logger.info(f"Late audio added to {rec.session_id}; not re-transcribing (limit reached or busy)")
    except Exception as e:
        logger.exception(f"Late re-stitch failed for {rec_id}: {e}")


# ─── Files of erased recordings ──────────────────────────────────────────────

def recording_file_targets(audio_file_path: Optional[str], chunk_paths: List[str], session_id: str) -> List[str]:
    """The m4a (+ leftover temp files) and the session chunk folder(s) to delete on erase."""
    targets: List[str] = []
    if audio_file_path:
        targets.append(audio_file_path)
        base = Path(audio_file_path)
        targets += [str(base.with_suffix(".tmp.wav")), str(base.with_suffix(".new.m4a"))]
    dirs = set()
    for p in chunk_paths:
        parent = Path(p).parent
        if parent.name == "chunks" and parent.parent.name == session_id:
            dirs.add(str(parent.parent))
        else:
            targets.append(p)
    root = Path(AUDIO_STORAGE_ROOT).resolve()
    out = []
    for t in targets + sorted(dirs):
        try:
            Path(t).resolve().relative_to(root)  # never delete outside the audio root
        except ValueError:
            logger.warning(f"Refusing to delete path outside AUDIO_STORAGE_ROOT: {t}")
            continue
        out.append(t)
    return out


def delete_paths(paths: List[str]):
    """Best effort. Returns (files_removed, pending_paths_that_could_not_be_removed)."""
    removed, pending = 0, []
    for p in paths:
        try:
            if os.path.isdir(p):
                n = sum(len(files) for _, _, files in os.walk(p))
                shutil.rmtree(p)
                removed += n
            elif os.path.exists(p):
                os.remove(p)
                removed += 1
        except (PermissionError, OSError) as e:
            logger.warning(f"Could not delete {p} now ({e}); will retry")
            pending.append(p)
    return removed, pending


# ─── Sweeper ─────────────────────────────────────────────────────────────────

def run_sweeper(only_ids: Optional[List[str]] = None) -> Dict[str, int]:
    """Every 60 s. No model calls happen here except by queuing normal finalize/transcription.
    only_ids limits the recording steps to these ids (tests)."""
    summary = {"awaiting_finalized": 0, "idle_finalized": 0, "abandoned": 0, "stuck_fixed": 0, "files_retried": 0}
    db = SessionLocal()
    try:
        if not db.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": SWEEPER_LOCK_KEY}).scalar():
            return summary
        try:
            for step in (_sweep_awaiting, _sweep_idle, _sweep_stuck, _sweep_files):
                try:
                    step(db, summary, only_ids)
                except Exception as e:
                    db.rollback()
                    logger.exception(f"Sweeper step {step.__name__} failed: {e}")
        finally:
            db.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": SWEEPER_LOCK_KEY})
            db.commit()
    finally:
        db.close()
    if any(summary.values()):
        logger.info(f"Sweeper: {summary}")
    return summary


def _id_filter(only_ids, column: str = "id") -> str:
    return f"AND {column} = ANY(CAST(:only_ids AS uuid[]))" if only_ids is not None else ""


def _id_params(only_ids) -> dict:
    return {"only_ids": [str(i) for i in only_ids]} if only_ids is not None else {}


def _sweep_awaiting(db, summary, only_ids=None) -> None:
    rows = db.execute(text(f"""
        SELECT id FROM isf_recorder.recording
        WHERE pipeline_stage = 'awaiting_chunks'
          AND finish_requested_at < NOW() - INTERVAL '{AWAIT_CHUNKS_TIMEOUT}'
          AND COALESCE(last_chunk_at, finish_requested_at) < NOW() - INTERVAL '{AWAIT_CHUNKS_QUIET}'
          {_id_filter(only_ids)}
    """), _id_params(only_ids)).fetchall()
    db.commit()
    for r in rows:
        if maybe_finalize(str(r.id), force=True):
            summary["awaiting_finalized"] += 1


def _sweep_idle(db, summary, only_ids=None) -> None:
    rows = db.execute(text(f"""
        SELECT id FROM isf_recorder.recording
        WHERE status = 'recording' AND finish_requested_at IS NULL
          -- Only sessions started after migration 005 was applied: older orphaned sessions are never
          -- transcribed automatically (that would be a surprise bill); an admin decides about those.
          AND started_at > (SELECT CAST(value AS timestamptz) FROM isf_recorder.app_config
                            WHERE key = 'idle_finalize_since')
          AND (
                (last_heartbeat_at IS NOT NULL
                 AND last_heartbeat_at < NOW() - INTERVAL '{IDLE_WITH_HEARTBEAT}'
                 AND COALESCE(last_chunk_at, started_at) < NOW() - INTERVAL '{IDLE_WITH_HEARTBEAT}')
             OR (last_heartbeat_at IS NULL
                 AND COALESCE(last_chunk_at, started_at) < NOW() - INTERVAL '{IDLE_WITHOUT_HEARTBEAT}')
          )
          {_id_filter(only_ids)}
    """), _id_params(only_ids)).fetchall()
    db.commit()
    for r in rows:
        outcome = finish_unfinished_session(db, str(r.id), "sweeper")
        if outcome == "abandoned":
            summary["abandoned"] += 1
        elif outcome == "finalized":
            summary["idle_finalized"] += 1


def finish_unfinished_session(db, recording_id: str, source: str, background: bool = False) -> Optional[str]:
    """
    Shared by the sweeper's idle finalize and the admin "force finish": claims a session that is still
    'recording' and was never finished (atomic), then finalizes it with whatever chunks arrived, or
    marks it abandoned when no audio ever arrived. Returns 'finalized' | 'finalize_failed' |
    'abandoned' | 'queued' (background finalize started) | None (someone else finished it first).
    """
    claimed = db.execute(text("""
        UPDATE isf_recorder.recording r
        SET finish_requested_at = NOW(), finish_source = :source, status = 'uploading',
            pipeline_stage = 'awaiting_chunks',
            expected_chunks = (SELECT COUNT(*) FROM isf_recorder.audio_chunk c WHERE c.recording_id = r.id)
        WHERE r.id = :id AND r.status = 'recording' AND r.finish_requested_at IS NULL
        RETURNING r.expected_chunks, r.session_id
    """), {"id": recording_id, "source": source}).fetchone()
    db.commit()
    if not claimed:
        return None
    if not claimed.expected_chunks:
        db.execute(text("""
            UPDATE isf_recorder.recording
            SET status = 'error', pipeline_stage = 'failed', error_message = :msg, finished_at = NOW()
            WHERE id = :id
        """), {"id": recording_id, "msg": ABANDONED_MESSAGE})
        db.commit()
        logger.info(f"Unfinished session {claimed.session_id} had no audio; marked abandoned ({source})")
        return "abandoned"
    logger.info(f"Unfinished session {claimed.session_id} finalized by {source} ({claimed.expected_chunks} chunks)")
    if background:
        start_finalize_thread(recording_id, force=True)
        return "queued"
    return "finalized" if maybe_finalize(recording_id, force=True) else "finalize_failed"


def _sweep_stuck(db, summary, only_ids=None) -> None:
    if time.time() - PROCESS_STARTED_AT < STUCK_GRACE_SECONDS:
        return
    running = inflight_ids()
    with _finalizing_lock:
        finalizing = set(_finalizing)

    # Stitching interrupted (restart/crash): hand back to the awaiting-chunks path
    for r in db.execute(text(f"""
        SELECT id FROM isf_recorder.recording WHERE status = 'uploading' AND pipeline_stage = 'stitching'
        {_id_filter(only_ids)}
    """), _id_params(only_ids)).fetchall():
        if str(r.id) not in finalizing:
            db.execute(text("""
                UPDATE isf_recorder.recording SET pipeline_stage = 'awaiting_chunks'
                WHERE id = :id AND pipeline_stage = 'stitching'
            """), {"id": r.id})
            summary["stuck_fixed"] += 1
    db.commit()

    # Queued but no job in this process (never ran, so queuing it again costs nothing extra)
    for r in db.execute(text(f"""
        SELECT id FROM isf_recorder.recording
        WHERE pipeline_stage = 'queued' AND status IN ('uploading', 'processing')
        {_id_filter(only_ids)}
    """), _id_params(only_ids)).fetchall():
        if str(r.id) not in running:
            if schedule_transcription_job(str(r.id)):
                summary["stuck_fixed"] += 1

    # 'processing' with no job here and no progress in the DB for 30 min: interrupted. No automatic
    # re-run (that costs money); the user gets Retry, and the transcript stays if there was one.
    rows = db.execute(text(f"""
        SELECT r.id, COALESCE(LENGTH(r.transcript), 0) > 0 AS has_transcript
        FROM isf_recorder.recording r
        WHERE r.status = 'processing' AND COALESCE(r.pipeline_stage, '') <> 'queued'
          AND NOT EXISTS (
              SELECT 1 FROM isf_recorder.transcription_run tr
              WHERE tr.recording_id = r.id AND tr.status = 'processing'
                AND GREATEST(tr.started_at,
                             COALESCE((SELECT MAX(tc.updated_at) FROM isf_recorder.transcription_chunk tc
                                       WHERE tc.run_id = tr.id), tr.started_at))
                    > NOW() - INTERVAL '30 minutes')
          {_id_filter(only_ids, "r.id")}
    """), _id_params(only_ids)).fetchall()
    for r in rows:
        if str(r.id) in running:
            continue
        db.execute(text("""
            UPDATE isf_recorder.transcription_run
            SET status = 'error', finished_at = NOW(), error_message = 'Interrupted (no worker running)'
            WHERE recording_id = :id AND status = 'processing'
        """), {"id": r.id})
        if r.has_transcript:
            db.execute(text("""
                UPDATE isf_recorder.recording SET status = 'completed', pipeline_stage = 'done'
                WHERE id = :id AND status = 'processing'
            """), {"id": r.id})
        else:
            db.execute(text("""
                UPDATE isf_recorder.recording
                SET status = 'error', pipeline_stage = 'failed',
                    error_message = 'Transcription was interrupted. Tap Retry.'
                WHERE id = :id AND status = 'processing'
            """), {"id": r.id})
        summary["stuck_fixed"] += 1
    # Completed recordings whose report step was cut off
    db.execute(text(f"""
        UPDATE isf_recorder.recording r SET pipeline_stage = 'done'
        WHERE r.status = 'completed' AND r.pipeline_stage IN ('reporting', 'transcribing')
          AND NOT (r.id = ANY(CAST(:running AS uuid[])))
          {_id_filter(only_ids, "r.id")}
    """), {"running": list(running), **_id_params(only_ids)})
    db.execute(text("""
        UPDATE isf_recorder.recording_report rr
        SET status = 'error', error_message = 'Report generation was interrupted. Tap Regenerate.', updated_at = NOW()
        WHERE rr.status = 'generating' AND rr.updated_at < NOW() - INTERVAL '30 minutes'
    """))
    db.commit()


def _sweep_files(db, summary, only_ids=None) -> None:
    rows = db.execute(text(f"""
        SELECT session_id, files_pending FROM isf_recorder.deleted_session
        WHERE files_pending <> '[]'::jsonb {_id_filter(only_ids, "recording_id")}
    """), _id_params(only_ids)).fetchall()
    for r in rows:
        pending = r.files_pending if isinstance(r.files_pending, list) else json.loads(r.files_pending or "[]")
        removed, still = delete_paths(pending)
        db.execute(text("UPDATE isf_recorder.deleted_session SET files_pending = CAST(:p AS jsonb) WHERE session_id = :sid"),
                   {"sid": r.session_id, "p": json.dumps(still)})
        summary["files_retried"] += removed
    db.commit()


# ─── Startup resume ──────────────────────────────────────────────────────────

def resume_interrupted_transcriptions() -> int:
    """
    Transcription runs inside this process, so a restart kills in-flight jobs. Recent ones (< 3 days)
    are queued again; finished windows are reused, so only the unfinished part is paid for again.
    Older ones are left to the sweeper, which marks them failed (user can Retry).
    """
    queued = 0
    db = SessionLocal()
    try:
        rows = db.execute(text("""
            SELECT id FROM isf_recorder.recording
            WHERE status = 'processing' AND COALESCE(is_deleted, false) = false
              AND audio_deleted_at IS NULL AND audio_file_path IS NOT NULL
              AND created_at > NOW() - INTERVAL '3 days'
            ORDER BY created_at
        """)).fetchall()
        for (rec_id,) in rows:
            if is_inflight(str(rec_id)):
                continue
            cut_run = db.execute(text("""
                UPDATE isf_recorder.transcription_run
                SET status = 'error', finished_at = NOW(), error_message = 'Interrupted by server restart'
                WHERE recording_id = :id AND status = 'processing'
                RETURNING model, requested_by
            """), {"id": rec_id}).fetchone()
            db.execute(text("""
                UPDATE isf_recorder.recording SET pipeline_stage = 'queued'
                WHERE id = :id AND status = 'processing'
            """), {"id": rec_id})
            db.commit()
            model = cut_run.model if cut_run and cut_run.model and not cut_run.model.startswith("whisper") else None
            requested_by = str(cut_run.requested_by) if cut_run and cut_run.requested_by else None
            logger.info(f"Resuming transcription interrupted by restart: {rec_id} (model: {model or 'org setting'})")
            if schedule_transcription_job(str(rec_id), model, requested_by):
                queued += 1
    finally:
        db.close()
    return queued


# ─── Daily raw-chunk cleanup ─────────────────────────────────────────────────

def _audit_recording(db, org_id, recording_id: str, action: str, meta: dict) -> None:
    db.execute(text("""
        INSERT INTO isf_recorder.audit_log (user_id, org_id, action, target_type, target_id, metadata)
        VALUES (NULL, :o, :a, 'recording', :t, CAST(:m AS jsonb))
    """), {"o": org_id, "a": action, "t": recording_id, "m": json.dumps(meta, default=str)})


def _delete_pieces(paths: List[str], session_id: str):
    """Deletes the piece files and their empty folders. Returns (files removed, any delete failed)."""
    removed, failed, dirs = 0, False, set()
    for p in paths:
        try:
            if p and os.path.exists(p):
                os.remove(p)
                removed += 1
            if p:
                dirs.add(Path(p).parent)
        except OSError as e:
            logger.warning(f"Could not delete chunk {p}: {e}")
            failed = True
    for d in sorted(dirs, key=lambda x: len(str(x)), reverse=True):
        for folder in (d, d.parent if d.name == "chunks" and d.parent.name == session_id else None):
            try:
                if folder and folder.exists() and not any(folder.iterdir()):
                    folder.rmdir()
            except OSError:
                pass
    return removed, failed


def verify_and_repair(m4a: str, pieces: List[str], dry_run: bool = False) -> Dict:
    """Verify the m4a against its pieces; on FAILED rebuild it from the pieces into a temp file, verify
    that, and replace the m4a only if the rebuild passes. Returns {"status": ok|rebuilt|failed|
    rebuild_failed|no_pieces|error, "check": first check, "rebuild_check"?, "duration"?, "size"?}.
    dry_run: verify only."""
    from backend.recordings.audio_verify import verify_m4a, OK, FAILED
    first = verify_m4a(m4a, pieces)
    out = {"status": first["status"], "check": first}
    if first["status"] != FAILED or dry_run:
        return out
    tmp = str(Path(m4a).with_name(Path(m4a).stem + ".rebuild.m4a"))
    try:
        ok, duration, size = assemble_recording(pieces, tmp)
        second = verify_m4a(tmp, pieces) if ok else {"status": "error", "error": "assemble failed"}
        out["rebuild_check"] = second
        if ok and second["status"] == OK:
            os.replace(tmp, m4a)
            out.update(status="rebuilt", duration=duration, size=size)
        else:
            out["status"] = "rebuild_failed"
    except Exception as e:
        logger.exception(f"Rebuild of {m4a} failed: {e}")
        out.update(status="rebuild_failed", error=str(e)[:300])
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
    return out


def cleanup_chunk_wavs(min_age_hours: int = 24, limit: int = 50, dry_run: bool = False,
                       recording_ids: Optional[List[str]] = None) -> Dict:
    """
    Hourly, at most `limit` recordings per run. For recordings completed >= min_age_hours ago: verify
    the m4a's CONTENT against the raw pieces (audio_verify), rebuild it from the pieces if it fails,
    and delete the pieces only when the (possibly rebuilt) m4a passed. Pieces of a recording whose m4a
    fails are never deleted; such recordings are re-checked after 24 h.
    dry_run: verify only (no rebuild, no delete, no DB writes).
    """
    result = {"checked": 0, "recordings": 0, "files": 0, "rebuilt": 0, "failed": 0, "no_pieces": 0,
              "skipped": 0, "details": []}
    db = SessionLocal()
    try:
        rows = db.execute(text(f"""
            SELECT id, org_id, session_id, audio_file_path FROM isf_recorder.recording
            WHERE status = 'completed' AND chunks_cleaned_at IS NULL AND audio_file_path IS NOT NULL
              AND COALESCE(finished_at, created_at) < NOW() - INTERVAL '{int(min_age_hours)} hours'
              AND COALESCE(pipeline_stage, 'done') IN ('done', 'failed')
              AND (CAST(:ids AS text[]) IS NULL OR CAST(id AS text) = ANY(CAST(:ids AS text[])))
              AND (:dry OR audio_verify_status IS NULL OR audio_verify_status IN ('ok', 'rebuilt')
                   OR audio_verified_at < NOW() - INTERVAL '24 hours')
            ORDER BY created_at LIMIT :lim
        """), {"lim": limit, "ids": recording_ids, "dry": dry_run}).fetchall()
        for r in rows:
            rid = str(r.id)
            m4a = r.audio_file_path
            if not (m4a and os.path.exists(m4a) and os.path.getsize(m4a) > 0):
                result["skipped"] += 1
                continue
            pieces = [p.file_path for p in db.execute(text(
                "SELECT file_path FROM isf_recorder.audio_chunk WHERE recording_id = :id ORDER BY chunk_index"),
                {"id": r.id}).fetchall()]
            if not pieces:
                result["skipped"] += 1
                continue
            result["checked"] += 1
            v = verify_and_repair(m4a, pieces, dry_run=dry_run)
            chk = v["check"]
            status = v["status"]
            result["details"].append({"id": rid, "status": status, "pieces": chk.get("pieces"),
                                      "voiced": chk.get("voiced"), "mismatches": chk.get("mismatches")})
            if status == "no_pieces":
                result["no_pieces"] += 1
            if dry_run:
                continue
            if status == "rebuilt":
                result["rebuilt"] += 1
                db.execute(text("""
                    UPDATE isf_recorder.recording SET duration_seconds = COALESCE(:dur, duration_seconds),
                           file_size_bytes = COALESCE(:size, file_size_bytes) WHERE id = :id
                """), {"id": rid, "dur": int(round(v.get("duration") or 0)) or None, "size": v.get("size") or None})
                _audit_recording(db, r.org_id, rid, "audio_rebuilt_from_pieces",
                                 {"before": {k: chk.get(k) for k in ("voiced", "mismatches", "bad")}})
                logger.warning(f"m4a of {rid} failed verification and was rebuilt from its pieces")
            elif status in ("failed", "rebuild_failed", "error"):
                result["failed"] += 1
                _audit_recording(db, r.org_id, rid, "audio_verify_failed",
                                 {"status": status, "voiced": chk.get("voiced"), "mismatches": chk.get("mismatches"),
                                  "bad": chk.get("bad"), "rebuild": (v.get("rebuild_check") or {}).get("status")})
                logger.error(f"m4a of {rid} failed verification ({status}); keeping its pieces")
            db.execute(text("""
                UPDATE isf_recorder.recording SET audio_verify_status = :st, audio_verified_at = NOW() WHERE id = :id
            """), {"id": rid, "st": status})
            db.commit()
            if status not in ("ok", "rebuilt"):
                continue
            removed, failed = _delete_pieces(pieces, r.session_id)
            result["files"] += removed
            if not failed:
                db.execute(text("UPDATE isf_recorder.recording SET chunks_cleaned_at = NOW() WHERE id = :id"),
                           {"id": rid})
                db.commit()
                result["recordings"] += 1
    finally:
        db.close()
    if result["checked"] or result["skipped"]:
        summary = {k: v for k, v in result.items() if k != "details"}
        logger.info(f"Raw chunk cleanup{' (dry run)' if dry_run else ''}: {summary}")
    return result
