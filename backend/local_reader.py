import asyncio
import base64
import hashlib
import logging
import math
import sqlite3
import time
import warnings
from collections.abc import Awaitable, Callable, Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from typing import TypeVar
from uuid import uuid4

from PIL import Image, UnidentifiedImageError

from backend.config import Settings
from backend.contracts import (
    EARLIER,
    RECENT_CAPTURE_LIMIT,
    Book,
    BookCover,
    Books,
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
LOCAL_MEDIA_LIMIT = 256 * 1024 * 1024


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
    book_id: str = EARLIER.id
    thumbnail: bytes = b""
    vision: VisionResult | None = None
    audio: list[bytes] = field(default_factory=list)
    edited: bool = False
    created: float = field(default_factory=time.monotonic)
    created_at: int = field(default_factory=lambda: int(time.time()))


class LocalReader:
    """Single-process evaluation storage; intentionally not a durable AWS worker."""

    def __init__(self, settings: Settings, providers: Providers) -> None:
        self.settings = settings
        self.providers = providers
        self.jobs: dict[str, Job] = {}
        self.cancelled: dict[tuple[str, str], float] = {}
        self.semaphore = asyncio.Semaphore(2)
        self.active: dict[str, tuple[Job, asyncio.Task[None]]] = {}
        self.usage: dict[str, int] = {}
        self.usage_day = datetime.now(UTC).date()

    @contextmanager
    def book_db(self) -> Iterator[sqlite3.Connection]:
        path = Path(self.settings.local_books_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(path)) as db, db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS books "
                "(owner TEXT, id TEXT, name TEXT NOT NULL, PRIMARY KEY(owner, id))"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS book_covers "
                "(owner TEXT, book_id TEXT, page_id TEXT NOT NULL, PRIMARY KEY(owner, book_id))"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS book_counts "
                "(owner TEXT, book_id TEXT, reads INTEGER NOT NULL, PRIMARY KEY(owner, book_id))"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS book_completions "
                "(owner TEXT, book_id TEXT, completion_id TEXT, "
                "PRIMARY KEY(owner, book_id, completion_id))"
            )
            yield db

    def _cover(self, owner: str, book_id: str, page_id: str) -> BookCover | None:
        job = self.jobs.get(page_id)
        if not job or job.run_id != owner or job.book_id != book_id or not job.thumbnail:
            return None
        return BookCover(
            run_id=owner,
            page_id=page_id,
            thumbnail_url="data:image/jpeg;base64," + base64.b64encode(job.thumbnail).decode(),
        )

    def books(self, owner: str) -> Books:
        self.expire()
        with self.book_db() as db:
            covers = dict(
                db.execute("SELECT book_id, page_id FROM book_covers WHERE owner=?", (owner,))
            )
            counts = dict(
                db.execute("SELECT book_id, reads FROM book_counts WHERE owner=?", (owner,))
            )
            rows = db.execute("SELECT id, name FROM books WHERE owner=? ORDER BY id", (owner,))
            return Books(
                items=[
                    Book(
                        id=i,
                        name=n,
                        read_count=counts.get(i),
                        cover=self._cover(owner, i, covers[i]) if i in covers else None,
                    )
                    for i, n in [(EARLIER.id, EARLIER.name), *rows]
                ]
            )

    def set_cover(self, owner: str, book_id: str, run: str, page_id: str) -> Book:
        self.expire()
        cover = self._cover(owner, book_id, page_id) if run == owner else None
        if cover is None:
            raise LocalError("Cover page not found.", 404)
        with self.book_db() as db:
            name = EARLIER.name
            if book_id != EARLIER.id:
                row = db.execute(
                    "SELECT name FROM books WHERE owner=? AND id=?", (owner, book_id)
                ).fetchone()
                if row is None:
                    raise LocalError("Book not found.", 404)
                name = row[0]
            db.execute(
                "INSERT OR REPLACE INTO book_covers(owner, book_id, page_id) VALUES(?, ?, ?)",
                (owner, book_id, page_id),
            )
            count = db.execute(
                "SELECT reads FROM book_counts WHERE owner=? AND book_id=?", (owner, book_id)
            ).fetchone()
        return Book(id=book_id, name=name, cover=cover, read_count=count[0] if count else None)

    def complete_book(self, owner: str, book_id: str, completion_id: str) -> int:
        with self.book_db() as db:
            db.execute("BEGIN IMMEDIATE")
            if (
                book_id != EARLIER.id
                and not db.execute(
                    "SELECT 1 FROM books WHERE owner=? AND id=?", (owner, book_id)
                ).fetchone()
            ):
                raise LocalError("Book not found.", 404)
            if db.execute(
                "INSERT INTO book_completions VALUES (?,?,?) ON CONFLICT DO NOTHING",
                (owner, book_id, completion_id),
            ).rowcount:
                db.execute(
                    "INSERT INTO book_counts VALUES (?,?,1) "
                    "ON CONFLICT(owner,book_id) DO UPDATE SET reads=reads+1",
                    (owner, book_id),
                )
            return int(
                db.execute(
                    "SELECT reads FROM book_counts WHERE owner=? AND book_id=?", (owner, book_id)
                ).fetchone()[0]
            )

    def create_book(self, owner: str, name: str) -> Book:
        book = Book(id=str(uuid4()), name=name)
        with self.book_db() as db:
            db.execute(
                "INSERT INTO books(owner, id, name) VALUES(?, ?, ?)", (owner, book.id, book.name)
            )
        return book

    def rename_book(self, owner: str, book_id: str, name: str) -> None:
        if book_id == EARLIER.id:
            raise LocalError("Earlier captures cannot be renamed.", 409)
        with self.book_db() as db:
            if not db.execute(
                "UPDATE books SET name=? WHERE owner=? AND id=?", (name, owner, book_id)
            ).rowcount:
                raise LocalError("Book not found.", 404)

    def delete_book(self, owner: str, book_id: str) -> None:
        if book_id == EARLIER.id:
            raise LocalError("Earlier captures cannot be deleted as a book.", 409)
        with self.book_db() as db:
            if not db.execute(
                "DELETE FROM books WHERE owner=? AND id=?", (owner, book_id)
            ).rowcount:
                raise LocalError("Book not found.", 404)
            db.execute("DELETE FROM book_covers WHERE owner=? AND book_id=?", (owner, book_id))
            db.execute("DELETE FROM book_counts WHERE owner=? AND book_id=?", (owner, book_id))
            db.execute("DELETE FROM book_completions WHERE owner=? AND book_id=?", (owner, book_id))
            for job_id, job in list(self.jobs.items()):
                if job.run_id == owner and job.book_id == book_id:
                    self.remove(owner, job_id)

    def check_book(self, owner: str, book_id: str | None) -> None:
        if book_id is None or book_id == EARLIER.id:
            return
        with self.book_db() as db:
            if not db.execute(
                "SELECT 1 FROM books WHERE owner=? AND id=?", (owner, book_id)
            ).fetchone():
                raise LocalError("Book not found.", 404)

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

    def recent(
        self, run_id: str, cursor: str | None = None, book_id: str | None = None
    ) -> RecentCaptures:
        self.expire()
        self.check_book(run_id, book_id)
        before = math.inf
        last_id = ""
        if cursor is not None:
            try:
                value, _, last_id = cursor.partition(":")
                before = float(value)
                if not math.isfinite(before):
                    raise ValueError
            except ValueError as exc:
                raise LocalError("Invalid history cursor.") from exc
        jobs = sorted(
            (
                job
                for job in self.jobs.values()
                if job.run_id == run_id
                and (book_id is None or book_id == job.book_id)
                and (job.created, job.view.id) < (before, last_id)
                and job.view.state != CaptureState.CANCELLED
                and (book_id is not None or job.view.state == CaptureState.READY or job.edited)
            ),
            key=lambda job: (job.created, job.view.id),
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
                    state=None if job.view.state == CaptureState.READY else job.view.state,
                    retryable=job.view.retryable if job.view.state != CaptureState.READY else None,
                    reason=job.view.reason if job.view.state != CaptureState.READY else None,
                    audio_chunks=job.view.audio_chunks
                    if job.view.state == CaptureState.READY
                    else 0,
                    thumbnail_url=(
                        "data:image/jpeg;base64," + base64.b64encode(job.thumbnail).decode()
                        if job.thumbnail
                        else None
                    ),
                )
                for job in page
            ],
            next_cursor=f"{page[-1].created}:{page[-1].view.id}" if len(jobs) > len(page) else None,
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
            active = self.active.get(run_id)
            if active and active[0] is job:
                active[1].cancel()
            del self.jobs[job_id]

    def media_fits(self, additional: int) -> bool:
        stored = sum(
            len(job.image) + len(job.thumbnail) + sum(map(len, job.audio))
            for job in self.jobs.values()
        )
        return stored + additional <= LOCAL_MEDIA_LIMIT

    def check_capacity(self, run_id: str) -> None:
        pending = sum(
            job.run_id == run_id
            and job.view.state in (CaptureState.QUEUED, CaptureState.PROCESSING)
            for job in self.jobs.values()
        )
        limit = self.settings.limits.max_captures_ahead
        if pending >= limit:
            raise LocalError(f"Already processing or queuing {limit} pages. Try again later.", 409)

    def submit(
        self, run_id: str, job_id: str, image: bytes, mode: str, book_id: str | None = None
    ) -> ReadingJob:
        self.expire()
        self.check_book(run_id, book_id)
        assigned = EARLIER.id if book_id is None else book_id
        if (run_id, job_id) in self.cancelled:
            raise LocalError("This capture was discarded. Start a new capture.", 409)
        digest = hashlib.sha256(image + mode.encode()).hexdigest()
        existing = self.jobs.get(job_id)
        if existing:
            if (
                existing.run_id != run_id
                or existing.digest != digest
                or existing.book_id != assigned
            ):
                raise LocalError("Capture request ID was already used for different input.", 409)
            return existing.view
        self.check_capacity(run_id)
        if sum(job.run_id == run_id for job in self.jobs.values()) >= (
            self.settings.limits.max_pages_per_session
        ):
            raise LocalError("Session page limit reached. Discard unused pages to continue.", 429)
        image = validate_image(image, self.settings)
        if not self.media_fits(len(image)):
            raise LocalError("The local reader is full. Discard unused pages to continue.", 429)
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
            book_id=assigned,
        )
        self.jobs[job_id] = job
        self.start(job)
        return job.view

    def start(self, job: Job) -> None:
        job.view.state = CaptureState.QUEUED
        job.view.reason = None
        job.view.retryable = False
        self.dispatch(job.run_id)

    def dispatch(self, run_id: str) -> None:
        if run_id in self.active:
            return
        # Dict insertion order preserves original acceptance order, including retries.
        for job in self.jobs.values():
            if job.run_id == run_id and job.view.state == CaptureState.QUEUED:
                task = asyncio.create_task(self.process(job))
                self.active[run_id] = (job, task)
                task.add_done_callback(lambda done: self.finished(job, done))
                return

    def finished(self, job: Job, task: asyncio.Task[None]) -> None:
        self.active.pop(job.run_id)
        if not task.cancelled() and (error := task.exception()) is not None:
            job.view.state = CaptureState.FAILED
            job.view.reason = "An internal processing error occurred. Check server logs and retry."
            job.view.retryable = True
            logger.error("local_job_failed job=%s error_type=%s", job.view.id, type(error).__name__)
        self.dispatch(job.run_id)

    def edit_narration(self, run_id: str, job_id: str, narration: str) -> ReadingJob:
        job = self.get(run_id, job_id)
        if job.view.state != CaptureState.READY and not (
            job.edited and job.view.state == CaptureState.FAILED
        ):
            raise LocalError("Wait for this page to finish before editing its narration.", 409)
        self.check_capacity(run_id)
        job.edited = True
        job.audio.clear()
        job.view.audio_chunks = 0
        job.view.narration = narration
        job.view.stage = CaptureStage.SPEECH
        self.start(job)
        return job.view

    def retry(self, run_id: str, job_id: str) -> ReadingJob:
        job = self.get(run_id, job_id)
        if job.view.state != CaptureState.FAILED or not job.view.retryable:
            raise LocalError("This capture cannot be retried. Capture a new page.", 409)
        self.check_capacity(run_id)
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
                        thumbnail = capture_thumbnail(job.image)
                        if self.media_fits(len(thumbnail)):
                            job.thumbnail = thumbnail
                if job.vision.needs_retake:
                    job.image = b""
                    job.view.state = CaptureState.REJECTED
                    job.view.reason = job.vision.retake_reason
                    return
                if not job.edited:
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
                    if not self.media_fits(len(audio)):
                        raise ProviderError(
                            "The local reader is full. Discard unused pages and retry.",
                            retryable=True,
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
        self.jobs.clear()
        tasks = [task for _, task in self.active.values()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
