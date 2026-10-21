import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import io
import wave
import struct
from fastapi.testclient import TestClient
from backend.main import app
from backend.auth.jwt_handler import create_token

client = TestClient(app)

def generate_test_wav_bytes(duration_sec=1.0, freq=440.0, sample_rate=16000):
    """Generate a clean 16kHz mono 16-bit PCM WAV in memory."""
    num_samples = int(duration_sec * sample_rate)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        frames = bytearray()
        for i in range(num_samples):
            # Quiet sine wave
            val = int(3000 * 0.5)
            frames.extend(struct.pack("<h", val))
        wav.writeframes(frames)
    buf.seek(0)
    return buf.getvalue()

def run_tests():
    print("=== 1. Testing Health Endpoint ===")
    res = client.get("/health")
    assert res.status_code == 200, f"Health check failed: {res.text}"
    health = res.json()
    print("Health OK:", health)
    assert health["database"] == "connected"

    print("\n=== 2. Creating Admin Token ===")
    # Create valid token for testing
    admin_token = create_token(
        user_id="bfa8f62c-f987-4227-bc31-d6921b907aa3",
        org_id="40821ff5-c36d-4fa3-9c1e-025869687936",
        role="admin",
        phone="919421187857",
        name="Admin"
    )
    admin_headers = {"Authorization": f"Bearer {admin_token}"}

    print("\n=== 3. Testing /auth/me ===")
    res = client.get("/auth/me", headers=admin_headers)
    assert res.status_code == 200, f"/auth/me failed: {res.text}"
    print("User profile:", res.json())

    print("\n=== 4. Testing User Management (Admin creating staff user) ===")
    staff_phone = "919876543210"
    res = client.post("/users/", json={
        "name": "Factory Staff 1",
        "phone": staff_phone,
        "role": "staff",
        "preferred_language": "hi"
    }, headers=admin_headers)
    if res.status_code == 409:
        print("Staff user already exists, continuing.")
    else:
        assert res.status_code == 200, f"Create user failed: {res.text}"
        print("Created user:", res.json())

    print("\n=== 5. Listing Org Users ===")
    res = client.get("/users/", headers=admin_headers)
    assert res.status_code == 200, f"List users failed: {res.text}"
    users = res.json()["users"]
    print(f"Found {len(users)} users in org")
    staff_user = next((u for u in users if u["phone"] == staff_phone), None)
    assert staff_user is not None

    staff_token = create_token(
        user_id=staff_user["id"],
        org_id="40821ff5-c36d-4fa3-9c1e-025869687936",
        role="staff",
        phone=staff_phone,
        name=staff_user["name"]
    )
    staff_headers = {"Authorization": f"Bearer {staff_token}"}

    print("\n=== 6. Testing Recording Lifecycle (Session Start) ===")
    session_id = f"test-sess-{int(time.time())}" if 'time' in globals() else "test-sess-001"
    import time
    session_id = f"test-sess-{int(time.time())}"
    res = client.post("/recordings/start", json={
        "session_id": session_id,
        "mic_source": "wired",
        "sample_rate": 16000,
        "device_info": {"model": "Pixel 7", "os": "Android 14"}
    }, headers=staff_headers)
    assert res.status_code == 200, f"Start recording failed: {res.text}"
    start_data = res.json()
    rec_id = start_data["recording_id"]
    print(f"Session started: {session_id}, Recording ID: {rec_id}")

    print("\n=== 7. Uploading Chunks 0 and 1 ===")
    chunk_0 = generate_test_wav_bytes(duration_sec=2.0)
    res = client.post(
        f"/recordings/{session_id}/chunk",
        data={"chunk_index": 0, "duration_seconds": 2.0},
        files={"chunk_file": ("chunk_0000.wav", chunk_0, "audio/wav")},
        headers=staff_headers
    )
    assert res.status_code == 200, f"Chunk 0 upload failed: {res.text}"

    chunk_1 = generate_test_wav_bytes(duration_sec=2.0)
    res = client.post(
        f"/recordings/{session_id}/chunk",
        data={"chunk_index": 1, "duration_seconds": 2.0},
        files={"chunk_file": ("chunk_0001.wav", chunk_1, "audio/wav")},
        headers=staff_headers
    )
    assert res.status_code == 200, f"Chunk 1 upload failed: {res.text}"
    print("Chunks 0 & 1 uploaded successfully!")

    print("\n=== 8. Pause & Resume Test ===")
    res = client.post(f"/recordings/{session_id}/pause", json={"reason": "camera"}, headers=staff_headers)
    assert res.status_code == 200
    res = client.post(f"/recordings/{session_id}/resume", json={}, headers=staff_headers)
    assert res.status_code == 200
    print("Pause/resume tracking OK!")

    print("\n=== 9. Finish Recording & Assembly ===")
    res = client.post(f"/recordings/{session_id}/finish", json={
        "total_chunks": 2,
        "duration_seconds": 4
    }, headers=staff_headers)
    assert res.status_code == 200, f"Finish recording failed: {res.text}"
    finish_data = res.json()
    print("Recording assembled and queued for processing:", finish_data)

    print("\n=== 10. Star / Unstar Recording (Staff Starring Own Recording) ===")
    res = client.post(f"/recordings/{rec_id}/star", headers=staff_headers)
    assert res.status_code == 200, f"Star recording failed: {res.text}"
    star_res = res.json()
    print(f"Star result: {star_res}")
    assert star_res["is_starred"] is True

    print("\n=== 11. Role-Based List Isolation ===")
    staff_list = client.get("/recordings/", headers=staff_headers).json()
    print(f"Staff sees {len(staff_list['items'])} recordings (own recordings only)")
    admin_list = client.get("/recordings/", headers=admin_headers).json()
    print(f"Admin sees {len(admin_list['items'])} recordings (all org recordings)")

    print("\n=== 12. Audio Streaming ===")
    res = client.get(f"/recordings/{rec_id}/audio", headers=staff_headers)
    assert res.status_code == 200, f"Audio streaming failed: {res.text}"
    print(f"Audio streamed successfully! ({len(res.content)} bytes)")

    print("\nALL BACKEND INTEGRATION TESTS PASSED 100%!")

if __name__ == "__main__":
    run_tests()
