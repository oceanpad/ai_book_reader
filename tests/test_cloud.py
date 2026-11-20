import asyncio
import json
import time
from io import BytesIO
from urllib.parse import urlsplit
from uuid import uuid4

import boto3
import httpx
import pytest
from botocore.exceptions import ClientError
from fastapi.testclient import TestClient
from moto import mock_aws
from PIL import Image

from backend.api import create_app
from backend.cloud import CloudStore, LostLease
from backend.config import Settings
from backend.contracts import Limits
from backend.local_reader import LocalError
from backend.maintenance import maintain
from backend.providers import Providers
from backend.worker import process


@pytest.fixture
def store():
    with mock_aws():
        region = "ap-southeast-1"
        boto3.client("dynamodb", region_name=region).create_table(
            TableName="reader-test",
            KeySchema=[
                {"AttributeName": "PK", "KeyType": "HASH"},
                {"AttributeName": "SK", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": name, "AttributeType": "S"}
                for name in ("PK", "SK", "workState", "updatedAt", "owner")
            ]
            + [{"AttributeName": "createdAt", "AttributeType": "N"}],
            BillingMode="PAY_PER_REQUEST",
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "MaintenanceIndex",
                    "KeySchema": [
                        {"AttributeName": "workState", "KeyType": "HASH"},
                        {"AttributeName": "updatedAt", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                },
                {
                    "IndexName": "OwnerCapturesIndex",
                    "KeySchema": [
                        {"AttributeName": "owner", "KeyType": "HASH"},
                        {"AttributeName": "createdAt", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "INCLUDE", "NonKeyAttributes": ["state"]},
                },
            ],
        )
        boto3.client("s3", region_name=region).create_bucket(
            Bucket="reader-test-media", CreateBucketConfiguration={"LocationConstraint": region}
        )
        queue = boto3.client("sqs", region_name=region).create_queue(QueueName="reader-test")[
            "QueueUrl"
        ]
        yield CloudStore(
            Settings(
                mode="aws",
                aws_region=region,
                table_name="reader-test",
                media_bucket="reader-test-media",
                queue_url=queue,
                secret_arn="test-secret",
                limits=Limits(stage_attempts=1),
            )
        )


def image():
    data = BytesIO()
    Image.new("RGB", (1200, 800), "beige").save(data, "JPEG")
    return data.getvalue()


def message(store, owner="owner-a", run="run-a", job="job-a"):
    store.submit(owner, run, job, image(), "spread")
    return {"owner": owner, "run": run, "id": job, "attempt": 1}


def vision():
    return httpx.Response(
        200,
        json={
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {
                        "content": json.dumps(
                            {
                                "page": {
                                    "is_book_page": True,
                                    "text_state": "readable",
                                    "ocr_text": "A green boat.",
                                    "illustration_description": "",
                                    "language": "en",
                                    "needs_retake": False,
                                    "retake_reason": None,
                                }
                            }
                        )
                    },
                }
            ]
        },
    )


def run(store, job, handler):
    async def execute():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await process(store, job, Providers(store.settings, client))

    asyncio.run(execute())


def test_reservation_is_idempotent_owned_and_single_active_capture(store):
    message(store)
    assert store.submit("owner-a", "run-a", "job-a", image(), "spread").state == "QUEUED"
    with pytest.raises(LocalError):
        message(store, job="job-b")
    with pytest.raises(LocalError):
        store.get("owner-b", "run-a", "job-a")
    store.cancel("owner-a", "run-a", "job-a")
    with pytest.raises(LocalError):
        message(store)
    assert message(store, job="job-b")["id"] == "job-b"


def test_worker_duplicate_delivery_calls_providers_only_once_and_saves_private_audio(store):
    job = message(store)
    calls = []

    def provider(request):
        calls.append(request.url.host)
        return (
            vision()
            if request.url.path == "/api/v1/chat/completions"
            else httpx.Response(
                200, content=b"\x00\x00" * 2400, headers={"content-type": "audio/pcm"}
            )
        )

    run(store, job, provider)
    run(store, job, provider)
    item = store.get("owner-a", "run-a", "job-a")
    assert item["state"] == "READY"
    assert len(calls) == 2
    assert len(item["audioKeys"]) == 1
    assert (
        urlsplit(store.audio_url("owner-a", "run-a", "job-a", 0)).hostname
        == "reader-test-media.s3.ap-southeast-1.amazonaws.com"
    )
    assert (
        store.s3.get_object(Bucket=store.settings.media_bucket, Key=item["audioKeys"][0])["Body"]
        .read()
        .startswith(b"RIFF")
    )
    assert "softExpires" not in item and "expiresAt" not in item and "workState" not in item
    thumb_key = item["thumbnailKey"]
    assert thumb_key.startswith("saved/") and item["audioKeys"][0].startswith("saved/")
    thumbnail = store.s3.get_object(Bucket=store.settings.media_bucket, Key=thumb_key)
    with Image.open(BytesIO(thumbnail["Body"].read())) as preview:
        assert preview.format == "JPEG" and max(preview.size) <= 256
    assert store.recent("owner-a").items[0].thumbnail_url.startswith("https://")
    assert thumb_key in store.thumbnail_url("owner-a", "run-a", "job-a")
    with pytest.raises(LocalError):
        store.thumbnail_url("owner-b", "run-a", "job-a")
    # A stale maintenance-index snapshot cannot expire a now-published job.
    with pytest.raises(ClientError):
        store.cancel("owner-a", "run-a", "job-a", expired_only=True)
    second = message(store, job="job-b")
    assert [entry.id for entry in store.recent("owner-a").items] == ["job-a"]
    run(store, second, provider)
    assert {entry.id for entry in store.recent("owner-a").items} == {"job-a", "job-b"}
    message(store, job="job-c")
    store.cancel("owner-a", "run-a", "job-a")
    with pytest.raises(LocalError):
        message(store, job="job-d")
    tombstone = store.raw("owner-a", "run-a", "job-a")
    assert "narration" not in tombstone and "visionJson" not in tombstone
    assert "audioKeys" not in tombstone and "leaseToken" not in tombstone
    assert "thumbnailKey" not in tombstone and "expiresAt" not in tombstone
    with pytest.raises(LocalError):
        store.thumbnail_url("owner-a", "run-a", "job-a")
    keys = [
        obj["Key"]
        for obj in store.s3.list_objects_v2(Bucket=store.settings.media_bucket)["Contents"]
    ]
    assert not any("/job-a/" in key for key in keys)
    assert any("/job-b/" in key for key in keys)


def test_speech_retry_checkpoints_vision_and_preserves_ownership(store):
    job = message(store)
    vision_calls, speech_calls = [], []

    def provider(request):
        if request.url.path == "/api/v1/chat/completions":
            vision_calls.append(True)
            return vision()
        speech_calls.append(True)
        return (
            httpx.Response(503)
            if len(speech_calls) == 1
            else httpx.Response(
                200, content=b"\x00\x00" * 2400, headers={"content-type": "audio/pcm"}
            )
        )

    run(store, job, provider)
    assert store.get("owner-a", "run-a", "job-a")["state"] == "FAILED"
    store.retry("owner-a", "run-a", "job-a")
    run(store, {**job, "attempt": 2}, provider)
    assert store.get("owner-a", "run-a", "job-a")["state"] == "READY"
    assert len(vision_calls) == 1 and len(speech_calls) == 2


def test_existing_capture_keeps_its_chunk_boundaries_across_configuration_changes(store):
    job = message(store)
    store.settings.speech_chunk_characters = 100
    spoken = []

    def provider(request):
        if request.url.path == "/api/v1/chat/completions":
            payload = vision().json()
            page = json.loads(payload["choices"][0]["message"]["content"])
            page["page"]["ocr_text"] = "A small green boat. " * 15
            payload["choices"][0]["message"]["content"] = json.dumps(page)
            return httpx.Response(200, json=payload)
        spoken.append(json.loads(request.content)["input"])
        return httpx.Response(
            200, content=b"\x00\x00" * 2400, headers={"content-type": "audio/pcm"}
        )

    run(store, job, provider)
    assert len(spoken) == 1
    assert store.get("owner-a", "run-a", "job-a")["state"] == "READY"


def test_expired_lease_is_repaired_and_old_worker_cannot_commit(store):
    job = message(store)
    item, token = store.claim("owner-a", "run-a", "job-a", 1)
    assert store.claim("owner-a", "run-a", "job-a", 1) is None
    store.table.update_item(
        Key=store.key("owner-a", "run-a", "job-a"),
        UpdateExpression="SET leaseUntil=:past",
        ExpressionAttributeValues={":past": int(time.time()) - 1},
    )
    maintain(store)
    recovered = store.get("owner-a", "run-a", "job-a")
    assert recovered["state"] == "QUEUED" and recovered["attempt"] == 2
    with pytest.raises(LostLease):
        store.complete(item, token, b"thumbnail")
    assert store.claim(job["owner"], job["run"], job["id"], 2) is not None


def test_delete_during_processing_fences_results_and_repeatedly_removes_late_media(store):
    job = message(store)

    def provider(request):
        store.cancel("owner-a", "run-a", "job-a")
        return vision()

    with pytest.raises(LostLease):
        run(store, job, provider)
    maintain(store)
    late_key = store.prefix("audio", "owner-a", "run-a", "job-a") + "late.wav"
    store.s3.put_object(Bucket=store.settings.media_bucket, Key=late_key, Body=b"late")
    maintain(store)
    assert not store.s3.list_objects_v2(Bucket=store.settings.media_bucket).get("Contents")
    with pytest.raises(LocalError):
        store.get("owner-a", "run-a", "job-a")


def test_cancel_during_upload_prevents_queued_job_and_removes_late_image(store, monkeypatch):
    put = store.s3.put_object

    def cancelled_upload(**kwargs):
        result = put(**kwargs)
        store.cancel("owner-a", "run-a", "job-a")
        return result

    monkeypatch.setattr(store.s3, "put_object", cancelled_upload)
    with pytest.raises(LocalError):
        message(store)
    assert store.raw("owner-a", "run-a", "job-a")["state"] == "CANCELLED"
    assert not store.s3.list_objects_v2(Bucket=store.settings.media_bucket).get("Contents")


def test_daily_paid_attempt_quota_survives_explicit_retry(store):
    store.settings.limits = Limits(stage_attempts=1, speech_attempts_per_day=1)
    job = message(store)
    paid = []

    def provider(request):
        if request.url.path == "/api/v1/chat/completions":
            return vision()
        paid.append(True)
        return httpx.Response(503)

    run(store, job, provider)
    store.retry("owner-a", "run-a", "job-a")
    run(store, {**job, "attempt": 2}, provider)
    assert len(paid) == 1
    assert "limit reached" in store.get("owner-a", "run-a", "job-a")["reason"]


def test_expiry_removes_only_its_own_media(store):
    message(store)
    message(store, owner="owner-b")
    store.table.update_item(
        Key=store.key("owner-a", "run-a", "job-a"),
        UpdateExpression="SET softExpires=:past",
        ExpressionAttributeValues={":past": int(time.time()) - 1},
    )
    maintain(store)
    maintain(store)
    remaining = store.s3.list_objects_v2(Bucket=store.settings.media_bucket)["Contents"]
    assert len(remaining) == 1 and "/owner-b/" in remaining[0]["Key"]


def test_maintenance_preserves_live_legacy_recording_beyond_original_expiry(store, monkeypatch):
    message(store)
    key = store.key("owner-a", "run-a", "job-a")
    old_audio = "audio/owner-a/run-a/job-a/old.wav"
    bucket = store.settings.media_bucket
    store.s3.put_object(Bucket=bucket, Key=old_audio, Body=b"old audio")
    store.table.update_item(
        Key=key,
        UpdateExpression="SET #s=:ready, workState=:ready, audioKeys=:keys",
        ExpressionAttributeNames={"#s": "state"},
        ExpressionAttributeValues={":ready": "READY", ":keys": [old_audio]},
    )
    maintain(store)
    deadline = time.time() + 86400 * 2
    monkeypatch.setattr(time, "time", lambda: deadline)
    store.s3.delete_object(Bucket=bucket, Key=old_audio)
    maintain(store)
    item = store.get("owner-a", "run-a", "job-a")
    assert "expiresAt" not in item and "softExpires" not in item
    assert store.recent("owner-a").items[0].thumbnail_url is None
    saved = store.s3.get_object(Bucket=bucket, Key=item["audioKeys"][0])
    assert saved["Body"].read() == b"old audio"


def test_cancel_during_publication_fences_durable_copies(store, monkeypatch):
    job = message(store)
    copy = store.s3.copy_object

    def cancel_then_copy(**kwargs):
        store.cancel(job["owner"], job["run"], job["id"])
        return copy(**kwargs)

    monkeypatch.setattr(store.s3, "copy_object", cancel_then_copy)

    def provider(request):
        if request.url.path == "/api/v1/chat/completions":
            return vision()
        return httpx.Response(200, content=bytes(4800), headers={"content-type": "audio/pcm"})

    with pytest.raises(LostLease):
        run(store, job, provider)
    maintain(store)
    assert not store.s3.list_objects_v2(Bucket=store.settings.media_bucket).get("Contents")
    assert not store.recent(job["owner"]).items


def test_recent_captures_pages_past_unplayable_jobs_and_limits_after_filtering(store):
    now = int(time.time())
    for i in range(28):
        state = "PROCESSING" if i >= 16 else "READY"
        owner = "other" if i == 15 else "owner"
        store.table.put_item(
            Item={
                **store.key(owner, f"run-{i}", f"job-{i}"),
                "id": f"job-{i}",
                "owner": owner,
                "run": f"run-{i}",
                "createdAt": now - 86400 + i,
                "state": state,
                "softExpires": now - 1 if i == 14 else now + 300,
                "audioKeys": [] if i == 13 else ["private.wav"],
                "narration": "Boat",
                "language": "en",
            }
        )
    store.cancel("owner", "run-12", "job-12")
    page = store.recent("owner")
    assert page.next_cursor
    assert page.model_dump()["items"] == [
        {
            "id": f"job-{i}",
            "run_id": f"run-{i}",
            "created_at": now - 86400 + i,
            "thumbnail_url": None,
            "narration": "Boat",
            "language": "en",
            "audio_chunks": 1,
        }
        for i in range(11, 1, -1)
    ]
    with pytest.raises(LocalError):
        store.recent("other", cursor=page.next_cursor)
    with pytest.raises(LocalError):
        store.recent("owner", cursor="not a cursor")
    page = store.recent("owner", limit=2, cursor=page.next_cursor)
    assert [entry.id for entry in page.items] == ["job-1", "job-0"]
    assert page.next_cursor is None


def test_cloud_routes_derive_owner_from_gateway_claims_and_check_scopes(store):
    app = create_app(store.settings)

    @app.middleware("http")
    async def fake_gateway(request, call_next):
        request.scope["aws.event"] = {
            "requestContext": {
                "authorizer": {
                    "jwt": {
                        "claims": {"sub": request.headers.get("X-Test-Owner", "")},
                        "scopes": request.headers.get(
                            "X-Test-Scope", "reader/read reader/write"
                        ).split(),
                    }
                }
            }
        }
        return await call_next(request)

    headers = {
        "X-Test-Owner": "owner-a",
        "X-Reader-Run": str(uuid4()),
        "Content-Type": "image/jpeg",
    }
    path = f"/api/readings/{uuid4()}"
    with TestClient(app) as client:
        assert client.post(path, headers=headers, content=image()).status_code == 202
        listing = "/api/readings"
        assert client.get(listing).status_code == 401
        assert (
            client.get(listing, headers={**headers, "X-Test-Scope": "reader/write"}).status_code
            == 403
        )
        store.table.update_item(
            Key=store.key("owner-a", headers["X-Reader-Run"], path.split("/")[-1]),
            UpdateExpression="SET #s=:ready, audioKeys=:keys",
            ExpressionAttributeNames={"#s": "state"},
            ExpressionAttributeValues={":ready": "READY", ":keys": ["private.wav"]},
        )
        listed = client.get(
            listing, headers={"X-Test-Owner": "owner-a", "X-Test-Scope": "reader/read"}
        )
        assert listed.json()["items"][0]["run_id"] == headers["X-Reader-Run"]
        assert client.get(
            listing + "?owner=owner-a", headers={"X-Test-Owner": "owner-b"}
        ).json() == {"items": [], "next_cursor": None}
        assert client.get(path, headers=headers).status_code == 200
        assert client.get(path, headers={**headers, "X-Test-Owner": "owner-b"}).status_code == 404
        assert (
            client.get(
                path + "/audio/0", headers={**headers, "X-Test-Owner": "owner-b"}
            ).status_code
            == 404
        )
        assert (
            client.post(
                path, headers={**headers, "X-Test-Scope": "reader/read"}, content=image()
            ).status_code
            == 403
        )
        assert client.get(path, headers={**headers, "X-Test-Owner": ""}).status_code == 401
