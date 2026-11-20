"""Explicit paid smoke check using only generated, original book-page content."""

import argparse
import asyncio
import json
import time
from io import BytesIO
from pathlib import Path

import httpx
from PIL import Image, ImageDraw, ImageFont
from pydantic import ValidationError

from backend.config import FileSettings
from backend.providers import PROMPT_VERSION, ProviderError, Providers

EXPECTED_TEXT = "A small green boat. It floats on a pond."


def synthetic_spread() -> bytes:
    image = Image.new("RGB", (1600, 1100), "#c9bda5")
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle(
        (80, 50, 1520, 1050), radius=12, fill="#fffbed", outline="#777060", width=6
    )
    draw.line((800, 55, 800, 1045), fill="#bbb29e", width=10)
    font = ImageFont.load_default(size=45)
    draw.text((145, 200), "A small green boat.", fill="#151c18", font=font)
    draw.text((850, 200), "It floats on a pond.", fill="#151c18", font=font)
    draw.ellipse((180, 530, 680, 850), fill="#a9d7e5")
    draw.polygon([(280, 640), (580, 640), (515, 745), (345, 745)], fill="#4b8c53")
    output = BytesIO()
    image.save(output, format="JPEG", quality=92)
    return output.getvalue()


async def check(output: Path) -> int:
    settings = FileSettings()
    public = settings.public_config()
    if not public.recognition_available or not public.speech_available:
        print(public.setup_message)
        return 1
    output.mkdir(parents=True, exist_ok=True)
    image = synthetic_spread()
    (output / "synthetic-spread.jpg").write_bytes(image)
    measurements: dict[str, str | float | int | bool] = {
        "vision_model": settings.vision_model,
        "speech_model": settings.speech_model,
        "voice": settings.speech_voice,
        "prompt_version": PROMPT_VERSION,
        "image_bytes": len(image),
    }

    async def diagnostic(response: httpx.Response) -> None:
        if response.is_error:
            raw = await response.aread()
            message = raw.decode(errors="replace")
            for secret in (settings.openai_api_key, settings.openrouter_api_key):
                if value := secret.get_secret_value():
                    message = message.replace(value, "[redacted]")
            # Only this explicit synthetic-content CLI prints redacted provider diagnostics.
            print("SYNTHETIC CHECK PROVIDER DIAGNOSTIC:", message[:2000])

    async with httpx.AsyncClient(event_hooks={"response": [diagnostic]}) as client:
        provider = Providers(settings, client)
        try:
            start = time.monotonic()
            vision = await provider.recognize(image, "spread")
            measurements["vision_seconds"] = round(time.monotonic() - start, 3)
            faithful = (
                vision.text_state == "readable"
                and " ".join(vision.ocr_text.split()) == EXPECTED_TEXT
            )
            measurements["exact_synthetic_text"] = faithful
            if not faithful:
                raise ProviderError(
                    "Synthetic vision output did not match the printed words exactly."
                )
            start = time.monotonic()
            audio = await provider.speak(vision.ocr_text)
            measurements["speech_seconds"] = round(time.monotonic() - start, 3)
            measurements["audio_bytes"] = len(audio)
            (output / "synthetic-narration.wav").write_bytes(audio)
            measurements["passed"] = True
        except ProviderError as exc:
            measurements["passed"] = False
            measurements["error"] = str(exc)
            if isinstance(exc.__cause__, ValidationError):
                measurements["validation_errors"] = json.dumps(
                    [
                        {"field": error["loc"], "type": error["type"], "message": error["msg"]}
                        for error in exc.__cause__.errors(include_input=False)
                    ]
                )
    (output / "result.json").write_text(json.dumps(measurements, indent=2) + "\n")
    print(json.dumps(measurements, indent=2))
    return 0 if measurements["passed"] else 1


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Allow two billable provider calls")
    args = parser.parse_args()
    if not args.live:
        parser.error("Pass --live to explicitly allow billable calls with synthetic content.")
    raise SystemExit(asyncio.run(check(Path(".local/provider-smoke"))))


if __name__ == "__main__":
    main()
