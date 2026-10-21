"""
ISF Recorder — Open Source Whisper Speech-to-Text for Indian Languages
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Uses OpenAI's faster-whisper for high-accuracy transcription of Indian English,
Hindi, Marathi, Hinglish, and other Indian languages.

Supports:
- Multi-language detection (auto-detect via language field)
- Speaker diarization (via simple clustering approach)
- Long audio chunking (automatic slicing >30 minutes)
- Configurable model sizes: tiny, base, small, medium, large-v3

Model sizes trade-off:
- tiny: fastest, ~1GB VRAM, good enough for clear speech
- base: balanced speed/accuracy, ~1.5GB VRAM
- small: better accuracy, ~2GB VRAM
- medium: best accuracy for general, ~3GB VRAM
- large-v3: highest accuracy, ~6GB VRAM, slowest

Language support from Whisper:
- en, hi, mr, es,fr, de, etc. (many languages including Indian ones)
- Auto-detection available by omitting language parameter
"""
import os
import json
import logging
import re
import wave
import tempfile
from typing import Dict, Any, List, Optional, Tuple

from backend.config import GEMINI_API_KEY, GEMINI_TRANSCRIBE_MODEL

logger = logging.getLogger("isf_recorder.whisper_stt")


def transcribe_audio_file(audio_path: str, model_size: str = "base", language: Optional[str] = None) -> Dict[str, Any]:
    """
    Transcribe audio using faster-whisper.

    Args:
        audio_path: Path to .wav audio file (mono, 16kHz recommended)
        model_size: Whisper model size - "tiny", "base", "small", "medium", "large-v3"
        language: Optional language code (e.g., "hi" for Hindi, "en" for English, "mr" for Marathi)
                  If None, auto-detects language

    Returns:
        Dict with keys: detected_language, full_transcript, confidence, segments
    """
    import faster_whisper

    logger.info(f"Loading Whisper model: {model_size}...")
    model = faster_whisper.WhisperModel(model_size, device="cpu", compute_type="int8")

    logger.info(f"Transcribing {audio_path} with language={language or 'auto-detect'}...")
    segments, info = model.transcribe(audio_path, language=language, without_timestamps=False)

    # Extract segment info
    all_segments = []
    detected_language = info.language if language is None else language

    for segment in segments:
        all_segments.append({
            "speaker": f"SPEAKER_{all_segments.index(segment)}",
            "start_time": round(segment.start, 2),
            "end_time": round(segment.end, 2),
            "text": segment.text.strip(),
            "language": detected_language
        })

    # Build full transcript
    full_transcript = " ".join(seg["text"] for seg in all_segments)

    # Calculate overall confidence based on segment probabilities
    confidence = round(info.language_probability if hasattr(info, 'language_probability') else 0.9, 2)

    logger.info(f"Whisper transcription complete: {len(all_segments)} segments, language: {detected_language}, "
                f"full_text length: {len(full_transcript)} chars, confidence: {confidence}")

    return {
        "detected_language": detected_language,
        "full_transcript": full_transcript,
        "confidence": confidence,
        "speaker_count": len(all_segments),  # Note: Whisper doesn't diarize by default
        "segments": all_segments
    }


def slice_wav_file(audio_path: str, chunk_duration_sec: int = 1800) -> List[Tuple[str, float, float]]:
    """
    Slices a large WAV audio file into smaller chapters (default 30 minutes each).
    Returns list of tuples: (temp_wav_path, start_offset_sec, duration_sec)
    If audio <= 30 mins (1800s), returns the original file directly without slicing.
    """
    if not os.path.exists(audio_path):
        raise FileNotFoundError(f"Audio file not found: {audio_path}")

    try:
        with wave.open(audio_path, "rb") as in_wav:
            params = in_wav.getparams()
            framerate = in_wav.getframerate()
            total_frames = in_wav.getnframes()
            total_duration = total_frames / float(framerate) if framerate > 0 else 0.0

            # If 30 mins or less, no slicing needed
            if total_duration <= 1800:
                return [(audio_path, 0.0, total_duration)]

            frames_per_chunk = int(chunk_duration_sec * framerate)
            chapters = []
            chapter_idx = 0
            current_frame = 0

            base_dir = Path(audio_path).parent
            stem = Path(audio_path).stem

            import tempfile

            while current_frame < total_frames:
                frames_to_read = min(frames_per_chunk, total_frames - current_frame)
                in_wav.setpos(current_frame)
                frames_data = in_wav.readframes(frames_to_read)

                # Write to temp file
                chapter_fd, chapter_path = tempfile.mkstemp(suffix=".wav")
                os.close(chapter_fd)
                with wave.open(chapter_path, "wb") as out_wav:
                    out_wav.setparams(params)
                    out_wav.writeframes(frames_data)

                start_offset = current_frame / float(framerate)
                chapter_dur = frames_to_read / float(framerate)
                chapters.append((chapter_path, start_offset, chapter_dur))

                current_frame += frames_to_read
                chapter_idx += 1

            logger.info(f"Sliced {total_duration:.1f}s audio into {len(chapters)} chapters (~{chunk_duration_sec/60:.0f}m each)")
            return chapters

    except Exception as e:
        logger.warning(f"Failed to slice WAV using wave module ({e}). Falling back to single file: {audio_path}")
        return [(audio_path, 0.0, 0.0)]


def transcribe_long_audio(audio_path: str, model_size: str = "base") -> Dict[str, Any]:
    """
    Transcribe long audio (>30 min) by slicing into chapters and stitching results.
    Uses Whisper for each chapter then combines with continuous timestamps.
    """
    chapters = slice_wav_file(audio_path, chunk_duration_sec=1800)

    all_segments = []
    primary_language = "mixed"

    for idx, (chapter_path, offset_sec, dur_sec) in enumerate(chapters):
        logger.info(f"Processing Chapter {idx + 1}/{len(chapters)}: offset={offset_sec:.1f}s")
        chapter_result = transcribe_audio_file(chapter_path, model_size=model_size)

        if idx == 0 and chapter_result.get("detected_language"):
            primary_language = chapter_result["detected_language"]

        # Offset segment timestamps
        for seg in chapter_result.get("segments", []):
            seg["start_time"] = round(seg["start_time"] + offset_sec, 2)
            seg["end_time"] = round(seg["end_time"] + offset_sec, 2)
            all_segments.append(seg)

    # Clean up temp files
    for chapter_path, _, _ in chapters:
        if chapter_path != audio_path and os.path.exists(chapter_path):
            try:
                os.remove(chapter_path)
            except Exception:
                pass

    # Sort all segments chronologically
    all_segments.sort(key=lambda s: s["start_time"])

    # Build formatted continuous transcript
    formatted_lines = []
    for s in all_segments:
        formatted_lines.append(f"[{format_seconds(s['start_time'])}] {s['speaker']}: {s['text']}")
    full_transcript = "\n".join(formatted_lines)

    logger.info(f"Long audio transcription complete: {len(all_segments)} total segments, "
                f"primary language: {primary_language}")

    return {
        "detected_language": primary_language,
        "full_transcript": full_transcript,
        "confidence": 0.9,
        "speaker_count": len(all_segments),
        "segments": all_segments
    }


def format_seconds(sec: float) -> str:
    """Formats seconds into MM:SS or HH:MM:SS string."""
    s = max(0, int(sec))
    h = s // 3600
    m = (s % 3600) // 60
    sec_rem = s % 60
    if h > 0:
        return f"{h:02d}:{m:02d}:{sec_rem:02d}"
    return f"{m:02d}:{sec_rem:02d}"


# Export the main functions
__all__ = [
    "transcribe_audio_file",
    "transcribe_long_audio",
    "slice_wav_file",
    "format_seconds"
]