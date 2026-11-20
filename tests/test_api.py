from fastapi.testclient import TestClient

from backend.api import create_app
from backend.config import Settings


def test_local_boot_without_credentials() -> None:
    with TestClient(create_app(Settings(mode="local"))) as client:
        response = client.get("/api/health")
        assert response.json() == {"ok": True, "service": "ai-book-reader"}
        assert response.headers["cache-control"] == "no-store"
        assert client.get("/api/config").json()["recognition_available"] is False


def test_public_config_does_not_serialize_server_secrets(monkeypatch) -> None:
    monkeypatch.setenv("READER_OPENAI_API_KEY", "test-only-not-a-real-secret")
    monkeypatch.setenv("READER_CAMERA__STABLE_WINDOW_MS", "1500")
    with TestClient(create_app(Settings(mode="local"))) as client:
        response = client.get("/api/config")
        assert "test-only-not-a-real-secret" not in response.text
        assert "api_key" not in response.text
        assert response.json()["camera"]["stable_window_ms"] == 1500


def test_deployed_config_requires_gateway_authorization() -> None:
    with TestClient(create_app(Settings(mode="aws"))) as client:
        assert client.get("/api/health").status_code == 200
        assert client.get("/api/config").status_code == 401
        assert client.post("/api/sessions").status_code == 404
