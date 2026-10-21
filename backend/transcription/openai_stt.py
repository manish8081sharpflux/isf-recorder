"""
ISF Recorder — OpenAI speech-to-text as a per-window engine of the chunked pipeline
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Model ids in our settings: "openai-gpt-4o-transcribe-diarize" and "openai-gpt-transcribe" (the API id is
the part after "openai-"). The pipeline (chunked_stt.transcribe_chunked) keeps doing everything else:
180 s windows with 3 s overlap, server VAD skip, overlap dedupe, window persistence and reuse.

Official behaviour (developers.openai.com/api/docs/guides/speech-to-text, checked 2026-10-12):
- POST /v1/audio/transcriptions, multipart, files up to 25 MB (a 180 s 16 kHz mono WAV is ~5.8 MB).
- gpt-4o-transcribe-diarize: response_format="diarized_json" -> segments [{id, speaker ("A","B"..),
  start, end, text, type}]; chunking_strategy is required for audio longer than 30 s ("auto").
  No prompt support. Max output 2,000 tokens per request.
- gpt-transcribe: response JSON {text, languages, usage} — no timestamps and no speakers. We split the
  text into sentences and spread them over the window by length (times are ESTIMATES; one speaker).
Usage: {"type": "tokens", input_tokens, output_tokens, input_token_details{audio_tokens,text_tokens}} or
{"type": "duration", "seconds"}. Prices live in gemini_model_pricing (never in code).
"""
import io
import logging
import os
import re
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger("isf_recorder.openai_stt")

API_URL = os.getenv("OPENAI_TRANSCRIBE_URL", "https://api.openai.com/v1/audio/transcriptions")
TIMEOUT_SECONDS = float(os.getenv("OPENAI_STT_TIMEOUT", "300"))
OPENAI_CALL_SEMAPHORE = threading.BoundedSemaphore(int(os.getenv("OPENAI_MAX_PARALLEL_CALLS", "8")))
_DEVANAGARI = re.compile(r"[ऀ-ॿ]")
_SENTENCE_END = re.compile(r"(?<=[.!?।॥])\s+")


def is_openai_model(model: Optional[str]) -> bool:
    return bool(model) and model.startswith("openai-")


def api_model(model: str) -> str:
    return model[len("openai-"):] if is_openai_model(model) else model


class OpenAIError(Exception):
    def __init__(self, message: str, code: Optional[int] = None):
        super().__init__(message)
        self.code = code


def _post(model: str, wav_bytes: bytes, http: Optional[httpx.Client] = None) -> Dict[str, Any]:
    key = os.getenv("OPENAI_API_KEY", "").strip()
    if not key:
        raise OpenAIError("OPENAI_API_KEY is not set on the server", 400)
    data = {"model": api_model(model)}
    if "diarize" in model:
        data["response_format"] = "diarized_json"
        data["chunking_strategy"] = "auto"
    else:
        data["response_format"] = "json"
    files = {"file": ("window.wav", io.BytesIO(wav_bytes), "audio/wav")}
    client = http or httpx.Client(timeout=TIMEOUT_SECONDS)
    try:
        with OPENAI_CALL_SEMAPHORE:
            resp = client.post(API_URL, headers={"Authorization": f"Bearer {key}"}, data=data, files=files)
    finally:
        if http is None:
            client.close()
    if resp.status_code != 200:
        try:
            msg = resp.json().get("error", {}).get("message") or resp.text
        except Exception:
            msg = resp.text
        raise OpenAIError(f"OpenAI {resp.status_code}: {str(msg)[:300]}", resp.status_code)
    return resp.json()


def _language(text: str) -> str:
    return "mixed" if _DEVANAGARI.search(text or "") else "en"


def _speaker(label: Any) -> str:
    s = str(label or "").strip()
    if len(s) == 1 and s.isalpha():
        return f"SPEAKER_{ord(s.upper()) - ord('A'):02d}"
    m = re.match(r"^(?:speaker[_ ]?)?(\d+)$", s, re.IGNORECASE)
    if m:
        return f"SPEAKER_{int(m.group(1)):02d}"
    return s or "SPEAKER_00"


def segments_from_response(model: str, body: Dict[str, Any], offset: float, duration: float) -> List[Dict[str, Any]]:
    """Maps an OpenAI response to our segments (absolute times inside the recording)."""
    end_of_window = offset + duration
    out: List[Dict[str, Any]] = []
    if isinstance(body.get("segments"), list) and body["segments"]:
        for seg in body["segments"]:
            text = " ".join(str(seg.get("text") or "").split())
            if not text:
                continue
            start = offset + float(seg.get("start") or 0.0)
            end = offset + float(seg.get("end") or seg.get("start") or 0.0)
            out.append({"speaker": _speaker(seg.get("speaker")), "text": text,
                        "start_time": round(min(max(start, offset), end_of_window), 2),
                        "end_time": round(min(max(end, start), end_of_window), 2), "language": _language(text)})
        return out
    text = " ".join(str(body.get("text") or "").split())
    if not text:
        return []
    # gpt-transcribe returns plain text: one speaker, sentence times estimated by length
    sentences = [s for s in _SENTENCE_END.split(text) if s.strip()] or [text]
    total_chars = sum(len(s) for s in sentences) or 1
    t = offset
    for s in sentences:
        span = duration * len(s) / total_chars
        out.append({"speaker": "SPEAKER_00", "text": s.strip(), "start_time": round(t, 2),
                    "end_time": round(min(t + span, end_of_window), 2), "language": _language(s),
                    "estimated_times": True})
        t += span
    return out


def _log_usage(model: str, body: Dict[str, Any], seconds: float, recording_id: Optional[str],
               started: float, status: str = "SUCCESS", error: Optional[str] = None) -> None:
    if not recording_id:
        return
    try:
        from backend.database import SessionLocal
        from backend.transcription.forex_sync import get_live_usd_to_inr_rate
        from backend.transcription.usage_tracker import get_model_pricing, log_gemini_call, calculate_cost
        usage = (body or {}).get("usage") or {}
        details = usage.get("input_token_details") or {}
        audio_tok = int(details.get("audio_tokens") or 0)
        text_tok = int(details.get("text_tokens") or 0)
        in_tok = int(usage.get("input_tokens") or 0)
        if in_tok and not (audio_tok or text_tok):
            audio_tok = in_tok
        out_tok = int(usage.get("output_tokens") or 0)
        billed_seconds = float(usage.get("seconds") or seconds)
        tokens = {"input_tokens": text_tok, "input_audio_tokens": audio_tok, "cached_input_tokens": 0,
                  "output_tokens": out_tok, "thinking_tokens": 0, "total_tokens": in_tok + out_tok,
                  "raw_json": {"usage": usage, "billed_seconds": billed_seconds}}
        with SessionLocal() as db:
            pricing = get_model_pricing(db, model)
            rate = get_live_usd_to_inr_rate(db)
            if pricing["audio_hour_price"] > 0:  # billed per audio minute (gpt-transcribe)
                usd = round(billed_seconds / 3600.0 * pricing["audio_hour_price"], 6)
                costs = {"input_cost_usd": 0.0, "audio_cost_usd": usd, "cached_cost_usd": 0.0,
                         "output_cost_usd": 0.0, "total_cost_usd": usd, "total_cost_inr": round(usd * rate, 4)}
            else:  # billed per token (gpt-4o-transcribe-diarize)
                costs = calculate_cost(tokens, pricing, rate)
            log_gemini_call(recording_id=recording_id, operation_type="TRANSCRIPTION", gemini_model=model,
                            gemini_request_id=f"openai_{int(started * 1000)}",
                            request_timestamp=datetime.fromtimestamp(started, tz=timezone.utc),
                            response_timestamp=datetime.now(timezone.utc),
                            duration_ms=int((time.time() - started) * 1000), tokens=tokens, costs=costs,
                            pricing_id=pricing["id"], exchange_rate=rate, api_status=status, error_message=error,
                            audio_duration_seconds=seconds, db=db)
    except Exception as e:
        logger.warning(f"Could not log OpenAI usage: {e}")


def transcribe_window_openai(model: str, pcm: bytes, offset: float, duration: float,
                             recording_id: Optional[str], http: Optional[httpx.Client] = None) -> Dict[str, Any]:
    """Same result shape as chunked_stt.transcribe_window:
    {status: done|failed, segments, language, model, finish_reason, output_tokens, attempts, error}."""
    from backend.transcription import chunked_stt as cs
    wav_bytes = cs.pcm_to_wav_bytes(pcm)
    attempts = 0
    other_tries = rate_tries = 0
    while True:
        attempts += 1
        started = time.time()
        try:
            body = _post(model, wav_bytes, http)
            break
        except Exception as e:
            code = getattr(e, "code", None)
            retryable = code in (429, 500, 502, 503, 504) or isinstance(e, (httpx.TransportError, TimeoutError))
            if retryable and code == 429 and rate_tries < len(cs.RATE_LIMIT_BACKOFF_SECONDS):
                wait = cs.RATE_LIMIT_BACKOFF_SECONDS[rate_tries]
                rate_tries += 1
            elif retryable and code != 429 and other_tries < len(cs.RETRY_BACKOFF_SECONDS):
                wait = cs.RETRY_BACKOFF_SECONDS[other_tries]
                other_tries += 1
            else:
                _log_usage(model, {}, 0.0, recording_id, started, status="ERROR", error=str(e)[:300])
                logger.warning(f"OpenAI window at {offset:.0f}s failed: {e}")
                return {"status": "failed", "segments": [], "language": None, "model": model, "finish_reason": None,
                        "output_tokens": 0, "attempts": attempts, "error": str(e)[:300]}
            logger.warning(f"{model} transient error ({e}); retrying in {wait:.0f}s")
            cs._sleep(wait)
    _log_usage(model, body, duration, recording_id, started)
    segments = segments_from_response(model, body, offset, duration)
    usage = body.get("usage") or {}
    langs = [s["language"] for s in segments]
    return {"status": "done", "segments": segments,
            "language": max(set(langs), key=langs.count) if langs else "mixed",
            "model": model, "finish_reason": "STOP", "output_tokens": int(usage.get("output_tokens") or 0),
            "attempts": attempts, "error": None}
