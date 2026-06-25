import pytest
import httpx
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.services.tts_service import synthesize
from app.database import Base, get_db
from app.main import app
from app.storage import audio_store

# In-memory SQLite for testing
SQLALCHEMY_DATABASE_URL = "sqlite:///./test_gateway.db"

engine = create_engine(
    SQLALCHEMY_DATABASE_URL, connect_args={"check_same_thread": False}
)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(scope="function")
def db_session():
    Base.metadata.create_all(bind=engine)
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)


@pytest.fixture(scope="function")
def client(db_session):
    def override_get_db():
        try:
            yield db_session
        finally:
            pass

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def test_health(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_signup_and_login(client, db_session):
    # Test Signup
    signup_payload = {
        "email": "test@example.com",
        "password": "securepassword123"
    }
    response = client.post("/signup", json=signup_payload)
    assert response.status_code == 201
    data = response.json()
    assert "api_key" in data
    assert data["email"] == "test@example.com"
    api_key = data["api_key"]

    # Verify OTP
    from app.models.otp import OTPVerification
    otp_record = db_session.query(OTPVerification).filter(OTPVerification.purpose == "signup").first()
    assert otp_record is not None
    verify_response = client.post(
        "/verify-otp",
        json={"email": "test@example.com", "otp_code": otp_record.otp_code}
    )
    assert verify_response.status_code == 200
    assert verify_response.json()["verified"] is True

    # Test Login
    login_payload = {
        "email": "test@example.com",
        "password": "securepassword123"
    }
    response = client.post("/login", json=login_payload)
    assert response.status_code == 200
    login_data = response.json()
    assert "access_token" in login_data
    assert login_data["api_key"] == api_key

    # Test Profile (JWT Auth)
    token = login_data["access_token"]
    response = client.get(
        "/profile",
        headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 200
    profile_data = response.json()
    assert profile_data["email"] == "test@example.com"
    assert profile_data["total_processing"] == 0


def test_signup_rejects_invalid_email(client):
    response = client.post(
        "/signup",
        json={"email": "not-an-email", "password": "securepassword123"},
    )

    assert response.status_code == 422


def test_text_to_speech_proxies_to_engine(client, db_session, monkeypatch):
    signup_response = client.post(
        "/signup",
        json={"email": "tts@example.com", "password": "securepassword123"},
    )
    api_key = signup_response.json()["api_key"]

    calls = []

    async def fake_synthesize(text: str, voice: str, format: str) -> bytes:
        calls.append({"text": text, "voice": voice, "format": format})
        return b"fake wav bytes"

    def fake_save_audio(relative_path: str, data: bytes) -> str:
        assert relative_path == "tts/1.wav"
        assert data == b"fake wav bytes"
        return "/audio/tts/1.wav"

    monkeypatch.setattr("app.routers.tts.synthesize", fake_synthesize)
    monkeypatch.setattr("app.routers.tts.save_audio", fake_save_audio)

    response = client.post(
        "/text-to-speech",
        json={"text": "Hello", "voice": "en-US-female-1", "format": "wav"},
        headers={"X-API-Key": api_key},
    )

    assert response.status_code == 200
    assert response.json()["audio_url"] == "/audio/tts/1.wav"
    assert calls == [{"text": "Hello", "voice": "en-US-female-1", "format": "wav"}]

    # Verify user with OTP
    from app.models.otp import OTPVerification
    from app.models.user import User
    user = db_session.query(User).filter(User.email == "tts@example.com").first()
    otp_record = db_session.query(OTPVerification).filter(OTPVerification.user_id == user.user_id, OTPVerification.purpose == "signup").first()
    assert otp_record is not None
    verify_response = client.post(
        "/verify-otp",
        json={"email": "tts@example.com", "otp_code": otp_record.otp_code}
    )
    assert verify_response.status_code == 200

    login_response = client.post(
        "/login",
        json={"email": "tts@example.com", "password": "securepassword123"},
    )
    token = login_response.json()["access_token"]
    profile_response = client.get(
        "/profile",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert profile_response.json()["total_processing"] == 1


def test_text_to_speech_rejects_unsupported_format(client):
    signup_response = client.post(
        "/signup",
        json={"email": "format@example.com", "password": "securepassword123"},
    )
    api_key = signup_response.json()["api_key"]

    response = client.post(
        "/text-to-speech",
        json={"text": "Hello", "voice": "en-US-female-1", "format": "mp3"},
        headers={"X-API-Key": api_key},
    )

    assert response.status_code == 422


@pytest.mark.anyio
async def test_tts_service_payload_matches_tts_microservice(monkeypatch):
    requests = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            status_code=200,
            content=b"RIFFfake wav",
            headers={"content-type": "audio/wav"},
        )

    transport = httpx.MockTransport(handler)
    original_client = httpx.AsyncClient

    def mock_client(*args, **kwargs):
        kwargs["transport"] = transport
        return original_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", mock_client)

    audio = await synthesize("Hello", "en-US-female-1", "wav")

    assert audio == b"RIFFfake wav"
    assert len(requests) == 1
    assert requests[0].url.path == "/v1/tts"
    assert requests[0].read() == b'{"text":"Hello","language":"en","voice":"Divya\'s voice is monotone yet slightly fast in delivery, with a very close recording that almost has no background noise."}'


def test_speech_to_text_passes_language_to_service(client, monkeypatch):
    signup_response = client.post(
        "/signup",
        json={"email": "stt@example.com", "password": "securepassword123"},
    )
    api_key = signup_response.json()["api_key"]

    calls = []

    async def fake_transcribe(
        audio_bytes: bytes,
        filename: str = "audio.wav",
        content_type: str = "audio/wav",
        language: str | None = None,
    ) -> dict:
        calls.append(
            {
                "audio_bytes": audio_bytes,
                "filename": filename,
                "content_type": content_type,
                "language": language,
            }
        )
        return {"text": "transcribed text", "language": "en", "words": []}

    def fake_save_audio(relative_path: str, data: bytes) -> str:
        assert relative_path == "stt/1_sample.wav"
        assert data == b"fake audio"
        return "/audio/stt/1_sample.wav"

    monkeypatch.setattr("app.routers.stt.transcribe", fake_transcribe)
    monkeypatch.setattr("app.routers.stt.save_audio", fake_save_audio)

    response = client.post(
        "/speech-to-text",
        files={"file": ("sample.wav", b"fake audio", "audio/wav")},
        data={"language": "en"},
        headers={"X-API-Key": api_key},
    )

    assert response.status_code == 200
    assert response.json()["detail"] == "transcribed text"
    assert calls == [
        {
            "audio_bytes": b"fake audio",
            "filename": "sample.wav",
            "content_type": "audio/wav",
            "language": "en",
        }
    ]


def test_speech_to_text_rejects_unsupported_media_type(client):
    signup_response = client.post(
        "/signup",
        json={"email": "media@example.com", "password": "securepassword123"},
    )
    api_key = signup_response.json()["api_key"]

    response = client.post(
        "/speech-to-text",
        files={"file": ("note.txt", b"not audio", "text/plain")},
        headers={"X-API-Key": api_key},
    )

    assert response.status_code == 415


def test_save_audio_sanitizes_file_names(tmp_path, monkeypatch):
    monkeypatch.setattr(audio_store.settings, "audio_storage_dir", str(tmp_path))

    url = audio_store.save_audio("stt/1_my unsafe file.wav", b"audio")

    assert url == "/audio/stt/1_my_unsafe_file.wav"
    assert (tmp_path / "stt" / "1_my_unsafe_file.wav").read_bytes() == b"audio"


def test_save_audio_rejects_path_traversal(tmp_path, monkeypatch):
    monkeypatch.setattr(audio_store.settings, "audio_storage_dir", str(tmp_path))

    with pytest.raises(ValueError):
        audio_store.save_audio("../escape.wav", b"audio")
