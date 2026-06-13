import sys
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base, get_db
from app.main import app
from app.config import get_settings

# In-memory SQLite for testing
SQLALCHEMY_DATABASE_URL = "sqlite:///./verify_gateway.db"

engine = create_engine(
    SQLALCHEMY_DATABASE_URL, connect_args={"check_same_thread": False}
)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def override_get_db():
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()


def run_tests():
    print("Setting up test database...")
    Base.metadata.create_all(bind=engine)

    app.dependency_overrides[get_db] = override_get_db
    client = TestClient(app)

    try:
        # 1. Health check
        print("Testing GET /health...")
        response = client.get("/health")
        assert response.status_code == 200, f"Expected 200, got {response.status_code}"
        assert response.json() == {"status": "ok"}, f"Expected status ok, got {response.json()}"
        print("[OK] GET /health passed.")

        # 2. Signup
        print("Testing POST /signup...")
        signup_payload = {
            "email": "verify@example.com",
            "password": "securepassword123"
        }
        response = client.post("/signup", json=signup_payload)
        assert response.status_code == 201, f"Expected 201, got {response.status_code}"
        data = response.json()
        assert "api_key" in data, "api_key missing in signup response"
        assert data["email"] == "verify@example.com"
        api_key = data["api_key"]
        print("[OK] POST /signup passed.")

        # 3. Login
        print("Testing POST /login...")
        login_payload = {
            "email": "verify@example.com",
            "password": "securepassword123"
        }
        response = client.post("/login", json=login_payload)
        assert response.status_code == 200, f"Expected 200, got {response.status_code}"
        login_data = response.json()
        assert "access_token" in login_data, "access_token missing in login response"
        assert login_data["api_key"] == api_key
        token = login_data["access_token"]
        print("[OK] POST /login passed.")

        # 4. Profile
        print("Testing GET /profile...")
        response = client.get(
            "/profile",
            headers={"Authorization": f"Bearer {token}"}
        )
        assert response.status_code == 200, f"Expected 200, got {response.status_code}"
        profile_data = response.json()
        assert profile_data["email"] == "verify@example.com"
        assert profile_data["total_processing"] == 0
        assert profile_data["total_failed"] == 0
        print("[OK] GET /profile passed.")

        # 5. Speech to Text (STT) stub
        print("Testing POST /speech-to-text...")
        # Create a mock wav file upload
        files = {"file": ("test.wav", b"fake audio bytes", "audio/wav")}
        headers = {"X-API-Key": api_key}
        response = client.post("/speech-to-text", files=files, headers=headers)
        assert response.status_code == 200, f"Expected 200, got {response.status_code}"
        stt_data = response.json()
        assert "request_id" in stt_data
        assert stt_data["detail"] == "Hello, welcome to the voice gateway."
        print("[OK] POST /speech-to-text (STT) passed.")

        # Verify that total_processing incremented
        response = client.get(
            "/profile",
            headers={"Authorization": f"Bearer {token}"}
        )
        profile_data = response.json()
        assert profile_data["total_processing"] == 1, f"Expected total_processing=1, got {profile_data['total_processing']}"
        print("[OK] Usage accounting checked.")

        print("\nAll integration checks completed successfully!")

    finally:
        app.dependency_overrides.clear()
        Base.metadata.drop_all(bind=engine)


if __name__ == "__main__":
    try:
        run_tests()
        sys.exit(0)
    except AssertionError as e:
        print(f"Assertion failed: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"Error during verification: {e}")
        sys.exit(1)
