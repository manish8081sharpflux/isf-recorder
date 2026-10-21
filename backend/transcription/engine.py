"""
ISF Recorder — Transcription Orchestrator Engine
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Coordinates the transcription & intelligence pipeline for one recording:
1. Claims the recording atomically (single-flight: status -> 'processing', pipeline_stage
   'queued' -> 'transcribing'); a pipeline that cannot claim exits without doing anything
2. Calls STT (Gemini 3-minute windows with server VAD, AssemblyAI or Whisper)
3. Persists transcript segments, intelligence (title, overview, action items)
4. Marks the recording 'completed' (or 'error' with a message), then generates the report

Job scheduling: schedule_transcription_job() never queues the same recording twice in this process
(in-flight set) and runs at most MAX_CONCURRENT_TRANSCRIPTIONS pipelines at once. The DB unique
index uq_isfrec_run_one_processing is the backstop across processes.
"""
import json
import logging
import asyncio
import os
import threading
from typing import Optional, Set

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.config import USE_WHISPER_STT, WHISPER_MODEL_SIZE, MAX_CONCURRENT_TRANSCRIPTIONS
from backend.database import SessionLocal
from backend.transcription import runs
from backend.transcription.run_context import current_run_id

logger = logging.getLogger("isf_recorder.engine")

NO_SPEECH_TITLE = "No speech detected"

# ─── Single-flight job scheduling ────────────────────────────────────────────
_inflight: Set[str] = set()
_inflight_lock = threading.Lock()
_job_semaphore = threading.BoundedSemaphore(max(1, MAX_CONCURRENT_TRANSCRIPTIONS))


def is_inflight(recording_id: str) -> bool:
    with _inflight_lock:
        return str(recording_id) in _inflight


def inflight_ids() -> Set[str]:
    with _inflight_lock:
        return set(_inflight)


def schedule_transcription_job(recording_id: str, model_override: Optional[str] = None,
                               requested_by: Optional[str] = None) -> bool:
    """
    Queues the pipeline in its own thread and event loop (never on the API's event loop: the
    pipeline makes blocking model calls). Returns False if this recording is already queued or
    running in this process. Callers set pipeline_stage='queued' first (see claim rules below).
    """
    rec_id = str(recording_id)
    with _inflight_lock:
        if rec_id in _inflight:
            logger.info(f"Transcription for {rec_id} is already queued/running; not queuing again")
            return False
        _inflight.add(rec_id)

    def _run():
        try:
            with _job_semaphore:
                asyncio.run(process_recording_pipeline(rec_id, model_override, requested_by))
        except Exception as e:
            logger.exception(f"Transcription job for {rec_id} crashed: {e}")
        finally:
            with _inflight_lock:
                _inflight.discard(rec_id)

    threading.Thread(target=_run, daemon=True, name=f"transcription-{rec_id[:8]}").start()
    return True


def queue_for_transcription(db: Session, recording_id: str, allowed_statuses=("completed", "error")) -> bool:
    """
    Atomic claim used by re-transcribe / Retry: only a recording that is not being finalized or
    transcribed moves to processing/queued. Returns False if someone else holds it.
    """
    if is_inflight(recording_id):
        return False
    row = db.execute(text("""
        UPDATE isf_recorder.recording
        SET status = 'processing', pipeline_stage = 'queued', error_message = NULL
        WHERE id = :id
          AND status = ANY(:allowed)
          AND COALESCE(pipeline_stage, '') NOT IN ('awaiting_chunks', 'stitching', 'queued', 'transcribing', 'reporting')
        RETURNING id
    """), {"id": recording_id, "allowed": list(allowed_statuses)}).fetchone()
    db.commit()
    return row is not None


def _claim(db: Session, recording_id: str):
    """Pipeline claim: queued -> transcribing. Exactly one caller can win (row lock + re-check)."""
    row = db.execute(text("""
        UPDATE isf_recorder.recording
        SET status = 'processing', pipeline_stage = 'transcribing'
        WHERE id = :id
          AND status <> 'recording'
          AND (pipeline_stage = 'queued' OR (pipeline_stage IS NULL AND status = 'uploading'))
        RETURNING id
    """), {"id": recording_id}).fetchone()
    db.commit()
    return row is not None


def _is_placeholder_title(title: Optional[str]) -> bool:
    t = (title or "").strip().lower()
    return (not t or t.startswith("untitled") or t == "audio recording" or t.startswith("recording ")
            or t == NO_SPEECH_TITLE.lower())


def _set_run_stats(db: Session, run_id: Optional[str], stats: Optional[dict]) -> None:
    if not run_id or not stats:
        return
    try:
        db.execute(text("""
            UPDATE isf_recorder.transcription_run
            SET chunks_total = :total, skipped_silent_chunks = :skipped
            WHERE id = :run
        """), {"run": run_id, "total": stats.get("chunks_total"), "skipped": stats.get("skipped_silent_chunks")})
        db.commit()
    except Exception as e:
        logger.warning(f"Could not save window stats for run {run_id}: {e}")
        db.rollback()


def _fail(db: Session, recording_id: str, message: str, keep_completed: bool = False) -> None:
    if keep_completed:
        db.execute(text("""
            UPDATE isf_recorder.recording SET status = 'completed', pipeline_stage = 'done' WHERE id = :id
        """), {"id": recording_id})
    else:
        db.execute(text("""
            UPDATE isf_recorder.recording
            SET status = 'error', pipeline_stage = 'failed', error_message = :err
            WHERE id = :id
        """), {"id": recording_id, "err": (message or "Transcription failed")[:500]})
    db.commit()


async def process_recording_pipeline(recording_id: str, model_override: Optional[str] = None,
                                     requested_by: Optional[str] = None):
    """
    Runs one transcription of a recording with its own DB session. model_override: transcribe with
    this model instead of the org setting (admin model test). Every run is stored in
    transcription_run so models can be compared on the same audio.
    """
    db: Session = SessionLocal()
    run_id = None
    run_token = None
    had_transcript = False
    stats = None
    try:
        if not _claim(db, recording_id):
            logger.info(f"Recording {recording_id} is not queued for transcription (already running, "
                        f"finalizing or erased); pipeline exits")
            return

        rec = db.execute(text("""
            SELECT r.id, r.org_id, r.user_id, r.audio_file_path, r.title, r.status, r.started_at, r.mode,
                   COALESCE(LENGTH(r.transcript), 0) > 0 AS has_transcript
            FROM isf_recorder.recording r WHERE r.id = :id
        """), {"id": recording_id}).fetchone()
        if not rec:
            return
        org_id_str = str(rec.org_id) if rec.org_id else None
        had_transcript = bool(rec.has_transcript)

        audio_path = rec.audio_file_path
        if not audio_path or not os.path.exists(audio_path):
            logger.error(f"Audio file for recording {recording_id} does not exist: {audio_path}")
            _fail(db, recording_id, "Audio file not found on the server", keep_completed=had_transcript)
            return

        if os.path.getsize(audio_path) < 1000:
            logger.info(f"Audio file {audio_path} is practically empty (<1KB). Skipping AI calls.")
            db.execute(text("""
                UPDATE isf_recorder.recording
                SET status = 'completed', pipeline_stage = 'done', error_message = NULL,
                    title = CASE WHEN COALESCE(title, '') = '' THEN :title ELSE title END,
                    transcript = '', overview = 'No speech detected in this recording.'
                WHERE id = :id
            """), {"id": recording_id, "title": NO_SPEECH_TITLE})
            db.commit()
            return

        logger.info(f"Beginning pipeline for recording {recording_id} ({audio_path})")

        from backend.settings.model_resolver import get_transcription_model
        active_model = model_override or get_transcription_model(org_id_str)
        if USE_WHISPER_STT:
            run_model, run_engine = f"whisper-{WHISPER_MODEL_SIZE}", "whisper"
        elif active_model.startswith("assemblyai"):
            run_model, run_engine = active_model, "assemblyai"
        elif active_model.startswith("openai-"):
            run_model, run_engine = active_model, "openai"  # per-window engine inside the chunked pipeline
        else:
            run_model, run_engine = active_model, "gemini"

        # Keep the transcript this run replaces, then tag every usage-log row of this run
        runs.snapshot_original_run(db, recording_id)
        try:
            run_id = runs.start_run(db, recording_id, run_model, run_engine, requested_by)
        except IntegrityError:
            db.rollback()
            logger.warning(f"Another transcription of {recording_id} is already running (DB backstop); exiting")
            return
        run_token = current_run_id.set(run_id)

        if USE_WHISPER_STT:
            from backend.transcription.whisper_stt import transcribe_long_audio as transcribe_whisper_long
            stt_result = transcribe_whisper_long(audio_path, model_size=WHISPER_MODEL_SIZE)
        elif run_engine == "assemblyai":
            from backend.transcription.chunked_stt import audio_voice_seconds
            voice = audio_voice_seconds(audio_path)
            if voice < 1.0:
                logger.info(f"Server VAD found {voice:.1f}s of voice in {recording_id}; not sending to AssemblyAI")
                stt_result = {"all_silent": True, "segments": [], "full_transcript": ""}
            else:
                from backend.transcription.assemblyai_stt import transcribe_audio_file as transcribe_assemblyai
                stt_result = await transcribe_assemblyai(audio_path, model=active_model, recording_id=recording_id)
        else:
            from backend.transcription.gemini_stt import transcribe_audio_file as transcribe_gemini
            try:
                stt_result = await transcribe_gemini(audio_path, recording_id=recording_id, org_id=org_id_str,
                                                     model=active_model, run_id=run_id)
            except Exception as e:
                stats = getattr(e, "stats", None)
                raise
        stats = stt_result.get("stats")
        _set_run_stats(db, run_id, stats)
        stt_engine_used = run_engine

        if stt_result.get("all_silent"):
            if had_transcript:
                # A re-run (Retry / another model) that hears no speech must never wipe a transcript the
                # recording already has: record the run, keep the current transcript and report.
                if run_id:
                    runs.complete_run(db, run_id, {"detected_language": None, "title": rec.title, "transcript": "",
                                                   "segments": [], "overview": "No speech detected in this run.",
                                                   "key_points": [], "decisions": [], "action_items": []})
                db.execute(text("UPDATE isf_recorder.recording SET status = 'completed', pipeline_stage = 'done', "
                                "error_message = NULL WHERE id = :id"), {"id": recording_id})
                db.commit()
                logger.warning(f"Recording {recording_id}: re-run heard no speech; existing transcript kept")
                return
            _complete_silent(db, recording_id, rec, run_id, stt_engine_used)
            logger.info(f"Recording {recording_id}: no speech in any window; completed without model calls")
            return

        full_transcript = stt_result.get("full_transcript", "")
        detected_language = stt_result.get("detected_language", "mixed")
        confidence = float(stt_result.get("confidence", 0.9))
        segments = stt_result.get("segments", [])

        db.execute(text("DELETE FROM isf_recorder.transcript_segment WHERE recording_id = :id"), {"id": recording_id})
        for seg in segments:
            speaker_label = seg.get("speaker", "SPEAKER_00")
            speaker_id = 0
            if "SPEAKER_" in speaker_label:
                try:
                    speaker_id = int(speaker_label.split("SPEAKER_")[1].split()[0])
                except (IndexError, ValueError):
                    pass
            db.execute(text("""
                INSERT INTO isf_recorder.transcript_segment
                (recording_id, speaker, speaker_id, text, language, start_time, end_time, confidence)
                VALUES (:rec_id, :speaker, :speaker_id, :text, :lang, :start_t, :end_t, :conf)
            """), {
                "rec_id": recording_id, "speaker": speaker_label, "speaker_id": speaker_id,
                "text": seg.get("text", ""), "lang": seg.get("language", detected_language),
                "start_t": float(seg.get("start_time", 0.0)), "end_t": float(seg.get("end_time", 0.0)),
                "conf": float(seg.get("confidence", confidence)),
            })

        from backend.transcription.intelligence import extract_intelligence
        try:
            intel = await extract_intelligence(full_transcript, recording_id=recording_id, org_id=org_id_str,
                                               mode=rec.mode)
        except Exception as ie:
            logger.error(f"Intelligence extraction failed, preserving transcript: {ie}")
            intel = {"title": "", "overview": full_transcript[:300] if full_transcript else "No speech detected.",
                     "key_points": [], "decisions": [], "action_items": []}

        ai_title = (intel.get("title") or "").strip()
        if _is_placeholder_title(rec.title):
            if ai_title and not _is_placeholder_title(ai_title):
                title = ai_title
            elif rec.started_at:
                title = f"Recording {rec.started_at.strftime('%d %b, %I:%M %p')}"
            else:
                title = "Recording Session"
        else:
            title = rec.title.strip()

        overview = _overview_text(intel.get("overview", ""))
        key_points = intel.get("key_points", [])
        decisions = intel.get("decisions", [])
        action_items = intel.get("action_items", [])

        db.execute(text("DELETE FROM isf_recorder.action_item WHERE recording_id = :id"), {"id": recording_id})
        for item in action_items:
            if not isinstance(item, dict):
                continue
            db.execute(text("""
                INSERT INTO isf_recorder.action_item
                (recording_id, org_id, user_id, assigned_to, description, urgency)
                VALUES (:rec_id, :org_id, :user_id, :assigned_to, :description, :urgency)
            """), {
                "rec_id": recording_id, "org_id": org_id_str,
                "user_id": str(rec.user_id) if rec.user_id else None,
                "assigned_to": item.get("assigned_to"), "description": item.get("description", ""),
                "urgency": item.get("urgency", "normal"),
            })

        db.execute(text("""
            UPDATE isf_recorder.recording
            SET title = :title, status = 'completed', pipeline_stage = 'done', error_message = NULL,
                transcript = :transcript, stt_engine = :stt_engine, stt_confidence = :conf,
                detected_language = :lang, overview = :overview,
                key_points = CAST(:key_points AS jsonb), decisions = CAST(:decisions AS jsonb),
                action_items = CAST(:action_items AS jsonb), active_run_id = :run_id
            WHERE id = :id
        """), {
            "id": recording_id, "run_id": run_id, "title": title, "transcript": full_transcript,
            "conf": confidence, "lang": detected_language, "overview": overview,
            "key_points": json.dumps(key_points), "decisions": json.dumps(decisions),
            "action_items": json.dumps(action_items), "stt_engine": stt_engine_used,
        })
        runs.complete_run(db, run_id, {
            "detected_language": detected_language, "title": title, "transcript": full_transcript,
            "segments": segments, "overview": overview, "key_points": key_points,
            "decisions": decisions, "action_items": action_items,
        })
        report_pending = _mark_report_generating(db, recording_id, rec.mode, run_id)
        db.commit()
        logger.info(f"Pipeline finished successfully for recording {recording_id} (run {run_id}, {run_model})")

        if report_pending:
            _generate_report_step(recording_id, run_id)

    except Exception as e:
        logger.exception(f"Pipeline failed for recording {recording_id}: {e}")
        try:
            db.rollback()
        except Exception:
            pass
        if run_id:
            _set_run_stats(db, run_id, stats)
            runs.fail_run(db, run_id, str(e))
        try:
            # A failed re-run (e.g. a model test) keeps the transcript the recording already had
            _fail(db, recording_id, str(e), keep_completed=had_transcript)
        except Exception:
            pass
    finally:
        if run_token is not None:
            current_run_id.reset(run_token)
        db.close()


def _overview_text(overview) -> str:
    if isinstance(overview, dict):
        parts = []
        for k, v in overview.items():
            if isinstance(v, list):
                parts.append(f"{k}:\n" + "\n".join(f"• {x}" for x in v))
            else:
                parts.append(f"{k}: {v}")
        overview = "\n\n".join(parts)
    elif isinstance(overview, list):
        overview = "\n".join(f"• {x}" if isinstance(x, str) else str(x) for x in overview)
    elif not isinstance(overview, str):
        overview = str(overview) if overview is not None else ""
    return overview.replace("\\r\\n", "\n").replace("\\n", "\n")


def _complete_silent(db: Session, recording_id: str, rec, run_id: Optional[str], engine: str) -> None:
    """Every window was silent: completed, no model calls, no report."""
    title = NO_SPEECH_TITLE if _is_placeholder_title(rec.title) else rec.title
    overview = "No speech detected in this recording."
    db.execute(text("DELETE FROM isf_recorder.transcript_segment WHERE recording_id = :id"), {"id": recording_id})
    db.execute(text("DELETE FROM isf_recorder.action_item WHERE recording_id = :id"), {"id": recording_id})
    db.execute(text("""
        UPDATE isf_recorder.recording
        SET status = 'completed', pipeline_stage = 'done', error_message = NULL, title = :title,
            transcript = '', overview = :overview, stt_engine = :engine, key_points = '[]'::jsonb,
            decisions = '[]'::jsonb, action_items = '[]'::jsonb, active_run_id = :run
        WHERE id = :id
    """), {"id": recording_id, "title": title, "overview": overview, "engine": engine, "run": run_id})
    if run_id:
        runs.complete_run(db, run_id, {"detected_language": None, "title": title, "transcript": "",
                                       "segments": [], "overview": overview, "key_points": [],
                                       "decisions": [], "action_items": []})
    db.commit()


def _mark_report_generating(db: Session, recording_id: str, mode: Optional[str], run_id: Optional[str]) -> bool:
    """Report step hook (reports module). Returns True if a report should be generated after commit."""
    try:
        from backend.reports.service import mark_generating_for_pipeline
    except ImportError:
        return False
    return mark_generating_for_pipeline(db, recording_id, mode, run_id)


def _generate_report_step(recording_id: str, run_id: Optional[str]) -> None:
    db = SessionLocal()
    try:
        db.execute(text("UPDATE isf_recorder.recording SET pipeline_stage = 'reporting' WHERE id = :id"),
                   {"id": recording_id})
        db.commit()
        from backend.reports.service import generate_report
        generate_report(recording_id, run_id=run_id)
    except Exception as e:
        logger.exception(f"Report generation failed for {recording_id}: {e}")
    finally:
        try:
            db.execute(text("""
                UPDATE isf_recorder.recording SET pipeline_stage = 'done'
                WHERE id = :id AND pipeline_stage = 'reporting'
            """), {"id": recording_id})
            db.commit()
        except Exception:
            pass
        db.close()
