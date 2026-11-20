import asyncio
import base64
import hashlib
import logging
import math
import time
import warnings
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from io import BytesIO
from typing import TypeVar

from PIL import Image, UnidentifiedImageError

from backend.config import Settings
from backend.contracts import (
    RECENT_CAPTURE_LIMIT,
    CaptureStage,
    CaptureState,
    PlayableCapture,
    ReadingJob,
    RecentCaptures,
    VisionResult,
)
from backend.providers import ProviderError, Providers, speech_chunks
from backend.vision_images import capture_thumbnail

logger = logging.getLogger(__name__)
T = TypeVar("T")


class LocalError(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def validate_image(data: bytes, settings: Settings) -> bytes:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(data)) as image:
                if image.format != "JPEG":
                    raise LocalError("Capture must be a JPEG image.")
                width, height = image.size
                if width * height > 16_000_000 or max(width, height) > 4096:
                    raise LocalError("The image dimensions exceed the decoding limit.")
                if (
                    width < settings.camera.min_crop_width
                    or height < settings.camera.min_crop_height
                ):
                    raise LocalError(
                        "The captured image is too small. Use a higher-resolution camera."
                    )
                image.load()
                output = BytesIO()
                image.convert("RGB").save(output, format="JPEG", quality=95, subsampling=0)
                return output.getvalue()
    except (
        UnidentifiedImageError,
        OSError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ) as exc:
        raise LocalError("The image could not be decoded safely. Capture the page again.") from exc


@dataclass
class Job:
    view: ReadingJob
    run_id: str
    digest: str
    image: bytes
    mode: str
    thumbnail: bytes = b""
    vision: VisionResult | None = None
    audio: list[bytes] = field(default_factory=list)
    created: float = field(default_factory=time.monotonic)
    created_at: int = field(default_factory=lambda: int(time.time()))
    task: asyncio.Task[None] | None = None


class LocalReader:
    """Single-process evaluation storage; intentionally not a durable AWS worker."""

    def __init__(self, settings: Settings, providers: Providers) -> None:
        self.settings = settings
        self.providers = providers
        self.jobs: dict[str, Job] = {}
        self.cancelled: dict[tuple[str, str], float] = {}
        self.semaphore = asyncio.Semaphore(2)
        self.usage: dict[str, int] = {}
        self.usage_day = datetime.now(UTC).date()

    def quota(self, kind: str, limit: int) -> None:
        day = datetime.now(UTC).date()
        if day != self.usage_day:
            self.usage.clear()
            self.usage_day = day
        if self.usage.get(kind, 0) >= limit:
            raise ProviderError(f"Daily local {kind} limit reached. Try again tomorrow.")
        self.usage[kind] = self.usage.get(kind, 0) + 1

    def get(self, run_id: str, job_id: str) -> Job:
        self.expire()
        job = self.jobs.get(job_id)
        if job is None or job.run_id != run_id:
            raise LocalError(
                "This capture expired or the local server restarted. Capture the page again.", 404
            )
        return job

    def recent(self, run_id: str, cursor: str | None = None) -> RecentCaptures:
        self.expire()
        before = math.inf
        if cursor is not None:
            try:
                before = float(cursor)
                if not math.isfinite(before):
                    raise ValueError
            except ValueError as exc:
                raise LocalError("Invalid history cursor.") from exc
        jobs = sorted(
            (
                job
                for job in self.jobs.values()
                if job.run_id == run_id
                and job.created < before
                and job.view.state == CaptureState.READY
                and job.audio
            ),
            key=lambda job: job.created,
            reverse=True,
        )
        page = jobs[:RECENT_CAPTURE_LIMIT]
        return RecentCaptures(
            items=[
                PlayableCapture(
                    id=job.view.id,
                    run_id=run_id,
                    created_at=job.created_at,
                    narration=job.view.narration,
                    language=job.view.language,
                    audio_chunks=len(job.audio),
                    thumbnail_url="data:image/jpeg;base64,"
                    + base64.b64encode(job.thumbnail).decode(),
                )
                for job in page
            ],
            next_cursor=str(page[-1].created) if len(jobs) > len(page) else None,
        )

    def expire(self) -> None:
        self.cancelled = {
            key: timestamp
            for key, timestamp in self.cancelled.items()
            if time.monotonic() - timestamp < 3600
        }
        for job_id, job in list(self.jobs.items()):
            if time.monotonic() - job.created > 3600:
                self.remove(job.run_id, job_id)

    def remove(self, run_id: str, job_id: str) -> None:
        self.cancelled[(run_id, job_id)] = time.monotonic()
        job = self.jobs.get(job_id)
        if job and job.run_id == run_id:
            if job.task:
                job.task.cancel()
            del self.jobs[job_id]

    def submit(self, run_id: str, job_id: str, image: bytes, mode: str) -> ReadingJob:
        self.expire()
        if (run_id, job_id) in self.cancelled:
            raise LocalError("This capture was discarded. Start a new capture.", 409)
        digest = hashlib.sha256(image + mode.encode()).hexdigest()
        existing = self.jobs.get(job_id)
        if existing:
            if existing.run_id != run_id or existing.digest != digest:
                raise LocalError("Capture request ID was already used for different input.", 409)
            return existing.view
        if any(
            job.run_id == run_id and job.view.state != CaptureState.READY
            for job in self.jobs.values()
        ):
            raise LocalError("Finish or discard the current page before another capture.", 409)
        if len(self.jobs) >= 16:
            raise LocalError(
                "The local reader is full. Discard unused pages or try again later.", 429
            )
        image = validate_image(image, self.settings)
        job = Job(
            view=ReadingJob(
                id=job_id,
                state=CaptureState.QUEUED,
                stage=CaptureStage.VISION,
                vision_model=self.settings.vision_model,
                speech_model=self.settings.speech_model,
                voice=self.settings.speech_voice,
            ),
            run_id=run_id,
            digest=digest,
            image=image,
            mode=mode,
        )
        self.jobs[job_id] = job
        self.start(job)
        return job.view

    def start(self, job: Job) -> None:
        job.view.state = CaptureState.QUEUED
        job.view.reason = None
        job.view.retryable = False
        job.task = asyncio.create_task(self.process(job))
        job.task.add_done_callback(lambda task: self.finished(job, task))

    @staticmethod
    def finished(job: Job, task: asyncio.Task[None]) -> None:
        if not task.cancelled() and (error := task.exception()) is not None:
            job.view.state = CaptureState.FAILED
            job.view.reason = "An internal processing error occurred. Check server logs and retry."
            job.view.retryable = True
            logger.error("local_job_failed job=%s error_type=%s", job.view.id, type(error).__name__)

    def retry(self, run_id: str, job_id: str) -> ReadingJob:
        job = self.get(run_id, job_id)
        if job.view.state != CaptureState.FAILED or not job.view.retryable:
            raise LocalError("This capture cannot be retried. Capture a new page.", 409)
        self.start(job)
        return job.view

    async def attempt(self, operation: Callable[[], Awaitable[T]], kind: str) -> T:
        limit = (
            self.settings.limits.capture_attempts_per_day
            if kind == "vision"
            else self.settings.limits.speech_attempts_per_day
        )
        for attempt in range(self.settings.limits.stage_attempts):
            self.quota(kind, limit)
            try:
                return await operation()
            except ProviderError as exc:
                if not exc.retryable or attempt + 1 == self.settings.limits.stage_attempts:
                    raise
                await asyncio.sleep(2**attempt)
        raise AssertionError("Stage attempt limit must be positive")

    async def process(self, job: Job) -> None:
        try:
            async with self.semaphore:
                job.view.state = CaptureState.PROCESSING
                if job.vision is None:
                    job.view.stage = CaptureStage.VISION
                    job.vision = await self.attempt(
                        lambda: self.providers.recognize(job.image, job.mode), "vision"
                    )
                    if not job.vision.needs_retake:
                        job.thumbnail = capture_thumbnail(job.image)
                    job.image = b""
                if job.vision.needs_retake:
                    job.view.state = CaptureState.REJECTED
                    job.view.reason = job.vision.retake_reason
                    return
                job.view.narration = job.vision.ocr_text or job.vision.illustration_description
                job.view.language = job.vision.language
                job.view.stage = CaptureStage.SPEECH
                chunks = speech_chunks(job.view.narration, self.settings.speech_chunk_characters)
                for chunk in chunks[len(job.audio) :]:
                    audio = await self.attempt(lambda: self.providers.speak(chunk), "speech")
                    if sum(map(len, job.audio)) + len(audio) > 16 * 1024 * 1024:
                        raise ProviderError(
                            "Local audio limit exceeded. Use a shorter single-page capture."
                        )
                    job.audio.append(audio)
                job.view.audio_chunks = len(job.audio)
                job.view.state = CaptureState.READY
        except ProviderError as exc:
            job.view.state = CaptureState.FAILED
            job.view.reason = str(exc)
            job.view.retryable = exc.retryable
            logger.warning(
                "local_job_failed job=%s stage=%s retryable=%s",
                job.view.id,
                job.view.stage,
                exc.retryable,
            )

    async def close(self) -> None:
        tasks = [job.task for job in self.jobs.values() if job.task]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.jobs.clear()
