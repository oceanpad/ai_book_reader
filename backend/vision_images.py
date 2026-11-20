from io import BytesIO

from PIL import Image, ImageOps

MAX_IMAGE_BYTES = 500_000
MAX_LONG_EDGE = 1600


def prepare_pixels(source: Image.Image) -> Image.Image:
    image = ImageOps.exif_transpose(source)
    image.thumbnail((MAX_LONG_EDGE, MAX_LONG_EDGE), Image.Resampling.LANCZOS)
    image = image.convert("RGB")
    image.info.clear()
    return image


def encode_view(image: Image.Image) -> bytes:
    # Keep small text fixtures lossless when PNG fits the same budget.
    if image.width * image.height <= 512 * 512:
        output = BytesIO()
        image.save(output, "PNG")
        if output.tell() < MAX_IMAGE_BYTES:
            return output.getvalue()
    while True:
        for quality in (90, 80, 70):
            output = BytesIO()
            image.save(output, "JPEG", quality=quality, subsampling=0, optimize=True)
            if output.tell() < MAX_IMAGE_BYTES:
                return output.getvalue()
        image = image.resize(
            (max(1, image.width * 4 // 5), max(1, image.height * 4 // 5)), Image.Resampling.LANCZOS
        )


def capture_thumbnail(image: bytes) -> bytes:
    with Image.open(BytesIO(image)) as source:
        preview = ImageOps.exif_transpose(source)
        preview.thumbnail((256, 256), Image.Resampling.LANCZOS)
        preview = preview.convert("RGB")
        preview.info.clear()
        output = BytesIO()
        preview.save(output, "JPEG", quality=76)
        return output.getvalue()


def media_type(data: bytes) -> str:
    return "image/png" if data.startswith(b"\x89PNG") else "image/jpeg"
