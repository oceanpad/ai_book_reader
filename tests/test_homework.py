import base64
import json
from io import BytesIO
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from backend.api import create_app
from backend.config import Settings


def photo(color: str) -> str:
    data = BytesIO()
    Image.new("RGB", (80, 80), color).save(data, "JPEG")
    return base64.b64encode(data.getvalue()).decode("ascii")


@pytest.mark.parametrize("year", [2, 6])
def test_checks_multiple_photos_in_order_and_saves_private_local_history(tmp_path, year):
    def completion(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        prompt, *images = body["messages"][1]["content"]
        assert prompt["text"] == f"Subject: chinese. School level: Primary {year}."
        assert len(images) == 2
        with Image.open(
            BytesIO(base64.b64decode(images[0]["image_url"]["url"].split(",")[1]))
        ) as first:
            assert first.getpixel((0, 0))[0] > 200
        with Image.open(
            BytesIO(base64.b64decode(images[1]["image_url"]["url"].split(",")[1]))
        ) as second:
            assert second.getpixel((0, 0))[2] > 200
        result = {
            "questions": [
                {
                    "question": "小狗在做什么？",
                    "answer": "跑步。",
                    "steps": ["看看小狗的脚。"],
                    "language": "zh",
                }
            ],
            "note": "",
        }
        return httpx.Response(
            200,
            json={
                "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(result)}}]
            },
        )

    settings = Settings(local_books_path=str(tmp_path / "local.db"), openrouter_api_key="test")
    headers = {"X-Reader-Run": str(uuid4())}
    endpoint = "/api/local/homework"
    with TestClient(create_app(settings, transport=httpx.MockTransport(completion))) as client:
        response = client.post(
            endpoint,
            headers=headers,
            json={
                "subject": "chinese",
                "level": f"primary{year}",
                "images": [photo("red"), photo("blue")],
            },
        )
        assert response.status_code == 202
        entry = response.json()
        assert entry["state"] == "ready"
        assert entry["level"] == f"primary{year}"
        assert entry["result"]["questions"][0]["answer"] == "跑步。"
        assert "images" not in entry
    resource = f"{endpoint}/{entry['id']}"
    with TestClient(create_app(settings)) as client:
        other = {"X-Reader-Run": str(uuid4())}
        assert client.get(endpoint, headers=other).json()["items"] == []
        assert client.get(resource, headers=other).status_code == 404
        assert client.delete(resource, headers=other).status_code == 204
        assert client.get(endpoint, headers=headers).json()["items"] == [entry]
        assert client.delete(resource, headers=headers).status_code == 204
        assert client.get(resource, headers=headers).status_code == 404


def test_rejects_bad_uploads_without_returning_their_contents_or_calling_provider(tmp_path):
    settings = Settings(local_books_path=str(tmp_path / "local.db"), openrouter_api_key="test")
    headers = {"X-Reader-Run": str(uuid4())}

    def unexpected(request: httpx.Request) -> httpx.Response:
        raise AssertionError("Invalid photos must not reach the provider")

    with TestClient(create_app(settings, transport=httpx.MockTransport(unexpected))) as client:
        endpoint = "/api/local/homework"
        data = {"subject": "math", "level": "kindergarten", "images": ["private photo!"]}
        response = client.post(endpoint, headers=headers, json=data)
        assert response.status_code == 400
        assert "private photo" not in response.text
        response = client.post(endpoint, headers=headers, json={**data, "images": ["x"] * 5})
        assert response.status_code == 422
        assert client.get(endpoint, headers=headers).json()["items"] == []
