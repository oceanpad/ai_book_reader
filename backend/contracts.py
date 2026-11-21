from enum import StrEnum
from typing import Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

VISION_PROMPT_VERSION = "reader-vision-v3"


class Contract(BaseModel):
    model_config = ConfigDict(
        extra="forbid", str_strip_whitespace=True, json_schema_serialization_defaults_required=True
    )


class CaptureState(StrEnum):
    UPLOADING = "UPLOADING"
    QUEUED = "QUEUED"
    PROCESSING = "PROCESSING"
    READY = "READY"
    DUPLICATE = "DUPLICATE"
    REJECTED = "REJECTED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class CaptureStage(StrEnum):
    VALIDATE = "validate"
    VISION = "vision"
    DEDUPLICATE = "deduplicate"
    SPEECH = "speech"


class SessionState(StrEnum):
    ACTIVE = "ACTIVE"
    STOPPED = "STOPPED"
    DELETING = "DELETING"


class AudioState(StrEnum):
    PENDING = "AUDIO_PENDING"
    READY = "READY"
    FAILED = "AUDIO_FAILED"


class CameraConfig(Contract):
    samples_per_second: int = Field(default=4, ge=1, le=10)
    stable_window_ms: int = Field(default=1000, ge=250, le=5000)
    motion_threshold: float = Field(default=0.025, gt=0, lt=1)
    scene_change_threshold: float = Field(default=0.12, gt=0, lt=1)
    sharpness_threshold: float = Field(default=100, gt=0)
    min_crop_width: int = Field(default=640, ge=320)
    min_crop_height: int = Field(default=480, ge=240)
    max_longest_edge: int = Field(default=2560, ge=640, le=4096)
    max_upload_bytes: int = Field(default=4 * 1024 * 1024, ge=65536, le=4 * 1024 * 1024)

    @model_validator(mode="after")
    def coherent_thresholds(self) -> Self:
        if self.scene_change_threshold <= self.motion_threshold:
            raise ValueError("Scene-change threshold must exceed the motion threshold")
        if max(self.min_crop_width, self.min_crop_height) > self.max_longest_edge:
            raise ValueError("Minimum crop dimensions exceed the longest-edge limit")
        return self


class Limits(Contract):
    max_captures_ahead: int = Field(default=10, ge=1, le=10)
    max_pages_per_session: int = Field(default=100, ge=1, le=100)
    capture_attempts_per_day: int = Field(default=200, ge=1, le=200)
    speech_attempts_per_day: int = Field(default=200, ge=1, le=200)
    stage_attempts: int = Field(default=3, ge=1, le=3)


class PublicConfig(Contract):
    mode: Literal["local", "aws"]
    camera: CameraConfig
    limits: Limits
    recognition_available: bool = False
    speech_available: bool = False
    setup_message: str | None = None


class Health(Contract):
    ok: Literal[True] = True
    service: Literal["ai-book-reader"] = "ai-book-reader"


class VisionResult(Contract):
    is_book_page: bool = Field(strict=True)
    text_state: Literal["readable", "absent", "unreadable"]
    ocr_text: str = Field(max_length=20000)
    illustration_description: str = Field(max_length=1000)
    language: str = Field(min_length=2, max_length=35)
    needs_retake: bool = Field(strict=True)
    retake_reason: str | None = Field(max_length=500)

    @model_validator(mode="after")
    def validate_result(self) -> Self:
        if self.needs_retake != bool(self.retake_reason):
            raise ValueError("A retake requires a nonempty reason, and vice versa")
        if not self.is_book_page or self.text_state == "unreadable":
            if not self.needs_retake or self.ocr_text or self.illustration_description:
                raise ValueError("Rejected input requires a retake reason and no narration")
        elif self.needs_retake:
            raise ValueError("Readable/absent book text cannot require a retake")
        elif self.text_state == "readable":
            if not self.ocr_text or self.illustration_description:
                raise ValueError("Readable text requires exact OCR and no description")
        elif self.ocr_text or not self.illustration_description:
            raise ValueError("Absent text requires a description and no OCR")
        return self


class ReadingJob(Contract):
    id: str
    state: CaptureState
    stage: CaptureStage
    narration: str = ""
    language: str = ""
    reason: str | None = None
    retryable: bool = False
    audio_chunks: int = 0
    vision_model: str
    speech_model: str
    voice: str
    prompt_version: str = VISION_PROMPT_VERSION


class NarrationEdit(Contract):
    narration: str = Field(strict=True, min_length=1, max_length=20000)


class BookCreate(Contract):
    name: str = Field(min_length=1, max_length=200)


class BookCompletion(Contract):
    completion_id: UUID


class ReadCount(Contract):
    read_count: int


class BookCoverRequest(Contract):
    run_id: UUID
    page_id: UUID


class BookCover(Contract):
    run_id: str
    page_id: str
    thumbnail_url: str


class Book(BookCreate):
    id: str
    created_at: int | None = Field(
        default=None,
        description="Book creation time in Unix milliseconds, estimated from the earliest saved "
        "page for legacy books", 
    )
    cover: BookCover | None = None
    read_count: int | None = None


class Books(Contract):
    items: list[Book]


EARLIER = Book(id="earlier", name="Earlier captures")
RECENT_CAPTURE_LIMIT = 10


class PlayableCapture(Contract):
    id: str
    run_id: str
    created_at: int = Field(description="Capture creation time in Unix seconds")
    narration: str
    language: str
    audio_chunks: int = Field(ge=0, description="Zero while speech is unavailable")
    state: CaptureState | None = Field(
        default=None, exclude_if=lambda value: value is None, description="Omitted for READY pages"
    )
    retryable: bool | None = Field(default=None, exclude_if=lambda value: value is None)
    reason: str | None = Field(default=None, exclude_if=lambda value: value is None)
    thumbnail_url: str | None = None


class RecentCaptures(Contract):
    items: list[PlayableCapture] = Field(max_length=RECENT_CAPTURE_LIMIT)
    next_cursor: str | None = None


class ReaderContracts(Contract):
    recent_captures: RecentCaptures
    public_config: PublicConfig
    health: Health
    capture_state: CaptureState
    capture_stage: CaptureStage
    session_state: SessionState
    audio_state: AudioState
    vision_result: VisionResult
    reading_job: ReadingJob
