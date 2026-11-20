from __future__ import annotations

import base64
import hashlib
import json
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import boto3
from boto3.dynamodb.conditions import Attr, Key
from boto3.dynamodb.types import TypeSerializer
from botocore.config import Config
from botocore.exceptions import ClientError

from backend.config import Settings
from backend.contracts import (
    RECENT_CAPTURE_LIMIT,
    CaptureStage,
    CaptureState,
    PlayableCapture,
    ReadingJob,
    RecentCaptures,
)
from backend.local_reader import LocalError
from backend.providers import ProviderError

if TYPE_CHECKING:
    from mypy_boto3_dynamodb.type_defs import UpdateTypeDef

LEASE_SECONDS = 120
JOB_SECONDS = 3600
LEASE_CONDITION = (
    "#state=:processing AND leaseToken=:token AND attempt=:attempt "
    "AND leaseUntil>:now AND softExpires>:now"
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

    def recent(
        self, owner: str, limit: int = RECENT_CAPTURE_LIMIT, cursor: str | None = None
    ) -> RecentCaptures:
        if not 1 <= limit <= RECENT_CAPTURE_LIMIT:
            raise LocalError("Invalid capture page size.", 400)
        items: list[PlayableCapture] = []
        start: dict[str, Any] = {}
        if cursor is not None:
            try:
                if len(cursor) > 4096:
                    raise ValueError
                data = json.loads(base64.b64decode(cursor, altchars=b"-_", validate=True))
                if not isinstance(data, list) or len(data) != 4:
                    raise ValueError
                subject, run, job_id, created_at = data
                if (
                    subject != owner
                    or not isinstance(run, str)
                    or not isinstance(job_id, str)
                    or not 0 < len(run.encode()) <= 256
                    or not 0 < len(job_id.encode()) <= 256
                    or type(created_at) is not int
                    or not 0 <= created_at < 2**63
                ):
                    raise ValueError
                start = {
                    "ExclusiveStartKey": {
                        **self.key(owner, run, job_id),
                        "owner": owner,
                        "createdAt": created_at,
                    }
                }
            except (ValueError, TypeError, RecursionError) as exc:
                raise LocalError("Invalid capture history cursor.", 400) from exc
        while True:
            page = self.table.query(
                IndexName="OwnerCapturesIndex",
                KeyConditionExpression=Key("owner").eq(owner),
                FilterExpression=Attr("state").eq("READY"),
                ScanIndexForward=False,
                Limit=RECENT_CAPTURE_LIMIT,
                **start,
            )
            for candidate in page.get("Items", []):
                # The index can lag a discard. Never serve narration from its projection.
                item: dict[str, Any] = self.table.get_item(
                    Key={"PK": candidate["PK"], "SK": candidate["SK"]}, ConsistentRead=True
                ).get("Item", {})
                if (
                    item.get("owner") == owner
                    and item.get("state") == "READY"
                    and ("softExpires" not in item or item["softExpires"] > int(time.time()))
                    and item.get("audioKeys")
                ):
                    if len(items) == limit:
                        last = items[-1]
                        position = json.dumps([owner, last.run_id, last.id, last.created_at])
                        return RecentCaptures(
                            items=items,
                            next_cursor=base64.urlsafe_b64encode(position.encode()).decode(),
                        )
                    items.append(
                        PlayableCapture(
                            thumbnail_url=self.media_url(item["thumbnailKey"], "image/jpeg")
                            if item.get("thumbnailKey")
                            else None,
                            id=item["id"],
                            run_id=item["run"],
                            created_at=int(item["createdAt"]),
                            narration=item.get("narration", ""),
                            language=item.get("language", ""),
                            audio_chunks=len(item["audioKeys"]),
                        )
                    )
            if "LastEvaluatedKey" not in page:
                return RecentCaptures(items=items)
            start = {"ExclusiveStartKey": page["LastEvaluatedKey"]}

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

    def submit(self, owner: str, run: str, job_id: str, image: bytes, mode: str) -> ReadingJob:
        digest = hashlib.sha256(image + mode.encode()).hexdigest()
        item = self.raw(owner, run, job_id)
        if item:
            if item["state"] == "CANCELLED" or item.get("digest") != digest:
                raise LocalError(
                    "This request was discarded or used for another image. Capture again.", 409
                )
            if item["state"] != "UPLOADING":
                return self.view(self.get(owner, run, job_id))
        else:
            now = int(time.time())
            from backend.contracts import VISION_PROMPT_VERSION

            item = {
                **self.key(owner, run, job_id),
                "id": job_id,
                "owner": owner,
                "run": run,
                "digest": digest,
                "imageHash": hashlib.sha256(image).hexdigest(),
                "mode": mode,
                "state": "UPLOADING",
                "stage": "validate",
                "attempt": 1,
                "stageCalls": 0,
                "audioKeys": [],
                "audioBytes": 0,
                "inputKey": self.prefix("incoming", owner, run, job_id) + "capture.jpg",
                "createdAt": now,
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
                self.ddb.transact_write_items(
                    TransactItems=[
                        {
                            "Put": {
                                "TableName": self.settings.table_name,
                                "Item": self.encoded(item),
                                "ConditionExpression": "attribute_not_exists(PK)",
                            }
                        },
                        {
                            "Update": {
                                "TableName": self.settings.table_name,
                                "Key": self.encoded({"PK": item["PK"], "SK": "RUN"}),
                                "UpdateExpression": "SET activeJob = :id, expiresAt = :expiry",
                                "ConditionExpression": "attribute_not_exists(activeJob) "
                                "OR activeJob = :id",
                                "ExpressionAttributeValues": self.encoded(
                                    {":id": job_id, ":expiry": now + 90000}
                                ),
                            }
                        },
                        {
                            "Update": self.quota_update(
                                owner, "captureCount", self.settings.limits.capture_attempts_per_day
                            )
                        },
                    ]
                )
            except ClientError as exc:
                if not conditional(exc):
                    raise
                existing = self.raw(owner, run, job_id)
                if (
                    existing
                    and existing.get("digest") == digest
                    and existing["state"] != "CANCELLED"
                ):
                    return self.view(existing)
                raise LocalError(
                    "Discard the previous capture or check the daily capture limit.", 409
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

    def cancel(self, owner: str, run: str, job_id: str, *, expired_only: bool = False) -> None:
        now = int(time.time())
        guard: dict[str, Any] = (
            {"ConditionExpression": "softExpires<=:cutoff"} if expired_only else {}
        )
        previous = self.table.update_item(
            Key=self.key(owner, run, job_id),
            ReturnValues="ALL_OLD",
            UpdateExpression="SET #state=:cancelled, workState=:deleting, updatedAt=:now, "
            "cleanupUntil=:until, #owner=:owner, #run=:run, id=:id "
            "REMOVE visionJson, narration, #language, audioKeys, audioBytes, inputKey, "
            "thumbnailKey, reason, imageHash, digest, leaseToken, leaseUntil, stageCalls, "
            "expiresAt, softExpires, createdAt",
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
                ":until": now + 1200,
                ":owner": owner,
                ":run": run,
                ":id": job_id,
            },
        )
        try:
            self.table.update_item(
                Key={"PK": self.key(owner, run, job_id)["PK"], "SK": "RUN"},
                UpdateExpression="REMOVE activeJob",
                ConditionExpression="activeJob=:id",
                ExpressionAttributeValues={":id": job_id},
            )
        except ClientError as exc:
            if not conditional(exc):
                raise

        old = previous.get("Attributes", {})
        if old.get("state") == "READY":
            # Invalidate outstanding media URLs before acknowledging deletion.
            self.cleanup(old)

    def retry(self, owner: str, run: str, job_id: str) -> ReadingJob:
        item = self.get(owner, run, job_id)
        if item["state"] != "FAILED" or not item.get("retryable"):
            raise LocalError("This capture cannot be retried. Take a fresh capture.", 409)
        self.quota(owner, "capture")
        try:
            response = self.table.update_item(
                Key=self.key(owner, run, job_id),
                UpdateExpression="SET #state=:queued, workState=:queued, attempt=attempt+:one, "
                "stageCalls=:zero, updatedAt=:now, retryable=:false REMOVE reason",
                ConditionExpression="#state=:failed AND attempt=:attempt",
                ExpressionAttributeNames={"#state": "state"},
                ExpressionAttributeValues={
                    ":queued": "QUEUED",
                    ":failed": "FAILED",
                    ":attempt": item["attempt"],
                    ":one": 1,
                    ":zero": 0,
                    ":now": timestamp(),
                    ":false": False,
                },
                ReturnValues="ALL_NEW",
            )
            return self.view(response["Attributes"])
        except ClientError as exc:
            if not conditional(exc):
                raise
            raise LocalError("The capture changed. Check its current status.", 409) from exc

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
                ConditionExpression="#state=:queued AND attempt=:attempt AND softExpires>:now",
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
        self, item: dict[str, Any], token: str, changes: dict[str, Any], *, publishing: bool = False
    ) -> dict[str, Any]:
        changes = {
            **changes,
            "updatedAt": timestamp(),
            "leaseUntil": int(time.time()) + LEASE_SECONDS,
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
                UpdateExpression="SET " + assignments + (" REMOVE expiresAt" if publishing else ""),
                ConditionExpression=LEASE_CONDITION,
                ExpressionAttributeNames=names,
                ExpressionAttributeValues=values,
                ReturnValues="ALL_NEW",
            )
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
        item = self.checkpoint(item, token, {}, publishing=True)
        prefix = self.prefix("saved", item["owner"], item["run"], item["id"])
        bucket = self.settings.media_bucket
        thumbnail_key = prefix + "thumbnail.jpg"
        self.s3.put_object(
            Bucket=bucket, Key=thumbnail_key, Body=thumbnail, ContentType="image/jpeg"
        )
        audio_keys = self.save_audio(item, prefix)
        now = int(time.time())
        try:
            self.ddb.transact_write_items(
                TransactItems=[
                    {
                        "Update": {
                            "TableName": self.settings.table_name,
                            "Key": self.encoded({"PK": item["PK"], "SK": item["SK"]}),
                            "UpdateExpression": "SET #state=:ready, audioKeys=:audio, "
                            "thumbnailKey=:thumbnail, retryable=:false, updatedAt=:updated "
                            "REMOVE leaseToken, leaseUntil, softExpires, expiresAt, workState, "
                            "visionJson, inputKey, imageHash, stageCalls",
                            "ConditionExpression": LEASE_CONDITION,
                            "ExpressionAttributeNames": {"#state": "state"},
                            "ExpressionAttributeValues": self.encoded(
                                {
                                    ":ready": "READY",
                                    ":audio": audio_keys,
                                    ":thumbnail": thumbnail_key,
                                    ":false": False,
                                    ":updated": timestamp(now),
                                    ":processing": "PROCESSING",
                                    ":token": token,
                                    ":attempt": item["attempt"],
                                    ":now": now,
                                }
                            ),
                        }
                    },
                    {
                        "Update": {
                            "TableName": self.settings.table_name,
                            "Key": self.encoded({"PK": item["PK"], "SK": "RUN"}),
                            "UpdateExpression": "REMOVE activeJob",
                            "ConditionExpression": "activeJob=:id",
                            "ExpressionAttributeValues": self.encoded({":id": item["id"]}),
                        }
                    },
                ]
            )
        except ClientError as exc:
            if any(
                reason.get("Code") == "ConditionalCheckFailed"
                for reason in exc.response.get("CancellationReasons", [])
            ):
                raise LostLease() from exc
            raise

    def audio_url(self, owner: str, run: str, job_id: str, chunk: int) -> str:
        item = self.get(owner, run, job_id)
        keys = item.get("audioKeys", [])
        if item["state"] != "READY" or chunk < 0 or chunk >= len(keys):
            raise LocalError("Audio is not ready for this capture.", 409)
        return self.media_url(keys[chunk], "audio/wav")

    def thumbnail_url(self, owner: str, run: str, job_id: str) -> str:
        item = self.get(owner, run, job_id)
        if item["state"] != "READY" or not item.get("thumbnailKey"):
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
        self.sqs.send_message(
            QueueUrl=self.settings.queue_url,
            MessageBody=json.dumps(
                {
                    "owner": item["owner"],
                    "run": item["run"],
                    "id": item["id"],
                    "attempt": int(item["attempt"]),
                }
            ),
        )

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
