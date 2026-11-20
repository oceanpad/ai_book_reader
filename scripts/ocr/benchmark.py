"""Compare plain-text OCR on identical labeled images, one billable request at a time."""

import argparse
import base64
import json
import math
import statistics
import time
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path

import httpx
from PIL import Image

from backend.config import FileSettings
from backend.vision_images import encode_view, media_type, prepare_pixels
from scripts.evaluate_vision import Fixture, character_errors, fixtures

ENDPOINTS = {
    "openai": "https://api.openai.com/v1/chat/completions",
    "openrouter": "https://openrouter.ai/api/v1/chat/completions",
}
MODELS = [
    "openai:gpt-6-luna",
    "openai:gpt-6.1-terra",
    "openrouter:qwen/qwen3.8-flash",
    "openrouter:google/gemini-3.5-flash-lite",
]
# Standard OpenAI USD per million input, cached input, and output tokens.
RATES = {"gpt-6-luna": (0.10, 0.01, 0.50), "gpt-6.1-terra": (1.00, 0.05, 5.00)}
PROMPT = "Transcribe all visible text in reading order, in its original language. Only output text."


def labeled_images(folder: Path) -> list[Fixture]:
    cases = []
    for path in sorted(folder.iterdir()):
        if path.suffix.lower() not in {".png", ".jpg", ".jpeg"}:
            continue
        expected = path.with_suffix(".txt").read_text(encoding="utf-8").strip()
        if not expected:
            raise ValueError(f"Empty reference for {path.name}")
        with Image.open(path) as raw:
            cases.append(Fixture(path.name, encode_view(prepare_pixels(raw)), expected))
    if not cases:
        raise ValueError("No labeled PNG/JPEG images found")
    return cases


def summarize(rows: list[dict]) -> dict:
    ok = [r for r in rows if "errors" in r]
    times = sorted(r["seconds"] for r in ok)
    return {
        "attempts": len(rows),
        "completed": len(ok),
        "exact": sum(r["errors"] == 0 for r in ok),
        "cer": sum(r["errors"] for r in ok) / sum(r["characters"] for r in ok) if ok else None,
        "p50_seconds": statistics.median(times) if ok else None,
        "p95_seconds": times[math.ceil(len(times) * 0.95) - 1] if ok else None,
        "usd_per_completed_image": (
            statistics.mean(r["usd"] for r in ok)
            if ok and all(r["usd"] is not None for r in ok)
            else None
        ),
    }


def measure(client: httpx.Client, route: str, key: str, case: Fixture) -> dict:
    provider, model = route.split(":", 1)
    mime = media_type(case.image)
    url = f"data:{mime};base64," + base64.b64encode(case.image).decode("ascii")
    parts = [{"type": "text", "text": PROMPT}, {"type": "image_url", "image_url": {"url": url}}]
    body = {
        "model": model,
        "messages": [{"role": "user", "content": parts}],
        "max_completion_tokens": 4096,
    }
    if provider == "openai" and model in RATES:
        body["reasoning_effort"] = "none"
    row = {
        "route": route,
        "case": case.name,
        "image_bytes": len(case.image),
        "characters": len("".join(case.expected.split())),
    }
    start = time.perf_counter()
    try:
        response = client.post(
            ENDPOINTS[provider], headers={"Authorization": f"Bearer {key}"}, json=body
        )
        row["seconds"] = time.perf_counter() - start
        response.raise_for_status()
        data = response.json()
        choice = data["choices"][0]
        text = choice["message"]["content"]
        if choice["finish_reason"] != "stop" or not isinstance(text, str) or not text.strip():
            raise ValueError("Incomplete transcription")
        usage = data.get("usage") or {}
        cost = usage.get("cost") if provider == "openrouter" else None
        if (
            provider == "openai"
            and model in RATES
            and {"prompt_tokens", "completion_tokens"} <= usage.keys()
        ):
            rate, discount, out = RATES[model]
            cached = (usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0)
            cost = (
                (usage["prompt_tokens"] - cached) * rate
                + cached * discount
                + usage["completion_tokens"] * out
            ) / 1_000_000
        row.update(
            errors=character_errors(case.expected, text),
            usd=cost,
            usage=usage,
            resolved_model=data.get("model"),
        )
    except httpx.HTTPStatusError as exc:
        row["error"] = f"HTTP {exc.response.status_code}"
    except (httpx.RequestError, ValueError, IndexError, TypeError, KeyError) as exc:
        row["error"] = type(exc).__name__
    row.setdefault("seconds", time.perf_counter() - start)
    return row


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Authorize paid calls and image upload")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--font", type=Path, help="Render synthetic cases with a Chinese font")
    source.add_argument("--images", type=Path, help="Authorized PNG/JPEG folder with .txt labels")
    parser.add_argument("--models", nargs="+", default=MODELS, metavar="PROVIDER:MODEL")
    parser.add_argument("--limit", type=int, help="Max images per model")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--report", type=Path, default=Path(".local/ocr-benchmark/results.json"))
    args = parser.parse_args()
    if not args.live or args.repeat < 1 or (args.limit is not None and args.limit < 1):
        parser.error("Require --live and positive repeat/limit")
    settings = FileSettings()
    keys = {"openai": settings.openai_api_key, "openrouter": settings.openrouter_api_key}
    for route in args.models:
        provider, sep, model = route.partition(":")
        if not sep or not model or provider not in keys or not keys[provider].get_secret_value():
            parser.error(f"Invalid route or missing key: {provider}")
    if len(set(args.models)) != len(args.models):
        parser.error("Duplicate route")
    try:
        if args.images:
            cases = labeled_images(args.images)
        else:
            # Interleave sentences and angles so a short smoke run covers different text.
            corpus = [case for case in fixtures(args.font) if not case.retake]
            cases = [corpus[s * 6 + (a + s) % 6] for a in range(6) for s in range(4)]
        cases = cases[: args.limit]
        if not args.images:
            for case in cases:
                with Image.open(BytesIO(case.image)) as raw:
                    case.image = encode_view(prepare_pixels(raw))
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    args.report.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    started = datetime.now(UTC).isoformat()
    with httpx.Client(timeout=90) as client:
        for repeat in range(args.repeat):
            for i, case in enumerate(cases):
                offset = (i + repeat) % len(args.models)
                for route in args.models[offset:] + args.models[:offset]:
                    row = measure(client, route, keys[route.split(":")[0]].get_secret_value(), case)
                    rows.append(dict(row, repeat=repeat + 1))
                    print(route, case.name, row.get("error", "ok"), flush=True)
    summary = {m: summarize([r for r in rows if r["route"] == m]) for m in args.models}
    report = {"started_utc": started, "prompt": PROMPT, "summary": summary, "rows": rows}
    args.report.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(summary, indent=2), args.report)
    raise SystemExit(int(any("error" in r for r in rows)))


if __name__ == "__main__":
    main()
