"""
ISF Recorder — Gemini Speech-to-Text with Speaker Diarization
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Transcribes audio recordings using Gemini API (Gemini 2.5 Flash / 3.5 Transcribe).
Performs multi-speaker diarization, segment timestamps, and multi-lingual
transcription (Hindi, English, Marathi, Hinglish, Gujarati).

Audio is transcribed in 3-minute windows with 3 s overlap (transcription/chunked_stt.py):
silent windows are skipped by server VAD, each window is capped at 4096 output tokens, and
truncated JSON is repaired by regex recovery. This module keeps the prompt and parsing helpers.
"""
import os
import json
import logging
import re
import wave
from typing import Dict, Any, List, Tuple, Optional
from pathlib import Path
import subprocess

from backend.config import GEMINI_API_KEY, GEMINI_TRANSCRIBE_MODEL

logger = logging.getLogger("isf_recorder.gemini_stt")

# Output cap per 3-minute window (chunked_stt.WINDOW_MAX_OUTPUT_TOKENS); a loop costs at most this much.
MAX_OUTPUT_TOKENS = 4096


def get_genai_client():
    """Initializes and returns Google GenAI client."""
    from google import genai
    api_key = GEMINI_API_KEY or os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    if not api_key:
        raise ValueError("GEMINI_API_KEY is not configured. Please set GEMINI_API_KEY in environment or .env file.")
    # Per-request timeout: without it a stalled Gemini call hangs the transcription forever
    # (seen in a real run). Timeouts are retried by the callers.
    timeout_ms = int(os.getenv("GEMINI_HTTP_TIMEOUT_MS", "180000"))
    return genai.Client(api_key=api_key, http_options=genai.types.HttpOptions(timeout=timeout_ms))


TRANSCRIPTION_PROMPT = """You are an expert multilingual audio transcription engine with speaker diarization.
Your task is to transcribe the provided audio recording with maximum accuracy, speaker identification, and segment timestamps.

CONTEXT & LANGUAGE GUIDELINES:
- This audio may contain Indian English, Hindi, Marathi, Hinglish (mixed Hindi-English), or other Indian languages.
- Transcribe accurately in natural speech. Transcribe Hindi/Marathi in Devanagari or standard Romanized script as spoken, preserving spoken words.
- Do not omit filler words or interruptions if they change the meaning.

SPEAKER DIARIZATION & TIMESTAMPS:
- Accurately identify different speakers as SPEAKER_00, SPEAKER_01, SPEAKER_02, SPEAKER_03, etc.
- If a speaker introduces themselves or is addressed by name in the audio (e.g. "Arnav", "Rahul", "Pooja", "Yadav"), label them with their name in parentheses like "SPEAKER_00 (Rahul)".
- There is NO LIMIT on the number of speakers.
- Provide start_time and end_time for each segment in seconds relative to this audio file (floating point numbers).

CRITICAL ANTI-HALLUCINATION & ANTI-REPETITION RULES:
- If there is silence, music, humming, ambient room noise, or inaudible audio, DO NOT invent words or output repeating phrases.
- NEVER repeat the same word, phrase, or conversational loop (e.g., repeating "Yes", "Okay", or identical words across consecutive segments).
- Only output segments for genuine human speech. If no one is speaking in a duration, emit NO segments for that duration.

OUTPUT FORMAT:
Return ONLY a valid, parseable JSON object matching this schema exactly (no markdown code blocks, no introductory text):
{
  "detected_language": "hi | en | mr | mixed",
  "confidence": 0.95,
  "speaker_count": 2,
  "segments": [
    {
      "speaker": "SPEAKER_00",
      "start_time": 0.0,
      "end_time": 4.2,
      "text": "spoken words here",
      "language": "hi"
    }
  ]
}
"""


def clean_json_response(raw_text: str) -> str:
    """Strips markdown code fences and whitespace from Gemini response."""
    text = raw_text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n", "", text)
        text = re.sub(r"\n```$", "", text)
    return text.strip()


def _as_seconds(value: Any, default: float) -> float:
    """Timestamp from a Gemini segment: number, numeric string or "MM:SS"/"HH:MM:SS"; default if missing or bad."""
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str) and value.strip():
        text_val = value.strip()
        try:
            return float(text_val)
        except ValueError:
            parts = text_val.split(":")
            try:
                if 2 <= len(parts) <= 3:
                    total = 0.0
                    for part in parts:
                        total = total * 60 + float(part)
                    return total
            except ValueError:
                pass
    return float(default)


def format_seconds(sec: float) -> str:
    """Formats seconds into MM:SS or HH:MM:SS string."""
    s = max(0, int(sec))
    h = s // 3600
    m = (s % 3600) // 60
    sec_rem = s % 60
    if h > 0:
        return f"{h:02d}:{m:02d}:{sec_rem:02d}"
    return f"{m:02d}:{sec_rem:02d}"


def parse_transcription_json(raw_text: str) -> Dict[str, Any]:
    """
    Robust JSON parser for Gemini transcription outputs.
    1. Tries standard json.loads
    2. If truncated / malformed, extracts all valid {speaker, start_time, end_time, text} segment objects via regex
    3. Never dumps raw unparsed JSON into user-facing transcript bubbles
    """
    cleaned = clean_json_response(raw_text)

    # 1. Standard JSON parse
    try:
        data = json.loads(cleaned)
        if isinstance(data, dict) and "segments" in data and isinstance(data["segments"], list):
            return data
    except Exception as jde:
        logger.warning(f"Standard JSON decode failed ({jde}), attempting regex segment recovery...")

    # 2. Resilient regex segment recovery
    segments = []
    seg_pattern = re.compile(
        r'\{\s*"speaker"\s*:\s*"([^"]+)"\s*,\s*"start_time"\s*:\s*([0-9.]+)\s*,\s*"end_time"\s*:\s*([0-9.]+)\s*,\s*"text"\s*:\s*"((?:[^"\\]|\\.)*)"',
        re.DOTALL
    )
    for m in seg_pattern.finditer(cleaned):
        spk = m.group(1).strip()
        try:
            st = float(m.group(2))
            et = float(m.group(3))
            raw_txt = m.group(4)
            try:
                txt = json.loads(f'"{raw_txt}"')
            except Exception:
                txt = raw_txt.replace('\\"', '"').replace('\\n', ' ')

            if txt.strip():
                segments.append({
                    "speaker": spk,
                    "start_time": st,
                    "end_time": et,
                    "text": txt.strip(),
                    "language": "mixed"
                })
        except Exception:
            continue

    # Extract detected_language if available
    detected_lang = "mixed"
    lang_match = re.search(r'"detected_language"\s*:\s*"([a-zA-Z_-]+)"', cleaned)
    if lang_match:
        detected_lang = lang_match.group(1).lower()

    if segments:
        logger.info(f"Regex recovery successfully salvaged {len(segments)} segments from truncated response")
        return {
            "detected_language": detected_lang,
            "confidence": 0.85,
            "speaker_count": len(set(s["speaker"] for s in segments)),
            "segments": segments
        }

    # 3. Nothing readable: return no segments and flag it. Never turn unparsed model output into
    #    placeholder transcript text; the caller treats this window as failed.
    logger.warning("Transcription output could not be parsed; no segments recovered")
    return {
        "detected_language": detected_lang,
        "confidence": 0.0,
        "speaker_count": 0,
        "segments": [],
        "parse_failed": True,
    }


# Repetition-loop filter limits. A real conversation almost never has 12 lines in a row with only 3 different
# texts, or 40 lines in a row where fewer than 40% are different; Gemini loops do (often hundreds of lines).
SHORT_LOOP_WINDOW = 12
SHORT_LOOP_MAX_UNIQUE = 3
LONG_LOOP_WINDOW = 40
LONG_LOOP_MAX_UNIQUE_RATIO = 0.4
INLINE_MAX_REPEATS = 3


def _norm_text(text_val: str) -> str:
    return re.sub(r'\s+', ' ', re.sub(r'[^\w\s]', '', text_val or '')).strip().lower()


def _collapse_inline_repeats(text_val: str) -> str:
    """'हो हो हो हो हो' or 'ok fine ok fine ok fine ok fine' inside one segment -> a single copy."""
    words = text_val.split()
    keys = [_norm_text(w) for w in words]
    n = len(keys)
    out, i = [], 0
    while i < n:
        skipped = False
        for size in range(1, 7):
            unit = keys[i:i + size]
            if len(unit) < size or not all(unit):
                break
            reps = 1
            while keys[i + reps * size:i + (reps + 1) * size] == unit:
                reps += 1
            if reps > INLINE_MAX_REPEATS:
                out.extend(words[i:i + size])
                i += reps * size
                skipped = True
                break
        if not skipped:
            out.append(words[i])
            i += 1
    return " ".join(out) if out else text_val


def deduplicate_segments(segments: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Filters out degenerative LLM repetition loops:
    - the same words repeated inside one segment
    - the same line repeated back to back
    - a few lines cycling for a long stretch (A, B, A, A, B, ... across speakers, or a whole block of
      lines repeated dozens of times); inside such a stretch only the first copy of each line is kept
    """
    if not segments:
        return segments

    # Pass 0: collapse repeats inside a single segment
    cleaned = []
    for s in segments:
        text_val = (s.get("text") or "").strip()
        if not text_val:
            continue
        collapsed = _collapse_inline_repeats(text_val)
        cleaned.append({**s, "text": collapsed} if collapsed != text_val else s)

    # Pass 1: collapse consecutive identical text (allow max 2 consecutive repeats)
    stage1 = []
    repeat_count = 0
    last_norm = None
    for s in cleaned:
        norm = _norm_text(s["text"])
        if norm and norm == last_norm:
            repeat_count += 1
            if repeat_count > 2:
                continue
        else:
            repeat_count = 1
            last_norm = norm
        stage1.append(s)

    # Pass 2: find loop stretches with sliding windows over the line texts
    norms = [_norm_text(s["text"]) for s in stage1]
    n = len(norms)
    loopy = [False] * n
    for size, max_unique in ((SHORT_LOOP_WINDOW, SHORT_LOOP_MAX_UNIQUE),
                             (LONG_LOOP_WINDOW, int(LONG_LOOP_WINDOW * LONG_LOOP_MAX_UNIQUE_RATIO))):
        if n < size:
            continue
        counts: Dict[str, int] = {}
        for k in range(size):
            counts[norms[k]] = counts.get(norms[k], 0) + 1
        for start in range(n - size + 1):
            if start:
                gone = norms[start - 1]
                counts[gone] -= 1
                if not counts[gone]:
                    del counts[gone]
                counts[norms[start + size - 1]] = counts.get(norms[start + size - 1], 0) + 1
            if len(counts) <= max_unique:
                for k in range(start, start + size):
                    loopy[k] = True

    final_segments = []
    seen_in_stretch: set = set()
    in_stretch = False
    stretches = pruned = 0
    for idx, s in enumerate(stage1):
        if loopy[idx]:
            if not in_stretch:
                in_stretch, seen_in_stretch = True, set()
                stretches += 1
            if norms[idx] in seen_in_stretch:
                pruned += 1
                continue
            seen_in_stretch.add(norms[idx])
        else:
            in_stretch = False
        final_segments.append(s)

    if pruned:
        logger.warning(f"Pruned {pruned} repeated lines in {stretches} repetition-loop stretch(es)")

    # Pass 3: short alternating 2-phrase loops too brief for the windows above (A, B, A, B, ...)
    result = []
    i = 0
    n = len(final_segments)
    while i < n:
        if i + 3 < n:
            t0, t1, t2, t3 = (_norm_text(final_segments[i + k]["text"]) for k in range(4))
            if t0 == t2 and t1 == t3 and t0 != t1:
                loop_len = 0
                while (i + loop_len + 1 < n
                       and _norm_text(final_segments[i + loop_len]["text"]) == t0
                       and _norm_text(final_segments[i + loop_len + 1]["text"]) == t1):
                    loop_len += 2
                if loop_len >= 4:
                    result.extend(final_segments[i:i + 2])
                    logger.warning(f"Pruned alternating repetition loop of {loop_len} segments ('{t0}' / '{t1}')")
                    i += loop_len
                    continue
        result.append(final_segments[i])
        i += 1
    return result


def get_audio_duration_seconds(audio_path: str) -> float:
    """Gets audio duration in seconds using wave module for WAV, or ffprobe for M4A/other formats."""
    if audio_path.lower().endswith(".wav"):
        try:
            with wave.open(audio_path, "rb") as w:
                frames = w.getnframes()
                rate = w.getframerate()
                if rate > 0:
                    return frames / float(rate)
        except Exception:
            pass

    # Try ffprobe
    from backend.recordings.chunk_assembler import find_binary
    ffprobe_bin = find_binary("ffprobe")
    try:
        cmd = [
            ffprobe_bin,
            "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            audio_path
        ]
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=15)
        if res.returncode == 0 and res.stdout.strip():
            return float(res.stdout.strip())
    except Exception as e:
        logger.warning(f"ffprobe duration check failed for {audio_path}: {e}")

    # Fallback estimate based on file size if m4a (64kbps = 8000 bytes/sec)
    try:
        size = os.path.getsize(audio_path)
        if audio_path.lower().endswith((".m4a", ".mp4", ".aac")):
            return size / 8000.0
        return size / 32000.0
    except Exception:
        return 0.0


from backend.transcription.usage_tracker import track_gemini_call


async def transcribe_audio_file(audio_path: str, recording_id: Optional[str] = None, org_id: Optional[str] = None,
                                model: Optional[str] = None, run_id: Optional[str] = None) -> Dict[str, Any]:
    """
    Main Gemini transcription entrypoint: 3-minute windows with 3 s overlap, server VAD skip of
    silent windows, inline audio, per-window reuse (see transcription/chunked_stt.py).
    Returns { detected_language, full_transcript, confidence, speaker_count, segments, all_silent, stats }.
    Raises chunked_stt.TranscriptionFailed when a window could not be transcribed.
    """
    from backend.settings.model_resolver import get_transcription_model
    from backend.transcription.chunked_stt import transcribe_chunked
    active_model = model or get_transcription_model(org_id)
    return transcribe_chunked(audio_path, recording_id=recording_id, model=active_model, run_id=run_id)
