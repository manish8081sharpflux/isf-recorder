"""
ISF Recorder — Content verification of a stitched m4a against its raw pieces
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
The raw chunk WAVs are deleted only after the final m4a is proven to hold their audio (this is the
check that found Jameel's broken 8 Oct files: the m4a was the right length but silent in places).

Method: decode the m4a to 16 kHz mono PCM (streamed through ffmpeg, bounded memory) and walk it
alongside the pieces in chunk order. For every piece louder than VOICED_DBFS, the m4a slice at the same
offset must be within TOLERANCE_DB of the piece's level and above MIN_SLICE_DBFS. If more than
MAX_MISMATCH_RATIO of the voiced pieces mismatch, the m4a FAILED.
"""
import logging
import math
import os
import subprocess
import wave
from typing import Dict, List, Optional

import numpy as np

logger = logging.getLogger("isf_recorder.audio_verify")

SAMPLE_RATE = 16000
# Every piece with real signal is compared, not only loud ones: with -45 a quiet room / soft speakers
# (-50 dBFS) had no "voiced" piece at all, so a silent m4a passed by default and its pieces were deleted.
VOICED_DBFS = -65.0
# The decoded m4a must hold (almost) all of the pieces' audio; a truncated m4a fails.
MIN_LENGTH_RATIO = 0.98
TOLERANCE_DB = 6.0
MIN_SLICE_DBFS = -70.0
MAX_MISMATCH_RATIO = 0.10

OK, FAILED, NO_PIECES, ERROR = "ok", "failed", "no_pieces", "error"


def _dbfs(pcm: np.ndarray) -> float:
    if pcm.size == 0:
        return float("-inf")
    x = pcm.astype(np.float64) / 32768.0
    rms = math.sqrt(float(np.mean(x * x)))
    return 20.0 * math.log10(rms) if rms > 0 else float("-inf")


def _ffmpeg_decode(path: str) -> np.ndarray:
    out = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-f", "s16le", "-ac", "1", "-ar", str(SAMPLE_RATE), "-"],
                         capture_output=True, check=True, timeout=600).stdout
    return np.frombuffer(out, dtype=np.int16)


def read_piece(path: str) -> np.ndarray:
    """A raw piece as 16 kHz mono int16 (phone WAVs are read directly, anything else via ffmpeg)."""
    if path.lower().endswith(".wav"):
        try:
            with wave.open(path, "rb") as w:
                if w.getframerate() == SAMPLE_RATE and w.getnchannels() == 1 and w.getsampwidth() == 2:
                    return np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
        except (wave.Error, EOFError):
            pass
    return _ffmpeg_decode(path)


class _M4aStream:
    """Sequential reader of the decoded m4a (ffmpeg pipe)."""

    def __init__(self, path: str):
        self.proc = subprocess.Popen(
            ["ffmpeg", "-v", "error", "-i", path, "-f", "s16le", "-ac", "1", "-ar", str(SAMPLE_RATE), "-"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)

    def read(self, samples: int) -> np.ndarray:
        want = samples * 2
        buf = bytearray()
        while len(buf) < want:
            data = self.proc.stdout.read(want - len(buf))
            if not data:
                break
            buf.extend(data)
        if len(buf) % 2:
            buf = buf[:-1]
        return np.frombuffer(bytes(buf), dtype=np.int16)

    def close(self):
        try:
            self.proc.stdout.close()
        except Exception:
            pass
        try:
            self.proc.kill()
        except Exception:
            pass
        self.proc.wait()


def verify_m4a(m4a_path: str, piece_paths: List[str]) -> Dict:
    """
    Returns {"status": ok|failed|no_pieces|error, "pieces", "voiced", "mismatches", "bad": [...], "error"?}.
    piece_paths must be in chunk order. Missing pieces -> no_pieces (cannot verify; never delete).
    """
    result = {"status": ERROR, "pieces": len(piece_paths), "voiced": 0, "mismatches": 0, "bad": []}
    if not piece_paths or any(not (p and os.path.exists(p)) for p in piece_paths):
        result["status"] = NO_PIECES
        return result
    if not (m4a_path and os.path.exists(m4a_path) and os.path.getsize(m4a_path) > 0):
        result["status"] = FAILED
        result["error"] = "m4a missing or empty"
        return result
    stream = None
    try:
        stream = _M4aStream(m4a_path)
        offset = 0.0
        piece_samples = m4a_samples = 0
        for i, p in enumerate(piece_paths):
            piece = read_piece(p)
            piece_db = _dbfs(piece)
            m4a_slice = stream.read(len(piece))
            piece_samples += len(piece)
            m4a_samples += len(m4a_slice)
            if piece_db > VOICED_DBFS:
                result["voiced"] += 1
                slice_db = _dbfs(m4a_slice)
                if not (slice_db > MIN_SLICE_DBFS and abs(slice_db - piece_db) <= TOLERANCE_DB):
                    result["mismatches"] += 1
                    result["bad"].append({"index": i, "at_sec": round(offset, 1), "piece_dbfs": round(piece_db, 1),
                                          "m4a_dbfs": round(slice_db, 1) if math.isfinite(slice_db) else None})
            offset += len(piece) / SAMPLE_RATE
        voiced = result["voiced"]
        result["length_ratio"] = round(m4a_samples / piece_samples, 4) if piece_samples else 1.0
        too_short = piece_samples > 0 and m4a_samples < MIN_LENGTH_RATIO * piece_samples
        mismatched = voiced and result["mismatches"] / voiced > MAX_MISMATCH_RATIO
        result["status"] = FAILED if (too_short or mismatched) else OK
        if too_short:
            result["error"] = f"m4a holds only {result['length_ratio']:.1%} of the pieces' audio"
        result["bad"] = result["bad"][:20]
        return result
    except Exception as e:
        logger.warning(f"Verification of {m4a_path} failed to run: {e}")
        result["status"] = ERROR
        result["error"] = str(e)[:300]
        return result
    finally:
        if stream:
            stream.close()
