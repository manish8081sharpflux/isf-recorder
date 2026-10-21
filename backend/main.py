"""
ISF Recorder — Backend Service
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Core FastAPI application for ISF Recorder.
Coordinates:
- WhatsApp OTP & JWT Authentication
- Multi-user role-based audio ingestion (30-50 concurrent staff)
- Offline & real-time audio chunk handling
- Gemini 2.0/3.5 Speech-to-Text with Speaker Diarization
- Executive summary & action item intelligence extraction
- Sweeper (idle/abandoned sessions, stuck stages) and daily raw-chunk cleanup; audio is kept
- Daily 9 PM WhatsApp executive digest
"""
import os
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.triggers.date import DateTrigger
from datetime import datetime, timedelta
from sqlalchemy import text

from backend.config import (
    SERVICE_PORT,
    SERVICE_HOST,
    AUDIO_STORAGE_ROOT,
    DAILY_SUMMARY_HOUR,
    DAILY_SUMMARY_MINUTE,
)
from backend.database import SessionLocal
from backend.notifications.daily_summary import generate_and_send_daily_summary

# Import routers
from backend.auth.routes import router as auth_router
from backend.users.routes import router as users_router
from backend.recordings.routes import router as recordings_router
from backend.reports.routes import router as reports_router
from backend.recordings.usage_routes import (
    recording_usage_router,
    admin_usage_router
)
from backend.notifications.routes import router as notifications_router
from backend.system.routes import router as system_router
from backend.chat.routes import router as chat_router
from backend.settings.routes import router as settings_router
from backend.settings.recorder import router as recorder_settings_router
from backend.calls.routes import router as calls_router
from backend.assistant.routes import router as assistant_router
from backend.reports.share import router as report_share_router
from backend.reports.director_pov import router as director_pov_router
from backend.names.routes import router as names_router
from backend.notifications.inbox_routes import router as phone_notifications_router
from backend.transcription.forex_sync import get_live_usd_to_inr_rate

# Logging setup
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("isf_recorder.main")

scheduler = AsyncIOScheduler()


def background_jobs_disabled() -> bool:
    """ISF_DISABLE_BACKGROUND_JOBS=1 turns off every scheduled job (local runs/tests: no paid API calls,
    no WhatsApp messages, no finalizing of copied production sessions)."""
    return os.getenv("ISF_DISABLE_BACKGROUND_JOBS", "").strip().lower() in ("1", "true", "yes")


def run_sweeper_job():
    from backend.recordings.lifecycle import run_sweeper
    try:
        run_sweeper()
    except Exception as e:
        logger.exception(f"Sweeper failed: {e}")


def run_chunk_cleanup_job():
    from backend.recordings.lifecycle import cleanup_chunk_wavs
    try:
        cleanup_chunk_wavs()
    except Exception as e:
        logger.exception(f"Raw chunk cleanup failed: {e}")


def run_backup_job():
    from backend.system.backup import run_backup_job as _run
    _run()  # never raises


async def run_health_report_job():
    from backend.notifications.health_report import send_health_report
    await send_health_report()  # never raises


def resume_interrupted_transcriptions():
    """Queues transcriptions that a restart cut off (recent ones only; see lifecycle)."""
    from backend.recordings.lifecycle import resume_interrupted_transcriptions as _resume
    try:
        _resume()
    except Exception as e:
        logger.exception(f"Resuming interrupted transcriptions failed: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """App startup and shutdown lifecycle management."""
    logger.info("Starting ISF Recorder Backend...")

    # Ensure storage root exists
    Path(AUDIO_STORAGE_ROOT).mkdir(parents=True, exist_ok=True)
    logger.info(f"Audio storage root verified at: {AUDIO_STORAGE_ROOT}")

    # Startup seed of recorder templates/config (idempotent)
    try:
        from backend.settings.recorder import seed_recorder_defaults
        seed_recorder_defaults()
    except ImportError:
        pass
    except Exception as e:
        logger.exception(f"Recorder defaults seed failed: {e}")

    if background_jobs_disabled():
        logger.warning("ISF_DISABLE_BACKGROUND_JOBS is set: scheduler not started (no sweeper, resume, "
                       "daily summary, forex sync or chunk cleanup).")
    else:
        # Audio and chat messages are kept forever (no 7-day retention jobs any more).
        # 1. Sweeper: finalize sessions whose chunks never all arrived / abandoned sessions, recover stuck stages
        scheduler.add_job(run_sweeper_job, trigger=IntervalTrigger(seconds=60), id="sweeper",
                          replace_existing=True, max_instances=1, coalesce=True)
        # 2. Hourly: delete raw chunk WAVs of recordings completed >= 24 h ago once the m4a's content was
        #    verified against them (rebuilt from the pieces if it failed); batch-limited per run
        scheduler.add_job(run_chunk_cleanup_job, trigger=IntervalTrigger(hours=1), id="chunk_cleanup",
                          replace_existing=True, max_instances=1, coalesce=True)
        # 3. Daily WhatsApp summary (9:00 PM IST by default)
        scheduler.add_job(generate_and_send_daily_summary,
                          trigger=CronTrigger(hour=DAILY_SUMMARY_HOUR, minute=DAILY_SUMMARY_MINUTE),
                          id="daily_whatsapp_summary", replace_existing=True)
        # 3b. Daily admin health message to super admins (21:05 IST, after the digest)
        scheduler.add_job(run_health_report_job, trigger=CronTrigger(hour=21, minute=5, timezone="Asia/Kolkata"),
                          id="daily_health_report", replace_existing=True, max_instances=1, coalesce=True)
        # 3c. Nightly pg_dump of the isf_recorder schema (02:30 server time), verified, newest 14 kept
        scheduler.add_job(run_backup_job, trigger=CronTrigger(hour=2, minute=30), id="nightly_backup",
                          replace_existing=True, max_instances=1, coalesce=True)
        # 4. Live forex sync every 12 hours
        scheduler.add_job(get_live_usd_to_inr_rate, trigger=IntervalTrigger(hours=12), id="forex_sync",
                          replace_existing=True)
        # 4b. Phone notifications (21 Oct): new failures / reports every 2 min, coach reminder 6 PM,
        #     admin daily summary 8 PM, old rows removed nightly
        from backend.notifications.inbox import run_job as run_notification_job
        scheduler.add_job(run_notification_job, args=["scan"], trigger=IntervalTrigger(minutes=2), id="notify_scan",
                          replace_existing=True, max_instances=1, coalesce=True)
        scheduler.add_job(run_notification_job, args=["norec"], trigger=CronTrigger(hour=18, minute=0, timezone="Asia/Kolkata"),
                          id="notify_no_recording", replace_existing=True, max_instances=1, coalesce=True)
        scheduler.add_job(run_notification_job, args=["daily"], trigger=CronTrigger(hour=20, minute=0, timezone="Asia/Kolkata"),
                          id="notify_daily_summary", replace_existing=True, max_instances=1, coalesce=True)
        scheduler.add_job(run_notification_job, args=["cleanup"], trigger=CronTrigger(hour=3, minute=15, timezone="Asia/Kolkata"),
                          id="notify_cleanup", replace_existing=True, max_instances=1, coalesce=True)
        # 5. One-off: queue transcriptions that a restart interrupted
        scheduler.add_job(resume_interrupted_transcriptions,
                          trigger=DateTrigger(run_date=datetime.now() + timedelta(seconds=60)),
                          id="resume_interrupted_transcriptions", replace_existing=True)
        scheduler.start()
        logger.info("Background scheduler initialized (sweeper, chunk cleanup, daily summary, forex sync).")

    yield

    logger.info("Shutting down ISF Recorder Backend...")
    if scheduler.running:
        scheduler.shutdown(wait=False)


app = FastAPI(
    title="ISF Recorder API",
    version="1.0.0",
    description="Multi-tenant, enterprise audio recording & AI transcription system for ISF and Ground Up.",
    lifespan=lifespan,
)

# CORS setup for mobile APK and web clients
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Register routes
app.include_router(auth_router)
app.include_router(users_router)
app.include_router(recordings_router)
app.include_router(reports_router)
app.include_router(recording_usage_router)
app.include_router(admin_usage_router)
app.include_router(notifications_router)
app.include_router(system_router)
app.include_router(chat_router)
app.include_router(settings_router)
app.include_router(recorder_settings_router)
app.include_router(calls_router)
app.include_router(assistant_router)
app.include_router(report_share_router)
app.include_router(director_pov_router)
app.include_router(names_router)
app.include_router(phone_notifications_router)


@app.get("/")
def root():
    return {
        "service": "ISF Recorder API",
        "version": "1.0.0",
        "status": "online",
        "endpoints": {
            "docs": "/docs",
            "health": "/health",
            "apk": "/apk",
            "auth": "/auth",
            "recordings": "/recordings",
            "users": "/users",
            "notifications": "/notifications",
            "chat": "/chat",
        }
    }


def _apk_file_name() -> str:
    """Download name with the published version (app_config latest_version_name); plain name as fallback."""
    try:
        from backend.database import SessionLocal
        from sqlalchemy import text as _text
        with SessionLocal() as db:
            v = db.execute(_text("SELECT value FROM isf_recorder.app_config WHERE key = 'latest_version_name'")).scalar()
        v = "".join(ch for ch in (v or "") if ch.isalnum() or ch in ".-")
        return f"ISFRecorder-{v}.apk" if v else "ISFRecorder.apk"
    except Exception:
        return "ISFRecorder.apk"


@app.get("/apk")
@app.get("/download/apk")
def download_apk():
    """Directly serve the latest signed release APK for staff mobile installation."""
    candidates = [
        Path("C:/GroundUp/isf-recorder/app-release.apk"),
        Path("app-release.apk"),
        Path(__file__).parent.parent / "app-release.apk",
        Path(__file__).parent.parent / "android" / "app" / "build" / "outputs" / "apk" / "release" / "app-release.apk",
    ]
    for c in candidates:
        if c.exists():
            return FileResponse(
                path=str(c),
                filename=_apk_file_name(),          # e.g. ISFRecorder-2.3.5.apk, so people can see the version
                media_type="application/vnd.android.package-archive"
            )
    raise HTTPException(status_code=404, detail="APK not found on server")


@app.get("/health")
def health_check():
    """Health check validating PostgreSQL connectivity and disk storage."""
    db_status = "error"
    db_error = None
    try:
        db = SessionLocal()
        db.execute(text("SELECT 1"))
        db.close()
        db_status = "connected"
    except Exception as e:
        db_error = str(e)

    storage_exists = os.path.exists(AUDIO_STORAGE_ROOT)

    return {
        "status": "healthy" if db_status == "connected" else "unhealthy",
        "database": db_status,
        "database_error": db_error,
        "audio_storage_root": AUDIO_STORAGE_ROOT,
        "storage_accessible": storage_exists,
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "backend.main:app",
        host=SERVICE_HOST,
        port=SERVICE_PORT,
        reload=False
    )
