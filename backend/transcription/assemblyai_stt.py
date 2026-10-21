"""
ISF Recorder — AssemblyAI Speech-to-Text Engine
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Alternative cloud STT provider alongside Google Gemini.
Supports:
- High-accuracy multilingual speech transcription (Hindi, Marathi, English, mixed)
- Speaker Diarization (multi-speaker detection with Speaker A/B labels)
- Word and sentence-level timestamps
- Models: Universal-3.5 Pro (best, Hindi/Marathi) and Universal-2 (cheaper)
"""
import os
import time
import json
import logging
import asyncio
from datetime import datetime, timezone
from typing import Dict, Any, Optional

import httpx

from backend.config import ASSEMBLYAI_API_KEY

logger = logging.getLogger("isf_recorder.assemblyai_stt")

BASE_URL = "https://api.assemblyai.com/v2"

# Our model id -> AssemblyAI `speech_models` (first one AssemblyAI supports for the language is used).
# Universal-3 Pro is left out on purpose: it has no Hindi/Marathi and silently falls back to Universal-2.
SPEECH_MODELS = {
    "assemblyai-universal-3-5-pro": ["universal-3-5-pro", "universal-2"],
    "assemblyai-universal-2": ["universal-2"],
}


def _get_api_key() -> str:
    key = ASSEMBLYAI_API_KEY or os.getenv("ASSEMBLYAI_API_KEY", "")
    if not key:
        raise ValueError(
            "AssemblyAI API key not configured. Set ASSEMBLYAI_API_KEY in .env "
            "or choose a Google Gemini model from Settings."
        )
    return key


async def upload_audio_file(audio_path: str, api_key: str) -> str:
    """
    Uploads local audio file to AssemblyAI's secure storage.
    Returns the temporary upload_url for transcription.
    """
    headers = {
        "authorization": api_key,
        "content-type": "application/octet-stream",
    }

    file_size = os.path.getsize(audio_path)
    logger.info(f"Uploading {audio_path} ({file_size / (1024*1024):.2f} MB) to AssemblyAI...")

    async with httpx.AsyncClient(timeout=600.0) as client:
        with open(audio_path, "rb") as f:
            resp = await client.post(
                f"{BASE_URL}/upload",
                headers=headers,
                content=f.read(),
            )

        if resp.status_code != 200:
            raise RuntimeError(f"AssemblyAI upload failed ({resp.status_code}): {resp.text}")

        data = resp.json()
        upload_url = data.get("upload_url")
        if not upload_url:
            raise RuntimeError(f"No upload_url in AssemblyAI response: {data}")

        return upload_url


async def transcribe_audio_file(
    audio_path: str,
    model: str = "assemblyai-universal-2",
    recording_id: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Transcribes an audio file using AssemblyAI.
    Returns standardized STT dictionary matching Gemini STT output format:
    {
        "full_transcript": str,
        "detected_language": str,
        "confidence": float,
        "speaker_count": int,
        "segments": [{"speaker": str, "start_time": float, "end_time": float, "text": str, "language": str}],
        "stt_engine": "assemblyai"
    }
    """
    api_key = _get_api_key()

    # Old ids (assemblyai-universal-1 / -nano) always ran Universal-2
    speech_models = SPEECH_MODELS.get(model, ["universal-2"])

    # Step 1: Upload audio
    upload_url = await upload_audio_file(audio_path, api_key)

    # Step 2: Request transcription with speaker diarization
    headers = {
        "authorization": api_key,
        "content-type": "application/json",
    }

    transcript_request = {
        "audio_url": upload_url,
        "speaker_labels": True,
        "language_detection": True,
        "speech_models": speech_models,
        "punctuate": True,
        "format_text": True,
    }

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            f"{BASE_URL}/transcript",
            headers=headers,
            json=transcript_request,
        )

        if resp.status_code not in (200, 201):
            raise RuntimeError(f"AssemblyAI transcript request failed ({resp.status_code}): {resp.text}")

        job = resp.json()
        transcript_id = job.get("id")
        if not transcript_id:
            raise RuntimeError(f"No transcript ID in AssemblyAI response: {job}")

    logger.info(f"AssemblyAI transcription job queued (id={transcript_id}, models={speech_models})")

    # Step 3: Poll for completion
    poll_url = f"{BASE_URL}/transcript/{transcript_id}"
    poll_headers = {"authorization": api_key}
    max_wait_seconds = 1800  # 2-3 hour meetings can take well over 10 minutes
    start_time = time.time()

    completed_data = None
    async with httpx.AsyncClient(timeout=30.0) as client:
        while (time.time() - start_time) < max_wait_seconds:
            await asyncio.sleep(3)
            p_resp = await client.get(poll_url, headers=poll_headers)
            if p_resp.status_code != 200:
                continue

            status_data = p_resp.json()
            status = status_data.get("status")

            if status == "completed":
                completed_data = status_data
                break
            elif status == "error":
                err_msg = status_data.get("error", "Unknown AssemblyAI error")
                if "no spoken audio" in err_msg.lower():
                    logger.info("AssemblyAI detected no spoken audio in file.")
                    return {
                        "full_transcript": "No spoken audio detected in this recording.",
                        "detected_language": "en",
                        "confidence": 0.0,
                        "speaker_count": 0,
                        "segments": [],
                        "stt_engine": "assemblyai",
                        "model": model,
                    }
                raise RuntimeError(f"AssemblyAI transcription failed: {err_msg}")
            else:
                logger.debug(f"AssemblyAI job {transcript_id} status: {status}")

    if not completed_data:
        raise TimeoutError(f"AssemblyAI transcription timed out after {max_wait_seconds}s (job={transcript_id})")

    # Step 4: Format segments into standardized structure
    full_transcript = completed_data.get("text", "") or ""
    detected_lang = completed_data.get("language_code", "en") or "en"
    confidence = float(completed_data.get("confidence") or 0.9)

    utterances = completed_data.get("utterances") or []
    segments = []
    speakers = set()

    if utterances:
        for u in utterances:
            spk = f"Speaker {u.get('speaker', '1')}"
            speakers.add(spk)
            st_sec = round(float(u.get("start", 0)) / 1000.0, 2)
            et_sec = round(float(u.get("end", 0)) / 1000.0, 2)
            txt = (u.get("text") or "").strip()
            if txt:
                segments.append({
                    "speaker": spk,
                    "start_time": st_sec,
                    "end_time": et_sec,
                    "text": txt,
                    "language": detected_lang,
                })
    else:
        # Fallback to single segment if utterances not generated
        words = completed_data.get("words") or []
        if words:
            # Group words by speaker or chunk into ~10s blocks
            current_speaker = "Speaker 1"
            current_words = []
            block_start = 0.0
            block_end = 0.0

            for w in words:
                w_spk = f"Speaker {w.get('speaker', '1')}"
                w_start = round(float(w.get("start", 0)) / 1000.0, 2)
                w_end = round(float(w.get("end", 0)) / 1000.0, 2)
                w_text = w.get("text", "")

                if not current_words:
                    block_start = w_start
                    current_speaker = w_spk

                if w_spk != current_speaker or (w_end - block_start) > 12.0:
                    if current_words:
                        segments.append({
                            "speaker": current_speaker,
                            "start_time": block_start,
                            "end_time": block_end,
                            "text": " ".join(current_words),
                            "language": detected_lang,
                        })
                    current_words = [w_text]
                    current_speaker = w_spk
                    block_start = w_start
                    block_end = w_end
                else:
                    current_words.append(w_text)
                    block_end = w_end

            if current_words:
                segments.append({
                    "speaker": current_speaker,
                    "start_time": block_start,
                    "end_time": block_end,
                    "text": " ".join(current_words),
                    "language": detected_lang,
                })
        else:
            segments.append({
                "speaker": "Speaker 1",
                "start_time": 0.0,
                "end_time": round(float(completed_data.get("audio_duration", 0)), 2),
                "text": full_transcript,
                "language": detected_lang,
            })

    speaker_count = len(speakers) if speakers else 1
    logger.info(
        f"AssemblyAI transcription completed: {len(segments)} segments, "
        f"{speaker_count} speakers, detected_lang={detected_lang}"
    )

    # Log usage into gemini_usage_log if recording_id is provided
    if recording_id:
        try:
            from backend.database import SessionLocal
            from backend.transcription.forex_sync import get_live_usd_to_inr_rate
            from backend.transcription.usage_tracker import get_model_pricing, log_gemini_call

            db_sess = SessionLocal()
            try:
                audio_dur = float(completed_data.get("audio_duration") or 0.0)
                elapsed_ms = int((time.time() - start_time) * 1000)
                ex_rate = get_live_usd_to_inr_rate(db_sess)

                # Billed by the model AssemblyAI actually used (3.5 Pro can fall back to Universal-2);
                # its per-audio-hour price comes from gemini_model_pricing
                used_model = completed_data.get("speech_model_used") or speech_models[-1]
                pricing = get_model_pricing(db_sess, f"assemblyai-{used_model}")
                cost_usd = round(audio_dur / 3600.0 * pricing["audio_hour_price"], 6)
                cost_inr = round(cost_usd * ex_rate, 4)

                tok_count = max(1, len(full_transcript) // 4)

                log_gemini_call(
                    recording_id=recording_id,
                    operation_type="TRANSCRIPTION",
                    gemini_model=model,
                    gemini_request_id=f"assemblyai_{transcript_id}",
                    request_timestamp=datetime.fromtimestamp(start_time, tz=timezone.utc),
                    response_timestamp=datetime.now(timezone.utc),
                    duration_ms=elapsed_ms,
                    tokens={
                        "input_tokens": tok_count,
                        "output_tokens": tok_count,
                        "total_tokens": tok_count * 2,
                        "input_audio_tokens": int(audio_dur * 100),
                        "cached_input_tokens": 0,
                        "thinking_tokens": 0,
                        "raw_json": {
                            "assemblyai_id": transcript_id,
                            "audio_duration": audio_dur,
                            "confidence": confidence,
                            "speech_models": speech_models,
                            "speech_model_used": used_model,
                        }
                    },
                    costs={
                        "input_cost_usd": cost_usd,
                        "audio_cost_usd": 0.0,
                        "cached_cost_usd": 0.0,
                        "output_cost_usd": 0.0,
                        "total_cost_usd": cost_usd,
                        "total_cost_inr": cost_inr,
                    },
                    pricing_id=pricing.get("id"),
                    exchange_rate=ex_rate,
                    api_status="SUCCESS",
                    error_message=None,
                    audio_duration_seconds=audio_dur,
                    db=db_sess
                )
            finally:
                db_sess.close()
        except Exception as ue:
            logger.warning(f"Could not log AssemblyAI usage: {ue}")

    return {
        "full_transcript": full_transcript,
        "detected_language": detected_lang,
        "confidence": confidence,
        "speaker_count": speaker_count,
        "segments": segments,
        "stt_engine": "assemblyai",
        "model": model,
    }
