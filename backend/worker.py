import asyncio
import hashlib
import json
import logging
from typing import Any

import boto3
import httpx
from botocore.exceptions import ClientError
from pydantic import BaseModel, SecretStr, ValidationError

from backend.cloud import CloudStore, LostLease
from backend.config import Settings
from backend.contracts import VisionResult
from backend.local_reader import LocalError, validate_image
from backend.providers import ProviderError, Providers, speech_chunks
from backend.vision_images import capture_thumbnail

logger = logging.getLogger(__name__)


class Credentials(BaseModel):
    openrouter_api_key: SecretStr


def provider_settings(settings: Settings) -> Settings:
    secret = boto3.client("secretsmanager", region_name=settings.aws_region).get_secret_value(
        SecretId=settings.secret_arn
    )
    try:
        credentials = Credentials.model_validate_json(secret.get("SecretString", "{}"))
    except ValidationError:
        raise RuntimeError("Provider secret must contain the OpenRouter credential") from None
    if not credentials.openrouter_api_key.get_secret_value():
        raise ProviderError("Reading providers are not configured. Contact the account owner.")
    return settings.model_copy(update=credentials.model_dump())


def load_image(store: CloudStore, item: dict[str, Any]) -> bytes:
    try:
        body = store.s3.get_object(Bucket=store.settings.media_bucket, Key=item["inputKey"])["Body"]
    except ClientError as exc:
        if exc.response["Error"]["Code"] in {"NoSuchKey", "404"}:
            raise ProviderError("The uploaded image expired. Take a fresh capture.") from exc
        raise
    try:
        image = body.read(store.settings.camera.max_upload_bytes + 1)
    finally:
        body.close()
    if (
        len(image) > store.settings.camera.max_upload_bytes
        or hashlib.sha256(image).hexdigest() != item["imageHash"]
    ):
        raise ProviderError("The uploaded image changed or exceeded its size limit. Capture again.")
    return validate_image(image, store.settings)


async def process(store: CloudStore, message: dict[str, Any], providers: Providers) -> None:
    claimed = store.claim(message["owner"], message["run"], message["id"], int(message["attempt"]))
    if claimed is None:
        return
    item, token = claimed
    providers = Providers(
        providers.settings.model_copy(
            update={
                "vision_model": item["visionModel"],
                "speech_model": item["speechModel"],
                "speech_voice": item["voice"],
                "speech_chunk_characters": int(item["chunkCharacters"]),
            }
        ),
        providers.client,
    )
    try:
        image = None
        editing = bool(item.get("audioEdit"))
        if not editing and "visionJson" not in item:
            image = load_image(store, item)
            item = store.checkpoint(item, token, {"stage": "vision"})
            while True:
                if int(item.get("stageCalls", 0)) >= store.settings.limits.stage_attempts:
                    raise ProviderError(
                        "Recognition reached its retry limit. Retry or capture again.",
                        retryable=True,
                    )
                item = store.checkpoint(
                    item, token, {"stageCalls": int(item.get("stageCalls", 0)) + 1}
                )
                store.quota(item["owner"], "vision")
                try:
                    async with asyncio.timeout(store.settings.provider_timeout_seconds + 5):
                        vision = await providers.recognize(image, item["mode"])
                    break
                except (ProviderError, TimeoutError) as exc:
                    if isinstance(exc, ProviderError) and not exc.retryable:
                        raise
                    if int(item["stageCalls"]) >= store.settings.limits.stage_attempts:
                        raise ProviderError(
                            "Recognition timed out or failed. Retry this capture.", retryable=True
                        ) from exc
                    await asyncio.sleep(2 ** (int(item["stageCalls"]) - 1))
            item = store.checkpoint(
                item, token, {"visionJson": vision.model_dump_json(), "stageCalls": 0}
            )
        if editing:
            # Also clean up if the API was interrupted after queuing the edit.
            store.delete_obsolete_audio(item.get("obsoleteAudio", []))
            item = store.checkpoint(item, token, {}, remove=("obsoleteAudio",))
        else:
            vision = VisionResult.model_validate_json(item["visionJson"])
            if vision.needs_retake:
                store.checkpoint(
                    item,
                    token,
                    {
                        "state": "REJECTED",
                        "workState": "REJECTED",
                        "reason": vision.retake_reason,
                        "retryable": False,
                    },
                )
                store.s3.delete_object(Bucket=store.settings.media_bucket, Key=item["inputKey"])
                return
            item = store.checkpoint(
                item,
                token,
                {
                    "stage": "speech",
                    "narration": vision.ocr_text or vision.illustration_description,
                    "language": vision.language,
                },
            )
        chunks = speech_chunks(item["narration"], int(item["chunkCharacters"]))
        while len(item["audioKeys"]) < len(chunks):
            index = len(item["audioKeys"])
            if int(item.get("stageCalls", 0)) >= store.settings.limits.stage_attempts:
                raise ProviderError(
                    "Speech reached its retry limit. Retry Audio to continue.", retryable=True
                )
            item = store.checkpoint(item, token, {"stageCalls": int(item.get("stageCalls", 0)) + 1})
            store.quota(item["owner"], "speech")
            try:
                async with asyncio.timeout(store.settings.provider_timeout_seconds + 5):
                    audio = await providers.speak(chunks[index])
            except (ProviderError, TimeoutError) as exc:
                if isinstance(exc, ProviderError) and not exc.retryable:
                    raise
                if int(item["stageCalls"]) >= store.settings.limits.stage_attempts:
                    raise ProviderError(
                        "Speech timed out or failed. Retry Audio to continue.", retryable=True
                    ) from exc
                await asyncio.sleep(2 ** (int(item["stageCalls"]) - 1))
                continue
            if int(item["audioBytes"]) + len(audio) > 16 * 1024 * 1024:
                raise ProviderError(
                    "This page exceeds the audio limit. Use a shorter single-page capture."
                )
            item = store.checkpoint(item, token, {})
            key = (
                store.prefix(
                    "saved" if editing else "audio", item["owner"], item["run"], item["id"]
                )
                + f"{token}/{index}.wav"
            )
            store.s3.put_object(
                Bucket=store.settings.media_bucket, Key=key, Body=audio, ContentType="audio/wav"
            )
            try:
                item = store.checkpoint(
                    item,
                    token,
                    {
                        "audioKeys": [*item["audioKeys"], key],
                        "audioBytes": int(item["audioBytes"]) + len(audio),
                        "stageCalls": 0,
                    },
                )
            except LostLease:
                store.s3.delete_object(Bucket=store.settings.media_bucket, Key=key)
                raise
        if editing:
            store.checkpoint(
                item,
                token,
                {"state": "READY"},
                remove=("workState", "audioEdit", "stageCalls", "leaseToken", "leaseUntil"),
            )
            return
        thumbnail = capture_thumbnail(image if image is not None else load_image(store, item))
        store.complete(item, token, thumbnail)
        for key in item["audioKeys"]:
            store.s3.delete_object(Bucket=store.settings.media_bucket, Key=key)
        store.s3.delete_object(Bucket=store.settings.media_bucket, Key=item["inputKey"])
    except (ProviderError, LocalError) as exc:
        store.checkpoint(
            item,
            token,
            {
                "state": "FAILED",
                "workState": "FAILED",
                "reason": str(exc),
                "retryable": isinstance(exc, ProviderError) and exc.retryable,
            },
        )
        logger.warning(
            "reading_failed job=%s stage=%s retryable=%s",
            item["id"],
            item["stage"],
            isinstance(exc, ProviderError) and exc.retryable,
        )


async def batch(event: dict[str, Any]) -> dict[str, Any]:
    settings = Settings()
    store = CloudStore(settings)
    failures: list[dict[str, str]] = []
    async with httpx.AsyncClient() as client:
        providers = Providers(provider_settings(settings), client)
        for record in event.get("Records", []):
            try:
                await process(store, json.loads(record["body"]), providers)
            except LostLease:
                logger.info("reading_fenced message=%s", record["messageId"])
            except (ClientError, OSError, ValueError, KeyError) as exc:
                # A failed infrastructure operation must be redelivered; never log its payload.
                logger.error(
                    "reading_internal_error message=%s error_type=%s",
                    record["messageId"],
                    type(exc).__name__,
                )
                failures.append({"itemIdentifier": record["messageId"]})
    return {"batchItemFailures": failures}


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    return asyncio.run(batch(event))
