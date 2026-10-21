"""
ISF Recorder — Current transcription run
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
The pipeline sets this while it runs, so every usage-log row written during a run
(Gemini or AssemblyAI) is tagged with that run and its cost can be shown per model.
"""
from contextvars import ContextVar
from typing import Optional

current_run_id: ContextVar[Optional[str]] = ContextVar("current_run_id", default=None)
