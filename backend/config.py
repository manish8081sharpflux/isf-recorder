"""
ISF Recorder — Configuration
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Single source of truth for database, API keys, and service settings.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""
import os

try:
    from dotenv import load_dotenv
    # Search local directory, parent directory, and C:\GroundUp for .env
    for env_path in [
        os.path.join(os.path.dirname(__file__), "..", ".env"),
        os.path.join(os.path.dirname(__file__), "..", "..", ".env"),
        r"C:\GroundUp\isf-recorder\.env",
        r"C:\GroundUp\.env",
        ".env"
    ]:
        if os.path.exists(env_path):
            load_dotenv(dotenv_path=env_path)
            break
except ImportError:
    pass

# ─── Database ────────────────────────────────────────────────────────────────
# Same PostgreSQL instance as all Ground Up services, but uses isf_recorder schema.
# Set DATABASE_URL in .env (see .env.example).
DATABASE_URL = os.getenv("DATABASE_URL", "")

# ─── Server ──────────────────────────────────────────────────────────────────
SERVICE_PORT = int(os.getenv("ISF_RECORDER_PORT", "8019"))
SERVICE_HOST = os.getenv("ISF_RECORDER_HOST", "0.0.0.0")

# ─── JWT Authentication ─────────────────────────────────────────────────────
JWT_SECRET = os.getenv("JWT_SECRET", "")
JWT_ALGORITHM = "HS256"
JWT_EXPIRY_DAYS = 90  # Token valid for 90 days

# ─── Audio Storage ───────────────────────────────────────────────────────────
# Audio files stored at: {AUDIO_STORAGE_ROOT}/{org-slug}/{YYYY-MM-DD}/{recording-id}.m4a
AUDIO_STORAGE_ROOT = os.getenv(
    "AUDIO_STORAGE_ROOT",
    r"D:\Ground Up\isf-recorder-audio"
)
AUDIO_RETENTION_DAYS = int(os.getenv("AUDIO_RETENTION_DAYS", "7"))

# ─── Gemini API (Transcription — Primary Engine) ────────────────────────────
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY", "")
# Model for transcription (batch, supports diarization + timestamps)
GEMINI_TRANSCRIBE_MODEL = os.getenv("GEMINI_TRANSCRIBE_MODEL", "gemini-2.5-flash")
# Model for intelligence extraction (summary, action items)
GEMINI_INTELLIGENCE_MODEL = os.getenv("GEMINI_INTELLIGENCE_MODEL", "gemini-2.5-flash")

# ─── Whisper STT (Open Source Alternative) ──────────────────────────────────
# Open source STT using faster-whisper for Indian languages
# Set WHISPER_MODEL_SIZE to use open source STT instead of Gemini
# Available: "tiny", "base", "small", "medium", "large-v3"
WHISPER_MODEL_SIZE = os.getenv("WHISPER_MODEL_SIZE", "base")
# Language code for Whisper auto-detection or specific language:
# "en" = English, "hi" = Hindi, "mr" = Marathi, "multi" = auto-detect
WHISPER_LANGUAGE = os.getenv("WHISPER_LANGUAGE", "multi")
# If True, use Whisper instead of Gemini for STT
USE_WHISPER_STT = os.getenv("USE_WHISPER_STT", "false").lower() == "true"

# ─── AssemblyAI STT (Alternative Cloud Provider) ────────────────────────────
ASSEMBLYAI_API_KEY = os.getenv("ASSEMBLYAI_API_KEY", "")

# ─── WhatsApp (for OTP + Daily Summary) ─────────────────────────────────────
# Re-uses the same WhatsApp Business API credentials as the rest of the platform.
META_API_VERSION = "v23.0"
WHATSAPP_PHONE_NUMBER_ID = os.getenv("WHATSAPP_PHONE_NUMBER_ID", "")
WHATSAPP_ACCESS_TOKEN = os.getenv("WHATSAPP_ACCESS_TOKEN", "")

# ─── Daily Summary ───────────────────────────────────────────────────────────
DAILY_SUMMARY_HOUR = int(os.getenv("DAILY_SUMMARY_HOUR", "21"))  # 9 PM IST
DAILY_SUMMARY_MINUTE = int(os.getenv("DAILY_SUMMARY_MINUTE", "0"))

# ─── Concurrency / Performance ───────────────────────────────────────────────
# Supports 30-50 simultaneous users uploading chunks.
# Each phone uploads independently; server processes with async workers.
DB_POOL_SIZE = int(os.getenv("DB_POOL_SIZE", "30"))
DB_MAX_OVERFLOW = int(os.getenv("DB_MAX_OVERFLOW", "20"))
MAX_CONCURRENT_TRANSCRIPTIONS = int(os.getenv("MAX_CONCURRENT_TRANSCRIPTIONS", "8"))

# ─── Super admins (contract §2) ─────────────────────────────────────────────
# Phones (last 10 digits) that are always admins and the only ones allowed to change roles.
SUPER_ADMIN_PHONES = {
    "".join(c for c in p if c.isdigit())[-10:]
    for p in os.getenv("SUPER_ADMIN_PHONES", "9421187857,9684019697").split(",")
    if "".join(c for c in p if c.isdigit())
}
