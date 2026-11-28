import logging
import time
from typing import Any

from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from backend.cloud import CloudStore, conditional, timestamp
from backend.config import Settings
from backend.homework import CloudHomework

logger = logging.getLogger(__name__)


def maintain(store: CloudStore) -> None:
    now = int(time.time())
    for state in ("UPLOADING", "QUEUED", "PROCESSING", "READY", "FAILED", "REJECTED", "DELETING"):
        start: dict[str, Any] = {}
        while True:
            response = store.table.query(
                IndexName="MaintenanceIndex",
                KeyConditionExpression=Key("workState").eq(state),
                **start,
            )
            for raw_item in response.get("Items", []):
                item: dict[str, Any] = dict(raw_item)
                try:
                    if state == "DELETING":
                        store.cleanup(item)
                        if item["cleanupUntil"] < now:
                            store.table.update_item(
                                Key={"PK": item["PK"], "SK": item["SK"]},
                                UpdateExpression="SET workState=:done, expiresAt=:expiry",
                                ConditionExpression="#state=:cancelled AND cleanupUntil<:now",
                                ExpressionAttributeNames={"#state": "state"},
                                ExpressionAttributeValues={
                                    ":done": "TOMBSTONE",
                                    ":expiry": now + 86400,
                                    ":cancelled": "CANCELLED",
                                    ":now": now,
                                },
                            )
                    elif item.get("softExpires", now + 1) <= now:
                        store.cancel(item["owner"], item["run"], item["id"], expired_only=True)
                    elif state == "READY":
                        store.preserve_legacy_ready(item)
                    elif state == "QUEUED" and item["updatedAt"] < timestamp(now - 60):
                        store.enqueue(item)
                    elif state == "PROCESSING" and item.get("leaseUntil", 0) < now:
                        store.table.update_item(
                            Key={"PK": item["PK"], "SK": item["SK"]},
                            UpdateExpression="SET #state=:queued, workState=:queued, "
                            "attempt=attempt+:one, updatedAt=:updated",
                            ConditionExpression="#state=:processing AND leaseUntil<:now "
                            "AND attempt=:attempt",
                            ExpressionAttributeNames={"#state": "state"},
                            ExpressionAttributeValues={
                                ":queued": "QUEUED",
                                ":processing": "PROCESSING",
                                ":one": 1,
                                ":now": now,
                                ":attempt": item["attempt"],
                                ":updated": timestamp(now),
                            },
                        )
                    elif state == "UPLOADING" and item["updatedAt"] < timestamp(now - 300):
                        previous = store.table.update_item(
                            Key={"PK": item["PK"], "SK": item["SK"]},
                            UpdateExpression="SET #state=:failed, workState=:failed, "
                            "reason=:reason, retryable=:false REMOVE pendingTicket",
                            ConditionExpression="#state=:uploading AND updatedAt<:cutoff",
                            ReturnValues="ALL_OLD",
                            ExpressionAttributeNames={"#state": "state"},
                            ExpressionAttributeValues={
                                ":failed": "FAILED",
                                ":uploading": "UPLOADING",
                                ":cutoff": timestamp(now - 300),
                                ":false": False,
                                ":reason": "The upload did not finish. Capture the page again.",
                            },
                        )
                        store.release(previous["Attributes"])
                except ClientError as exc:
                    if not conditional(exc):
                        raise
            if "LastEvaluatedKey" not in response:
                break
            start = {"ExclusiveStartKey": response["LastEvaluatedKey"]}
    CloudHomework(store).maintain()


def handler(event: dict[str, Any], context: Any) -> None:
    maintain(CloudStore(Settings()))
    logger.info("reader_maintenance_complete")
