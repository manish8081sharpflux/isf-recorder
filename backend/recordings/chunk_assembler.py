"""
ISF Recorder — Audio Chunk Assembler
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Stitches sequentially uploaded audio chunks into a single unified audio file.
Handles WAV PCM concatenation with proper RIFF header recalculation,
as well as generic binary / container concatenation.
"""
import os
import wave
import logging
import hashlib
import shutil
import subprocess
from pathlib import Path
from typing import List, Tuple

from backend.config import AUDIO_STORAGE_ROOT

logger = logging.getLogger("isf_recorder.chunk_assembler")


def find_binary(binary_name: str) -> str:
    """Finds binary in system PATH or known Windows fallback paths."""
    found = shutil.which(binary_name)
    if found:
        return found
    candidates = [
        Path(r"C:\Users\Administrator\AppData\Local\Microsoft\WinGet\Links") / f"{binary_name}.exe",
        Path(r"C:\Users\User\AppData\Local\Microsoft\WinGet\Links") / f"{binary_name}.exe",
        Path(r"C:\ProgramData\chocolatey\bin") / f"{binary_name}.exe",
        Path(r"C:\ffmpeg\bin") / f"{binary_name}.exe",
    ]
    for c in candidates:
        if c.exists():
            return str(c)
    return binary_name


def get_recording_storage_dir(org_slug: str, date_str: str, recording_id: str) -> Path:
    """Returns directory path for storing a recording's chunks and final audio."""
    path = Path(AUDIO_STORAGE_ROOT) / org_slug / date_str / recording_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def calculate_checksum(file_path: str) -> str:
    """Calculate SHA256 checksum of a file for integrity and deduplication."""
    hasher = hashlib.sha256()
    with open(file_path, "rb") as f:
        while chunk := f.read(65536):
            hasher.update(chunk)
    return hasher.hexdigest()


def wav_to_m4a(wav_path: str, m4a_path: str, bitrate: str = "64k") -> bool:
    """
    Converts a WAV file to AAC-encoded .m4a using ffmpeg.
    Uses 16kHz mono sampling and 64kbps AAC for high-clarity voice compression.
    Includes +faststart for instant streaming playback on mobile/web.
    """
    ffmpeg_bin = find_binary("ffmpeg")
    try:
        cmd = [
            ffmpeg_bin,
            "-y",
            "-i", wav_path,
            "-c:a", "aac",
            "-b:a", bitrate,
            "-ar", "16000",
            "-ac", "1",
            "-movflags", "+faststart",
            m4a_path
        ]
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=300)
        if result.returncode == 0 and os.path.exists(m4a_path) and os.path.getsize(m4a_path) > 0:
            logger.info(f"Converted WAV ({os.path.getsize(wav_path)} bytes) -> M4A ({os.path.getsize(m4a_path)} bytes): {m4a_path}")
            return True
        else:
            logger.error(f"FFmpeg conversion failed (code {result.returncode}): {result.stderr.decode('utf-8', errors='ignore')}")
            return False
    except Exception as e:
        logger.error(f"Error converting WAV to M4A: {e}")
        return False


def stitch_wav_chunks(chunk_paths: List[str], output_path: str) -> Tuple[bool, float, int]:
    """
    Stitches multiple WAV chunk files into a single WAV file using python's wave module.
    Returns: (success, duration_seconds, file_size_bytes)
    """
    if not chunk_paths:
        logger.error("No chunk paths provided for stitching")
        return False, 0.0, 0

    total_frames = 0
    sample_rate = 16000

    try:
        with wave.open(chunk_paths[0], "rb") as first_wav:
            params = first_wav.getparams()
            sample_rate = first_wav.getframerate()

        with wave.open(output_path, "wb") as out_wav:
            out_wav.setparams(params)
            for path in chunk_paths:
                if not os.path.exists(path):
                    logger.warning(f"Chunk missing: {path}")
                    continue
                with wave.open(path, "rb") as in_wav:
                    frames = in_wav.readframes(in_wav.getnframes())
                    out_wav.writeframes(frames)
                    total_frames += in_wav.getnframes()

        file_size = os.path.getsize(output_path)
        duration_seconds = total_frames / float(sample_rate) if sample_rate > 0 else 0.0
        logger.info(f"Successfully stitched {len(chunk_paths)} WAV chunks -> {output_path} ({duration_seconds:.1f}s, {file_size} bytes)")
        return True, duration_seconds, file_size

    except Exception as e:
        logger.warning(f"WAV wave module stitching failed ({e}). Falling back to binary stitch.")
        return stitch_binary_chunks(chunk_paths, output_path)


def stitch_binary_chunks(chunk_paths: List[str], output_path: str) -> Tuple[bool, float, int]:
    """
    Fallback binary concatenation for non-WAV formats.
    """
    try:
        with open(output_path, "wb") as outfile:
            for path in chunk_paths:
                if not os.path.exists(path):
                    continue
                with open(path, "rb") as infile:
                    while data := infile.read(65536):
                        outfile.write(data)

        file_size = os.path.getsize(output_path)
        estimated_duration = file_size / 32000.0
        return True, estimated_duration, file_size
    except Exception as e:
        logger.error(f"Binary stitching failed: {e}")
        return False, 0.0, 0


def concat_audio_files_to_m4a(audio_paths: List[str], output_m4a_path: str, bitrate: str = "64k") -> Tuple[bool, float, int]:
    """
    Merges multiple audio files (WAV, M4A, etc.) into a single unified .M4A file via FFmpeg concat filter.
    Returns: (success, duration_seconds, file_size_bytes)
    """
    valid_paths = [p for p in audio_paths if os.path.exists(p) and os.path.getsize(p) > 0]
    if not valid_paths:
        return False, 0.0, 0

    ffmpeg_bin = find_binary("ffmpeg")
    try:
        # Build concat filter
        inputs = []
        for p in valid_paths:
            inputs.extend(["-i", p])
        
        filter_str = "".join([f"[{i}:a]" for i in range(len(valid_paths))]) + f"concat=n={len(valid_paths)}:v=0:a=1[aout]"
        cmd = [
            ffmpeg_bin,
            "-y",
            *inputs,
            "-filter_complex", filter_str,
            "-map", "[aout]",
            "-c:a", "aac",
            "-b:a", bitrate,
            "-ar", "16000",
            "-ac", "1",
            "-movflags", "+faststart",
            output_m4a_path
        ]
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=600)
        if res.returncode == 0 and os.path.exists(output_m4a_path) and os.path.getsize(output_m4a_path) > 0:
            file_size = os.path.getsize(output_m4a_path)
            # Check duration with ffprobe
            ffprobe_bin = find_binary("ffprobe")
            probe_cmd = [
                ffprobe_bin, "-v", "error", "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1", output_m4a_path
            ]
            pres = subprocess.run(probe_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=15)
            duration = float(pres.stdout.strip()) if pres.returncode == 0 and pres.stdout.strip() else 0.0
            return True, duration, file_size
        else:
            logger.error(f"FFmpeg concat failed: {res.stderr.decode('utf-8', errors='ignore')}")
            return False, 0.0, 0
    except Exception as e:
        logger.error(f"Error in concat_audio_files_to_m4a: {e}")
        return False, 0.0, 0


def assemble_recording(chunk_paths: List[str], final_file_path: str) -> Tuple[bool, float, int]:
    """
    Main entrypoint: takes an ordered list of chunk file paths and produces the final file.
    If final_file_path ends with .m4a:
      1. Stitches WAV chunks into an intermediate WAV file.
      2. Converts to AAC-encoded .m4a with +faststart via FFmpeg.
      3. Cleans up intermediate WAV.
      4. Returns exact duration and compressed .m4a file size.
    If final_file_path ends with .wav:
      Stitches directly to WAV (legacy/compatibility).
    """
    if not chunk_paths:
        return False, 0.0, 0

    chunk_paths = [p for p in chunk_paths if os.path.exists(p)]
    if not chunk_paths:
        return False, 0.0, 0

    is_m4a = final_file_path.lower().endswith(".m4a")

    if is_m4a:
        temp_wav_path = str(Path(final_file_path).with_suffix(".tmp.wav"))
        first_ext = Path(chunk_paths[0]).suffix.lower()
        if first_ext == ".wav":
            success, duration, _ = stitch_wav_chunks(chunk_paths, temp_wav_path)
        else:
            success, duration, _ = stitch_binary_chunks(chunk_paths, temp_wav_path)

        if not success or not os.path.exists(temp_wav_path):
            logger.error(f"Failed to stitch chunks to temp WAV: {temp_wav_path}")
            return False, 0.0, 0

        # Protect against race conditions: if final_file_path already exists with substantial size,
        # refuse to overwrite it if the new stitch has only 1-2 chunks and is under 30 seconds
        if os.path.exists(final_file_path):
            existing_size = os.path.getsize(final_file_path)
            if existing_size > 100_000 and duration < 30.0 and len(chunk_paths) <= 2:
                logger.error(
                    f"Refusing to overwrite existing master audio ({existing_size} bytes) "
                    f"with tiny fragment ({duration:.1f}s, {len(chunk_paths)} chunks): {final_file_path}"
                )
                try:
                    os.remove(temp_wav_path)
                except Exception:
                    pass
                return True, duration, existing_size

        temp_m4a_path = str(Path(final_file_path).with_suffix(".new.m4a"))
        converted = wav_to_m4a(temp_wav_path, temp_m4a_path)

        # Cleanup temporary WAV
        try:
            if os.path.exists(temp_wav_path):
                os.remove(temp_wav_path)
        except Exception as e:
            logger.warning(f"Could not remove temp WAV {temp_wav_path}: {e}")

        if converted and os.path.exists(temp_m4a_path) and os.path.getsize(temp_m4a_path) > 0:
            new_size = os.path.getsize(temp_m4a_path)
            # If target exists and new file is inexplicably tiny (<10KB) while old was big (>100KB), keep old
            if os.path.exists(final_file_path):
                old_size = os.path.getsize(final_file_path)
                if old_size > 100_000 and new_size < 20_000:
                    logger.error(f"New M4A is tiny ({new_size}b) compared to existing ({old_size}b); preserving existing!")
                    try:
                        os.remove(temp_m4a_path)
                    except Exception:
                        pass
                    return True, duration, old_size

            # Atomic replace
            shutil.move(temp_m4a_path, final_file_path)
            file_size = os.path.getsize(final_file_path)
            logger.info(f"Assembled M4A recording -> {final_file_path} ({duration:.1f}s, {file_size} bytes)")
            return True, duration, file_size
        else:
            if os.path.exists(temp_m4a_path):
                try:
                    os.remove(temp_m4a_path)
                except Exception:
                    pass
            logger.error(f"M4A conversion failed for {final_file_path}")
            return False, duration, 0
    else:
        first_ext = Path(chunk_paths[0]).suffix.lower()
        if first_ext == ".wav":
            return stitch_wav_chunks(chunk_paths, final_file_path)
        else:
            return stitch_binary_chunks(chunk_paths, final_file_path)

