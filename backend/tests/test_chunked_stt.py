"""Chunked STT (contract §7) with a fake Gemini client: skip, retry, reuse, errors, usage tagging."""
import asyncio
import json
import os
import subprocess
import uuid
from pathlib import Path

import numpy as np
import pytest
from sqlalchemy import text

from backend.database import SessionLocal
from backend.tests.conftest import FakeGenaiClient, FakeResponse, speech_pcm, write_wav
from backend.transcription import chunked_stt, engine
from backend.transcription.chunked_stt import TranscriptionFailed, transcribe_chunked, audio_fingerprint


@pytest.fixture(autouse=True)
def _fast_sleep(monkeypatch):
    monkeypatch.setattr(chunked_stt, "_sleep", lambda s: None)
    # main-model behaviour is tested with the rescue pass off; rescue has its own tests below
    monkeypatch.setattr(chunked_stt, "RESCUE_MODEL", "")


def _audio(tmp_path: Path, layout, name="a.wav") -> str:
    """layout: list of ("speech"|"silence", seconds). Returns path to a 16 kHz WAV."""
    parts = []
    sp = np.frombuffer(speech_pcm(), dtype=np.int16)
    for kind, secs in layout:
        n = int(secs * 16000)
        parts.append(np.resize(sp, n) if kind == "speech" else np.zeros(n, dtype=np.int16))
    return str(write_wav(tmp_path / name, np.concatenate(parts).astype(np.int16).tobytes()))


def _to_m4a(wav_path: str) -> str:
    out = wav_path.replace(".wav", ".m4a")
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", wav_path, "-c:a", "aac", "-b:a", "64k", out], check=True)
    return out


def _json(text_value, start=1.0, end=3.0):
    """A one-line answer in the compact transcription format (name kept from the JSON days)."""
    return f"{int(start) // 60:02d}:{int(start) % 60:02d}|S1|{text_value}"


def test_silent_audio_makes_no_calls(tmp_path):
    path = _audio(tmp_path, [("silence", 400)])
    fake = FakeGenaiClient(lambda *a: pytest.fail("no call expected"))
    res = transcribe_chunked(path, recording_id=None, model="gemini-2.5-flash", client=fake)
    assert res["all_silent"] is True and res["segments"] == []
    assert res["stats"]["chunks_total"] == 3 and res["stats"]["skipped_silent_chunks"] == 3
    assert fake.calls == []


def test_only_windows_with_voice_are_sent_and_offsets_applied(tmp_path):
    # window 0 has speech, window 1 [177, 360) silent, window 2 [357, 400) has speech
    path = _audio(tmp_path, [("speech", 5), ("silence", 375), ("speech", 20)])
    seen = []

    def responder(model, contents, config):
        part, prompt = contents
        assert config.max_output_tokens == 4096 and config.response_mime_type is None
        assert prompt == chunked_stt.COMPACT_PROMPT and config.temperature == 0.2
        assert part.inline_data.mime_type == "audio/wav"
        seen.append(len(part.inline_data.data))
        return FakeResponse(_json(f"window {len(seen)}"))
    fake = FakeGenaiClient(responder)
    res = transcribe_chunked(path, recording_id=None, model="gemini-2.5-flash", client=fake)
    assert len(fake.calls) == 2 and res["stats"]["skipped_silent_chunks"] == 1
    starts = sorted(s["start_time"] for s in res["segments"])
    assert starts[0] == 1.0 and starts[1] == 358.0  # window 2 starts at 357 s
    assert "[05:58]" in res["full_transcript"]


def _loop_answer(n=40):
    lines = ["00:01|S1|namaste bacchon"] + [f"00:{5 + i:02d}|S2|ek do teen" for i in range(n)]
    return "\n".join(lines)


def test_looping_window_gets_one_continuation(tmp_path):
    path = _audio(tmp_path, [("speech", 60)])
    answers = [FakeResponse(_loop_answer(), finish="MAX_TOKENS"),
               FakeResponse("00:00|S2|ek do teen chaar\n00:10|S1|ab kavita padhenge")]
    fake = FakeGenaiClient(lambda *a: answers.pop(0))
    res = transcribe_chunked(path, recording_id=None, model="gemini-2.5-flash", client=fake)
    assert len(fake.calls) == 2
    first_audio = len(fake.calls[0]["contents"][0].inline_data.data)
    second_audio = len(fake.calls[1]["contents"][0].inline_data.data)
    assert second_audio < first_audio  # the continuation sends only [T, window end]
    texts = [(s["start_time"], s["text"]) for s in res["segments"]]
    # good lines before the loop are kept; the continuation starts at T = 6 s (last good line)
    assert texts == [(1.0, "namaste bacchon"), (5.0, "ek do teen"), (6.0, "ek do teen chaar"),
                     (16.0, "ab kavita padhenge")]
    assert res["segments"][-1]["end_time"] == 60.0
    assert res["stats"]["loop_truncated_chunks"] == 0


def test_continuation_that_loops_again_is_loop_truncated(tmp_path):
    path = _audio(tmp_path, [("speech", 60)])
    fake = FakeGenaiClient(lambda *a: FakeResponse(_loop_answer(), finish="MAX_TOKENS"))
    res = transcribe_chunked(path, recording_id=None, model="gemini-2.5-flash", client=fake)
    assert len(fake.calls) == 2  # never more than one continuation
    assert res["stats"]["loop_truncated_chunks"] == 1 and res["segments"]


def test_long_line_is_a_loop_and_no_continuation_near_window_end(tmp_path):
    path = _audio(tmp_path, [("speech", 30)])
    answer = "00:02|S1|shuru karte hain\n00:12|S2|theek hai\n00:15|S1|" + "haan " * 200
    fake = FakeGenaiClient(lambda *a: FakeResponse(answer))
    res = transcribe_chunked(path, recording_id=None, model="gemini-2.5-flash", client=fake)
    # the 1000-char line is a loop; T = 12 s is only 18 s before the window end -> no continuation
    assert len(fake.calls) == 1
    assert [s["text"] for s in res["segments"]] == ["shuru karte hain", "theek hai"]
    assert res["stats"]["loop_truncated_chunks"] == 0


def test_parse_compact_normal_lines():
    out = chunked_stt.parse_compact("00:01|S1|नमस्ते बच्चों\n00:07|S2|Good morning didi\n01:30|S1|चलो शुरू करते हैं",
                                    177.0, 357.0)
    assert [(s["start_time"], s["end_time"], s["speaker"]) for s in out] == [
        (178.0, 184.0, "SPEAKER_01"), (184.0, 267.0, "SPEAKER_02"), (267.0, 357.0, "SPEAKER_01")]
    assert out[0]["language"] == "mixed" and out[1]["language"] == "en"


def test_parse_compact_hours_and_garbage():
    text_out = "```\nHere is the transcript:\n0:00:05|S1|hello\n1:02:03|3|later\nnot a line\n00:09|S1|\n```"
    out = chunked_stt.parse_compact(text_out, 0.0, 4000.0)
    assert [(s["start_time"], s["speaker"], s["text"]) for s in out] == [
        (5.0, "SPEAKER_01", "hello"), (3723.0, "SPEAKER_03", "later")]


def test_parse_compact_empty_and_clamped():
    assert chunked_stt.parse_compact("", 0.0, 180.0) == []
    out = chunked_stt.parse_compact("09:59|S1|beyond the clip", 0.0, 180.0)
    assert out[0]["start_time"] == 180.0 and out[0]["end_time"] == 180.0


def test_clean_empty_answer_is_no_speech_not_error(tmp_path):
    path = _audio(tmp_path, [("speech", 10)])
    fake = FakeGenaiClient(lambda *a: FakeResponse(""))  # finish STOP, nothing written: no speech
    res = transcribe_chunked(path, recording_id=None, model="gemini-2.5-flash", client=fake)
    assert res["segments"] == [] and res["stats"]["failed_chunks"] == 0


def test_429_retried_with_backoff_and_no_model_fallback_on_loops(tmp_path):
    path = _audio(tmp_path, [("speech", 10)])

    class RateLimited(Exception):
        code = 429
    state = {"n": 0}

    def responder(model, contents, config):
        state["n"] += 1
        if state["n"] <= 2:
            raise RateLimited("429 RESOURCE_EXHAUSTED")
        return FakeResponse(_json("after retries"))
    fake = FakeGenaiClient(responder)
    res = transcribe_chunked(path, recording_id=None, model="gemini-3.5-flash", client=fake)
    assert [c["model"] for c in fake.calls] == ["gemini-3.5-flash"] * 3
    assert res["segments"][0]["text"] == "after retries"


def test_api_error_falls_back_to_default_model(tmp_path):
    path = _audio(tmp_path, [("speech", 10)])

    class BadRequest(Exception):
        code = 400

    def responder(model, contents, config):
        if model == "gemini-3.5-flash":
            raise BadRequest("model not available")
        return FakeResponse(_json("fallback ok"))
    fake = FakeGenaiClient(responder)
    res = transcribe_chunked(path, recording_id=None, model="gemini-3.5-flash", client=fake)
    assert [c["model"] for c in fake.calls] == ["gemini-3.5-flash", "gemini-2.5-flash"]
    assert res["segments"][0]["text"] == "fallback ok"


def test_empty_output_is_an_error_not_placeholder(tmp_path):
    path = _audio(tmp_path, [("speech", 10)])
    fake = FakeGenaiClient(lambda *a: FakeResponse("", finish="OTHER"))
    with pytest.raises(TranscriptionFailed) as exc:
        transcribe_chunked(path, recording_id=None, model="gemini-2.5-flash", client=fake)
    assert "could not be transcribed" in str(exc.value)
    assert exc.value.stats["failed_chunks"] == 1


def test_unparseable_output_is_failed_window(tmp_path):
    path = _audio(tmp_path, [("speech", 10)])
    fake = FakeGenaiClient(lambda *a: FakeResponse("I could not hear anything clearly, sorry."))
    with pytest.raises(TranscriptionFailed):
        transcribe_chunked(path, recording_id=None, model="gemini-2.5-flash", client=fake)


# ─── With the database: window persistence + reuse, full pipeline ────────────

def _recording(staff, audio_path, status="uploading", stage="queued", mode=None):
    with SessionLocal() as s:
        rid = s.execute(text("""
            INSERT INTO isf_recorder.recording (user_id, org_id, session_id, status, started_at, audio_file_path,
                finish_requested_at, finish_source, pipeline_stage, mode)
            VALUES (:u, :o, :sid, :st, NOW(), :p, NOW(), 'client', :stage, :mode) RETURNING id
        """), {"u": staff["id"], "o": staff["org_id"], "sid": f"rec_test_{uuid.uuid4().hex}", "p": audio_path,
               "st": status, "stage": stage, "mode": mode}).scalar()
        s.commit()
    return str(rid)


def _run(rec_id, model="gemini-2.5-flash"):
    with SessionLocal() as s:
        rid = s.execute(text("""
            INSERT INTO isf_recorder.transcription_run (recording_id, model, stt_engine, status)
            VALUES (:r, :m, 'gemini', 'error') RETURNING id
        """), {"r": rec_id, "m": model}).scalar()
        s.commit()
    return str(rid)


def test_done_windows_are_reused_on_retry(tmp_path, staff):
    path = _audio(tmp_path, [("speech", 5), ("silence", 180), ("speech", 15)])
    rec_id = _recording(staff, path)
    calls = {"n": 0}

    def first(model, contents, config):
        calls["n"] += 1
        if calls["n"] == 2:
            return FakeResponse("", finish="OTHER")  # second window fails in the first run
        return FakeResponse(_json("first run"))
    with pytest.raises(TranscriptionFailed):
        transcribe_chunked(path, recording_id=rec_id, model="gemini-2.5-flash", run_id=_run(rec_id),
                           client=FakeGenaiClient(first))
    retry = FakeGenaiClient(lambda *a: FakeResponse(_json("retry run")))
    res = transcribe_chunked(path, recording_id=rec_id, model="gemini-2.5-flash", run_id=_run(rec_id), client=retry)
    assert len(retry.calls) == 1  # only the failed window is paid for again
    assert res["stats"]["reused_chunks"] == 1
    assert sorted(s["text"] for s in res["segments"]) == ["first run", "retry run"]
    with SessionLocal() as s:
        rows = s.execute(text("""
            SELECT status, COUNT(*) FROM isf_recorder.transcription_chunk WHERE recording_id = :r GROUP BY status
        """), {"r": rec_id}).fetchall()
    assert dict(rows) == {"done": 3, "failed": 1}


def test_full_pipeline_tags_usage_and_completes(tmp_path, staff, monkeypatch):
    path = _to_m4a(_audio(tmp_path, [("speech", 8), ("silence", 4)], name="p.wav"))
    rec_id = _recording(staff, path, mode="meeting")
    stt = FakeGenaiClient(lambda *a: FakeResponse(_json("Namaste sabko, meeting shuru karte hain")))
    intel = FakeGenaiClient(lambda *a: FakeResponse(json.dumps({
        "title": "Weekly review", "overview": "Context: test", "key_points": ["a"], "decisions": [],
        "action_items": [{"description": "Send list", "assigned_to": "Asmita", "urgency": "normal"}]})))
    import backend.transcription.gemini_stt as gstt
    import backend.transcription.intelligence as intelligence
    monkeypatch.setattr(gstt, "get_genai_client", lambda: stt)
    monkeypatch.setattr(intelligence, "get_genai_client", lambda: intel)
    try:
        import backend.reports.service as report_service
        monkeypatch.setattr(report_service, "get_genai_client", lambda: FakeGenaiClient(
            lambda *a: FakeResponse("**Meeting overview** - test")), raising=False)
    except ImportError:
        pass

    asyncio.run(engine.process_recording_pipeline(rec_id))
    with SessionLocal() as s:
        rec = s.execute(text("SELECT * FROM isf_recorder.recording WHERE id = :id"), {"id": rec_id}).fetchone()
        usage = s.execute(text("""
            SELECT operation_type, run_id, org_id FROM isf_recorder.gemini_usage_log WHERE recording_id = :id
        """), {"id": rec_id}).fetchall()
        run = s.execute(text("SELECT status, chunks_total, skipped_silent_chunks FROM isf_recorder.transcription_run WHERE id = :r"),
                        {"r": rec.active_run_id}).fetchone()
    assert rec.status == "completed" and rec.pipeline_stage == "done"
    assert rec.title == "Weekly review" and "Namaste" in rec.transcript
    assert run.status == "completed" and run.chunks_total == 1 and run.skipped_silent_chunks == 0
    stt_rows = [u for u in usage if u.operation_type == "TRANSCRIPTION"]
    assert len(stt_rows) == 1
    # usage rows written from worker threads still carry the run id (contextvars propagation)
    assert all(str(u.run_id) == str(rec.active_run_id) for u in usage if u.operation_type in ("TRANSCRIPTION", "SUMMARY"))
    assert all(str(u.org_id) == staff["org_id"] for u in usage)


def test_pipeline_all_silent_completes_without_calls(tmp_path, staff, monkeypatch):
    path = _to_m4a(_audio(tmp_path, [("silence", 30)], name="s.wav"))
    rec_id = _recording(staff, path)
    import backend.transcription.gemini_stt as gstt
    monkeypatch.setattr(gstt, "get_genai_client", lambda: FakeGenaiClient(lambda *a: pytest.fail("no call")))
    asyncio.run(engine.process_recording_pipeline(rec_id))
    with SessionLocal() as s:
        rec = s.execute(text("SELECT status, title, transcript FROM isf_recorder.recording WHERE id = :id"),
                        {"id": rec_id}).fetchone()
    assert rec.status == "completed" and rec.title == engine.NO_SPEECH_TITLE and rec.transcript == ""


def test_pipeline_failure_sets_error_with_message(tmp_path, staff, monkeypatch):
    path = _to_m4a(_audio(tmp_path, [("speech", 6)], name="f.wav"))
    rec_id = _recording(staff, path)
    import backend.transcription.gemini_stt as gstt
    monkeypatch.setattr(gstt, "get_genai_client", lambda: FakeGenaiClient(lambda *a: FakeResponse("", finish="OTHER")))
    asyncio.run(engine.process_recording_pipeline(rec_id))
    with SessionLocal() as s:
        rec = s.execute(text("SELECT status, pipeline_stage, error_message, transcript FROM isf_recorder.recording WHERE id = :id"),
                        {"id": rec_id}).fetchone()
    assert rec.status == "error" and rec.pipeline_stage == "failed"
    assert "could not be transcribed" in rec.error_message and not rec.transcript


def test_assemblyai_skipped_when_file_is_silent(tmp_path, staff, monkeypatch):
    path = _to_m4a(_audio(tmp_path, [("silence", 20)], name="aai.wav"))
    rec_id = _recording(staff, path)
    asyncio.run(engine.process_recording_pipeline(rec_id, model_override="assemblyai-universal-2"))
    with SessionLocal() as s:
        rec = s.execute(text("SELECT status, title FROM isf_recorder.recording WHERE id = :id"), {"id": rec_id}).fetchone()
    assert rec.status == "completed" and rec.title == engine.NO_SPEECH_TITLE  # upload_audio_file is blocked in tests


def test_recitation_block_keeps_placeholder_not_whole_failure(tmp_path):
    """Children reciting a poem -> Gemini RECITATION: that window gets a placeholder, the recording does not fail."""
    path = _audio(tmp_path, [("speech", 10)])
    fake = FakeGenaiClient(lambda *a: FakeResponse("", finish="RECITATION"))
    res = transcribe_chunked(path, recording_id=None, model="gemini-2.5-flash", client=fake)
    assert res["stats"]["failed_chunks"] == 0
    assert len(res["segments"]) == 1 and res["segments"][0]["text"] == chunked_stt.BLOCKED_PLACEHOLDER


def test_fingerprint_includes_prompt_version(tmp_path):
    """Windows transcribed with an older prompt/format must never be reused."""
    path = _audio(tmp_path, [("speech", 3)])
    assert audio_fingerprint(path).endswith(":" + chunked_stt.PROMPT_VERSION)


def test_rescue_model_redoes_only_windows_the_main_model_cannot(tmp_path, monkeypatch):
    monkeypatch.setattr(chunked_stt, "RESCUE_MODEL", "gemini-3.5-flash")
    path = _audio(tmp_path, [("speech", 10)])

    def answer(model, *a):
        if model == "gemini-2.5-flash":
            return FakeResponse("", finish="RECITATION")
        return FakeResponse("00:01|S1|roly poly00:04|S2|shabbas", finish="STOP")
    fake = FakeGenaiClient(answer)
    res = transcribe_chunked(path, recording_id=None, model="gemini-2.5-flash", client=fake)
    assert [s["text"] for s in res["segments"]] == ["roly poly", "shabbas"]
    assert [c["model"] for c in fake.calls] == ["gemini-2.5-flash", "gemini-3.5-flash"]


def test_no_rescue_when_main_model_succeeds(tmp_path, monkeypatch):
    monkeypatch.setattr(chunked_stt, "RESCUE_MODEL", "gemini-3.5-flash")
    path = _audio(tmp_path, [("speech", 10)])
    fake = FakeGenaiClient(lambda *a: FakeResponse("00:01|S1|namaskar", finish="STOP"))
    transcribe_chunked(path, recording_id=None, model="gemini-2.5-flash", client=fake)
    assert [c["model"] for c in fake.calls] == ["gemini-2.5-flash"]


def test_parse_compact_without_newlines():
    """gemini-3.5-flash writes every utterance on one line."""
    segs = chunked_stt.parse_compact("00:00|S1|भाऊ आले का?00:02|S2|नाही 12:30 वाजता00:04|S1|ठीक", 10, 190)
    assert [(s["start_time"], s["text"]) for s in segs] == [(10, "भाऊ आले का?"), (12, "नाही 12:30 वाजता"), (14, "ठीक")]


def test_long_monologue_line_is_not_a_loop(tmp_path):
    """Gemini 2.5 Flash writes a storyteller's minute as one long line: keep it, no continuation/rescue call."""
    path = _audio(tmp_path, [("speech", 60)])
    story = " ".join(f"गोपाल{i} ने बलराम{i} को मिठाई{i} दी{i}" for i in range(25))  # varied words, ~700 chars
    assert len(story) > chunked_stt.LOOP_LINE_CHARS
    answer = "00:02|S1|चलो बच्चों कहानी सुनो\n00:05|S1|" + story
    fake = FakeGenaiClient(lambda *a: FakeResponse(answer))
    res = transcribe_chunked(path, recording_id=None, model="gemini-2.5-flash", client=fake)
    assert len(fake.calls) == 1
    assert [s["text"] for s in res["segments"]] == ["चलो बच्चों कहानी सुनो", story]
    assert res["stats"]["loop_truncated_chunks"] == 0


def test_is_repetitive():
    assert chunked_stt._is_repetitive("haan " * 200)
    assert chunked_stt._is_repetitive("ek do teen char " * 60)
    assert not chunked_stt._is_repetitive(" ".join(f"w{i}" for i in range(150)))
    assert chunked_stt._is_repetitive("x" * 5000)


def test_429_waits_longer_than_server_errors_before_giving_up(tmp_path, monkeypatch):
    """Rate limits get 4 retries (5/15/30/60 s + jitter) on the same model before the window fails over."""
    path = _audio(tmp_path, [("speech", 10)])
    waits = []
    monkeypatch.setattr(chunked_stt, "_sleep", lambda s: waits.append(s))

    class RateLimited(Exception):
        code = 429
    state = {"n": 0}

    def responder(model, contents, config):
        state["n"] += 1
        if state["n"] <= 4:
            raise RateLimited("429 RESOURCE_EXHAUSTED")
        return FakeResponse(_json("after four 429s"))
    fake = FakeGenaiClient(responder)
    res = transcribe_chunked(path, recording_id=None, model="gemini-2.5-flash", client=fake)
    assert [c["model"] for c in fake.calls] == ["gemini-2.5-flash"] * 5
    assert res["segments"][0]["text"] == "after four 429s" and res["stats"]["failed_chunks"] == 0
    assert len(waits) == 4 and all(lo <= w <= lo * 1.3 for w, lo in zip(waits, (5, 15, 30, 60)))


def test_concurrency_defaults():
    from backend import config
    import os
    if "MAX_CONCURRENT_TRANSCRIPTIONS" not in os.environ:
        assert config.MAX_CONCURRENT_TRANSCRIPTIONS == 8
    if "GEMINI_MAX_PARALLEL_CALLS" not in os.environ:
        assert chunked_stt.GLOBAL_CALL_SEMAPHORE._initial_value == 12


def test_pricing_by_date_and_thinking_billed_as_output():
    from datetime import datetime, timezone
    from backend.transcription.usage_tracker import get_model_pricing, calculate_cost
    with SessionLocal() as s:
        now = get_model_pricing(s, "gemini-3.8-flash", datetime(2026, 11, 1, tzinfo=timezone.utc))
        later = get_model_pricing(s, "gemini-3.8-flash", datetime(2027, 2, 1, tzinfo=timezone.utc))
    assert (now["input_price"], now["output_price"], now["audio_price"], now["cached_price"]) == (0.75, 3.75, 0.75, 0.075)
    assert (later["input_price"], later["output_price"]) == (1.5, 7.5)
    cost = calculate_cost({"input_tokens": 0, "input_audio_tokens": 0, "cached_input_tokens": 0,
                           "output_tokens": 1_000_000, "thinking_tokens": 1_000_000}, now, 1.0)
    assert cost["output_cost_usd"] == 7.5


def test_gemini_38_flash_is_selectable():
    from backend.settings.routes import AVAILABLE_MODELS
    m = next(m for m in AVAILABLE_MODELS if m["id"] == "gemini-3.8-flash")
    assert {"transcription", "intelligence"} <= set(m["supports"])


def test_silent_rerun_never_wipes_existing_transcript(tmp_path, staff, monkeypatch):
    """Retry / another model hearing no speech must keep the transcript the recording already has."""
    path = _to_m4a(_audio(tmp_path, [("silence", 30)], name="rr.wav"))
    rec_id = _recording(staff, path, status="processing", stage="queued")
    with SessionLocal() as s:
        s.execute(text("UPDATE isf_recorder.recording SET transcript = 'old good transcript', title = 'Kept title' WHERE id = :id"),
                  {"id": rec_id})
        s.execute(text("""INSERT INTO isf_recorder.transcript_segment (recording_id, speaker, text, start_time, end_time)
                          VALUES (:id, 'SPEAKER_01', 'old line', 0, 2)"""), {"id": rec_id})
        s.commit()
    import backend.transcription.gemini_stt as gstt
    monkeypatch.setattr(gstt, "get_genai_client", lambda: FakeGenaiClient(lambda *a: pytest.fail("no call")))
    asyncio.run(engine.process_recording_pipeline(rec_id))
    with SessionLocal() as s:
        rec = s.execute(text("SELECT status, title, transcript FROM isf_recorder.recording WHERE id = :id"), {"id": rec_id}).fetchone()
        n = s.execute(text("SELECT COUNT(*) FROM isf_recorder.transcript_segment WHERE recording_id = :id"), {"id": rec_id}).scalar()
    assert rec.status == "completed" and rec.transcript == "old good transcript" and rec.title == "Kept title" and n == 1
