import pytest
from pydantic import ValidationError

from backend.contracts import CameraConfig, Limits, VisionResult


def result(**overrides):
    return VisionResult.model_validate(
        {
            "is_book_page": True,
            "text_state": "readable",
            "ocr_text": "A small red boat.",
            "illustration_description": "",
            "language": "en",
            "needs_retake": False,
            "retake_reason": None,
            **overrides,
        }
    )


def test_recognition_preserves_printed_words() -> None:
    assert result().ocr_text == "A small red boat."
    assert result(text_state="absent", ocr_text="", illustration_description="A red boat.")
    assert result(
        text_state="unreadable", ocr_text="", needs_retake=True, retake_reason="Text is blurred."
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {"ocr_text": "   "},
        {"text_state": "absent"},
        {"text_state": "unreadable", "needs_retake": True, "retake_reason": "Blurred"},
        {"is_book_page": False},
        {"needs_retake": True},
        {"retake_reason": "Move closer"},
        {"illustration_description": "Invented extra plot"},
        {"is_book_page": "true"},
        {"ocr_text": "x" * 20001},
        {"unexpected": "field"},
    ],
)
def test_invalid_or_unsafe_narration_combinations_are_rejected(overrides) -> None:
    with pytest.raises(ValidationError):
        result(**overrides)


def test_configuration_enforces_capture_bound_and_coherent_thresholds() -> None:
    with pytest.raises(ValidationError):
        Limits(max_captures_ahead=4)
    with pytest.raises(ValidationError):
        CameraConfig(scene_change_threshold=0.01)
