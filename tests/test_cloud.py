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
                for name in ("PK", "SK", "workState", "updatedAt", "owner", "ownerBook")
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
                {
                    "IndexName": "BookCapturesIndex",
                    "KeySchema": [
                        {"AttributeName": "ownerBook", "KeyType": "HASH"},
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


def test_owner_cap_counts_processing_across_books_and_runs_atomically(store, monkeypatch):
    book = store.create_book("owner-a", "Another book")
    message(store)
    store.claim("owner-a", "run-a", "job-a", 1)
    for i in range(8):
        store.submit("owner-a", "run-b", str(i), image(), "spread", book.id)
    transact = store.ddb.transact_write_items

    def interleave(**kwargs):
        monkeypatch.setattr(store.ddb, "transact_write_items", transact)
        message(store, job="winner")
        return transact(**kwargs)

    monkeypatch.setattr(store.ddb, "transact_write_items", interleave)
    with pytest.raises(LocalError, match="Queue full"):
        message(store, job="loser")
    assert store.raw("owner-a", "run-a", "loser") is None
    assert store.submit("owner-a", "run-a", "winner", image(), "spread").state == "QUEUED"
    assert store.table.get_item(Key=store.quota_key("owner-a"))["Item"]["captureCount"] == 10
    with pytest.raises(LocalError):
        store.get("owner-b", "run-a", "job-a")
    message(store, owner="owner-b")
    store.cancel("owner-a", "run-a", "job-a")
    with pytest.raises(LocalError):
        message(store)
    message(store, job="loser")


def test_concurrent_identical_admissions_consume_one_slot_and_one_quota(store, monkeypatch):
    transact = store.ddb.transact_write_items
    store.settings.limits.max_captures_ahead = 1

    def interleave(**kwargs):
        monkeypatch.setattr(store.ddb, "transact_write_items", transact)
        message(store)
        return transact(**kwargs)

    monkeypatch.setattr(store.ddb, "transact_write_items", interleave)
    assert store.submit("owner-a", "run-a", "job-a", image(), "spread").state == "QUEUED"
    assert store.table.get_item(Key=store.quota_key("owner-a"))["Item"]["captureCount"] == 1
    with pytest.raises(LocalError, match="Queue full"):
        message(store, job="another")


def test_retry_admission_recovers_interrupted_release_and_fences_late_cleanup(store, monkeypatch):
    store.settings.limits.max_captures_ahead = 1
    message(store)
    identity = ("owner-a", "run-a", "job-a")
    item, lease = store.claim(*identity, 1)
    with monkeypatch.context() as patch:
        patch.setattr(store, "release", lambda item: None)
        store.checkpoint(item, lease, {"state": "FAILED", "workState": "FAILED", "retryable": True})
    message(store, job="next")  # repairs the interrupted release at capacity
    with pytest.raises(LocalError, match="Queue full"):
        store.retry(*identity)
    assert store.table.get_item(Key=store.quota_key("owner-a"))["Item"]["captureCount"] == 2
    store.cancel("owner-a", "run-a", "next")
    assert store.retry(*identity).state == "QUEUED"
    store.release(item)  # obsolete admission ticket
    with pytest.raises(LocalError, match="Queue full"):
        message(store, job="blocked")
    # Expiry is recoverable on demand, without depending on TTL or a scheduled sweep.
    store.table.update_item(
        Key=store.key(*identity),
        UpdateExpression="SET softExpires=:past",
        ExpressionAttributeValues={":past": 1},
    )
    message(store, job="replacement")
    assert store.raw(*identity)["state"] == "CANCELLED"


def test_interrupted_upload_retains_slot_until_timeout_then_allows_next_capture(store, monkeypatch):
    store.settings.limits.max_captures_ahead = 1

    def interrupt(**kwargs):
        raise OSError("interrupted")

    with monkeypatch.context() as patch:
        patch.setattr(store.s3, "put_object", interrupt)
        with pytest.raises(OSError, match="interrupted"):
            message(store)
    [page] = store.recent("owner-a", book_id="earlier").items
    assert page.state == "UPLOADING" and page.audio_chunks == 0
    with pytest.raises(LocalError, match="Queue full"):
        message(store, job="later")
    store.table.update_item(
        Key=store.key("owner-a", "run-a", "job-a"),
        UpdateExpression="SET updatedAt=:old",
        ExpressionAttributeValues={":old": "2000-01-01T00:00:00+00:00"},
    )
    maintain(store)
    assert store.get("owner-a", "run-a", "job-a")["state"] == "FAILED"
    message(store, job="later")


@pytest.mark.parametrize("fifo", [False, True])
def test_enqueue_supports_standard_and_single_group_fifo_repair(store, fifo):
    if fifo:
        store.settings.queue_url = store.sqs.create_queue(
            QueueName="repair.fifo",
            Attributes={"FifoQueue": "true", "ContentBasedDeduplication": "true"},
        )["QueueUrl"]
    job = message(store)
    store.enqueue(job)
    store.enqueue(job)
    response = store.sqs.receive_message(
        QueueUrl=store.settings.queue_url, MaxNumberOfMessages=10, AttributeNames=["All"]
    )
    messages = response["Messages"]
    assert len(messages) == (1 if fifo else 2)
    if fifo:
        assert messages[0]["Attributes"]["MessageGroupId"] == "reader"
    assert json.loads(messages[0]["Body"]) == job


def test_book_listing_keeps_submission_order_through_out_of_order_results(store):
    book = store.create_book("owner-a", "Story")
    identities = [("owner-a", "run", str(i)) for i in range(4)]
    for identity in identities:
        store.submit(*identity, image(), "spread", book.id)
    for index, state in [(2, "FAILED"), (0, "REJECTED")]:
        item, lease = store.claim(*identities[index], 1)
        store.checkpoint(
            item,
            lease,
            {
                "state": state,
                "workState": state,
                "reason": "detail",
                "retryable": state == "FAILED",
            },
        )
    store.claim(*identities[1], 1)
    pages = store.recent("owner-a", book_id=book.id).items
    assert [(p.id, p.state, p.audio_chunks) for p in pages] == [
        ("3", "QUEUED", 0),
        ("2", "FAILED", 0),
        ("1", "PROCESSING", 0),
        ("0", "REJECTED", 0),
    ]
    assert pages[1].retryable and pages[1].reason == "detail"
    assert not pages[-1].retryable and pages[-1].reason == "detail"
    assert not store.recent("owner-a").items


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
    original_key = item["originalKey"]
    original = store.s3.get_object(Bucket=store.settings.media_bucket, Key=original_key)
    with Image.open(BytesIO(original["Body"].read())) as page:
        assert page.size == (1200, 800)
    assert original_key in store.original_url("owner-a", "run-a", "job-a")
    with pytest.raises(LocalError):
        store.original_url("owner-b", "run-a", "job-a")
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


def test_edit_synthesizes_only_new_audio_and_survives_failed_retry(store):
    job = message(store)
    identity = (job["owner"], job["run"], job["id"])
    spoken = []

    def provider(request):
        if request.url.path == "/api/v1/chat/completions":
            assert not spoken
            return vision()
        text = json.loads(request.content)["input"]
        spoken.append(text)
        if len(spoken) == 2:
            return httpx.Response(503)
        return httpx.Response(200, content=b"\x01\x00" * 240, headers={"content-type": "audio/pcm"})

    run(store, job, provider)
    saved = store.get(*identity)
    for owner, run_id in [("other", job["run"]), (job["owner"], "other")]:
        with pytest.raises(LocalError) as denied:
            store.edit_narration(owner, run_id, job["id"], "No.")
        assert denied.value.status == 404
    # An edit shares capacity but must not release the other page's reservation.
    store.settings.limits.max_captures_ahead = 2
    message(store, job="other-page")
    queued = store.edit_narration(*identity, "An updated page.")
    assert (queued.state, queued.stage, queued.audio_chunks) == ("QUEUED", "speech", 0)
    with pytest.raises(LocalError, match="finish"):
        store.edit_narration(*identity, "Conflicting edit.")
    with pytest.raises(ClientError):
        store.s3.head_object(Bucket=store.settings.media_bucket, Key=saved["audioKeys"][0])
    run(store, job, provider)  # stale delivery
    run(store, {**job, "attempt": 2}, provider)
    failed = store.view(store.get(*identity))
    assert failed.retryable and failed.state == "FAILED"
    [page] = store.recent(job["owner"]).items
    assert page.narration == "An updated page." and page.audio_chunks == 0
    with pytest.raises(LocalError):
        store.audio_url(*identity, 0)
    assert saved["originalKey"] in store.original_url(*identity)
    store.retry(*identity)
    run(store, {**job, "attempt": 3}, provider)
    updated = store.get(*identity)
    assert updated["state"] == "READY" and updated["audioKeys"] != saved["audioKeys"]
    assert updated["originalKey"] == saved["originalKey"]
    assert updated["thumbnailKey"] == saved["thumbnailKey"]
    assert spoken == ["A green boat.", "An updated page.", "An updated page."]
    message(store, job="third-page")
    with pytest.raises(LocalError, match="Queue full"):
        message(store, job="fourth-page")


def test_stale_edit_cannot_replace_a_newly_finished_edit(store, monkeypatch):
    job = message(store)
    identity = (job["owner"], job["run"], job["id"])

    def provider(request):
        if request.url.path == "/api/v1/chat/completions":
            return vision()
        return httpx.Response(200, content=b"\x01\x00" * 240, headers={"content-type": "audio/pcm"})

    run(store, job, provider)
    update = store.ddb.transact_write_items

    def interleave(**kwargs):
        monkeypatch.setattr(store.ddb, "transact_write_items", update)
        store.edit_narration(*identity, "Winning text.")
        run(store, {**job, "attempt": 2}, provider)
        return update(**kwargs)

    monkeypatch.setattr(store.ddb, "transact_write_items", interleave)
    with pytest.raises(LocalError) as conflict:
        store.edit_narration(*identity, "Stale text.")
    assert conflict.value.status == 409
    current = store.get(*identity)
    assert current["state"] == "READY" and current["narration"] == "Winning text."
    store.s3.head_object(Bucket=store.settings.media_bucket, Key=current["audioKeys"][0])


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


def test_book_history_pages_stably_across_equal_seconds_and_scopes(store):
    book = store.create_book("owner", "Named")
    empty = store.create_book("owner", "Empty")
    for i in range(24):
        # Repeated IDs across runs; legacy rows lack both membership and subsecond time.
        run, job = f"run-{i:02}", "page"
        item = {
            **store.key("owner", run, job),
            "owner": "owner",
            "id": job,
            "run": run,
            "createdAt": 123,
            "state": "READY",
            "audioKeys": ["audio.wav"],
        }
        if i >= 12:
            item.update(bookId=book.id, ownerBook=f"owner#{book.id}", createdNs=100 - i)
        store.table.put_item(Item=item)

    for scope, expected in [(book.id, list(range(12, 24))), ("earlier", list(reversed(range(12))))]:
        first = store.recent("owner", book_id=scope)
        assert first.next_cursor
        with pytest.raises(LocalError):
            store.recent("owner", cursor=first.next_cursor, book_id=empty.id)
        second = store.recent("owner", cursor=first.next_cursor, book_id=scope)
        assert second.next_cursor is None
        assert [row.run_id for row in first.items + second.items] == [
            f"run-{i:02}" for i in expected
        ]
    assert not store.recent("owner", book_id=empty.id).items


def test_cloud_earlier_completion_interleaved_with_another_reader(store, monkeypatch):
    first, second = str(uuid4()), str(uuid4())
    transact = store.ddb.transact_write_items

    def interleave(**kwargs):
        monkeypatch.setattr(store.ddb, "transact_write_items", transact)
        assert store.complete_book("owner", "earlier", first) == 1
        assert store.complete_book("owner", "earlier", second) == 2
        raise ClientError(
            {
                "Error": {"Code": "TransactionCanceledException"},
                "CancellationReasons": [{"Code": "TransactionConflict"}, {"Code": "None"}],
            },
            "TransactWriteItems",
        )

    monkeypatch.setattr(store.ddb, "transact_write_items", interleave)
    assert store.complete_book("owner", "earlier", first) == 2
    assert store.complete_book("other", "earlier", first) == 1
    assert CloudStore(store.settings).books("owner").items[0].read_count == 2


def test_delete_book_fences_unlisted_and_processing_pages_and_scopes_media(store):
    owner, job = "owner", "shared-page"
    book = store.create_book(owner, "Target")
    keep = store.create_book(owner, "Keep")
    store.submit(owner, "claimed", job, image(), "spread", book.id)
    leased, token = store.claim(owner, "claimed", job, 1)
    # A strongly consistent membership lookup must find a page missing from the GSI.
    store.table.update_item(
        Key=store.key(owner, "claimed", job), UpdateExpression="REMOVE ownerBook"
    )
    saved = store.prefix("saved", owner, "old", job) + "original.jpg"
    store.table.put_item(
        Item={
            **store.key(owner, "old", job),
            "owner": owner,
            "run": "old",
            "id": job,
            "state": "READY",
            "bookId": book.id,
            "ownerBook": f"{owner}#{book.id}",
            "createdAt": 100,
            "originalKey": saved,
        }
    )
    store.s3.put_object(Bucket=store.settings.media_bucket, Key=saved, Body=b"private")
    for run, who, scope in [("keep", owner, keep.id), ("claimed", "other", None)]:
        store.submit(who, run, job, image(), "spread", scope)

    store.delete_book(owner, book.id)
    assert [b.id for b in store.books(owner).items] == ["earlier", keep.id]
    with pytest.raises(LostLease):
        store.checkpoint(leased, token, {})
    assert (
        store.raw(owner, "claimed", job)["state"]
        == store.raw(owner, "old", job)["state"]
        == "CANCELLED"
    )
    assert (
        store.get(owner, "keep", job)["state"]
        == store.get("other", "claimed", job)["state"]
        == "QUEUED"
    )
    keys = {
        row["Key"]
        for row in store.s3.list_objects_v2(Bucket=store.settings.media_bucket)["Contents"]
    }
    assert keys == {
        store.get(owner, "keep", job)["inputKey"],
        store.get("other", "claimed", job)["inputKey"],
    }


def test_cover_is_resolved_from_current_private_page(store, monkeypatch):
    owner, run, page = "owner-a", "run-a", "page-a"
    book = "earlier"  # Legacy pages have no bookId.
    key = store.prefix("saved", owner, run, page) + "thumbnail.jpg"
    store.s3.put_object(Bucket=store.settings.media_bucket, Key=key, Body=b"jpeg")
    row = {**store.key(owner, run, page), "owner": owner, "state": "READY", "thumbnailKey": key}
    store.table.put_item(Item=row)
    store.set_cover(owner, book, run, page)
    fresh = CloudStore(store.settings)
    monkeypatch.setattr(fresh, "media_url", lambda key, mime: "fresh-url")
    [listed] = fresh.books(owner).items
    assert listed.cover.thumbnail_url == "fresh-url"
    assert fresh.books("owner-b").items[0].cover is None
    for changed in [
        {"state": "CANCELLED"},
        {"softExpires": 1},
        {"expiresAt": 1},
        {"bookId": "another"},
        {"thumbnailKey": "saved/other/private.jpg"},
    ]:
        store.table.put_item(Item={**row, **changed})
        assert fresh.books(owner).items[0].cover is None
        with pytest.raises(LocalError, match="Cover page not found"):
            fresh.set_cover(owner, book, run, page)
    store.table.put_item(Item=row)
    store.s3.delete_object(Bucket=store.settings.media_bucket, Key=key)
    assert fresh.books(owner).items[0].cover is None
    store.table.delete_item(Key=store.key(owner, run, page))
    assert fresh.books(owner).items[0].cover is None


def test_delete_book_retries_cleanup_after_storage_error(store, monkeypatch):
    book = store.create_book("owner", "Target")
    store.submit("owner", "run", "job", image(), "spread", book.id)
    with monkeypatch.context() as patch:
        patch.setattr(store.s3, "delete_objects", lambda **kwargs: {"Errors": [{}]})
        with pytest.raises(RuntimeError, match="cleanup"):
            store.delete_book("owner", book.id)
        assert book in store.books("owner").items
        with pytest.raises(LocalError, match="Book not found"):
            store.complete_book("owner", book.id, str(uuid4()))
    store.delete_book("owner", book.id)
    assert book not in store.books("owner").items
    assert not store.s3.list_objects_v2(Bucket=store.settings.media_bucket).get("Contents")


def test_delete_book_fences_new_capture_between_book_check_and_transaction(store, monkeypatch):
    book = store.create_book("owner", "Target")
    transact = store.ddb.transact_write_items

    def race(**kwargs):
        store.delete_book("owner", book.id)
        return transact(**kwargs)

    monkeypatch.setattr(store.ddb, "transact_write_items", race)
    with pytest.raises(LocalError):
        store.submit("owner", "run", "new", image(), "spread", book.id)
    assert store.raw("owner", "run", "new") is None
    assert not store.s3.list_objects_v2(Bucket=store.settings.media_bucket).get("Contents")


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
        books = "/api/books"
        auth = {"X-Test-Owner": "owner-a"}
        assert client.get(books).status_code == 401
        assert (
            client.post(
                books, headers={**auth, "X-Test-Scope": "reader/read"}, json={"name": "A"}
            ).status_code
            == 403
        )
        assert client.post(books, headers=auth, json={"name": " "}).status_code == 422
        created = client.post(books, headers=auth, json={"name": " A "})
        assert created.status_code == 201
        book = created.json()
        assert book == {"id": book["id"], "name": "A"}
        earlier = {"id": "earlier", "name": "Earlier captures"}
        assert client.get(books, headers=auth).json() == {"items": [earlier, book]}
        assert client.get(books, headers={"X-Test-Owner": "owner-b"}).json() == {"items": [earlier]}
        target = f"{books}/{book['id']}"
        completions = target + "/completions"
        body = {"completion_id": str(uuid4())}
        assert client.post(completions, json=body).status_code == 401
        assert (
            client.post(
                completions, json=body, headers={**auth, "X-Test-Scope": "reader/read"}
            ).status_code
            == 403
        )
        assert (
            client.post(completions, json=body, headers={"X-Test-Owner": "other"}).status_code
            == 404
        )
        assert client.post(completions, json=body, headers=auth).json() == {"read_count": 1}
        assert client.post(
            completions, json={"completion_id": str(uuid4())}, headers=auth
        ).json() == {"read_count": 2}
        assert client.post(completions, json=body, headers=auth).json() == {"read_count": 2}
        book["read_count"] = 2
        rename = {"name": "  新书名  "}
        assert (
            client.patch(target, json=rename, headers={"X-Test-Owner": "owner-b"}).status_code
            == 404
        )
        assert (
            client.patch(
                target, json=rename, headers={**auth, "X-Test-Scope": "reader/read"}
            ).status_code
            == 403
        )
        assert client.patch(target, json={"name": " "}, headers=auth).status_code == 422
        assert client.patch(target, json=rename, headers=auth).status_code == 204
        book = {**book, "name": "新书名"}
        assert client.get(books, headers=auth).json()["items"][1] == book
        params = {"book_id": book["id"]}
        assert (
            client.post(
                path, params=params, headers={**headers, "X-Test-Owner": "owner-b"}, content=image()
            ).status_code
            == 404
        )
        assert client.post(path, params=params, headers=headers, content=image()).status_code == 202
        assert client.post(path, headers=headers, content=image()).status_code == 409
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
        assert client.get(listing, headers=auth, params=params).json() == listed.json()
        assert not client.get(listing, headers=auth, params={"book_id": "earlier"}).json()["items"]
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
        edit_headers = {**headers, "Content-Type": "application/json"}
        cover_path = f"{books}/{book['id']}/cover"
        ref = {"run_id": headers["X-Reader-Run"], "page_id": path.split("/")[-1]}
        library = {**edit_headers, "X-Reader-Run": str(uuid4())}
        assert client.post(cover_path, headers=library, json=ref).status_code == 404
        key = store.prefix("saved", "owner-a", ref["run_id"], ref["page_id"]) + "thumbnail.jpg"
        store.table.update_item(
            Key=store.key("owner-a", ref["run_id"], ref["page_id"]),
            UpdateExpression="SET thumbnailKey=:k",
            ExpressionAttributeValues={":k": key},
        )
        store.s3.put_object(Bucket=store.settings.media_bucket, Key=key, Body=b"thumbnail")
        for overrides, status in [
            ({"X-Test-Owner": ""}, 401),
            ({"X-Test-Scope": "reader/read"}, 403),
            ({"X-Test-Owner": "owner-b"}, 404),
        ]:
            assert (
                client.post(cover_path, headers={**library, **overrides}, json=ref).status_code
                == status
            )
        assert client.post(books + "/earlier/cover", headers=library, json=ref).status_code == 404
        saved = client.post(cover_path, headers=library, json=ref)
        assert saved.status_code == 200, saved.text
        thumb = saved.json()["cover"]["thumbnail_url"]
        assert key in thumb and "X-Amz-Signature=" in thumb
        assert saved.json() == {**book, "cover": {**ref, "thumbnail_url": thumb}}
        assert key in client.get(books, headers=auth).json()["items"][1]["cover"]["thumbnail_url"]
        edit = path + "/narration"
        body = {"narration": "Changed."}
        for overrides, code in [
            ({"X-Test-Owner": "owner-b"}, 404),
            ({"X-Test-Scope": "reader/read"}, 403),
        ]:
            assert (
                client.post(edit, headers={**edit_headers, **overrides}, json=body).status_code
                == code
            )
        assert client.post(edit, headers=edit_headers, json=body).status_code == 202
        target = f"{books}/{book['id']}"
        assert client.delete(target).status_code == 401
        read_only = {**auth, "X-Test-Scope": "reader/read"}
        assert client.delete(target, headers=read_only).status_code == 403
        assert client.delete(target, headers={"X-Test-Owner": "owner-b"}).status_code == 404
        assert client.delete(f"{books}/earlier", headers=auth).status_code == 409
        assert client.delete(target, headers=auth).status_code == 204
        assert (
            client.post(completions, json={"completion_id": str(uuid4())}, headers=auth).status_code
            == 404
        )
        assert not any(str(r["SK"]).startswith("COMPLETION#") for r in store.table.scan()["Items"])
        assert client.get(books, headers=auth).json() == {"items": [earlier]}
        assert client.get(path, headers=headers).status_code == 404
