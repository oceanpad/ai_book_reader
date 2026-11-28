import logging
from typing import Any

from boto3.dynamodb.types import TypeDeserializer
from botocore.exceptions import ClientError

from backend.cloud import CloudStore
from backend.config import Settings
from backend.homework import PENDING, CloudHomework

logger = logging.getLogger(__name__)


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    store = CloudStore(Settings())
    decoder = TypeDeserializer()
    failures: list[dict[str, str]] = []
    for record in event.get("Records", []):
        stream = record.get("dynamodb", {})
        if "NewImage" not in stream:
            continue
        item = {key: decoder.deserialize(value) for key, value in stream["NewImage"].items()}
        old = {key: decoder.deserialize(value) for key, value in stream.get("OldImage", {}).items()}
        is_homework = item.get("workState") == PENDING and old.get("workState") != PENDING
        is_reading = (
            item.get("SK", "").startswith("JOB#")
            and item.get("state") == "QUEUED"
            and not (old.get("state") == "QUEUED" and old.get("attempt") == item["attempt"])
        )
        if not (is_homework or is_reading):
            continue
        try:
            if is_homework:
                CloudHomework(store).enqueue(item)
            else:
                store.enqueue(item)
        except ClientError as exc:
            logger.error("dispatch_failed job=%s error_type=%s", item["id"], type(exc).__name__)
            failures.append({"itemIdentifier": stream["SequenceNumber"]})
    return {"batchItemFailures": failures}
