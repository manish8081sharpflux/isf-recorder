"""
Test harness for the ISF Recorder backend.

Runs against a LOCAL copy of the production schema (database isf_jomi_test, migration 005 applied).
- DATABASE_URL: taken from env ISF_TEST_DATABASE_URL, else built from config.DEFAULT_LOCAL_URL with the
  database name replaced by isf_jomi_test. Any other database name is refused.
- AUDIO_STORAGE_ROOT: a fresh temp directory per test session.
- ISF_DISABLE_BACKGROUND_JOBS=1: no scheduler (no sweeper/resume/WhatsApp/forex jobs).
- Every Gemini / AssemblyAI call must be mocked by the test; the forex internet fetch is disabled.
"""
import ast
import os
import sys
import tempfile
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def _test_db_url() -> str:
    url = os.getenv("ISF_TEST_DATABASE_URL")
    if not url:
        src = (ROOT / "backend" / "config.py").read_text(encoding="utf-8")
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, ast.Assign) and any(getattr(t, "id", None) == "DEFAULT_LOCAL_URL" for t in node.targets):
                url = node.value.value.rsplit("/", 1)[0] + "/isf_jomi_test"
    if not url or not url.rstrip("/").endswith("/isf_jomi_test"):
        raise RuntimeError("Tests only run against the local isf_jomi_test database")
    return url


os.environ["DATABASE_URL"] = _test_db_url()
os.environ["ISF_DISABLE_BACKGROUND_JOBS"] = "1"
AUDIO_ROOT = tempfile.mkdtemp(prefix="isf_audio_test_")
os.environ["AUDIO_STORAGE_ROOT"] = AUDIO_ROOT
# Fake keys win over any .env file (load_dotenv never overrides existing variables)
for _k in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "ASSEMBLYAI_API_KEY", "WHATSAPP_ACCESS_TOKEN"):
    os.environ[_k] = "test-key-not-used"

from sqlalchemy import text  # noqa: E402

from backend.database import SessionLocal  # noqa: E402
from backend.auth.jwt_handler import create_token  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"
ISF_ORG_SLUG = "isf"


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    from backend.transcription import forex_sync
    monkeypatch.setattr(forex_sync, "_fetch_from_internet", lambda: None)

    def _blocked(*a, **k):
        raise AssertionError("Real Gemini client requested in a test; mock it")
    import backend.transcription.gemini_stt as gstt
    import backend.transcription.intelligence as intel
    import backend.chat.gemini_chat as gchat
    import backend.transcription.assemblyai_stt as aai
    for mod in (gstt, intel, gchat):
        monkeypatch.setattr(mod, "get_genai_client", _blocked)

    async def _blocked_async(*a, **k):
        raise AssertionError("Real AssemblyAI call in a test; mock it")
    monkeypatch.setattr(aai, "upload_audio_file", _blocked_async)
    yield


@pytest.fixture(scope="session")
def app():
    from backend.main import app as fastapi_app
    return fastapi_app


@pytest.fixture(scope="session")
def client(app):
    from fastapi.testclient import TestClient
    with TestClient(app) as c:
        yield c


@pytest.fixture
def db():
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


def org_id() -> str:
    with SessionLocal() as s:
        return str(s.execute(text("SELECT id FROM isf_recorder.organization WHERE slug = :s"), {"s": ISF_ORG_SLUG}).scalar())


def make_user(role: str = "staff", name: str = None, phone: str = None) -> dict:
    """Inserts a throwaway user in the ISF org and returns {id, name, phone, role, org_id, token}."""
    name = name or f"Test {uuid.uuid4().hex[:6]}"
    phone = phone or ("91" + "7" + str(uuid.uuid4().int)[:9])
    oid = org_id()
    with SessionLocal() as s:
        uid = s.execute(text("""
            INSERT INTO isf_recorder.user (org_id, name, phone, role) VALUES (:o, :n, :p, :r) RETURNING id
        """), {"o": oid, "n": name, "p": phone, "r": role}).scalar()
        s.commit()
    user = {"id": str(uid), "name": name, "phone": phone, "role": role, "org_id": oid}
    user["token"] = create_token(user_id=user["id"], org_id=oid, role=role, phone=phone, name=name)
    user["headers"] = {"Authorization": f"Bearer {user['token']}"}
    return user


def delete_user(user_id: str) -> None:
    with SessionLocal() as s:
        s.execute(text("DELETE FROM isf_recorder.recording WHERE user_id = :u"), {"u": user_id})
        s.execute(text("DELETE FROM isf_recorder.user WHERE id = :u"), {"u": user_id})
        s.commit()


@pytest.fixture
def staff():
    u = make_user("staff")
    yield u
    delete_user(u["id"])


@pytest.fixture
def admin():
    u = make_user("admin")
    yield u
    delete_user(u["id"])


def wav_bytes(seconds: float = 2.0, amplitude: int = 0, freq: float = 0.0, rate: int = 16000) -> bytes:
    """16 kHz mono 16-bit WAV: silence (amplitude 0), a tone, or white noise (freq < 0)."""
    import io
    import wave
    import numpy as np
    n = int(seconds * rate)
    if amplitude == 0:
        pcm = np.zeros(n, dtype=np.int16)
    elif freq < 0:
        pcm = (np.random.default_rng(1).standard_normal(n) * amplitude).clip(-32768, 32767).astype(np.int16)
    else:
        t = np.arange(n) / rate
        pcm = (np.sin(2 * np.pi * freq * t) * amplitude).astype(np.int16)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


def speech_pcm() -> bytes:
    import wave
    with wave.open(str(FIXTURES / "speech_en_16k.wav"), "rb") as w:
        return w.readframes(w.getnframes())


def write_wav(path: Path, pcm: bytes, rate: int = 16000) -> Path:
    import wave
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return path


class FakeResponse:
    def __init__(self, text_out: str, finish: str = "STOP", out_tokens: int = 100):
        self.text = text_out
        self.candidates = [type("C", (), {"finish_reason": f"FinishReason.{finish}"})()]
        self.usage_metadata = type("U", (), {
            "prompt_token_count": 1000, "candidates_token_count": out_tokens, "total_token_count": 1000 + out_tokens,
            "cached_content_token_count": 0, "thoughts_token_count": 0, "prompt_tokens_details": None,
        })()


class FakeGenaiClient:
    """Stands in for google.genai.Client; `responder(model, contents, config)` returns a FakeResponse."""

    def __init__(self, responder):
        self.calls = []
        outer = self

        class _Models:
            def generate_content(self, model, contents, config=None):
                outer.calls.append({"model": model, "contents": contents, "config": config})
                return responder(model, contents, config)

        self.models = _Models()
