import asyncio
import base64
import json
import sqlite3
import time
import wave
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from threading import Event
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from backend.api import create_app
from backend.config import FileSettings, Settings
from backend.contracts import Limits, VisionResult
from backend.local_reader import LocalError, LocalReader
from backend.providers import ProviderError, speech_chunks


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
        original = client.get(path + "/original", headers=headers)
        assert original.headers["content-type"] == "image/jpeg"
        with Image.open(BytesIO(original.content)) as page:
            assert page.size == (1200, 800)
        assert (
            client.get(path + "/original", headers={"X-Reader-Run": str(uuid4())}).status_code
            == 404
        )
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


def test_local_completions_are_atomic_scoped_and_durable(tmp_path):
    db = tmp_path / "books.sqlite3"
    config = settings(local_books_path=str(db))
    owner = {"X-Reader-Run": str(uuid4())}
    other = {"X-Reader-Run": str(uuid4())}
    books = "/api/local/books"
    ids = [str(uuid4()) for _ in range(4)]
    body = {"completion_id": ids[0]}
    with TestClient(create_app(config)) as client:
        book = client.post(books, headers=owner, json={"name": "A"}).json()
        path = f"{books}/{book['id']}/completions"
        assert client.post(path, json=body).status_code == 422
        assert client.post(path, headers=other, json=body).status_code == 404
        assert client.post(path, headers=owner, json={"completion_id": "bad"}).status_code == 422

        def post(cid):
            return client.post(path, headers=owner, json={"completion_id": cid}).json()[
                "read_count"
            ]

        with ThreadPoolExecutor(max_workers=4) as pool:
            assert max(pool.map(post, ids * 3)) == len(ids)
        earlier = books + "/earlier/completions"
        for headers in (owner, other):
            assert client.post(earlier, headers=headers, json=body).json() == {"read_count": 1}
    with TestClient(create_app(config)) as client:
        assert client.post(path, headers=owner, json=body).json() == {"read_count": 4}
        assert [b["read_count"] for b in client.get(books, headers=owner).json()["items"]] == [1, 4]
        assert client.delete(f"{books}/{book['id']}", headers=owner).status_code == 204
        assert client.post(path, headers=owner, json=body).status_code == 404
        with sqlite3.connect(db) as conn:
            assert not conn.execute(
                "SELECT 1 FROM book_completions WHERE book_id=?", (book["id"],)
            ).fetchone()


def test_delete_local_book_removes_nonplayable_pages_and_preserves_earlier(tmp_path):
    config = settings(local_books_path=str(tmp_path / "books.sqlite3"))
    fail = False

    def provider(request):
        if fail:
            return httpx.Response(400)
        if request.url.path == "/api/v1/chat/completions":
            return completion(result())
        return httpx.Response(
            200, content=b"\x00\x00" * 2400, headers={"content-type": "audio/pcm"}
        )

    books = "/api/local/books"
    earlier, headers = new_request()
    target, stranger = new_request()
    with TestClient(create_app(config, transport=httpx.MockTransport(provider))) as client:
        book = client.post(books, headers=headers, json={"name": "Target"}).json()
        path = f"{books}/{book['id']}"
        assert submit(client, earlier, headers).status_code == 202
        assert wait_for_job(client, earlier, headers)["state"] == "READY"
        fail = True
        assert submit(client, target + f"?book_id={book['id']}", headers).status_code == 202
        assert wait_for_job(client, target, headers)["state"] == "FAILED"
        assert client.delete(path).status_code == 422
        assert client.delete(path, headers=stranger).status_code == 404
        assert client.delete(books + "/earlier", headers=headers).status_code == 409
        assert client.delete(path, headers=headers).status_code == 204
        assert client.get(target, headers=headers).status_code == 404
        assert client.get(earlier + "/audio/0", headers=headers).status_code == 200
    with TestClient(create_app(config)) as client:
        assert book not in client.get(books, headers=headers).json()["items"]


@pytest.mark.parametrize("unavailable", ["delete", "expire", "restart"])
def test_local_book_cover_on_legacy_schema(tmp_path, unavailable):
    db = tmp_path / "books.sqlite3"
    config = settings(local_books_path=str(db))
    page, headers = new_request()
    _, other = new_request()
    run, page_id = headers["X-Reader-Run"], page.split("/")[-1]
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE books(owner TEXT, id TEXT, name TEXT NOT NULL, PRIMARY KEY(owner,id))"
        )
        conn.execute("INSERT INTO books VALUES(?,?,?)", (run, "old", "My book"))

    def provider(request):
        if request.url.path == "/api/v1/chat/completions":
            return completion(result())
        return httpx.Response(200, content=b"\\0\\0" * 2400, headers={"content-type": "audio/pcm"})

    app = create_app(config, transport=httpx.MockTransport(provider))
    books = "/api/local/books"
    endpoint = books + "/old/cover"
    ref = {"run_id": run, "page_id": page_id}
    with TestClient(app) as client:
        assert submit(client, page + "?book_id=old", headers).status_code == 202
        assert wait_for_job(client, page, headers)["state"] == "READY"
        assert client.post(endpoint, json=ref).status_code == 422
        assert client.post(endpoint, headers=other, json=ref).status_code == 404
        assert client.post(books + "/earlier/cover", headers=headers, json=ref).status_code == 404
        assert client.post(
            books + "/old/completions", headers=headers, json={"completion_id": str(uuid4())}
        ).json() == {"read_count": 1}
        saved = client.post(endpoint, headers=headers, json=ref)
        assert saved.status_code == 200
        thumb = client.get("/api/local/readings", headers=headers).json()["items"][0][
            "thumbnail_url"
        ]
        assert saved.json()["cover"] == {**ref, "thumbnail_url": thumb}
        assert (
            client.post(
                endpoint, headers=headers, json={**ref, "run_id": other["X-Reader-Run"]}
            ).status_code
            == 404
        )
        assert (
            client.patch(books + "/old", headers=other, json={"name": "Other"}).status_code == 404
        )
        assert (
            client.patch(books + "/old", headers=headers, json={"name": "Renamed"}).status_code
            == 204
        )
        assert {**saved.json(), "name": "Renamed"} in client.get(books, headers=headers).json()[
            "items"
        ]
        with sqlite3.connect(db) as conn:
            assert conn.execute("SELECT * FROM book_covers").fetchall() == [(run, "old", page_id)]
        if unavailable == "delete":
            assert client.delete(page, headers=headers).status_code == 204
        elif unavailable == "expire":
            app.state.reader.jobs[page_id].created -= 3601
        if unavailable != "restart":
            assert not client.get(books, headers=headers).json()["items"][1].get("cover")
            assert client.post(endpoint, headers=headers, json=ref).status_code == 404
    with TestClient(create_app(config)) as client:
        assert client.get(books, headers=headers).json()["items"][1] == {
            "id": "old",
            "name": "Renamed",
            "read_count": 1,
        }


def test_successive_captures_keep_latest_ten_playable_in_same_run(tmp_path):
    def provider(request):
        if request.url.path == "/api/v1/chat/completions":
            return completion(result())
        return httpx.Response(
            200, content=b"\x00\x00" * 2400, headers={"content-type": "audio/pcm"}
        )

    config = settings(local_books_path=str(tmp_path / "books.sqlite3"))
    books = "/api/local/books"
    _, headers = new_request()
    with TestClient(create_app(config)) as client:
        assert client.get(books).status_code == 422
        created = client.post(books, headers=headers, json={"name": "My pages"})
        assert created.status_code == 201
        book = created.json()

    with TestClient(create_app(config, transport=httpx.MockTransport(provider))) as client:
        _, other_run = new_request()
        assert book in client.get(books, headers=headers).json()["items"]
        assert book not in client.get(books, headers=other_run).json()["items"]
        path, _ = new_request()
        query = f"?book_id={book['id']}"
        assert submit(client, path + query, other_run).status_code == 404
        ids = []
        for _ in range(11):
            path, _ = new_request()
            assert submit(client, path + query, headers).status_code == 202
            job = wait_for_job(client, path, headers)
            assert job["state"] == "READY"
            ids.append(job["id"])
        listing = "/api/local/readings"
        assert not client.get(listing + "?book_id=earlier", headers=headers).json()["items"]
        assert submit(client, path, headers).status_code == 409
        items = client.get(listing + query, headers=headers).json()["items"]
        assert [item["id"] for item in items] == ids[:0:-1]
        assert client.get(f"{listing}/{ids[1]}/audio/0", headers=headers).status_code == 200
        cursor = client.get(listing + query, headers=headers).json()["next_cursor"]
        more = client.get(
            listing, headers=headers, params={"cursor": cursor, "book_id": book["id"]}
        ).json()
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
        if expected == "REJECTED":
            [entry] = client.get("/api/local/readings?book_id=earlier", headers=headers).json()[
                "items"
            ]
            assert not entry["retryable"] and entry["reason"] == page["retake_reason"]
            assert (entry["state"], entry["audio_chunks"], entry["thumbnail_url"]) == (
                "REJECTED",
                0,
                None,
            )


def test_edit_validates_owner_and_text_and_keeps_failed_page_in_history():
    spoken = []

    def provider(request):
        if request.url.path == "/api/v1/chat/completions":
            assert not spoken
            return completion(result())
        spoken.append(json.loads(request.content)["input"])
        if len(spoken) == 2:
            return httpx.Response(503)
        return httpx.Response(
            200, content=bytes([len(spoken), 0]) * 240, headers={"content-type": "audio/pcm"}
        )

    with TestClient(create_app(settings(), transport=httpx.MockTransport(provider))) as client:
        path, headers = new_request()
        submit(client, path, headers)
        initial = wait_for_job(client, path, headers)
        edit = path + "/narration"
        audio = path + "/audio/0"
        original_audio = client.get(audio, headers=headers).content
        for invalid in [" \t", "x" * 20001, 123, None]:
            assert (
                client.post(edit, headers=headers, json={"narration": invalid}).status_code == 422
            )
        body = {"narration": " Updated text. "}
        assert client.post(edit, headers=new_request()[1], json=body).status_code == 404
        response = client.post(edit, headers=headers, json=body)
        assert response.status_code == 202 and response.json()["id"] == initial["id"]
        failed = wait_for_job(client, path, headers)
        assert failed["state"] == "FAILED" and failed["narration"] == "Updated text."
        assert client.get(audio, headers=headers).status_code == 409
        [entry] = client.get("/api/local/readings", headers=headers).json()["items"]
        assert entry["id"] == initial["id"] and entry["audio_chunks"] == 0
        assert entry["retryable"] and entry["reason"] == failed["reason"]
        assert client.get(path + "/original", headers=headers).status_code == 200
        assert client.post(path + "/retry", headers=headers).status_code == 202
        assert wait_for_job(client, path, headers)["state"] == "READY"
        assert client.get(audio, headers=headers).content != original_audio
        assert spoken == [initial["narration"], "Updated text.", "Updated text."]


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
        assert submit(client, other_path, headers).json()["state"] == "QUEUED"
        assert client.delete(path, headers=headers).status_code == 204
        assert cancelled.wait(1)
        assert client.get(path, headers=headers).status_code == 404
        assert submit(client, path, headers).status_code == 409


def test_process_wide_media_budget_is_separate_from_queue_capacity(monkeypatch):
    async def provider(request):
        await asyncio.sleep(30)
        return completion(result())

    app = create_app(settings(), transport=httpx.MockTransport(provider))
    with TestClient(app) as client:
        path, headers = new_request()
        second, other = new_request()
        assert submit(client, path, headers).status_code == 202
        used = len(app.state.reader.jobs[path.rsplit("/", 1)[1]].image)
        monkeypatch.setattr("backend.local_reader.LOCAL_MEDIA_LIMIT", used)
        assert submit(client, second, other).status_code == 429
        assert client.delete(path, headers=headers).status_code == 204
        assert submit(client, second, other).status_code == 202


def test_queue_limit_is_per_run_and_retries_keep_acceptance_order():
    async def exercise():
        started = asyncio.Queue()
        outcomes = asyncio.Queue()

        class GatedProviders:
            async def recognize(self, image, mode):
                await started.put(mode)
                if await outcomes.get() == "fail":
                    raise ProviderError("Try again", retryable=True)
                return VisionResult.model_validate(result())

            async def speak(self, text):
                return b"audio"

        reader = LocalReader(settings(), GatedProviders())
        image = jpeg()

        def capture(index, owner="owner"):
            page = f"{owner}-{index}"
            return reader.submit(owner, page, image, page)

        async def next_started():
            return await asyncio.wait_for(started.get(), timeout=2)

        try:
            pages = [capture(i) for i in range(10)]
            assert await next_started() == pages[0].id
            assert [page.state for page in pages] == ["PROCESSING", *["QUEUED"] * 9]
            assert capture(3) is pages[3]
            with pytest.raises(LocalError) as full:
                capture(10)
            assert full.value.status == 409
            other = [capture(i, "other") for i in range(10)]
            assert await next_started() == other[0].id
            for page in other:
                reader.remove("other", page.id)
            await asyncio.sleep(0)

            entries = reader.recent("owner", book_id="earlier").items
            assert [p.id for p in entries] == [p.id for p in reversed(pages)]
            assert all(p.audio_chunks == 0 and p.thumbnail_url is None for p in entries)
            assert [p.state for p in entries] == [p.state for p in reversed(pages)]

            await outcomes.put("fail")
            assert await next_started() == pages[1].id
            failed = reader.recent("owner", book_id="earlier").items[-1]
            assert (failed.state, failed.retryable, failed.reason) == ("FAILED", True, "Try again")
            tail = capture(10)
            with pytest.raises(LocalError):
                reader.retry("owner", pages[0].id)
            reader.remove("owner", pages[2].id)
            reader.retry("owner", pages[0].id)
            await outcomes.put("ok")
            assert await next_started() == pages[0].id
            last = capture(11)
            for page in [*pages[3:], tail, last]:
                await outcomes.put("ok")
                assert await next_started() == page.id
            assert pages[1].state == pages[0].state == "READY"
        finally:
            await reader.close()

    asyncio.run(exercise())


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
