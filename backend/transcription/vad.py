"""
ISF Recorder — Server-side voice activity detection (Silero VAD v5 via onnxruntime, no torch)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
A 3-minute window is sent to the speech model only if it contains at least MIN_VOICE_SECONDS of
voice. A 512-sample frame (32 ms at 16 kHz) counts as voice when (contract §10):

    silero_prob >= VAD_THRESHOLD (default 0.4)   OR   frame RMS > VAD_LOUD_DBFS (default -40 dBFS)

The loudness rule keeps noisy rooms (where Silero can score low-SNR speech near 0) from ever being
skipped; only near-silent windows are skipped. The model file is the same one the Android app uses.

Silero v5 input: 64 samples of context from the previous frame + 512 new samples ([1, 576] float32
in [-1, 1]), state float32 [2, 1, 128], sr int64 16000.
"""
import os
import logging
import threading
from typing import Optional

import numpy as np

logger = logging.getLogger("isf_recorder.vad")

SAMPLE_RATE = 16000
FRAME_SAMPLES = 512
CONTEXT_SAMPLES = 64
FRAME_SECONDS = FRAME_SAMPLES / SAMPLE_RATE  # 0.032 s
MIN_VOICE_SECONDS = 1.0

MODEL_PATH = os.path.join(os.path.dirname(__file__), "models", "silero_vad.onnx")


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


VAD_THRESHOLD = _env_float("VAD_THRESHOLD", 0.4)
VAD_LOUD_DBFS = _env_float("VAD_LOUD_DBFS", -40.0)

_session = None
_session_lock = threading.Lock()


def _get_session():
    """One shared onnxruntime session (InferenceSession.run is thread-safe)."""
    global _session
    if _session is None:
        with _session_lock:
            if _session is None:
                import onnxruntime as ort
                opts = ort.SessionOptions()
                opts.intra_op_num_threads = 1
                opts.inter_op_num_threads = 1
                _session = ort.InferenceSession(MODEL_PATH, sess_options=opts,
                                                providers=["CPUExecutionProvider"])
    return _session


def to_float(pcm) -> np.ndarray:
    """int16 PCM (bytes or array) -> float32 in [-1, 1]."""
    if isinstance(pcm, (bytes, bytearray, memoryview)):
        pcm = np.frombuffer(pcm, dtype=np.int16)
    arr = np.asarray(pcm)
    if arr.dtype == np.int16:
        return arr.astype(np.float32) / 32768.0
    return arr.astype(np.float32)


def frame_dbfs(frames: np.ndarray) -> np.ndarray:
    """RMS level in dBFS of each row of a [n, 512] float array (digital silence -> -inf)."""
    rms = np.sqrt(np.mean(np.square(frames, dtype=np.float64), axis=1))
    with np.errstate(divide="ignore"):
        return 20.0 * np.log10(rms)


def voice_frames(pcm, threshold: Optional[float] = None, loud_dbfs: Optional[float] = None,
                 stop_after_seconds: Optional[float] = None) -> np.ndarray:
    """
    Boolean voice flag per 512-sample frame (a trailing partial frame is ignored).
    stop_after_seconds: stop early once that much voice has been found (the remaining frames are
    returned as False) — enough to decide "is there >= 1 s of voice" on a long file quickly.
    """
    thr = VAD_THRESHOLD if threshold is None else threshold
    loud = VAD_LOUD_DBFS if loud_dbfs is None else loud_dbfs
    x = to_float(pcm)
    n = len(x) // FRAME_SAMPLES
    flags = np.zeros(n, dtype=bool)
    if n == 0:
        return flags
    frames = x[: n * FRAME_SAMPLES].reshape(n, FRAME_SAMPLES)
    loud_flags = frame_dbfs(frames) > loud

    session = _get_session()
    state = np.zeros((2, 1, 128), dtype=np.float32)
    context = np.zeros((1, CONTEXT_SAMPLES), dtype=np.float32)
    sr = np.array(SAMPLE_RATE, dtype=np.int64)
    stop_frames = None if stop_after_seconds is None else int(np.ceil(stop_after_seconds / FRAME_SECONDS))
    count = 0
    for i in range(n):
        if loud_flags[i]:
            # Loud enough to count as voice by itself; still feed Silero so its state stays continuous
            flags[i] = True
        inp = np.concatenate([context, frames[i:i + 1]], axis=1)
        out, state = session.run(None, {"input": inp, "state": state, "sr": sr})
        context = inp[:, -CONTEXT_SAMPLES:]
        if not flags[i] and float(out[0, 0]) >= thr:
            flags[i] = True
        if flags[i]:
            count += 1
            if stop_frames is not None and count >= stop_frames:
                break
    return flags


def voice_seconds(pcm, threshold: Optional[float] = None, loud_dbfs: Optional[float] = None,
                  stop_after_seconds: Optional[float] = None) -> float:
    """Total seconds of voice frames in 16 kHz mono PCM."""
    return round(float(voice_frames(pcm, threshold, loud_dbfs, stop_after_seconds).sum()) * FRAME_SECONDS, 3)


def has_voice(pcm, min_seconds: float = MIN_VOICE_SECONDS) -> bool:
    return voice_seconds(pcm, stop_after_seconds=min_seconds) >= min_seconds


def file_voice_seconds(wav_path: str, stop_after_seconds: Optional[float] = None,
                       block_seconds: int = 600) -> float:
    """Voice seconds of a 16 kHz mono 16-bit WAV file, read in blocks (bounded memory)."""
    import wave
    total = 0.0
    with wave.open(wav_path, "rb") as w:
        if w.getframerate() != SAMPLE_RATE or w.getnchannels() != 1 or w.getsampwidth() != 2:
            raise ValueError("file_voice_seconds expects 16 kHz mono 16-bit PCM WAV")
        block = block_seconds * SAMPLE_RATE
        while True:
            data = w.readframes(block)
            if not data:
                break
            remaining = None if stop_after_seconds is None else max(0.0, stop_after_seconds - total)
            total += voice_seconds(data, stop_after_seconds=remaining)
            if stop_after_seconds is not None and total >= stop_after_seconds:
                break
    return round(total, 3)
