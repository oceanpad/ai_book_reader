import httpx
import pytest

from scripts.evaluate_vision import Fixture
from scripts.ocr.benchmark import measure, summarize


def test_comparison_keeps_provider_failures_separate_from_accuracy():
    case = Fixture("rotated", b"pixels", "小熊")
    answer = {
        "choices": [{"message": {"content": "小 猫"}, "finish_reason": "stop"}],
        "usage": {
            "prompt_tokens": 100,
            "completion_tokens": 10,
            "prompt_tokens_details": {"cached_tokens": 50},
        },
    }
    with httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=answer))
    ) as client:
        valid = measure(client, "openai:gpt-6-luna", "test", case)
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(429))) as client:
        failed = measure(client, "openrouter:qwen/qwen3.8-flash", "test", case)
    metrics = summarize([valid, failed])
    assert failed["error"] == "HTTP 429"
    assert (metrics["attempts"], metrics["completed"], metrics["exact"]) == (2, 1, 0)
    assert metrics["cer"] == 0.5
    assert metrics["usd_per_completed_image"] == pytest.approx(0.0000105)
    assert summarize([failed])["p50_seconds"] is None
