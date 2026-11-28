from __future__ import annotations

import base64
import hashlib
import json
import random
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from itertools import groupby
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import boto3
from boto3.dynamodb.conditions import Attr, ConditionBase, Key
from boto3.dynamodb.types import TypeSerializer
from botocore.config import Config
from botocore.exceptions import ClientError

from backend.config import Settings
from backend.contracts import (
    EARLIER,
    RECENT_CAPTURE_LIMIT,
    Book,
    BookCover,
    BookLanguage,
    BookLevel,
    BookPageMove,
    BookPageOrder,
    Books,
    BookUpdate,
    CaptureStage,
    CaptureState,
    PlayableCapture,
    ReadingJob,
    RecentCaptures,
)
from backend.local_reader import LocalError, move_page_in_order
from backend.providers import ProviderError

if TYPE_CHECKING:
    from mypy_boto3_dynamodb.type_defs import TransactWriteItemTypeDef, UpdateTypeDef
    from mypy_boto3_sqs.type_defs import SendMessageRequestTypeDef

LEASE_SECONDS = 120
JOB_SECONDS = 3600
PENDING_STATES = ("UPLOADING", "QUEUED", "PROCESSING")
BOOK_STATES = (*PENDING_STATES, "READY", "FAILED", "REJECTED", "DUPLICATE")
LEASE_CONDITION = (
    "#state=:processing AND leaseToken=:token AND attempt=:attempt "
    "AND leaseUntil>:now AND (softExpires>:now OR attribute_exists(audioEdit))"
)


def timestamp(seconds: int | None = None) -> str:
    return datetime.fromtimestamp(seconds or int(time.time()), UTC).isoformat(timespec="seconds")


def conditional(exc: ClientError) -> bool:
    return exc.response["Error"]["Code"] in {
        "ConditionalCheckFailedException",
        "TransactionCanceledException",
    }


class LostLease(Exception):
    pass


class CloudStore:
    def __init__(self, settings: Settings, session: boto3.Session | None = None) -> None:
        self.settings = settings
        session = session or boto3.Session(region_name=settings.aws_region)
        self.table = session.resource("dynamodb", region_name=settings.aws_region).Table(
            settings.table_name
        )
        self.ddb = session.client("dynamodb", region_name=settings.aws_region)
        self.s3 = session.client(
            "s3",
            region_name=settings.aws_region,
            config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"}),
        )
        self.sqs = session.client("sqs", region_name=settings.aws_region)

    @staticmethod
    def key(owner: str, run: str, job_id: str) -> dict[str, str]:
        return {"PK": f"USER#{owner}#RUN#{run}", "SK": f"JOB#{job_id}"}

    @staticmethod
    def prefix(kind: str, owner: str, run: str, job_id: str) -> str:
        return f"{kind}/{owner}/{run}/{job_id}/"

    def raw(self, owner: str, run: str, job_id: str) -> dict[str, Any] | None:
        return self.table.get_item(Key=self.key(owner, run, job_id), ConsistentRead=True).get(
            "Item"
        )

    def get(self, owner: str, run: str, job_id: str) -> dict[str, Any]:
        item = self.raw(owner, run, job_id)
        if item is None or item["state"] == "CANCELLED":
            raise LocalError(
                "This capture was discarded or has expired. Capture the page again.", 404
            )
        if "softExpires" in item and item["softExpires"] <= int(time.time()):
            try:
                self.cancel(owner, run, job_id, expired_only=True)
            except ClientError as exc:
                if not conditional(exc):
                    raise
                return self.get(owner, run, job_id)
            raise LocalError("This capture has expired. Capture the page again.", 404)
        return item

    def create_book(
        self,
        owner: str,
        name: str,
        *,
        level: BookLevel | None = None,
        language: BookLanguage | None = None,
    ) -> Book:
        book = Book(
            id=str(uuid4()),
            name=name,
            level=level,
            language=language,
            created_at=time.time_ns() // 1_000_000,
        )
        self.table.put_item(
            Item={
                "PK": f"USER#{owner}",
                "SK": f"BOOK#{book.id}",
                "name": book.name,
                "createdAtMs": book.created_at,
                **({"level": book.level} if book.level is not None else {}),
                **({"language": book.language} if book.language is not None else {}),
            },
            ConditionExpression="attribute_not_exists(PK)",
        )
        return book

    def complete_book(self, owner: str, book_id: str, completion_id: str) -> int:
        if len(book_id.encode()) > 256:
            raise LocalError("Book not found.", 404)
        key = {"PK": f"USER#{owner}", "SK": f"BOOK#{book_id}"}
        earlier = book_id == EARLIER.id
        update: UpdateTypeDef = {
            "TableName": self.settings.table_name,
            "Key": self.encoded(key),
            "UpdateExpression": ("SET #n=if_not_exists(#n,:name) " if earlier else "")
            + "ADD readCount :one",
            "ConditionExpression": "attribute_not_exists(deleting)"
            + ("" if earlier else " AND attribute_exists(PK)"),
            "ExpressionAttributeValues": self.encoded(
                {":one": 1, **({":name": EARLIER.name} if earlier else {})}
            ),
        }
        if earlier:
            update["ExpressionAttributeNames"] = {"#n": "name"}
        receipt = {"PK": f"USER#{owner}#BOOK#{book_id}", "SK": f"COMPLETION#{completion_id}"}
        for attempt in range(6):
            try:
                self.ddb.transact_write_items(
                    TransactItems=[
                        {"Update": update},
                        {
                            "Put": {
                                "TableName": self.settings.table_name,
                                "Item": self.encoded(receipt),
                                "ConditionExpression": "attribute_not_exists(PK)",
                            }
                        },
                    ]
                )
                break
            except ClientError as exc:
                if exc.response["Error"]["Code"] != "TransactionCanceledException":
                    raise
                codes = [r.get("Code") for r in exc.response.get("CancellationReasons", [])]
                if codes[:1] == ["ConditionalCheckFailed"]:
                    raise LocalError("Book not found.", 404) from exc
                if codes[1:2] == ["ConditionalCheckFailed"]:
                    break  # Already committed; return the current count, not the original one.
                if "TransactionConflict" not in codes:
                    raise
                if attempt == 5:
                    raise LocalError("Book is busy. Retry this completion.", 503) from exc
                time.sleep(random.uniform(0.01, 0.02) * 2**attempt)
        row = self.table.get_item(Key=key, ConsistentRead=True).get("Item")
        if not row or row.get("deleting"):
            raise LocalError("Book not found.", 404)
        return int(str(row["readCount"]))

    def update_book(self, owner: str, book_id: str, changes: BookUpdate) -> None:
        if book_id == EARLIER.id:
            raise LocalError("Earlier captures cannot be edited as a book.", 409)
        values = changes.model_dump(mode="json", exclude_unset=True)
        names = {f"#{field}": field for field in values}
        updates = [f"#{field}=:{field}" for field, value in values.items() if value is not None]
        removes = [f"#{field}" for field, value in values.items() if value is None]
        expression = " ".join(
            part
            for part in (
                "SET " + ", ".join(updates) if updates else "",
                "REMOVE " + ", ".join(removes) if removes else "",
            )
            if part
        )
        params: dict[str, Any] = {}
        if updates:
            params["ExpressionAttributeValues"] = {
                f":{field}": value for field, value in values.items() if value is not None
            }
        try:
            self.table.update_item(
                Key={"PK": f"USER#{owner}", "SK": f"BOOK#{book_id}"},
                UpdateExpression=expression,
                ConditionExpression="attribute_exists(PK) AND attribute_not_exists(deleting)",
                ExpressionAttributeNames=names,
                **params,
            )
        except ClientError as exc:
            if not conditional(exc):
                raise
            raise LocalError("Book not found.", 404) from exc

    def _cover(self, owner: str, book_id: str, run: str, page_id: str) -> BookCover | None:
        page = self.raw(owner, run, page_id)
        key = self.prefix("saved", owner, run, page_id) + "thumbnail.jpg"
        if (
            not page
            or page.get("owner") != owner
            or page.get("bookId", EARLIER.id) != book_id
            or page.get("state") == "CANCELLED"
            or page.get("thumbnailKey") != key
            or any(
                field in page and page[field] <= int(time.time())
                for field in ("softExpires", "expiresAt")
            )
        ):
            return None
        try:
            self.s3.head_object(Bucket=self.settings.media_bucket, Key=key)
        except ClientError as exc:
            if exc.response["Error"]["Code"] in {"404", "NotFound", "NoSuchKey"}:
                return None
            raise
        return BookCover(
            run_id=run, page_id=page_id, thumbnail_url=self.media_url(key, "image/jpeg")
        )

    def set_cover(self, owner: str, book_id: str, run: str, page_id: str) -> Book:
        cover = self._cover(owner, book_id, run, page_id)
        if not cover:
            raise LocalError("Cover page not found.", 404)
        key = {"PK": f"USER#{owner}", "SK": f"BOOK#{book_id}"}
        book = self.table.get_item(Key=key, ConsistentRead=True).get("Item")
        if book is None and book_id != EARLIER.id:
            raise LocalError("Book not found.", 404)
        name = str(book["name"]) if book else EARLIER.name
        membership = "bookId=:book"
        if book_id == EARLIER.id:
            membership = "(attribute_not_exists(bookId) OR bookId=:book)"
        try:
            self.ddb.transact_write_items(
                TransactItems=[
                    {
                        "ConditionCheck": {
                            "TableName": self.settings.table_name,
                            "Key": self.encoded(self.key(owner, run, page_id)),
                            "ConditionExpression": f"{membership} AND #s<>:cancel AND "
                            "thumbnailKey=:key AND "
                            "(attribute_not_exists(softExpires) OR softExpires>:now) AND "
                            "(attribute_not_exists(expiresAt) OR expiresAt>:now)",
                            "ExpressionAttributeNames": {"#s": "state"},
                            "ExpressionAttributeValues": self.encoded(
                                {
                                    ":book": book_id,
                                    ":cancel": "CANCELLED",
                                    ":now": int(time.time()),
                                    ":key": self.prefix("saved", owner, run, page_id)
                                    + "thumbnail.jpg",
                                }
                            ),
                        }
                    },
                    {
                        "Update": {
                            "TableName": self.settings.table_name,
                            "Key": self.encoded(key),
                            "UpdateExpression": "SET #n=if_not_exists(#n,:name), "
                            "coverRun=:run, coverPage=:page",
                            "ConditionExpression": "attribute_not_exists(deleting)"
                            + (" AND attribute_exists(PK)" if book_id != EARLIER.id else ""),
                            "ExpressionAttributeNames": {"#n": "name"},
                            "ExpressionAttributeValues": self.encoded(
                                {":name": name, ":run": run, ":page": page_id}
                            ),
                        }
                    },
                ]
            )
        except ClientError as exc:
            if not any(
                reason.get("Code") == "ConditionalCheckFailed"
                for reason in exc.response.get("CancellationReasons", [])
            ):
                raise
            raise LocalError("Book or cover page not found.", 404) from exc
        return Book(
            id=book_id,
            name=name,
            level=BookLevel(str(book["level"])) if book and book.get("level") is not None else None,
            language=BookLanguage(str(book["language"]))
            if book and book.get("language") is not None
            else None,
            cover=cover,
            created_at=self._book_created_at(owner, book_id, book),
            read_count=int(str(book["readCount"])) if book and "readCount" in book else None,
        )

    def _book_created_at(self, owner: str, book_id: str, row: dict[str, Any] | None) -> int | None:
        if row and row.get("createdAtMs") is not None:
            return int(row["createdAtMs"])
        first = next(self._history_rows(owner, book_id, None, oldest_first=True), None)
        return int(first["createdAt"]) * 1000 if first else None

    def books(self, owner: str) -> Books:
        items: list[Book] = []
        earlier = None
        start: dict[str, Any] = {}
        while True:
            page = self.table.query(
                KeyConditionExpression=Key("PK").eq(f"USER#{owner}")
                & Key("SK").begins_with("BOOK#"),
                ConsistentRead=True,
                **start,
            )
            for row in page["Items"]:
                book_id = str(row["SK"])[5:]
                run, page_id = row.get("coverRun"), row.get("coverPage")
                cover = (
                    self._cover(owner, book_id, run, page_id)
                    if isinstance(run, str) and isinstance(page_id, str) and not row.get("deleting")
                    else None
                )
                book = Book(
                    id=book_id,
                    name=str(row["name"]),
                    level=BookLevel(str(row["level"])) if row.get("level") is not None else None,
                    language=BookLanguage(str(row["language"]))
                    if row.get("language") is not None
                    else None,
                    created_at=self._book_created_at(owner, book_id, row),
                    cover=cover,
                    read_count=int(str(row["readCount"])) if "readCount" in row else None,
                )
                if book_id == EARLIER.id:
                    earlier = book
                else:
                    items.append(book)
            if "LastEvaluatedKey" not in page:
                if earlier is None:
                    earlier = Book(
                        id=EARLIER.id,
                        name=EARLIER.name,
                        created_at=self._book_created_at(owner, EARLIER.id, None),
                    )
                return Books(items=[earlier, *items])
            start = {"ExclusiveStartKey": page["LastEvaluatedKey"]}

    def check_book(self, owner: str, book_id: str | None) -> None:
        if book_id is not None and book_id != EARLIER.id:
            self._book_row(owner, book_id)

    def _book_row(self, owner: str, book_id: str) -> dict[str, Any] | None:
        if len(book_id.encode()) > 256:
            raise LocalError("Book not found.", 404)
        row = self.table.get_item(
            Key={"PK": f"USER#{owner}", "SK": f"BOOK#{book_id}"}, ConsistentRead=True
        ).get("Item")
        if row is None and book_id != EARLIER.id:
            raise LocalError("Book not found.", 404)
        return row

    def move_page(self, owner: str, book_id: str, move: BookPageMove) -> BookPageOrder:
        book = self._book_row(owner, book_id) or {}
        if book.get("deleting"):
            raise LocalError("Book not found.", 404)
        saved = book.get("pageOrder")
        rows = sorted(self._history_rows(owner, book_id, None), key=self._history_position)
        order = move_page_in_order([f"{row['run']}/{row['id']}" for row in rows], saved or [], move)

        # Replace the entire order atomically without touching the capture rows or other metadata.
        expression = "SET pageOrder=:order"
        condition = "attribute_not_exists(deleting)"
        values: dict[str, Any] = {":order": order.page_order}
        if saved is None:
            condition += " AND attribute_not_exists(pageOrder)"
        else:
            condition += " AND pageOrder=:saved"
            values[":saved"] = saved
        params: dict[str, Any] = {}
        if book_id == EARLIER.id:
            # Legacy collections may not have a book row until their first edit.
            expression += ", #n=if_not_exists(#n,:name)"
            params["ExpressionAttributeNames"] = {"#n": "name"}
            values[":name"] = EARLIER.name
        else:
            condition += " AND attribute_exists(PK)"
        try:
            self.table.update_item(
                Key={"PK": f"USER#{owner}", "SK": f"BOOK#{book_id}"},
                UpdateExpression=expression,
                ConditionExpression=condition,
                ExpressionAttributeValues=values,
                **params,
            )
        except ClientError as exc:
            if not conditional(exc):
                raise
            raise LocalError("The book changed. Refresh and try the move again.", 409) from exc
        return order

    def book_membership_write(
        self, owner: str, book_id: str, run: str, job_id: str
    ) -> list[TransactWriteItemTypeDef]:
        if book_id == EARLIER.id:
            return []
        table = self.settings.table_name
        return [
            {
                "ConditionCheck": {
                    "TableName": table,
                    "Key": self.encoded({"PK": f"USER#{owner}", "SK": f"BOOK#{book_id}"}),
                    "ConditionExpression": "attribute_exists(PK) AND "
                    "attribute_not_exists(deleting)",
                }
            },
            {
                "Put": {
                    "TableName": table,
                    "Item": self.encoded(
                        {
                            "PK": f"USER#{owner}#BOOK#{book_id}",
                            "SK": f"{run}#{job_id}",
                            "run": run,
                            "id": job_id,
                        }
                    ),
                }
            },
        ]

    def delete_book(self, owner: str, book_id: str) -> None:
        if book_id == EARLIER.id:
            raise LocalError("Earlier captures cannot be deleted as a book.", 409)
        self.check_book(owner, book_id)
        key = {"PK": f"USER#{owner}", "SK": f"BOOK#{book_id}"}
        try:
            self.table.update_item(
                Key=key,
                UpdateExpression="SET deleting=:yes",
                ExpressionAttributeValues={":yes": True},
                ConditionExpression="attribute_exists(PK)",
            )
        except ClientError as exc:
            if not conditional(exc):
                raise
            raise LocalError("Book not found.", 404) from exc

        # Older named books have only GSI entries. New captures also write a consistent
        # membership row, so a delete immediately after upload cannot miss a lagging GSI.
        for members in (True, False):
            query: dict[str, Any] = (
                {
                    "KeyConditionExpression": Key("PK").eq(f"USER#{owner}#BOOK#{book_id}"),
                    "ConsistentRead": True,
                }
                if members
                else {
                    "IndexName": "BookCapturesIndex",
                    "KeyConditionExpression": Key("ownerBook").eq(f"{owner}#{book_id}"),
                }
            )
            while True:
                page = self.table.query(**query)
                for row in page["Items"]:
                    if members:
                        if str(row["SK"]).startswith("COMPLETION#"):
                            self.table.delete_item(Key={"PK": row["PK"], "SK": row["SK"]})
                            continue
                        run, job_id = str(row["run"]), str(row["id"])
                    else:
                        prefix = f"USER#{owner}#RUN#"
                        pk, sk = str(row["PK"]), str(row["SK"])
                        if not pk.startswith(prefix) or not sk.startswith("JOB#"):
                            continue
                        run, job_id = pk[len(prefix) :], sk[4:]
                    try:
                        self.cancel(owner, run, job_id, book_id=book_id)
                    except ClientError as exc:
                        if not conditional(exc):
                            raise
                    if members:
                        self.table.delete_item(Key={"PK": row["PK"], "SK": row["SK"]})
                if "LastEvaluatedKey" not in page:
                    break
                query["ExclusiveStartKey"] = page["LastEvaluatedKey"]
        self.table.delete_item(Key=key)

    def _history_rows(
        self, owner: str, book_id: str | None, before: int | None, *, oldest_first: bool = False
    ) -> Iterator[dict[str, Any]]:
        named = book_id is not None and book_id != EARLIER.id
        condition: ConditionBase = (
            Key("ownerBook").eq(f"{owner}#{book_id}") if named else Key("owner").eq(owner)
        )
        if before is not None:
            condition &= Key("createdAt").lte(before)
        start: dict[str, Any] = {}
        while True:
            page = self.table.query(
                IndexName="BookCapturesIndex" if named else "OwnerCapturesIndex",
                KeyConditionExpression=condition,
                FilterExpression=Attr("state").is_in(list(BOOK_STATES)),
                ScanIndexForward=oldest_first,
                Limit=RECENT_CAPTURE_LIMIT,
                **start,
            )
            candidates = page.get("Items", [])
            current: dict[tuple[str, str], dict[str, Any]] = {}
            pending: dict[str, Any] = (
                {
                    self.settings.table_name: {
                        "Keys": [{"PK": row["PK"], "SK": row["SK"]} for row in candidates],
                        "ConsistentRead": True,
                    }
                }
                if candidates
                else {}
            )
            for attempt in range(5):
                if not pending:
                    break
                batch = self.table.meta.client.batch_get_item(RequestItems=pending)
                for row in batch.get("Responses", {}).get(self.settings.table_name, []):
                    current[str(row["PK"]), str(row["SK"])] = row
                pending = batch.get("UnprocessedKeys", {})
                if pending:
                    if attempt == 4:
                        raise LocalError("Capture history is busy. Refresh and try again.", 503)
                    time.sleep(random.uniform(0.01, 0.02) * 2**attempt)
            for candidate in candidates:
                # The index can lag a discard. Never serve narration from its projection.
                item = current.get((str(candidate["PK"]), str(candidate["SK"])), {})
                if (
                    item.get("owner") == owner
                    and (book_id is None or item.get("bookId", EARLIER.id) == book_id)
                    and (
                        book_id is not None
                        or (item.get("state") == "READY" and item.get("audioKeys"))
                        or item.get("audioEdit")
                    )
                    and "createdAt" in item
                    and item.get("state") in BOOK_STATES
                    and ("expiresAt" not in item or item["expiresAt"] > int(time.time()))
                    and ("softExpires" not in item or item["softExpires"] > int(time.time()))
                ):
                    yield item
            if "LastEvaluatedKey" not in page:
                return
            start = {"ExclusiveStartKey": page["LastEvaluatedKey"]}

    @staticmethod
    def _history_position(row: dict[str, Any]) -> tuple[int, int, str, str]:
        return int(row["createdAt"]), int(row.get("createdNs", 0)), row["run"], row["id"]

    def recent(
        self,
        owner: str,
        limit: int = RECENT_CAPTURE_LIMIT,
        cursor: str | None = None,
        book_id: str | None = None,
    ) -> RecentCaptures:
        order = None
        if book_id is not None and cursor is None:
            book = self._book_row(owner, book_id)
            order = book.get("pageOrder") if book else None
        else:
            self.check_book(owner, book_id)
        if not 1 <= limit <= RECENT_CAPTURE_LIMIT:
            raise LocalError("Invalid capture page size.", 400)
        boundary = None
        if cursor is not None:
            try:
                if len(cursor) > 4096:
                    raise ValueError
                data = json.loads(base64.b64decode(cursor, altchars=b"-_", validate=True))
                if not isinstance(data, list) or len(data) not in (4, 6):
                    raise ValueError
                subject, run, job_id, second = data[:4]
                scope, nanos = data[4:] if len(data) == 6 else (None, 0)
                if (
                    subject != owner
                    or scope != book_id
                    or not isinstance(run, str)
                    or not isinstance(job_id, str)
                    or not 0 < len(run.encode()) <= 256
                    or not 0 < len(job_id.encode()) <= 256
                    or type(second) is not int
                    or not 0 <= second < 2**63
                    or type(nanos) is not int
                    or not 0 <= nanos < 2**63
                ):
                    raise ValueError
                boundary = (second, nanos, run, job_id)
            except (ValueError, TypeError, RecursionError) as exc:
                raise LocalError("Invalid capture history cursor.", 400) from exc

        items: list[PlayableCapture] = []
        last = None
        rows = self._history_rows(owner, book_id, boundary[0] if boundary else None)
        # A GSI does not order equal range keys, even across DynamoDB query pages.
        for _, group in groupby(rows, key=lambda row: row["createdAt"]):
            for item in sorted(group, key=self._history_position, reverse=True):
                key = self._history_position(item)
                if boundary is not None and key >= boundary:
                    continue
                if len(items) == limit and last is not None:
                    second, nanos, run, job_id = last
                    token = json.dumps([owner, run, job_id, second, book_id, nanos])
                    return RecentCaptures(
                        items=items,
                        next_cursor=base64.urlsafe_b64encode(token.encode()).decode(),
                        page_order=order,
                    )
                items.append(
                    PlayableCapture(
                        thumbnail_url=self.media_url(item["thumbnailKey"], "image/jpeg")
                        if item.get("thumbnailKey")
                        else None,
                        id=item["id"],
                        run_id=item["run"],
                        created_at=key[0],
                        narration=item.get("narration", ""),
                        language=item.get("language", ""),
                        audio_chunks=len(item.get("audioKeys", []))
                        if item["state"] == "READY"
                        else 0,
                        state=None if item["state"] == "READY" else CaptureState(item["state"]),
                        retryable=None
                        if item["state"] == "READY"
                        else item.get("retryable", False),
                        reason=None if item["state"] == "READY" else item.get("reason"),
                    )
                )
                last = key
        return RecentCaptures(items=items, page_order=order)

    def view(self, item: dict[str, Any]) -> ReadingJob:
        return ReadingJob(
            id=item["id"],
            state=CaptureState(item["state"]),
            stage=CaptureStage(item["stage"]),
            narration=item.get("narration", ""),
            language=item.get("language", ""),
            reason=item.get("reason"),
            retryable=item.get("retryable", False),
            audio_chunks=len(item.get("audioKeys", [])) if item["state"] == "READY" else 0,
            vision_model=item["visionModel"],
            speech_model=item["speechModel"],
            voice=item["voice"],
            prompt_version=item["promptVersion"],
        )

    @staticmethod
    def encoded(item: dict[str, Any]) -> dict[str, Any]:
        serializer = TypeSerializer()
        return {name: serializer.serialize(value) for name, value in item.items()}

    def quota_key(self, owner: str) -> dict[str, str]:
        return {"PK": f"USER#{owner}", "SK": "USAGE#" + datetime.now(UTC).date().isoformat()}

    def quota_update(self, owner: str, field: str, limit: int) -> UpdateTypeDef:
        return {
            "TableName": self.settings.table_name,
            "Key": self.encoded(self.quota_key(owner)),
            "UpdateExpression": "SET expiresAt = :expiry ADD #count :one",
            "ConditionExpression": "attribute_not_exists(#count) OR #count < :limit",
            "ExpressionAttributeNames": {"#count": field},
            "ExpressionAttributeValues": self.encoded(
                {
                    ":expiry": int(time.time()) + 172800,
                    ":one": 1,
                    ":limit": limit,
                }
            ),
        }

    def quota(self, owner: str, kind: str) -> None:
        limit = (
            self.settings.limits.speech_attempts_per_day
            if kind == "speech"
            else self.settings.limits.capture_attempts_per_day
        )
        try:
            self.ddb.update_item(**self.quota_update(owner, kind + "Count", limit))
        except ClientError as exc:
            if conditional(exc):
                raise ProviderError(f"Daily {kind} limit reached. Try again tomorrow.") from exc
            raise

    @staticmethod
    def _ticket(run: str, job_id: str) -> str:
        # Unique per admission: a late cleanup must never release a newer retry's slot.
        return json.dumps([run, job_id, uuid4().hex], separators=(",", ":"))

    @staticmethod
    def _pending_key(owner: str) -> dict[str, str]:
        return {"PK": f"USER#{owner}", "SK": "PENDING"}

    def release(self, item: dict[str, Any]) -> None:
        if ticket := item.get("pendingTicket"):
            self.table.update_item(
                Key=self._pending_key(item["owner"]),
                UpdateExpression="DELETE tickets :ticket",
                ExpressionAttributeValues={":ticket": {ticket}},
            )

    def _recover_slots(self, owner: str) -> None:
        # Bounded by the cap. Repairs a crash after a terminal write but before release,
        # TTL removal, and expiry before the maintenance sweep. Never trust a GSI here.
        row: dict[str, Any] = self.table.get_item(
            Key=self._pending_key(owner), ConsistentRead=True
        ).get("Item", {})
        for ticket in row.get("tickets", set()):
            run, job_id, _ = json.loads(ticket)
            item = self.raw(owner, run, job_id)
            if item and item.get("pendingTicket") == ticket and item["state"] in PENDING_STATES:
                if item.get("softExpires", float("inf")) <= time.time():
                    try:
                        self.cancel(owner, run, job_id, expired_only=True)
                    except ClientError as exc:
                        if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
                            raise
                continue
            self.release({"owner": owner, "pendingTicket": ticket})

    def _admit(self, owner: str, ticket: str, writes: list[TransactWriteItemTypeDef]) -> None:
        limit = self.settings.limits.max_captures_ahead
        writes = [
            *writes,
            {
                "Update": {
                    "TableName": self.settings.table_name,
                    "Key": self.encoded(self._pending_key(owner)),
                    "UpdateExpression": "ADD tickets :ticket",
                    "ConditionExpression": "attribute_not_exists(tickets) OR size(tickets)<:limit",
                    "ExpressionAttributeValues": self.encoded(
                        {":ticket": {ticket}, ":limit": limit}
                    ),
                }
            },
        ]
        repaired = False
        for attempt in range(8):
            try:
                self.ddb.transact_write_items(TransactItems=writes)
                return
            except ClientError as exc:
                if exc.response["Error"]["Code"] != "TransactionCanceledException":
                    raise
                codes = [r.get("Code") for r in exc.response.get("CancellationReasons", [])]
                if "ConditionalCheckFailed" in codes[:-1]:
                    raise
                if codes[-1:] == ["ConditionalCheckFailed"]:
                    if repaired:
                        raise LocalError(
                            f"Queue full. Wait for one of the {limit} pending pages.", 409
                        ) from exc
                    self._recover_slots(owner)
                    repaired = True
                elif "TransactionConflict" in codes:
                    time.sleep(random.uniform(0.005, 0.01) * 2**attempt)
                else:
                    raise
        raise LocalError("The queue is busy. Retry this request.", 503)

    def submit(
        self, owner: str, run: str, job_id: str, image: bytes, mode: str, book_id: str | None = None
    ) -> ReadingJob:
        self.check_book(owner, book_id)
        assigned = EARLIER.id if book_id is None else book_id
        digest = hashlib.sha256(image + mode.encode()).hexdigest()
        item = self.raw(owner, run, job_id)
        if item:
            if (
                item["state"] == "CANCELLED"
                or item.get("digest") != digest
                or item.get("bookId", EARLIER.id) != assigned
            ):
                raise LocalError(
                    "This request was discarded or used for another image. Capture again.", 409
                )
            if item["state"] != "UPLOADING":
                return self.view(self.get(owner, run, job_id))
        else:
            nanos = time.time_ns()
            now = nanos // 1_000_000_000
            from backend.contracts import VISION_PROMPT_VERSION

            item = {
                **self.key(owner, run, job_id),
                "id": job_id,
                "owner": owner,
                "run": run,
                "digest": digest,
                "imageHash": hashlib.sha256(image).hexdigest(),
                "pendingTicket": self._ticket(run, job_id),
                "mode": mode,
                "state": "UPLOADING",
                "stage": "validate",
                "attempt": 1,
                "stageCalls": 0,
                "audioKeys": [],
                "audioBytes": 0,
                "inputKey": self.prefix("incoming", owner, run, job_id) + "capture.jpg",
                "createdAt": now,
                "createdNs": nanos,
                **(
                    {"bookId": assigned, "ownerBook": f"{owner}#{assigned}"}
                    if assigned != EARLIER.id
                    else {}
                ),
                "softExpires": now + JOB_SECONDS,
                "expiresAt": now + JOB_SECONDS + 86400,
                "updatedAt": timestamp(now),
                "workState": "UPLOADING",
                "visionModel": self.settings.vision_model,
                "speechModel": self.settings.speech_model,
                "voice": self.settings.speech_voice,
                "promptVersion": VISION_PROMPT_VERSION,
                "chunkCharacters": self.settings.speech_chunk_characters,
            }
            try:
                self._admit(
                    owner,
                    item["pendingTicket"],
                    [
                        *self.book_membership_write(owner, assigned, run, job_id),
                        {
                            "Put": {
                                "TableName": self.settings.table_name,
                                "Item": self.encoded(item),
                                "ConditionExpression": "attribute_not_exists(PK)",
                            }
                        },
                        {
                            "Update": self.quota_update(
                                owner, "captureCount", self.settings.limits.capture_attempts_per_day
                            )
                        },
                    ],
                )
            except ClientError as exc:
                if not conditional(exc):
                    raise
                codes = [r.get("Code") for r in exc.response.get("CancellationReasons", [])]
                if "ConditionalCheckFailed" not in codes:
                    raise
                existing = self.raw(owner, run, job_id)
                if (
                    existing
                    and existing.get("bookId", EARLIER.id) == assigned
                    and existing.get("digest") == digest
                    and existing["state"] != "CANCELLED"
                ):
                    return self.view(existing)
                raise LocalError(
                    "Capture changed, book unavailable, or daily capture limit reached.", 409
                ) from exc
        self.s3.put_object(
            Bucket=self.settings.media_bucket,
            Key=item["inputKey"],
            Body=image,
            ContentType="image/jpeg",
        )
        try:
            response = self.table.update_item(
                Key=self.key(owner, run, job_id),
                UpdateExpression="SET #state=:queued, workState=:queued, updatedAt=:now",
                ConditionExpression="#state=:uploading AND attempt=:attempt",
                ExpressionAttributeNames={"#state": "state"},
                ExpressionAttributeValues={
                    ":queued": "QUEUED",
                    ":uploading": "UPLOADING",
                    ":attempt": item["attempt"],
                    ":now": timestamp(),
                },
                ReturnValues="ALL_NEW",
            )
            return self.view(response["Attributes"])
        except ClientError as exc:
            if not conditional(exc):
                raise
            latest = self.raw(owner, run, job_id)
            if latest and latest["state"] != "CANCELLED":
                return self.view(latest)
            self.s3.delete_object(Bucket=self.settings.media_bucket, Key=item["inputKey"])
            raise LocalError("The capture was discarded during upload.", 409) from exc

    def cancel(
        self,
        owner: str,
        run: str,
        job_id: str,
        *,
        expired_only: bool = False,
        book_id: str | None = None,
    ) -> None:
        now = int(time.time())
        guard: dict[str, Any] = (
            {"ConditionExpression": "softExpires<=:cutoff"} if expired_only else {}
        )
        membership_fields = ", createdAt, bookId, ownerBook"
        if book_id is not None:
            guard["ConditionExpression"] = "bookId=:book AND #owner=:owner AND #run=:run AND id=:id"
            # Keep the book lookup intact until S3 succeeds, including for pre-membership pages.
            membership_fields = ""
        previous = self.table.update_item(
            Key=self.key(owner, run, job_id),
            ReturnValues="ALL_OLD",
            UpdateExpression="SET #state=:cancelled, workState=:deleting, updatedAt=:now, "
            "cleanupUntil=:until, #owner=:owner, #run=:run, id=:id "
            "REMOVE visionJson, narration, #language, audioKeys, audioBytes, inputKey, "
            "audioEdit, obsoleteAudio, originalKey, thumbnailKey, reason, imageHash, digest, "
            "leaseToken, leaseUntil, "
            f"pendingTicket, stageCalls, expiresAt, softExpires, createdNs{membership_fields}",
            **guard,
            ExpressionAttributeNames={
                "#state": "state",
                "#owner": "owner",
                "#run": "run",
                "#language": "language",
            },
            ExpressionAttributeValues={
                ":cancelled": "CANCELLED",
                ":deleting": "DELETING",
                ":now": timestamp(now),
                **({":cutoff": now} if expired_only else {}),
                **({":book": book_id} if book_id is not None else {}),
                ":until": now + 1200,
                ":owner": owner,
                ":run": run,
                ":id": job_id,
            },
        )
        old = previous.get("Attributes", {})
        self.release(old)
        if old.get("state") == "READY" or old.get("audioEdit") or book_id is not None:
            # Invalidate outstanding media URLs before acknowledging deletion.
            self.cleanup(old)
        if book_id is not None:
            self.table.update_item(
                Key=self.key(owner, run, job_id),
                UpdateExpression="REMOVE createdAt, bookId, ownerBook",
                ConditionExpression="#state=:cancelled AND bookId=:book",
                ExpressionAttributeNames={"#state": "state"},
                ExpressionAttributeValues={":cancelled": "CANCELLED", ":book": book_id},
            )
        elif isinstance(old_book := old.get("bookId"), str):
            self.table.delete_item(
                Key={"PK": f"USER#{owner}#BOOK#{old_book}", "SK": f"{run}#{job_id}"}
            )

    def edit_narration(self, owner: str, run: str, job_id: str, narration: str) -> ReadingJob:
        item = self.get(owner, run, job_id)
        if item["state"] != "READY" and not (item.get("audioEdit") and item["state"] == "FAILED"):
            raise LocalError("Wait for this page to finish before editing its narration.", 409)
        obsolete = [*item.get("obsoleteAudio", []), *item["audioKeys"]]
        result = self._requeue(
            item,
            "stage=:speech, narration=:text, audioEdit=:true, obsoleteAudio=:old, "
            "audioKeys=:empty, audioBytes=:zero, ",
            {
                ":speech": "speech",
                ":text": narration,
                ":true": True,
                ":old": obsolete,
                ":empty": [],
            },
            remove=", softExpires, expiresAt",
        )
        self.delete_obsolete_audio(obsolete)
        return result

    def delete_obsolete_audio(self, keys: list[str]) -> None:
        if keys:
            response = self.s3.delete_objects(
                Bucket=self.settings.media_bucket,
                Delete={"Objects": [{"Key": key} for key in keys]},
            )
            if response.get("Errors"):
                raise OSError("Superseded audio could not be removed")

    def retry(self, owner: str, run: str, job_id: str) -> ReadingJob:
        item = self.get(owner, run, job_id)
        if item["state"] != "FAILED" or not item.get("retryable"):
            raise LocalError("This capture cannot be retried. Take a fresh capture.", 409)
        return self._requeue(item, "", {}, charge_capture=not item.get("audioEdit"))

    def _requeue(
        self,
        item: dict[str, Any],
        assignments: str,
        values: dict[str, Any],
        *,
        remove: str = "",
        charge_capture: bool = False,
    ) -> ReadingJob:
        owner, run, job_id = item["owner"], item["run"], item["id"]
        ticket = self._ticket(run, job_id)
        writes: list[TransactWriteItemTypeDef] = [
            {
                "Update": {
                    "TableName": self.settings.table_name,
                    "Key": self.encoded(self.key(owner, run, job_id)),
                    "UpdateExpression": "SET " + assignments + "#state=:queued, workState=:queued, "
                    "attempt=attempt+:one, stageCalls=:zero, updatedAt=:now, "
                    "retryable=:false, pendingTicket=:ticket REMOVE reason" + remove,
                    "ConditionExpression": "#state=:previous AND attempt=:attempt AND "
                    "(attribute_not_exists(softExpires) OR softExpires>:cutoff)",
                    "ExpressionAttributeNames": {"#state": "state"},
                    "ExpressionAttributeValues": self.encoded(
                        {
                            **values,
                            ":queued": "QUEUED",
                            ":previous": item["state"],
                            ":attempt": item["attempt"],
                            ":one": 1,
                            ":zero": 0,
                            ":cutoff": int(time.time()),
                            ":now": timestamp(),
                            ":false": False,
                            ":ticket": ticket,
                        }
                    ),
                }
            }
        ]
        if charge_capture:
            writes.append(
                {
                    "Update": self.quota_update(
                        owner, "captureCount", self.settings.limits.capture_attempts_per_day
                    )
                }
            )
        try:
            self._admit(owner, ticket, writes)
        except ClientError as exc:
            codes = [r.get("Code") for r in exc.response.get("CancellationReasons", [])]
            if charge_capture and codes[1:2] == ["ConditionalCheckFailed"]:
                raise LocalError("Daily capture limit reached. Try again tomorrow.", 409) from exc
            if codes[:1] == ["ConditionalCheckFailed"]:
                raise LocalError("The capture changed. Check its current status.", 409) from exc
            raise
        return self.view(self.get(owner, run, job_id))

    def claim(
        self, owner: str, run: str, job_id: str, attempt: int
    ) -> tuple[dict[str, Any], str] | None:
        token = str(uuid4())
        now = int(time.time())
        try:
            response = self.table.update_item(
                Key=self.key(owner, run, job_id),
                UpdateExpression="SET #state=:processing, workState=:processing, "
                "leaseToken=:token, "
                "leaseUntil=:until, updatedAt=:updated",
                ConditionExpression="#state=:queued AND attempt=:attempt "
                "AND (softExpires>:now OR attribute_exists(audioEdit))",
                ExpressionAttributeNames={"#state": "state"},
                ExpressionAttributeValues={
                    ":processing": "PROCESSING",
                    ":queued": "QUEUED",
                    ":attempt": attempt,
                    ":token": token,
                    ":until": now + LEASE_SECONDS,
                    ":now": now,
                    ":updated": timestamp(now),
                },
                ReturnValues="ALL_NEW",
            )
            return response["Attributes"], token
        except ClientError as exc:
            if conditional(exc):
                return None
            raise

    def checkpoint(
        self,
        item: dict[str, Any],
        token: str,
        changes: dict[str, Any],
        *,
        remove: tuple[str, ...] = (),
    ) -> dict[str, Any]:
        terminal = changes.get("state") in ("READY", "REJECTED", "FAILED", "DUPLICATE")
        if terminal:
            remove = (*remove, "pendingTicket")
        changes = {
            **changes,
            "updatedAt": timestamp(),
            **({} if "leaseUntil" in remove else {"leaseUntil": int(time.time()) + LEASE_SECONDS}),
        }
        names = {f"#f{i}": name for i, name in enumerate(changes)}
        values = {f":v{i}": value for i, value in enumerate(changes.values())}
        assignments = ", ".join(f"#f{i}=:v{i}" for i in range(len(changes)))
        names["#state"] = "state"
        values.update(
            {
                ":processing": "PROCESSING",
                ":token": token,
                ":attempt": item["attempt"],
                ":now": int(time.time()),
            }
        )
        try:
            result = self.table.update_item(
                Key={"PK": item["PK"], "SK": item["SK"]},
                # Keep a cleanup record until durable copies are published or removed.
                UpdateExpression="SET "
                + assignments
                + (" REMOVE " + ", ".join(remove) if remove else ""),
                ConditionExpression=LEASE_CONDITION,
                ExpressionAttributeNames=names,
                ExpressionAttributeValues=values,
                ReturnValues="ALL_NEW",
            )
            if terminal:
                self.release(item)
            return result["Attributes"]
        except ClientError as exc:
            if conditional(exc):
                raise LostLease() from exc
            raise

    def save_audio(self, item: dict[str, Any], prefix: str) -> list[str]:
        bucket = self.settings.media_bucket
        keys = []
        for index, source in enumerate(item["audioKeys"]):
            key = prefix + f"{index}.wav"
            self.s3.copy_object(
                Bucket=bucket, Key=key, CopySource={"Bucket": bucket, "Key": source}
            )
            keys.append(key)
        return keys

    def preserve_legacy_ready(self, item: dict[str, Any]) -> None:
        # The old worker deleted originals; these entries can retain audio but not a thumbnail.
        key = {"PK": item["PK"], "SK": item["SK"]}
        guard = "#state=:ready AND softExpires>:now"
        latest = self.table.update_item(
            Key=key,
            UpdateExpression="REMOVE expiresAt",
            ConditionExpression=guard,
            ExpressionAttributeNames={"#state": "state"},
            ExpressionAttributeValues={":ready": "READY", ":now": int(time.time())},
            ReturnValues="ALL_NEW",
        )["Attributes"]
        prefix = self.prefix("saved", item["owner"], item["run"], item["id"])
        try:
            keys = self.save_audio(latest, prefix)
        except ClientError as exc:
            if exc.response["Error"]["Code"] not in {"NoSuchKey", "404"}:
                raise
            # Let the normal soft-expiry sweep remove an incomplete legacy recording.
            return
        self.table.update_item(
            Key=key,
            UpdateExpression="SET audioKeys=:keys REMOVE expiresAt, softExpires, workState",
            ConditionExpression=guard,
            ExpressionAttributeNames={"#state": "state"},
            ExpressionAttributeValues={":ready": "READY", ":now": int(time.time()), ":keys": keys},
        )

    def complete(self, item: dict[str, Any], token: str, thumbnail: bytes) -> None:
        item = self.checkpoint(item, token, {}, remove=("expiresAt",))
        prefix = self.prefix("saved", item["owner"], item["run"], item["id"])
        bucket = self.settings.media_bucket
        original_key = prefix + "original.jpg"
        self.s3.copy_object(
            Bucket=bucket, Key=original_key, CopySource={"Bucket": bucket, "Key": item["inputKey"]}
        )
        thumbnail_key = prefix + "thumbnail.jpg"
        self.s3.put_object(
            Bucket=bucket, Key=thumbnail_key, Body=thumbnail, ContentType="image/jpeg"
        )
        # A publisher that loses its lease must not overwrite another attempt's signed audio.
        audio_keys = self.save_audio(item, prefix + token + "/")
        try:
            self.checkpoint(
                item,
                token,
                {
                    "state": "READY",
                    "audioKeys": audio_keys,
                    "originalKey": original_key,
                    "thumbnailKey": thumbnail_key,
                    "retryable": False,
                },
                remove=(
                    "leaseToken",
                    "leaseUntil",
                    "softExpires",
                    "expiresAt",
                    "workState",
                    "visionJson",
                    "inputKey",
                    "imageHash",
                    "stageCalls",
                ),
            )
        except LostLease:
            self.delete_obsolete_audio(audio_keys)
            raise

    def audio_url(self, owner: str, run: str, job_id: str, chunk: int) -> str:
        item = self.get(owner, run, job_id)
        keys = item.get("audioKeys", [])
        if item["state"] != "READY" or chunk < 0 or chunk >= len(keys):
            raise LocalError("Audio is not ready for this capture.", 409)
        return self.media_url(keys[chunk], "audio/wav")

    def original_url(self, owner: str, run: str, job_id: str) -> str:
        item = self.get(owner, run, job_id)
        if not item.get("originalKey"):
            raise LocalError("No original image is available for this capture.", 404)
        return self.media_url(item["originalKey"], "image/jpeg")

    def thumbnail_url(self, owner: str, run: str, job_id: str) -> str:
        item = self.get(owner, run, job_id)
        if not item.get("thumbnailKey"):
            raise LocalError("No thumbnail is available for this capture.", 404)
        return self.media_url(item["thumbnailKey"], "image/jpeg")

    def media_url(self, key: str, content_type: str) -> str:
        return self.s3.generate_presigned_url(
            "get_object",
            Params={
                "Bucket": self.settings.media_bucket,
                "Key": key,
                "ResponseContentType": content_type,
                "ResponseCacheControl": "private, no-store",
            },
            ExpiresIn=180,
        )

    def enqueue(self, item: dict[str, Any]) -> None:
        request: SendMessageRequestTypeDef = {
            "QueueUrl": self.settings.queue_url,
            "MessageBody": json.dumps(
                {
                    "owner": item["owner"],
                    "run": item["run"],
                    "id": item["id"],
                    "attempt": int(item["attempt"]),
                }
            ),
        }
        if self.settings.queue_url.endswith(".fifo"):
            request["MessageGroupId"] = "reader"
        self.sqs.send_message(**request)

    def cleanup(self, item: dict[str, Any]) -> None:
        for kind in ("incoming", "audio", "saved"):
            prefix = self.prefix(kind, item["owner"], item["run"], item["id"])
            paginator = self.s3.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=self.settings.media_bucket, Prefix=prefix):
                objects = [{"Key": obj["Key"]} for obj in page.get("Contents", [])]
                if objects:
                    result = self.s3.delete_objects(
                        Bucket=self.settings.media_bucket,
                        Delete={
                            "Objects": [{"Key": obj["Key"]} for obj in page.get("Contents", [])]
                        },
                    )
                    if result.get("Errors"):
                        raise RuntimeError("Private media cleanup was incomplete")
