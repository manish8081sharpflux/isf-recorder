"""OpenAI transcription engine (mocked HTTP): request shape, mapping, retries, pipeline, usage cost."""
import json
import uuid

import httpx
import numpy as np
import pytest
from sqlalchemy import text

from backend.database import SessionLocal
from backend.tests.conftest import speech_pcm, write_wav
from backend.transcription import chunked_stt, openai_stt
from backend.transcription.chunked_stt import transcribe_chunked

DIARIZED = {"task": "transcribe", "duration": 20.0, "text": "नमस्ते बच्चों Good morning",
            "segments": [{"id": "seg_0", "speaker": "A", "start": 0.5, "end": 3.2, "text": "नमस्ते बच्चों",
                          "type": "transcript.text.segment"},
                         {"id": "seg_1", "speaker": "B", "start": 3.4, "end": 5.0, "text": " Good morning ",
                          "type": "transcript.text.segment"}],
            "usage": {"type": "tokens", "input_tokens": 600, "output_tokens": 40, "total_tokens": 640,
                      "input_token_details": {"audio_tokens": 600, "text_tokens": 0}}}


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(chunked_stt, "_sleep", lambda s: None)


def _run(rec_id, model="openai-gpt-4o-transcribe-diarize"):
    with SessionLocal() as s:
        rid = s.execute(text("""
            INSERT INTO isf_recorder.transcription_run (recording_id, model, stt_engine, status)
            VALUES (:r, :m, 'openai', 'error') RETURNING id
        """), {"r": rec_id, "m": model}).scalar()
        s.commit()
    return str(rid)


def _audio(tmp_path, seconds):
    sp = np.frombuffer(speech_pcm(), dtype=np.int16)
    return str(write_wav(tmp_path / "a.wav", np.resize(sp, int(seconds * 16000)).astype(np.int16).tobytes()))


def test_diarized_mapping_and_offsets():
    segs = openai_stt.segments_from_response("openai-gpt-4o-transcribe-diarize", DIARIZED, 177.0, 180.0)
    assert [(s["speaker"], s["start_time"], s["end_time"], s["text"], s["language"]) for s in segs] == [
        ("SPEAKER_00", 177.5, 180.2, "नमस्ते बच्चों", "mixed"), ("SPEAKER_01", 180.4, 182.0, "Good morning", "en")]


def test_plain_text_is_split_with_estimated_times():
    body = {"text": "Hello children. Sit down please! Who has the book?", "languages": [{"code": "en"}]}
    segs = openai_stt.segments_from_response("openai-gpt-transcribe", body, 0.0, 30.0)
    assert [s["text"] for s in segs] == ["Hello children.", "Sit down please!", "Who has the book?"]
    assert segs[0]["start_time"] == 0.0 and segs[-1]["end_time"] == 30.0 and all(s["estimated_times"] for s in segs)
    assert {s["speaker"] for s in segs} == {"SPEAKER_00"}


def test_request_fields_and_429_retry():
    seen = []

    def handler(request: httpx.Request):
        body = request.content.decode("latin-1")
        seen.append(body)
        assert request.headers["authorization"] == "Bearer sk-test"
        if len(seen) == 1:
            return httpx.Response(429, json={"error": {"message": "Rate limit"}})
        return httpx.Response(200, json=DIARIZED)
    http = httpx.Client(transport=httpx.MockTransport(handler))
    pcm = np.zeros(16000 * 5, dtype=np.int16).tobytes()
    res = openai_stt.transcribe_window_openai("openai-gpt-4o-transcribe-diarize", pcm, 0.0, 5.0, None, http=http)
    assert res["status"] == "done" and res["attempts"] == 2 and len(res["segments"]) == 2
    body = seen[-1]
    assert 'name="model"\r\n\r\ngpt-4o-transcribe-diarize' in body
    assert 'name="response_format"\r\n\r\ndiarized_json' in body and 'name="chunking_strategy"\r\n\r\nauto' in body


def test_gpt_transcribe_request_and_hard_failure():
    def handler(request):
        body = request.content.decode("latin-1")
        assert 'name="model"\r\n\r\ngpt-transcribe' in body and "diarized_json" not in body
        return httpx.Response(400, json={"error": {"message": "Invalid file"}})
    http = httpx.Client(transport=httpx.MockTransport(handler))
    res = openai_stt.transcribe_window_openai("openai-gpt-transcribe", b"\0\0" * 16000, 0.0, 1.0, None, http=http)
    assert res["status"] == "failed" and "Invalid file" in res["error"] and res["attempts"] == 1


def test_missing_key_fails_cleanly(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY")
    res = openai_stt.transcribe_window_openai("openai-gpt-transcribe", b"\0\0" * 16000, 0.0, 1.0, None,
                                              http=httpx.Client(transport=httpx.MockTransport(lambda r: pytest.fail())))
    assert res["status"] == "failed" and "OPENAI_API_KEY" in res["error"]


def test_pipeline_uses_openai_per_window_and_logs_cost(tmp_path, staff, monkeypatch):
    calls = []

    def fake_post(model, wav_bytes, http=None):
        calls.append((model, len(wav_bytes)))
        return DIARIZED
    monkeypatch.setattr(openai_stt, "_post", fake_post)
    path = _audio(tmp_path, 200)  # two windows: [0,180), [177,200)
    with SessionLocal() as s:
        rid = str(s.execute(text("""
            INSERT INTO isf_recorder.recording (user_id, org_id, session_id, status, started_at)
            VALUES (:u, :o, :sid, 'processing', NOW()) RETURNING id
        """), {"u": staff["id"], "o": staff["org_id"], "sid": f"rec_test_{uuid.uuid4().hex}"}).scalar())
        s.commit()
    res = transcribe_chunked(path, recording_id=rid, model="openai-gpt-4o-transcribe-diarize", run_id=_run(rid))
    assert [c[0] for c in calls] == ["openai-gpt-4o-transcribe-diarize"] * 2
    assert res["segments"] and res["segments"][0]["speaker"] == "SPEAKER_00" and res["stats"]["failed_chunks"] == 0
    with SessionLocal() as s:
        rows = s.execute(text("""SELECT gemini_model, input_audio_tokens, output_tokens, total_cost_usd
                                 FROM isf_recorder.gemini_usage_log WHERE recording_id = :id"""), {"id": rid}).fetchall()
    assert len(rows) == 2 and rows[0].gemini_model == "openai-gpt-4o-transcribe-diarize"
    # 600 audio tokens * $2.50/1M + 40 output * $10/1M
    assert abs(float(rows[0].total_cost_usd) - (600 * 2.5 + 40 * 10) / 1e6) < 1e-6
    # a re-run reuses the finished windows (no new calls)
    calls.clear()
    transcribe_chunked(path, recording_id=rid, model="openai-gpt-4o-transcribe-diarize", run_id=_run(rid))
    assert calls == []


def test_models_selectable_for_transcription_only():
    from backend.settings.routes import AVAILABLE_MODELS
    ids = {m["id"]: m for m in AVAILABLE_MODELS}
    for mid in ("openai-gpt-4o-transcribe-diarize", "openai-gpt-transcribe"):
        assert ids[mid]["supports"] == ["transcription"]
