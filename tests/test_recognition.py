import base64
from io import BytesIO

from PIL import Image, ImageDraw

from backend.providers import recognition_views


def test_recognition_contains_every_upright_candidate_without_losing_pixels():
    source = Image.new("RGB", (120, 80), "white")
    ImageDraw.Draw(source).rectangle((0, 0, 30, 20), fill="blue")
    image = BytesIO()
    source.save(image, "JPEG", quality=95)
    original = Image.open(BytesIO(image.getvalue()))
    original.load()
    views = recognition_views(image.getvalue())
    images = [view["image_url"] for view in views if view["type"] == "image_url"]
    assert len(images) == 4
    for image_view, rotation in zip(
        images,
        (None, Image.Transpose.ROTATE_90, Image.Transpose.ROTATE_180, Image.Transpose.ROTATE_270),
        strict=True,
    ):
        prefix, data = image_view["url"].split(",")
        assert image_view["detail"] == "high"
        with Image.open(BytesIO(base64.b64decode(data))) as decoded:
            expected = original.transpose(rotation) if rotation is not None else original
            assert decoded.size == expected.size
            assert decoded.tobytes() == expected.tobytes()
            if rotation is not None:
                assert prefix == "data:image/png;base64"


def test_noisy_capture_fits_budget_in_every_provider_view():
    source = Image.effect_noise((2400, 1800), 95).convert("RGB")
    raw = BytesIO()
    source.save(raw, "PNG")
    assert len(raw.getvalue()) > 500_000
    views = [v for v in recognition_views(raw.getvalue()) if v["type"] == "image_url"]
    assert len(views) == 4
    for view in views:
        prefix, payload = view["image_url"]["url"].split(",")
        data = base64.b64decode(payload)
        assert len(data) < 500_000
        with Image.open(BytesIO(data)) as image:
            image.load()
            assert prefix == f"data:image/{image.format.lower()};base64"
            assert max(image.size) <= 1600
