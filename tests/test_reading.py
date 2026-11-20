import asyncio
import base64
import json
import time
import wave
from io import BytesIO
from threading import Event
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from backend.api import create_app
from backend.config import FileSettings, Settings
from backend.contracts import Limits
from backend.providers import speech_chunks


def jpeg(width=1200, height=800):
    output = BytesIO()
    image = Image.new("RGB", (width, height), "beige")
    exif = Image.Exif()
    exif[270] = "synthetic metadata that must not reach the provider"
    image.save(output, format="JPEG", exif=exif)
    return output.getvalue()


def settings(**overrides):
    return Settings(
        mode="local",
        openrouter_api_key="test-openrouter",
        limits=Limits(stage_attempts=1),
        **overrides,
    )


def result(**overrides):
    return {
        "is_book_page": True,
        "text_state": "readable",
        "ocr_text": "A small green boat.",
        "illustration_description": "",
        "language": "en",
        "needs_retake": False,
        "retake_reason": None,
        **overrides,
    }


def completion(page):
    return httpx.Response(
        200,
        json={
            "choices": [
                {"finish_reason": "stop", "message": {"content": json.dumps({"page": page})}}
            ]
        },
    )


def wait_for_job(client, path, headers):
    for _ in range(200):
        response = client.get(path, headers=headers)
        assert response.status_code == 200, response.text
        job = response.json()
        if job["state"] not in ("QUEUED", "PROCESSING"):
            return job
        time.sleep(0.01)
    pytest.fail("Local background processing did not finish")


def new_request():
    return f"/api/local/readings/{uuid4()}", {"X-Reader-Run": str(uuid4())}


def submit(client, path, headers, image=None):
    return client.post(
        path,
        headers={**headers, "Content-Type": "image/jpeg"},
        content=jpeg() if image is None else image,
    )


def test_capture_to_vision_to_wav_is_idempotent_and_private_to_run():
    calls = []

    def provider(request):
        payload = json.loads(request.content)
        calls.append(request.url.path)
        if request.url.path == "/api/v1/chat/completions":
            assert request.url.host == "openrouter.ai"
            assert request.headers["authorization"] == "Bearer test-openrouter"
            assert payload["model"] == "google/gemini-3.5-flash-lite"
            assert payload["response_format"]["json_schema"]["strict"] is True
            images = [
                part["image_url"]
                for part in payload["messages"][1]["content"]
                if part["type"] == "image_url"
            ]
            assert len(images) == 4
            assert all(image["detail"] == "high" for image in images)
            for image in images:
                with Image.open(BytesIO(base64.b64decode(image["url"].split(",")[1]))) as decoded:
                    assert not decoded.getexif()
            return completion(result(ocr_text="小船慢慢地走。", language="zh"))
        assert payload["input"] == "小船慢慢地走。"
        assert payload["response_format"] == "pcm"
        return httpx.Response(
            200, content=b"\x01\x00" * 2400, headers={"content-type": "audio/pcm"}
        )

    with TestClient(create_app(settings(), transport=httpx.MockTransport(provider))) as client:
        path, headers = new_request()
        image = jpeg()
        assert submit(client, path, headers, image).status_code == 202
        job = wait_for_job(client, path, headers)
        assert job["state"] == "READY"
        assert job["narration"] == "小船慢慢地走。"
        listing = "/api/local/readings"
        assert client.get(listing).status_code == 422
        assert client.get(listing, headers={"X-Reader-Run": str(uuid4())}).json() == {
            "items": [],
            "next_cursor": None,
        }
        [entry] = client.get(listing, headers=headers).json()["items"]
        prefix, encoded = entry.pop("thumbnail_url").split(",", 1)
        assert prefix == "data:image/jpeg;base64"
        with Image.open(BytesIO(base64.b64decode(encoded))) as thumbnail:
            assert thumbnail.format == "JPEG" and max(thumbnail.size) <= 256
        assert entry == {
            "id": job["id"],
            "run_id": headers["X-Reader-Run"],
            "created_at": entry["created_at"],
            "language": "zh",
            "narration": job["narration"],
            "audio_chunks": 1,
        }
        assert abs(time.time() - entry["created_at"]) < 60
        audio = client.get(path + "/audio/0", headers=headers)
        assert audio.headers["content-type"] == "audio/wav"
        with wave.open(BytesIO(audio.content)) as wav:
            assert wav.getframerate() == 24000
            assert wav.getnframes() == 2400
            assert wav.readframes(2400) == b"\x01\x00" * 2400
        assert submit(client, path, headers, image).status_code == 202
        assert len(calls) == 2
        assert submit(client, path, headers, jpeg(1000, 800)).status_code == 409
        assert client.get(path, headers={"X-Reader-Run": str(uuid4())}).status_code == 404
        assert client.get(path + "/audio/0").status_code == 422
        assert client.delete(path, headers=headers).status_code == 204
        assert client.get(path, headers=headers).status_code == 404
        assert client.get(listing, headers=headers).json() == {"items": [], "next_cursor": None}
        # Discard before/after upload completion cannot resurrect a cancelled ID.
        assert submit(client, path, headers, image).status_code == 409


def test_successive_captures_keep_latest_ten_playable_in_same_run():
    def provider(request):
        if request.url.path == "/api/v1/chat/completions":
            return completion(result())
        return httpx.Response(
            200, content=b"\x00\x00" * 2400, headers={"content-type": "audio/pcm"}
        )

    with TestClient(create_app(settings(), transport=httpx.MockTransport(provider))) as client:
        _, headers = new_request()
        ids = []
        for _ in range(11):
            path, _ = new_request()
            assert submit(client, path, headers).status_code == 202
            job = wait_for_job(client, path, headers)
            assert job["state"] == "READY"
            ids.append(job["id"])
        listing = "/api/local/readings"
        items = client.get(listing, headers=headers).json()["items"]
        assert [item["id"] for item in items] == ids[:0:-1]
        assert client.get(f"{listing}/{ids[1]}/audio/0", headers=headers).status_code == 200
        cursor = client.get(listing, headers=headers).json()["next_cursor"]
        more = client.get(listing, headers=headers, params={"cursor": cursor}).json()
        assert [item["id"] for item in more["items"]] == [ids[0]]
        assert more["next_cursor"] is None
        assert client.get(f"{listing}/{ids[0]}/audio/0", headers=headers).status_code == 200
        assert client.get(listing, headers=headers, params={"cursor": "invalid"}).status_code == 400


@pytest.mark.parametrize(
    "page,expected,speech_calls",
    [
        (
            result(
                text_state="absent", ocr_text="", illustration_description="A green boat floats."
            ),
            "READY",
            1,
        ),
        (
            result(
                text_state="unreadable",
                ocr_text="",
                needs_retake=True,
                retake_reason="Move closer.",
            ),
            "REJECTED",
            0,
        ),
        (
            result(
                is_book_page=False,
                text_state="absent",
                ocr_text="",
                needs_retake=True,
                retake_reason="Frame a book.",
            ),
            "REJECTED",
            0,
        ),
    ],
)
def test_picture_only_and_retake_paths(page, expected, speech_calls):
    speech = []

    def provider(request):
        if request.url.path == "/api/v1/chat/completions":
            return completion(page)
        speech.append(json.loads(request.content)["input"])
        return httpx.Response(
            200, content=b"\x00\x00" * 2400, headers={"content-type": "audio/pcm"}
        )

    with TestClient(create_app(settings(), transport=httpx.MockTransport(provider))) as client:
        path, headers = new_request()
        assert submit(client, path, headers).status_code == 202
        assert wait_for_job(client, path, headers)["state"] == expected
        assert len(speech) == speech_calls


def test_speech_retry_reuses_vision_and_completed_audio_chunks():
    text = "A small boat floats. " * 12
    chunks = speech_chunks(text.strip(), 100)
    vision_calls = []
    spoken = []

    def provider(request):
        if request.url.path == "/api/v1/chat/completions":
            vision_calls.append(True)
            return completion(result(ocr_text=text))
        spoken.append(json.loads(request.content)["input"])
        if len(spoken) == 2:
            return httpx.Response(503, json={"error": "transient test failure"})
        return httpx.Response(
            200, content=b"\x00\x00" * 2400, headers={"content-type": "audio/pcm"}
        )

    with TestClient(
        create_app(settings(speech_chunk_characters=100), transport=httpx.MockTransport(provider))
    ) as client:
        path, headers = new_request()
        submit(client, path, headers)
        failed = wait_for_job(client, path, headers)
        assert failed["state"] == "FAILED" and failed["stage"] == "speech"
        assert failed["narration"] == text.strip()
        assert client.post(path + "/retry", headers=headers).status_code == 202
        assert wait_for_job(client, path, headers)["state"] == "READY"
        assert len(vision_calls) == 1
        assert spoken == [chunks[0], chunks[1], *chunks[1:]]


@pytest.mark.parametrize(
    "reply",
    [
        httpx.Response(401, json={"error": "must-not-leak-provider-body"}),
        httpx.Response(
            200, json={"choices": [{"finish_reason": "stop", "message": {"refusal": "no"}}]}
        ),
        completion(result(text_state="unreadable")),
    ],
)
def test_provider_failures_are_visible_without_leaking_response_body(reply):
    with TestClient(
        create_app(settings(), transport=httpx.MockTransport(lambda request: reply))
    ) as client:
        path, headers = new_request()
        submit(client, path, headers)
        job = wait_for_job(client, path, headers)
        assert job["state"] == "FAILED"
        assert job["reason"]
        assert "must-not-leak" not in json.dumps(job)


def test_invalid_images_and_cross_origin_requests_never_reach_provider():
    calls = []
    with TestClient(
        create_app(settings(), transport=httpx.MockTransport(lambda request: calls.append(request)))
    ) as client:
        path, headers = new_request()
        assert submit(client, path, headers, b"not an image").status_code == 400
        assert submit(client, path, headers, jpeg(4200, 800)).status_code == 400
        assert submit(client, path, headers, b"x" * (4 * 1024 * 1024 + 1)).status_code == 413
        assert (
            submit(client, path, {**headers, "Origin": "https://untrusted.invalid"}).status_code
            == 403
        )
        assert not calls


def test_missing_configuration_is_actionable_and_local_routes_do_not_exist_on_aws(monkeypatch):
    for name in (
        "OPENAI_API_KEY",
        "OPENROUTER_API_KEY",
        "READER_OPENAI_API_KEY",
        "READER_OPENROUTER_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    with TestClient(create_app(Settings(mode="local"))) as client:
        path, headers = new_request()
        response = submit(client, path, headers)
        assert response.status_code == 503
        assert "Configure server-only" in response.json()["detail"]
    with TestClient(create_app(Settings(mode="aws"))) as client:
        assert submit(client, path, headers).status_code == 404


def test_dotenv_style_envrc_keys_are_loaded_only_server_side(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".envrc").write_text(
        "export OPENAI_API_KEY=test-ai\nexport OPENROUTER_API_KEY=test-router\n"
    )
    (tmp_path / ".env").write_text("READER_OPENAI_API_KEY=\nREADER_OPENROUTER_API_KEY=\n")
    config = FileSettings()
    assert config.openai_api_key.get_secret_value() == "test-ai"
    assert config.public_config().speech_available is True
    assert "test-ai" not in config.public_config().model_dump_json()


def test_sentence_chunks_preserve_every_character_without_truncation():
    text = "One sentence. Another sentence!\n" * 100 + "长" * 300
    chunks = speech_chunks(text, 100)
    assert "".join(chunks) == text
    assert all(0 < len(chunk) <= 100 for chunk in chunks)
    assert speech_chunks("First ends. " + "word " * 30, 100)[0] == "First ends. "


def test_discard_cancels_inflight_provider_work_and_blocks_late_submission():
    started, cancelled = Event(), Event()

    async def provider(request):
        started.set()
        try:
            await asyncio.sleep(30)
        finally:
            cancelled.set()
        return completion(result())

    with TestClient(create_app(settings(), transport=httpx.MockTransport(provider))) as client:
        path, headers = new_request()
        assert submit(client, path, headers).status_code == 202
        assert started.wait(1)
        other_path, _ = new_request()
        assert submit(client, other_path, headers).status_code == 409
        assert client.delete(path, headers=headers).status_code == 204
        assert cancelled.wait(1)
        assert client.get(path, headers=headers).status_code == 404
        assert submit(client, path, headers).status_code == 409


def test_transient_vision_failure_has_three_attempt_bound_and_enforced_quota(monkeypatch):
    calls = []

    async def no_backoff(seconds):
        return None

    monkeypatch.setattr("backend.local_reader.asyncio.sleep", no_backoff)

    def provider(request):
        calls.append(True)
        return httpx.Response(429, json={"error": "test rate limit"})

    config = settings()
    config.limits = Limits(stage_attempts=3, capture_attempts_per_day=3)
    with TestClient(create_app(config, transport=httpx.MockTransport(provider))) as client:
        path, headers = new_request()
        submit(client, path, headers)
        job = wait_for_job(client, path, headers)
        assert job["state"] == "FAILED" and job["retryable"]
        assert len(calls) == 3
        client.post(path + "/retry", headers=headers)
        job = wait_for_job(client, path, headers)
        assert "limit reached" in job["reason"]
        assert len(calls) == 3


@pytest.mark.parametrize(
    "audio,media_type",
    [
        (b"", "audio/pcm"),
        (b"x", "audio/pcm"),
        (b"RIFFbad", "audio/wav"),
        (b'{"error":"no audio"}', "application/json"),
    ],
)
def test_invalid_audio_is_a_visible_speech_failure_not_a_ready_page(audio, media_type):
    def provider(request):
        if request.url.path == "/api/v1/chat/completions":
            return completion(result())
        return httpx.Response(200, content=audio, headers={"content-type": media_type})

    with TestClient(create_app(settings(), transport=httpx.MockTransport(provider))) as client:
        path, headers = new_request()
        submit(client, path, headers)
        job = wait_for_job(client, path, headers)
        assert job["state"] == "FAILED" and job["stage"] == "speech"
        assert job["narration"] == "A small green boat."
        if media_type != "application/json":
            assert job["retryable"]
        assert client.get(path + "/audio/0", headers=headers).status_code == 409
