"""
ISF Recorder — Chunked Gemini transcription (3-minute windows, VAD skip, window reuse)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Contract §7:
- Decode the recording ONCE to 16 kHz mono PCM WAV (ffmpeg), then cut exact windows with `wave`.
- Window k = [max(0, k·180 − 3), (k+1)·180); audio <= 180 s is one window.
- Server VAD: a window with < 1 s of voice is skipped (no API call).
- Each window is sent inline (Part.from_bytes, audio/wav) and answered in a compact line format
  ("MM:SS|S<n>|text", ~4x fewer output tokens than JSON) with max_output_tokens=4096. A looped window
  (MAX_TOKENS, > 30% lines pruned by the loop filter, or a line > 600 chars) keeps its lines before the
  loop and gets ONE continuation call from the last good line to the window end. 429/5xx get 2 retries
  with 2 s / 8 s backoff. The fallback model is only tried after API errors, never because of a loop.
- <= 4 windows in parallel per recording, and a process-wide semaphore caps concurrent model calls.
  Worker tasks run inside a copy of the caller's context so current_run_id still tags usage rows.
- Every window is persisted in transcription_chunk. A `done` window from an earlier run of the same
  recording with the same model, bounds (±0.05 s) and audio fingerprint is reused (no new call).
- Empty or unparseable output is a failed window, never placeholder text.
"""
import contextvars
import json
import logging
import os
import re
import subprocess
import tempfile
import random
import threading
import time
import wave
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import text

from backend.database import SessionLocal
from backend.transcription import vad
from backend.transcription.overlap import CHUNK_SECONDS, OVERLAP_SECONDS, plan_windows, merge_windows

logger = logging.getLogger("isf_recorder.chunked_stt")

WINDOW_MAX_OUTPUT_TOKENS = 4096
PARALLEL_WINDOWS = 4
RETRY_BACKOFF_SECONDS = (2.0, 8.0)              # 5xx / network errors
# 429 (quota / rate limit): Gemini quotas refill per minute, so wait longer before the window is given up
# on this model (~2 min in total, with jitter so parallel windows do not retry in lockstep).
RATE_LIMIT_BACKOFF_SECONDS = (5.0, 15.0, 30.0, 60.0)
FALLBACK_MODEL = "gemini-2.5-flash"
# Windows the main model cannot do (still looping after the continuation, refused, or failed) get ONE pass
# with this model. Measured 2026-10-08 on a real 91-min Balgruha session: gemini-2.5-flash breaks down on
# ~1 in 3 windows (often children reciting), gemini-3.5-flash did those cleanly at ~Rs 1.6/window.
# Empty = no rescue.
RESCUE_MODEL = os.getenv("RESCUE_TRANSCRIPTION_MODEL", "gemini-3.5-flash").strip()
REUSE_TOLERANCE_SECONDS = 0.05

# Process-wide cap on concurrent Gemini transcription calls (threads, not asyncio: every pipeline
# runs its own event loop in its own thread).
GLOBAL_CALL_SEMAPHORE = threading.BoundedSemaphore(int(os.getenv("GEMINI_MAX_PARALLEL_CALLS", "12")))

_sleep = time.sleep  # patched in tests


class TranscriptionFailed(Exception):
    """Raised when one or more windows could not be transcribed. `stats` describes the run."""

    def __init__(self, message: str, stats: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.stats = stats or {}


# ─── Audio helpers ───────────────────────────────────────────────────────────

# Bump when the transcription prompt/output format changes: windows made with another prompt are never reused.
PROMPT_VERSION = "compact-v1"

# Gemini refuses some audio (e.g. children reciting a known poem -> RECITATION). That is not retryable
# and must not fail the whole recording: the window keeps a visible placeholder instead.
BLOCKED_FINISH_REASONS = ("RECITATION", "SAFETY", "BLOCKLIST", "PROHIBITED_CONTENT", "SPII")
BLOCKED_PLACEHOLDER = "[This part could not be transcribed by the AI (recitation/blocked) — listen to the audio]"


def audio_fingerprint(path: str) -> str:
    st = os.stat(path)
    return f"{st.st_size}:{int(st.st_mtime)}:{PROMPT_VERSION}"


def decode_to_wav(audio_path: str, out_path: str) -> float:
    """Decodes any audio file to 16 kHz mono 16-bit PCM WAV. Returns the duration in seconds."""
    from backend.recordings.chunk_assembler import find_binary
    res = subprocess.run(
        [find_binary("ffmpeg"), "-y", "-v", "error", "-i", audio_path, "-vn", "-ac", "1", "-ar", "16000",
         "-c:a", "pcm_s16le", "-f", "wav", out_path],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=1800,
    )
    if res.returncode != 0 or not os.path.exists(out_path):
        raise RuntimeError(f"Could not decode audio: {res.stderr.decode('utf-8', errors='ignore')[-300:]}")
    with wave.open(out_path, "rb") as w:
        return w.getnframes() / float(w.getframerate() or 16000)


def read_pcm(wav_path: str, start: float, end: float) -> bytes:
    with wave.open(wav_path, "rb") as w:
        rate = w.getframerate()
        first = int(round(start * rate))
        last = min(int(round(end * rate)), w.getnframes())
        w.setpos(min(first, w.getnframes()))
        return w.readframes(max(0, last - first))


def pcm_to_wav_bytes(pcm: bytes, rate: int = 16000) -> bytes:
    import io
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


def audio_voice_seconds(audio_path: str, stop_after_seconds: Optional[float] = vad.MIN_VOICE_SECONDS) -> float:
    """Voice seconds in a whole recording (stops early once stop_after_seconds is reached)."""
    fd, tmp = tempfile.mkstemp(suffix=".wav", prefix="vad_")
    os.close(fd)
    try:
        decode_to_wav(audio_path, tmp)
        return vad.file_voice_seconds(tmp, stop_after_seconds=stop_after_seconds)
    finally:
        _remove(tmp)


def _remove(path: str) -> None:
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except OSError as e:
        logger.warning(f"Could not remove temp file {path}: {e}")


# ─── Model calls ─────────────────────────────────────────────────────────────

def _is_retryable(exc: Exception) -> bool:
    code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    try:
        code = int(code) if code is not None else None
    except (TypeError, ValueError):
        code = None
    if code in (429, 500, 502, 503, 504):
        return True
    name = type(exc).__name__
    if name in ("ServerError", "TimeoutException", "ConnectTimeout", "ReadTimeout", "ConnectError",
                "RemoteProtocolError", "ReadError", "WriteError"):
        return True
    return isinstance(exc, (TimeoutError, ConnectionError))


def _response_info(response) -> Tuple[str, str, int]:
    text_out = ""
    try:
        text_out = response.text or ""
    except Exception:
        text_out = ""
    finish = ""
    try:
        cands = getattr(response, "candidates", None) or []
        if cands:
            finish = str(getattr(cands[0], "finish_reason", "") or "")
    except Exception:
        finish = ""
    out_tokens = 0
    try:
        out_tokens = int(getattr(getattr(response, "usage_metadata", None), "candidates_token_count", 0) or 0)
    except Exception:
        out_tokens = 0
    return text_out, finish, out_tokens


COMPACT_PROMPT = """Transcribe this audio exactly as spoken (Marathi, Hindi, English or mixed; write each language in its own script: Marathi/Hindi in Devanagari, English in Latin).
Output ONE line per utterance, nothing else, in this exact format:
MM:SS|S<n>|text
- MM:SS = start time of the utterance from the beginning of this clip.
- S<n> = speaker number (S1, S2, ...), consistent within this clip.
- Do not translate. Do not summarise. Skip silence, music and noise (write nothing for them).
- Never write the same line more than twice in a row. If someone repeats a word or phrase many times (chanting, counting), write it once and add " (repeated)".
- If there is no speech at all, output nothing."""

# 'MM:SS|S<n>|' markers. Matched anywhere, not only at line starts: gemini-3.5-flash often writes all
# utterances on one line with no newlines between them.
_MARK_RE = re.compile(r"(?<![\d:])(\d{1,3}(?::\d{2}){1,2})\s*\|\s*S?(\d+)\s*\|", re.IGNORECASE)
_DEVANAGARI = re.compile(r"[ऀ-ॿ]")
LOOP_PRUNED_RATIO = 0.30
LOOP_LINE_CHARS = 600          # a line this long is a loop only if its words repeat (see _is_repetitive)
LOOP_LINE_HARD_CHARS = 4000    # ... and always a loop beyond this (more than a 3-min window holds)
LOOP_MIN_DISTINCT_NGRAMS = 0.5
CONTINUATION_MIN_SECONDS = 20.0


def _clock_seconds(value: str) -> float:
    total = 0.0
    for part in value.split(":"):
        total = total * 60 + float(part)
    return total


def parse_compact(text_out: str, offset: float, window_end: float) -> List[Dict[str, Any]]:
    """
    'MM:SS|S<n>|text' lines (H:MM:SS tolerated) -> segments with absolute times. start = offset + clock,
    end = next segment's start (the last one ends at window_end). Other lines are ignored.
    """
    raw = []
    text_out = text_out or ""
    marks = list(_MARK_RE.finditer(text_out))
    for i, m in enumerate(marks):
        body_end = marks[i + 1].start() if i + 1 < len(marks) else len(text_out)
        # text runs to the next marker or the end of its line, whichever comes first
        body = text_out[m.end():body_end].split("\n", 1)[0].strip()
        if not body:
            continue
        try:
            start = offset + _clock_seconds(m.group(1))
        except ValueError:
            continue
        start = min(max(start, offset), window_end)
        raw.append({"speaker": f"SPEAKER_{int(m.group(2)):02d}", "text": body, "start_time": round(start, 2),
                    "language": "mixed" if _DEVANAGARI.search(body) else "en"})
    for i, seg in enumerate(raw):
        nxt = raw[i + 1]["start_time"] if i + 1 < len(raw) else window_end
        seg["end_time"] = round(max(nxt, seg["start_time"]), 2)
    return raw


def _call_once(client, model_name: str, wav_bytes: bytes, recording_id: Optional[str], duration: float):
    from google.genai import types
    from backend.transcription.usage_tracker import track_gemini_call

    config = types.GenerateContentConfig(
        temperature=0.2,
        thinking_config=types.ThinkingConfig(thinking_budget=0),
        max_output_tokens=WINDOW_MAX_OUTPUT_TOKENS,
    )
    part = types.Part.from_bytes(data=wav_bytes, mime_type="audio/wav")

    def _do():
        return client.models.generate_content(model=model_name, contents=[part, COMPACT_PROMPT], config=config)

    with GLOBAL_CALL_SEMAPHORE:
        return track_gemini_call(recording_id=recording_id, operation_type="TRANSCRIPTION",
                                 gemini_model=model_name, call_fn=_do, audio_duration_seconds=duration)


def _status_code(exc: Exception) -> Optional[int]:
    code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    try:
        return int(code) if code is not None else None
    except (TypeError, ValueError):
        return None


def _call_with_retries(client, model_name: str, wav_bytes: bytes, recording_id: Optional[str],
                       duration: float, counter: Dict[str, int]):
    """Retries 5xx/network errors (2 s, 8 s) and 429 rate limits (5, 15, 30, 60 s + jitter). The semaphore
    is held only during a call, never while sleeping."""
    other_tries = rate_tries = 0
    while True:
        counter["attempts"] += 1
        try:
            return _call_once(client, model_name, wav_bytes, recording_id, duration)
        except Exception as e:
            if not _is_retryable(e):
                raise
            if _status_code(e) == 429:
                if rate_tries >= len(RATE_LIMIT_BACKOFF_SECONDS):
                    raise
                wait = RATE_LIMIT_BACKOFF_SECONDS[rate_tries] * random.uniform(1.0, 1.3)
                rate_tries += 1
            else:
                if other_tries >= len(RETRY_BACKOFF_SECONDS):
                    raise
                wait = RETRY_BACKOFF_SECONDS[other_tries]
                other_tries += 1
            logger.warning(f"{model_name} transient error ({e}); retrying in {wait:.0f}s")
            _sleep(wait)


def _is_repetitive(text: str) -> bool:
    """True when a long line is mostly the same words over and over (a model loop), not a monologue.
    Gemini 2.5 Flash often writes a storyteller's whole minute as one line; that is a real transcript."""
    if len(text) > LOOP_LINE_HARD_CHARS:
        return True
    words = text.lower().split()
    grams = [tuple(words[i:i + 3]) for i in range(len(words) - 2)]
    if len(grams) < 10:
        return len(set(words)) <= max(1, len(words) // 4)
    return len(set(grams)) / len(grams) < LOOP_MIN_DISTINCT_NGRAMS


def analyse_loop(segments: List[Dict[str, Any]], finish: str) -> Tuple[bool, List[Dict[str, Any]]]:
    """
    Returns (looped, good_segments). Looped = MAX_TOKENS finish, or the loop filter pruned > 30% of
    the lines, or one line is longer than 600 chars AND repetitive. good_segments = the lines before the loop starts
    (all lines, de-duplicated, when there is no loop).
    """
    from backend.transcription.gemini_stt import deduplicate_segments
    tagged = [{**s, "_i": i} for i, s in enumerate(segments)]
    kept = deduplicate_segments(tagged)
    kept_ids = {s["_i"] for s in kept}
    long_idx = [i for i, s in enumerate(segments)
                if len(s.get("text") or "") > LOOP_LINE_CHARS and _is_repetitive(s.get("text") or "")]
    pruned = len(segments) - len(kept)
    looped = ("MAX_TOKENS" in (finish or "")) or bool(long_idx) or (
        bool(segments) and pruned / len(segments) > LOOP_PRUNED_RATIO)
    if not looped:
        return False, [{k: v for k, v in s.items() if k != "_i"} for s in kept]
    starts = [i for i in range(len(segments)) if i not in kept_ids] + long_idx
    if starts:
        loop_start = min(starts)
    else:
        loop_start = max(0, len(segments) - 1)  # cut at the token cap: the last line may be partial
    good = [s for s in kept if s["_i"] < loop_start]
    return True, [{k: v for k, v in s.items() if k != "_i"} for s in good]


def _model_pass(client, models: List[str], pcm: bytes, offset: float, end: float, recording_id: Optional[str],
                counter: Dict[str, int]) -> Dict[str, Any]:
    """One transcription of [offset, end] (with model fallback on API errors only)."""
    wav_bytes = pcm_to_wav_bytes(pcm)
    last_error = "no model available"
    blocked = None  # (model, finish, tokens) of the last model that refused the audio
    for model_name in models:
        try:
            response = _call_with_retries(client, model_name, wav_bytes, recording_id, end - offset, counter)
        except Exception as e:
            last_error = f"{model_name}: {e}"
            logger.warning(f"Window at {offset:.0f}s failed on {model_name}: {e}")
            continue
        text_out, finish, out_tokens = _response_info(response)
        segments = parse_compact(text_out, offset, end)
        if not text_out.strip():
            if finish and any(r in finish for r in BLOCKED_FINISH_REASONS):
                blocked = (model_name, finish, out_tokens)
                logger.warning(f"Window at {offset:.0f}s refused by {model_name} ({finish}); trying next model if any")
                continue
            if finish and "STOP" not in finish:
                last_error = f"{model_name} returned no text (finish reason {finish})"
                logger.warning(f"Window at {offset:.0f}s: {last_error}")
                continue
            # 'If there is no speech at all, output nothing': a clean empty answer is a real result
        elif not segments and "MAX_TOKENS" not in finish:
            return {"ok": False, "model": model_name, "finish": finish, "tokens": out_tokens,
                    "error": "Model output could not be read"}
        return {"ok": True, "model": model_name, "finish": finish, "tokens": out_tokens, "segments": segments}
    if blocked:
        model_name, finish, out_tokens = blocked
        return {"ok": True, "model": model_name, "finish": finish, "tokens": out_tokens, "segments": [{
            "speaker": "SPEAKER_00", "speaker_id": 0, "text": BLOCKED_PLACEHOLDER, "language": "mixed",
            "start_time": offset, "end_time": end, "confidence": 0.0}]}
    return {"ok": False, "model": models[0] if models else None, "finish": None, "tokens": 0, "error": last_error[:500]}


def _needs_rescue(result: Dict[str, Any]) -> bool:
    if result["status"] in ("failed", "loop_truncated"):
        return True
    return any(s.get("text") == BLOCKED_PLACEHOLDER for s in result["segments"])


def transcribe_window(client, models: List[str], pcm: bytes, offset: float, duration: float,
                      recording_id: Optional[str]) -> Dict[str, Any]:
    """Transcribes one window; if the main model cannot do it, one rescue pass with RESCUE_MODEL.
    Returns {status, segments, language, model, finish_reason, output_tokens, attempts, error}."""
    result = _transcribe_window_with(client, models, pcm, offset, duration, recording_id)
    if not RESCUE_MODEL or RESCUE_MODEL in models[:1] or not _needs_rescue(result):
        return result
    logger.warning(f"Window at {offset:.0f}s: {result['status']} on {result['model']}; rescuing with {RESCUE_MODEL}")
    counter = {"attempts": 0}
    rescue = _model_pass(client, [RESCUE_MODEL], pcm, offset, offset + duration, recording_id, counter)
    tokens = (result.get("output_tokens") or 0) + (rescue.get("tokens") or 0)
    attempts = result["attempts"] + counter["attempts"]
    if rescue["ok"]:
        looped, good = analyse_loop(rescue["segments"], rescue["finish"])
        blocked = any(s.get("text") == BLOCKED_PLACEHOLDER for s in good)
        if (good and not looped) or (blocked and not result["segments"]):
            langs = [s.get("language") for s in good if s.get("language")]
            return {"status": "done", "segments": good, "language": max(set(langs), key=langs.count) if langs else "mixed",
                    "model": RESCUE_MODEL, "finish_reason": rescue["finish"], "output_tokens": tokens,
                    "attempts": attempts, "error": None}
    # rescue did not help: keep the main model's (partial) result
    return {**result, "output_tokens": tokens, "attempts": attempts}


def _transcribe_window_with(client, models: List[str], pcm: bytes, offset: float, duration: float,
                            recording_id: Optional[str]) -> Dict[str, Any]:
    """Transcribes one window of 16 kHz mono PCM with the main model (+ one continuation on a loop).
    status: done | loop_truncated | failed."""
    counter = {"attempts": 0}
    end = offset + duration
    first = _model_pass(client, models, pcm, offset, end, recording_id, counter)
    if not first["ok"]:
        return {"status": "failed", "segments": [], "language": None, "model": first["model"],
                "finish_reason": first["finish"], "output_tokens": first["tokens"],
                "attempts": counter["attempts"], "error": first["error"]}

    looped, segments = analyse_loop(first["segments"], first["finish"])
    status, finish, tokens, model_used = "done", first["finish"], first["tokens"], first["model"]
    if looped:
        t_cont = segments[-1]["start_time"] if segments else offset
        logger.warning(f"Window at {offset:.0f}s looped on {model_used} ({len(first['segments'])} lines, "
                       f"finish {finish}); kept {len(segments)} good lines")
        if end - t_cont >= CONTINUATION_MIN_SECONDS:
            # ONE continuation call from the last good line to the window end (it re-covers that line)
            keep = segments[:-1] if segments else []
            byte_from = int(round((t_cont - offset) * vad.SAMPLE_RATE)) * 2
            cont = _model_pass(client, [model_used], pcm[byte_from:], t_cont, end, recording_id, counter)
            tokens += cont.get("tokens") or 0
            if cont["ok"]:
                cont_looped, cont_good = analyse_loop(cont["segments"], cont["finish"])
                segments = keep + cont_good
                finish = cont["finish"]
                if cont_looped:
                    status = "loop_truncated"
            else:
                logger.warning(f"Continuation of window at {offset:.0f}s failed: {cont.get('error')}")

    langs = [s.get("language") for s in segments if s.get("language")]
    language = max(set(langs), key=langs.count) if langs else "mixed"
    return {"status": status, "segments": segments, "language": language, "model": model_used,
            "finish_reason": finish, "output_tokens": tokens, "attempts": counter["attempts"], "error": None}


# ─── Persistence ─────────────────────────────────────────────────────────────

def _find_reusable(recording_id: str, model: str, start: float, end: float, fingerprint: str) -> Optional[Any]:
    with SessionLocal() as db:
        return db.execute(text("""
            SELECT segments, speech_sec, finish_reason, output_tokens, model
            FROM isf_recorder.transcription_chunk
            WHERE recording_id = :rid AND model = ANY(:models) AND status = 'done'
              AND audio_fingerprint = :fp
              AND ABS(start_sec - :s) <= :tol AND ABS(end_sec - :e) <= :tol
            ORDER BY updated_at DESC LIMIT 1
        """), {"rid": recording_id, "models": [m for m in (model, None if model.startswith("openai-") else RESCUE_MODEL) if m], "fp": fingerprint, "s": start, "e": end,
               "tol": REUSE_TOLERANCE_SECONDS}).fetchone()


def _save_window(run_id: Optional[str], recording_id: str, idx: int, start: float, end: float,
                 fingerprint: str, **fields) -> None:
    if not run_id:
        return
    params = {
        "run": run_id, "rid": recording_id, "idx": idx, "s": start, "e": end, "fp": fingerprint,
        "status": fields.get("status", "pending"), "speech": fields.get("speech_sec"),
        "attempts": fields.get("attempts", 0), "model": fields.get("model"),
        "segments": json.dumps(fields["segments"]) if fields.get("segments") is not None else None,
        "finish": fields.get("finish_reason"), "tokens": fields.get("output_tokens"),
        "err": (fields.get("error") or None) and str(fields.get("error"))[:500],
    }
    try:
        with SessionLocal() as db:
            db.execute(text("""
                INSERT INTO isf_recorder.transcription_chunk
                    (run_id, recording_id, idx, start_sec, end_sec, speech_sec, status, attempts, model,
                     segments, finish_reason, output_tokens, audio_fingerprint, error_message, updated_at)
                VALUES (:run, :rid, :idx, :s, :e, :speech, :status, :attempts, :model,
                        CAST(:segments AS jsonb), :finish, :tokens, :fp, :err, NOW())
                ON CONFLICT (run_id, idx) DO UPDATE SET
                    speech_sec = EXCLUDED.speech_sec, status = EXCLUDED.status, attempts = EXCLUDED.attempts,
                    model = EXCLUDED.model, segments = EXCLUDED.segments, finish_reason = EXCLUDED.finish_reason,
                    output_tokens = EXCLUDED.output_tokens, error_message = EXCLUDED.error_message,
                    updated_at = NOW()
            """), params)
            db.commit()
    except Exception as e:
        logger.warning(f"Could not save transcription window {idx} of run {run_id}: {e}")


# ─── Orchestrator ────────────────────────────────────────────────────────────

def _format_seconds(sec: float) -> str:
    s = max(0, int(sec))
    h, m, r = s // 3600, (s % 3600) // 60, s % 60
    return f"{h:02d}:{m:02d}:{r:02d}" if h else f"{m:02d}:{r:02d}"


def transcribe_chunked(audio_path: str, recording_id: str, model: str, run_id: Optional[str] = None,
                       client=None) -> Dict[str, Any]:
    """
    Returns {detected_language, full_transcript, confidence, speaker_count, segments, all_silent, stats}.
    Raises TranscriptionFailed if any window failed (finished windows stay saved for reuse on Retry).
    """
    from backend.transcription.gemini_stt import get_genai_client, deduplicate_segments
    if not os.path.exists(audio_path):
        raise FileNotFoundError(f"Audio file not found: {audio_path}")

    from backend.transcription.openai_stt import is_openai_model, transcribe_window_openai
    openai_engine = is_openai_model(model)  # no Gemini fallback/rescue for another vendor's engine
    models = [model] if openai_engine else list(dict.fromkeys([model] + ([FALLBACK_MODEL] if model != FALLBACK_MODEL else [])))
    fingerprint = audio_fingerprint(audio_path)
    fd, wav_path = tempfile.mkstemp(suffix=".wav", prefix=f"stt_{(recording_id or 'x')[:8]}_")
    os.close(fd)
    try:
        total = decode_to_wav(audio_path, wav_path)
        windows = plan_windows(total, CHUNK_SECONDS, OVERLAP_SECONDS)
        logger.info(f"Transcribing {recording_id}: {total:.1f}s in {len(windows)} window(s) with {model}")

        results: Dict[int, Dict[str, Any]] = {}
        todo = []
        for idx, start, end in windows:
            reused = _find_reusable(recording_id, model, start, end, fingerprint) if recording_id else None
            if reused is not None:
                segs = reused.segments if isinstance(reused.segments, list) else json.loads(reused.segments or "[]")
                results[idx] = {"idx": idx, "start": start, "end": end, "status": "done", "segments": segs,
                                "reused": True, "speech_sec": reused.speech_sec, "language": None}
                _save_window(run_id, recording_id, idx, start, end, fingerprint, status="done",
                             speech_sec=reused.speech_sec, model=reused.model, segments=segs,
                             finish_reason=reused.finish_reason, output_tokens=reused.output_tokens, attempts=0)
            else:
                _save_window(run_id, recording_id, idx, start, end, fingerprint, status="pending")
                todo.append((idx, start, end))

        holder = {"client": client}
        client_lock = threading.Lock()

        def _client():
            with client_lock:
                if holder["client"] is None:
                    holder["client"] = get_genai_client()
                return holder["client"]

        def _work(idx: int, start: float, end: float) -> Dict[str, Any]:
            pcm = read_pcm(wav_path, start, end)
            speech = vad.voice_seconds(pcm)
            if speech < vad.MIN_VOICE_SECONDS:
                _save_window(run_id, recording_id, idx, start, end, fingerprint, status="skipped_silent",
                             speech_sec=speech)
                return {"idx": idx, "start": start, "end": end, "status": "skipped_silent", "segments": [],
                        "speech_sec": speech}
            if openai_engine:
                res = transcribe_window_openai(model, pcm, start, end - start, recording_id)
            else:
                res = transcribe_window(_client(), models, pcm, start, end - start, recording_id)
            _save_window(run_id, recording_id, idx, start, end, fingerprint, speech_sec=speech, **res)
            return {"idx": idx, "start": start, "end": end, "speech_sec": speech, **res}

        if todo:
            with ThreadPoolExecutor(max_workers=min(PARALLEL_WINDOWS, len(todo)),
                                    thread_name_prefix=f"stt-{(recording_id or 'x')[:8]}") as pool:
                futures = []
                for idx, start, end in todo:
                    ctx = contextvars.copy_context()  # keeps current_run_id for usage tagging
                    futures.append(pool.submit(ctx.run, _work, idx, start, end))
                for fut in futures:
                    try:
                        r = fut.result()
                    except Exception as e:
                        logger.exception(f"Window task crashed: {e}")
                        continue
                    results[r["idx"]] = r
            for idx, start, end in todo:
                if idx not in results:
                    results[idx] = {"idx": idx, "start": start, "end": end, "status": "failed", "segments": [],
                                    "error": "internal error"}
                    _save_window(run_id, recording_id, idx, start, end, fingerprint, status="failed",
                                 error="internal error")
    finally:
        _remove(wav_path)

    ordered = [results[i] for i, _, _ in windows]
    stats = {
        "chunks_total": len(ordered),
        "skipped_silent_chunks": sum(1 for r in ordered if r["status"] == "skipped_silent"),
        "failed_chunks": sum(1 for r in ordered if r["status"] == "failed"),
        "reused_chunks": sum(1 for r in ordered if r.get("reused")),
        "loop_truncated_chunks": sum(1 for r in ordered if r["status"] == "loop_truncated"),
        "duration_seconds": round(total, 1),
    }
    logger.info(f"Windows for {recording_id}: {stats}")

    failed = [r for r in ordered if r["status"] == "failed"]
    if failed:
        first = next((r.get("error") for r in failed if r.get("error")), "unknown error")
        raise TranscriptionFailed(
            f"{len(failed)} of {len(ordered)} part(s) of the audio could not be transcribed ({first}). "
            f"Tap Retry: the finished parts are kept.", stats)

    if all(r["status"] == "skipped_silent" for r in ordered):
        return {"detected_language": "mixed", "full_transcript": "", "confidence": 0.0, "speaker_count": 0,
                "segments": [], "all_silent": True, "stats": stats}

    merged = merge_windows([{"idx": r["idx"], "start": r["start"], "end": r["end"],
                             "segments": r.get("segments") or []} for r in ordered])
    merged = deduplicate_segments(merged)

    langs = [r.get("language") for r in ordered if r.get("language")]
    language = max(set(langs), key=langs.count) if langs else "mixed"
    transcript = "\n".join(f"[{_format_seconds(s['start_time'])}] {s['speaker']}: {s['text']}" for s in merged)
    return {
        "detected_language": language,
        "full_transcript": transcript,
        "confidence": 0.95,
        "speaker_count": len(set(s["speaker"] for s in merged)),
        "segments": merged,
        "all_silent": False,
        "stats": stats,
    }
