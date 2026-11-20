"""Billable OCR comparison using original synthetic text, never user book photographs."""

import argparse
import asyncio
import json
import time
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

import httpx
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont

from backend.config import FileSettings
from backend.providers import PROMPT_VERSION, ProviderError, Providers


@dataclass
class Fixture:
    name: str
    image: bytes
    expected: str
    retake: bool = False


def fixtures(font_path: Path) -> list[Fixture]:
    examples = [
        "小熊把蓝色雨伞放在门口。",
        "河边有七只小鸭，树下有两只白兔。",
        "妹妹没有拿走哥哥的红帽子。",
        "今天的月亮像一艘弯弯的小船。",
    ]
    cases: list[Fixture] = []
    for index, text in enumerate(examples):
        for angle, brightness, size, blur in [
            (0, 1.0, 30, 0),
            (90, 0.55, 24, 0.35),
            (180, 0.8, 22, 0),
            (270, 0.55, 24, 0.35),
            (85, 0.55, 22, 0.45),
            (275, 0.55, 22, 0.45),
        ]:
            canvas = Image.new("RGB", (1728, 810), "#d4cab6")
            draw = ImageDraw.Draw(canvas)
            draw.rounded_rectangle(
                (210, 60, 1490, 760), radius=8, fill="#edce6a", outline="#847045", width=4
            )
            draw.text(
                (270, 100), text, font=ImageFont.truetype(str(font_path), size), fill="#423b22"
            )
            draw.ellipse((500, 240, 900, 600), fill="#79a65b")
            draw.polygon([(1000, 400), (1300, 400), (1250, 550), (1050, 550)], fill="#4d80a3")
            canvas = ImageEnhance.Brightness(canvas).enhance(brightness)
            canvas = canvas.filter(ImageFilter.GaussianBlur(blur)).rotate(angle, expand=True)
            output = BytesIO()
            canvas.save(output, "JPEG", quality=92)
            cases.append(Fixture(f"zh-{index}-rotate-{angle}", output.getvalue(), text))

    # Unrecoverable pixels still visibly contain text, so describing them as text-absent is wrong.
    unreadable = Image.new("RGB", (1200, 800), "#fff3cd")
    draw = ImageDraw.Draw(unreadable)
    draw.text((100, 100), examples[0], font=ImageFont.truetype(str(font_path), 30), fill="black")
    unreadable = unreadable.filter(ImageFilter.GaussianBlur(14))
    output = BytesIO()
    unreadable.save(output, "JPEG", quality=92)
    cases.append(Fixture("zh-unreadable-blur", output.getvalue(), "", retake=True))
    return cases


def character_errors(expected: str, actual: str) -> int:
    expected = "".join(expected.split())
    actual = "".join(actual.split())
    row = list(range(len(actual) + 1))
    for index, left in enumerate(expected, start=1):
        next_row = [index]
        for column, right in enumerate(actual, start=1):
            next_row.append(
                min(next_row[-1] + 1, row[column] + 1, row[column - 1] + (left != right))
            )
        row = next_row
    return row[-1]


async def evaluate(args: argparse.Namespace) -> int:
    settings = FileSettings()
    cases = fixtures(args.font)
    if args.limit:
        cases = cases[: args.limit]
    output = Path(".local/vision-evaluation")
    output.mkdir(parents=True, exist_ok=True)
    for fixture in cases:
        (output / (fixture.name + ".jpg")).write_bytes(fixture.image)
    results: list[dict[str, str | bool | float | int]] = []
    semaphore = asyncio.Semaphore(2)
    async with httpx.AsyncClient() as client:

        async def run(model: str, fixture: Fixture) -> None:
            async with semaphore:
                provider = Providers(settings.model_copy(update={"vision_model": model}), client)
                start = time.monotonic()
                row: dict[str, str | bool | float | int] = {
                    "model": model,
                    "case": fixture.name,
                    "expected": fixture.expected,
                    "prompt": PROMPT_VERSION,
                }
                try:
                    result = await provider.recognize(fixture.image, "single")
                    errors = character_errors(fixture.expected, result.ocr_text)
                    row.update(
                        actual=result.ocr_text,
                        retake=result.needs_retake,
                        character_errors=errors,
                        passed=(
                            result.needs_retake
                            if fixture.retake
                            else result.text_state == "readable"
                            and not result.needs_retake
                            and errors == 0
                        ),
                    )
                except ProviderError as exc:
                    row.update(passed=False, error=str(exc))
                row["seconds"] = round(time.monotonic() - start, 3)
                results.append(row)
                print(json.dumps(row, ensure_ascii=False), flush=True)

        await asyncio.gather(*(run(model, case) for model in args.models for case in cases))
    target = output / args.report
    target.write_text(json.dumps(results, indent=2, ensure_ascii=False) + "\n")
    for model in args.models:
        rows = [row for row in results if row["model"] == model]
        print(
            model,
            "exact/retake passes:",
            sum(row["passed"] is True for row in rows),
            "/",
            len(rows),
        )
    return 0 if all(row["passed"] for row in results) else 1


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Allow billable vision calls")
    parser.add_argument("--font", type=Path, required=True, help="Local font with Chinese glyphs")
    parser.add_argument("--models", nargs="+", default=[FileSettings().vision_model])
    parser.add_argument("--limit", type=int, default=0, help="Use only the first N fixtures")
    parser.add_argument("--report", default="results.json")
    args = parser.parse_args()
    if not args.live:
        parser.error("Pass --live to allow billable synthetic vision calls.")
    if not args.font.is_file():
        parser.error("Font must be an existing local file containing Chinese glyphs.")
    if args.limit < 0:
        parser.error("Fixture limit must be nonnegative.")
    if Path(args.report).name != args.report:
        parser.error("Report must be a filename, not a path.")
    raise SystemExit(asyncio.run(evaluate(args)))


if __name__ == "__main__":
    main()
