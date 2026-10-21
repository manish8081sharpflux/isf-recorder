"""Window planning, overlap de-duplication and server VAD (no DB, no network)."""
import numpy as np

from backend.tests.conftest import speech_pcm
from backend.transcription import vad
from backend.transcription.overlap import plan_windows, dedupe_overlap, merge_windows, norm_word


def seg(start, end, text, speaker="SPEAKER_00"):
    return {"start_time": start, "end_time": end, "text": text, "speaker": speaker}


# ─── Window planning ─────────────────────────────────────────────────────────

def test_short_audio_is_one_window():
    assert plan_windows(42.5) == [(0, 0.0, 42.5)]
    assert plan_windows(180.0) == [(0, 0.0, 180.0)]


def test_windows_are_180s_with_3s_overlap():
    w = plan_windows(600.0)
    assert w == [(0, 0.0, 180.0), (1, 177.0, 360.0), (2, 357.0, 540.0), (3, 537.0, 600.0)]
    for (_, _, end), (_, start, _) in zip(w, w[1:]):
        assert round(end - start, 3) == 3.0


def test_window_boundary_just_over_one_chunk():
    assert plan_windows(181.0) == [(0, 0.0, 180.0), (1, 177.0, 181.0)]


def test_long_recording_window_count():
    w = plan_windows(91 * 60)  # Asmita's 91-minute session
    assert len(w) == 31 and w[-1][2] == 5460.0


# ─── Overlap dedupe ──────────────────────────────────────────────────────────

def test_devanagari_overlap_words_removed():
    prev = [seg(170.0, 176.5, "आज हम बच्चों के साथ"), seg(176.6, 180.0, "गिनती का अभ्यास करेंगे")]
    nxt = [seg(177.2, 182.0, "गिनती का अभ्यास करेंगे और फिर"), seg(182.1, 186.0, "कविता पढ़ेंगे")]
    out = dedupe_overlap(prev, nxt, boundary=180.0)
    assert out[0]["text"] == "और फिर"
    assert out[1]["text"] == "कविता पढ़ेंगे"


def test_hinglish_partial_word_at_cut():
    prev = [seg(175.0, 180.0, "Asmita ne worksheet complete ki, coins mi")]
    nxt = [seg(177.1, 183.0, "worksheet complete ki, coins milenge sabko")]
    out = dedupe_overlap(prev, nxt, boundary=180.0)
    assert out[0]["text"] == "sabko"


def test_whole_segment_inside_overlap_removed_on_word_match():
    prev = [seg(176.0, 179.8, "Okay, chalo shuru karte hain.")]
    nxt = [seg(177.0, 179.9, "okay chalo shuru karte hain"), seg(180.5, 184.0, "Pehla activity reading hai.")]
    out = dedupe_overlap(prev, nxt, boundary=180.0)
    assert [s["text"] for s in out] == ["Pehla activity reading hai."]


def test_no_match_keeps_text():
    prev = [seg(150.0, 160.0, "We finished the math worksheet.")]
    nxt = [seg(181.0, 185.0, "Now let's read a story together.")]
    out = dedupe_overlap(prev, nxt, boundary=180.0)
    assert [s["text"] for s in out] == ["Now let's read a story together."]


def test_script_switch_keeps_duplicate_rather_than_losing_words():
    prev = [seg(176.0, 180.0, "बहुत अच्छा किया")]
    nxt = [seg(177.0, 179.5, "bahut accha kiya"), seg(180.2, 183.0, "ab agla sawal")]
    out = dedupe_overlap(prev, nxt, boundary=180.0)
    assert [s["text"] for s in out] == ["bahut accha kiya", "ab agla sawal"]


def test_fuzzy_segment_match_without_word_match():
    prev = [seg(175.0, 180.0, "please sit down everyone and open your books")]
    # Same overlap speech, transcribed slightly differently; the first word differs so the word match fails
    nxt = [seg(177.0, 179.9, "pls sit down everyone and open your books"), seg(180.3, 184.0, "page twelve")]
    out = dedupe_overlap(prev, nxt, boundary=180.0)
    assert [s["text"] for s in out] == ["page twelve"]


def test_skipped_neighbour_means_no_dedupe():
    windows = [
        {"idx": 0, "start": 0.0, "end": 180.0, "segments": [seg(170.0, 180.0, "hello hello world")]},
        {"idx": 1, "start": 177.0, "end": 360.0, "segments": None},          # skipped as silent
        {"idx": 2, "start": 357.0, "end": 540.0, "segments": [seg(357.5, 362.0, "hello world again")]},
    ]
    merged = merge_windows(windows)
    assert [s["text"] for s in merged] == ["hello hello world", "hello world again"]


def test_merge_windows_dedupes_neighbours_in_order():
    windows = [
        {"idx": 1, "start": 177.0, "end": 360.0, "segments": [seg(177.5, 181.0, "two three four five"), seg(181.0, 185.0, "six")]},
        {"idx": 0, "start": 0.0, "end": 180.0, "segments": [seg(175.0, 180.0, "one two three four")]},
    ]
    merged = merge_windows(windows)
    assert [s["text"] for s in merged] == ["one two three four", "five", "six"]


def test_norm_word_keeps_devanagari_marks():
    assert norm_word("करेंगे।") == "करेंगे"
    assert norm_word("Hello,") == "hello"


# ─── VAD ─────────────────────────────────────────────────────────────────────

def test_vad_silence_is_skipped():
    silence = np.zeros(16000 * 10, dtype=np.int16)
    assert vad.voice_seconds(silence) == 0.0
    assert not vad.has_voice(silence)


def test_vad_quiet_hiss_is_not_voice():
    noise = (np.random.default_rng(0).standard_normal(16000 * 10) * 50).astype(np.int16)  # about -56 dBFS
    assert vad.voice_seconds(noise) < vad.MIN_VOICE_SECONDS


def test_vad_loud_noise_counts_as_voice():
    """Loudness rule (contract §10): a noisy room is never skipped even if Silero scores it low."""
    noise = (np.random.default_rng(0).standard_normal(16000 * 5) * 3000).astype(np.int16)  # about -21 dBFS
    assert vad.voice_seconds(noise) >= 4.5


def test_vad_real_speech_is_kept():
    pcm = speech_pcm()  # 4 s of synthetic (Windows TTS) English speech
    assert vad.voice_seconds(pcm) >= 2.0
    # Silero alone (loudness rule disabled) also hears the speech
    assert vad.voice_seconds(pcm, loud_dbfs=0.0) >= 1.0


def test_vad_speech_inside_silence_window():
    silence = np.zeros(16000 * 60, dtype=np.int16)
    pcm = np.concatenate([silence, np.frombuffer(speech_pcm(), dtype=np.int16), silence])
    assert vad.has_voice(pcm)


def _tone(hz, dbfs, seconds=5.0):
    t = np.arange(int(16000 * seconds)) / 16000.0
    amp = 10 ** (dbfs / 20.0) * np.sqrt(2) * 32767
    return (amp * np.sin(2 * np.pi * hz * t)).astype(np.int16)


def test_vad_loud_hum_below_80hz_is_not_loud():
    """Speech band (21 Oct, Bhavnesh): a loud 50 Hz electrical hum is not counted as voice by loudness."""
    hum = _tone(50, -20)
    band = vad.speech_band(vad.to_float(hum))[512 * 10: 512 * 110].reshape(100, 512)
    assert (vad.frame_dbfs(band) < -40).all()
    assert vad.voice_seconds(hum) < vad.MIN_VOICE_SECONDS


def test_vad_band_level_matches_plain_level_for_speech_frequencies():
    x = vad.to_float(_tone(500, -25))
    band = vad.speech_band(x)[512 * 10: 512 * 60].reshape(50, 512)
    plain = x[512 * 10: 512 * 60].reshape(50, 512)
    assert np.allclose(vad.frame_dbfs(band), vad.frame_dbfs(plain), atol=1.0)
