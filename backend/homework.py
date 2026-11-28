import asyncio
import base64
import binascii
import json
import logging
import re
import sqlite3
import time
from io import BytesIO
from typing import Any
from uuid import uuid4

from boto3.dynamodb.conditions import ConditionBase, Key
from botocore.exceptions import ClientError
from fastapi import Request
from PIL import Image, ImageOps, UnidentifiedImageError
from pydantic import ValidationError

from backend.cloud import CloudStore, conditional, timestamp
from backend.contracts import HomeworkEntry, HomeworkHistory, HomeworkSubmission
from backend.local_reader import LocalError, LocalReader
from backend.providers import ProviderError, Providers

logger = logging.getLogger(__name__)
SK_PREFIX = "HOMEWORK#"
PENDING = "HOMEWORK_PENDING"
PAGE_SIZE = 15
MAX_IMAGES_BYTES = 5 * 1024 * 1024 // 2
MAX_BODY = (MAX_IMAGES_BYTES * 4 // 3) + 1024
MAX_PHOTO_EDGE = 2200


def homework_id(value: str) -> str:
    if not re.fullmatch(r"[0-9]{20}-[a-f0-9]{32}", value):
        raise LocalError("Homework entry not found.", 404)
    return value


def prepare_images(encoded: list[str]) -> list[bytes]:
    images = []
    total = 0
    for photo in encoded:
        try:
            raw = base64.b64decode(photo, validate=True)
            total += len(raw)
            if total > MAX_IMAGES_BYTES:
                raise LocalError("Your photos are too large together. Try fewer photos.", 413)
            with Image.open(BytesIO(raw)) as image:
                if image.format != "JPEG" or max(image.size) > MAX_PHOTO_EDGE:
                    raise LocalError("Send JPEG photos up to 2,200 pixels per side.", 415)
                output = BytesIO()
                ImageOps.exif_transpose(image).convert("RGB").save(output, "JPEG", quality=88)
                images.append(output.getvalue())
        except (
            binascii.Error,
            ValueError,
            UnidentifiedImageError,
            Image.DecompressionBombError,
            OSError,
        ) as exc:
            raise LocalError(
                "One of the photos could not be opened. Choose a clear JPEG photo."
            ) from exc
    if sum(map(len, images)) > 4 * 1024 * 1024:
        raise LocalError("Your photos are too large together. Try fewer photos.", 413)
    return images


async def read_submission(request: Request) -> tuple[HomeworkEntry, list[bytes]]:
    if request.headers.get("content-type", "").split(";")[0] != "application/json":
        raise LocalError("Send homework photos as JSON.", 415)
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > MAX_BODY:
            raise LocalError("Your photos are too large together. Try fewer photos.", 413)
        body.extend(chunk)
    try:
        submission = HomeworkSubmission.model_validate_json(body)
    except ValidationError as exc:
        raise LocalError("Select a subject, school level, and one to four photos.", 422) from exc
    images = await asyncio.to_thread(prepare_images, submission.images)
    return (
        HomeworkEntry(
            id=f"{time.time_ns():020d}-{uuid4().hex}",
            created_at=timestamp(),
            subject=submission.subject,
            level=submission.level,
            state="pending",
        ),
        images,
    )


async def solve(providers: Providers, entry: HomeworkEntry, images: list[bytes]) -> HomeworkEntry:
    try:
        async with asyncio.timeout(providers.settings.provider_timeout_seconds + 5):
            result = await providers.check_homework(images, entry.subject, entry.level)
    except (ProviderError, TimeoutError):
        return entry.model_copy(
            update={
                "state": "failed",
                "error": "Could not check these photos. Try again with fewer or clearer photos.",
            }
        )
    return entry.model_copy(update={"state": "ready", "result": result})


class LocalHomework:
    def __init__(self, reader: LocalReader) -> None:
        self.reader = reader
        self.slots = asyncio.Semaphore(2)

    async def submit(self, owner: str, entry: HomeworkEntry, images: list[bytes]) -> HomeworkEntry:
        if not self.reader.providers.settings.public_config().recognition_available:
            raise LocalError("Configure the vision model before checking homework.", 503)
        if self.slots.locked():
            raise LocalError("Homework help is busy. Try again in a moment.", 429)
        try:
            self.reader.quota("homework", self.reader.settings.limits.capture_attempts_per_day)
        except ProviderError as exc:
            raise LocalError(str(exc), 429) from exc
        async with self.slots:
            entry = await solve(self.reader.providers, entry, images)
        with self.reader.book_db() as db:
            self.ensure_table(db).execute(
                "INSERT INTO homework VALUES (?, ?, ?)", (owner, entry.id, entry.model_dump_json())
            )
        return entry

    @staticmethod
    def ensure_table(db: sqlite3.Connection) -> sqlite3.Connection:
        db.execute(
            "CREATE TABLE IF NOT EXISTS homework (owner TEXT, id TEXT, entry TEXT NOT NULL, "
            "PRIMARY KEY(owner, id))"
        )
        return db

    def history(self, owner: str, cursor: str | None) -> HomeworkHistory:
        with self.reader.book_db() as db:
            rows = (
                self.ensure_table(db)
                .execute(
                    "SELECT entry FROM homework WHERE owner=? AND id<? ORDER BY id DESC LIMIT ?",
                    (owner, homework_id(cursor) if cursor is not None else "z", PAGE_SIZE + 1),
                )
                .fetchall()
            )
        items = [HomeworkEntry.model_validate_json(row[0]) for row in rows[:PAGE_SIZE]]
        return HomeworkHistory(
            items=items, next_cursor=items[-1].id if len(rows) > PAGE_SIZE else None
        )

    def get(self, owner: str, entry_id: str) -> HomeworkEntry:
        with self.reader.book_db() as db:
            row = (
                self.ensure_table(db)
                .execute(
                    "SELECT entry FROM homework WHERE owner=? AND id=?",
                    (owner, homework_id(entry_id)),
                )
                .fetchone()
            )
        if not row:
            raise LocalError("Homework entry not found.", 404)
        return HomeworkEntry.model_validate_json(row[0])

    def delete(self, owner: str, entry_id: str) -> None:
        with self.reader.book_db() as db:
            self.ensure_table(db).execute(
                "DELETE FROM homework WHERE owner=? AND id=?", (owner, homework_id(entry_id))
            )


class CloudHomework:
    def __init__(self, store: CloudStore) -> None:
        self.store = store

    @staticmethod
    def key(owner: str, entry_id: str) -> dict[str, str]:
        return {"PK": f"USER#{owner}#HOMEWORK", "SK": SK_PREFIX + homework_id(entry_id)}

    @staticmethod
    def photo_key(owner: str, entry_id: str, index: int) -> str:
        return f"incoming/homework/{owner}/{entry_id}/{index}.jpg"

    def submit(self, owner: str, entry: HomeworkEntry, images: list[bytes]) -> HomeworkEntry:
        store = self.store
        item: dict[str, Any] = {
            **self.key(owner, entry.id),
            "id": entry.id,
            "owner": owner,
            "workState": PENDING,
            "updatedAt": entry.created_at,
            "photoCount": len(images),
            "entry": entry.model_dump_json(),
        }
        try:
            store.quota(owner, "homework")
        except ProviderError as exc:
            raise LocalError(str(exc), 429) from exc
        for index, image in enumerate(images):
            store.s3.put_object(
                Bucket=store.settings.media_bucket,
                Key=self.photo_key(owner, entry.id, index),
                Body=image,
                ContentType="image/jpeg",
            )
        # The existing DynamoDB stream dispatches only after all photos are written.
        store.table.put_item(Item=item, ConditionExpression="attribute_not_exists(PK)")
        return entry

    def get(self, owner: str, entry_id: str) -> HomeworkEntry:
        item = self.raw(owner, entry_id)
        if not item:
            raise LocalError("Homework entry not found.", 404)
        return HomeworkEntry.model_validate_json(item["entry"])

    def raw(self, owner: str, entry_id: str) -> dict[str, Any] | None:
        return self.store.table.get_item(Key=self.key(owner, entry_id), ConsistentRead=True).get(
            "Item"
        )

    def history(self, owner: str, cursor: str | None) -> HomeworkHistory:
        bounds: ConditionBase = Key("PK").eq(f"USER#{owner}#HOMEWORK")
        if cursor is not None:
            bounds &= Key("SK").lt(SK_PREFIX + homework_id(cursor))
        page = self.store.table.query(
            KeyConditionExpression=bounds,
            ScanIndexForward=False,
            ConsistentRead=True,
            Limit=PAGE_SIZE + 1,
        )
        rows: list[dict[str, Any]] = [dict(row) for row in page.get("Items", [])]
        items = [HomeworkEntry.model_validate_json(row["entry"]) for row in rows[:PAGE_SIZE]]
        has_more = len(rows) > PAGE_SIZE or bool(page.get("LastEvaluatedKey"))
        return HomeworkHistory(
            items=items, next_cursor=items[-1].id if items and has_more else None
        )

    def delete(self, owner: str, entry_id: str) -> None:
        try:
            self.store.table.delete_item(
                Key=self.key(owner, entry_id),
                ConditionExpression="attribute_not_exists(workState)",
            )
        except ClientError as exc:
            if not conditional(exc):
                raise
            raise LocalError("Wait until the check finishes before deleting it.", 409) from exc

    def finish(self, item: dict[str, Any], entry: HomeworkEntry) -> None:
        try:
            self.store.table.update_item(
                Key=self.key(item["owner"], entry.id),
                ConditionExpression="workState = :pending",
                UpdateExpression="SET entry = :entry REMOVE workState, updatedAt, photoCount",
                ExpressionAttributeValues={":pending": PENDING, ":entry": entry.model_dump_json()},
            )
        except ClientError as exc:
            if conditional(exc):
                return
            raise
        try:
            self.store.s3.delete_objects(
                Bucket=self.store.settings.media_bucket,
                Delete={
                    "Objects": [
                        {"Key": self.photo_key(item["owner"], entry.id, index)}
                        for index in range(int(item["photoCount"]))
                    ]
                },
            )
        except ClientError:
            logger.warning("homework_photo_cleanup_deferred id=%s", entry.id)

    def enqueue(self, item: dict[str, Any]) -> None:
        self.store.sqs.send_message(
            QueueUrl=self.store.settings.queue_url,
            MessageBody=json.dumps({"kind": "homework", "owner": item["owner"], "id": item["id"]}),
            MessageGroupId=f"homework-{item['owner']}",
        )

    async def process(self, message: dict[str, Any], providers: Providers) -> None:
        item = self.raw(message["owner"], message["id"])
        if not item or item.get("workState") != PENDING:
            return
        entry = HomeworkEntry.model_validate_json(item["entry"])
        images: list[bytes] = []
        for index in range(int(item["photoCount"])):
            try:
                data = self.store.s3.get_object(
                    Bucket=self.store.settings.media_bucket,
                    Key=self.photo_key(item["owner"], entry.id, index),
                )["Body"]
            except ClientError as exc:
                if exc.response["Error"]["Code"] not in {"NoSuchKey", "404"}:
                    raise
                break
            try:
                images.append(data.read())
            finally:
                data.close()
        if len(images) == int(item["photoCount"]):
            entry = await solve(providers, entry, images)
        else:
            entry = entry.model_copy(
                update={"state": "failed", "error": "Photos expired. Upload them again."}
            )
        self.finish(item, entry)

    def maintain(self) -> None:
        start: dict[str, Any] = {}
        now = int(time.time())
        while True:
            page = self.store.table.query(
                IndexName="MaintenanceIndex",
                KeyConditionExpression=Key("workState").eq(PENDING),
                **start,
            )
            for raw_item in page.get("Items", []):
                item: dict[str, Any] = dict(raw_item)
                if item["updatedAt"] < timestamp(now - 20 * 60):
                    entry = HomeworkEntry.model_validate_json(item["entry"])
                    self.finish(
                        item,
                        entry.model_copy(
                            update={
                                "state": "failed",
                                "error": "The check took too long. Upload your photos again.",
                            }
                        ),
                    )
                elif item["updatedAt"] < timestamp(now - 5 * 60):
                    self.enqueue(item)
            if not page.get("LastEvaluatedKey"):
                break
            start = {"ExclusiveStartKey": page["LastEvaluatedKey"]}
