"""Safe image validation and deterministic derivative generation."""

from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO

from PIL import Image, ImageOps, UnidentifiedImageError

Image.MAX_IMAGE_PIXELS = 80_000_000


class MediaValidationError(ValueError):
    pass


@dataclass(frozen=True)
class ProcessedImage:
    original: bytes
    original_content_type: str
    original_extension: str
    width: int
    height: int
    variants: dict[str, tuple[bytes, str, str, int, int]]


def _encode_original(image: Image.Image, source_format: str) -> tuple[bytes, str, str]:
    output = BytesIO()
    has_alpha = image.mode in {"RGBA", "LA"} or (
        image.mode == "P" and "transparency" in image.info
    )
    if has_alpha or source_format == "PNG":
        image.convert("RGBA").save(output, format="PNG", optimize=True)
        return output.getvalue(), "image/png", ".png"
    image.convert("RGB").save(output, format="JPEG", quality=92, optimize=True, progressive=True)
    return output.getvalue(), "image/jpeg", ".jpg"


def _encode_variant(image: Image.Image, target_width: int) -> tuple[bytes, str, str, int, int]:
    copy = image.copy()
    copy.thumbnail((target_width, target_width * 4), Image.Resampling.LANCZOS)
    output = BytesIO()
    if copy.mode not in {"RGB", "RGBA"}:
        copy = copy.convert("RGBA" if "transparency" in copy.info else "RGB")
    copy.save(output, format="WEBP", quality=84, method=6)
    return output.getvalue(), "image/webp", ".webp", copy.width, copy.height


def process_image_variants(content: bytes) -> ProcessedImage:
    if not content:
        raise MediaValidationError("Image is empty")
    try:
        with Image.open(BytesIO(content)) as probe:
            probe.verify()
        with Image.open(BytesIO(content)) as source:
            source_format = (source.format or "").upper()
            if source_format not in {"JPEG", "PNG", "WEBP", "GIF"}:
                raise MediaValidationError("Unsupported image format")
            source.seek(0)
            image = ImageOps.exif_transpose(source).copy()
    except (UnidentifiedImageError, OSError, SyntaxError, Image.DecompressionBombError) as exc:
        raise MediaValidationError("Invalid or corrupted image") from exc
    if image.width < 64 or image.height < 64:
        raise MediaValidationError("Image dimensions are too small")
    if image.width * image.height > 80_000_000:
        raise MediaValidationError("Image dimensions are too large")
    original, original_type, original_ext = _encode_original(image, source_format)
    variants: dict[str, tuple[bytes, str, str, int, int]] = {}
    for name, width in (
        ("thumbnail", 320),
        ("preview", 960),
        ("fullscreen", 1600),
        ("adminReview", 2000),
    ):
        variants[name] = _encode_variant(image, min(width, image.width))
    return ProcessedImage(
        original=original,
        original_content_type=original_type,
        original_extension=original_ext,
        width=image.width,
        height=image.height,
        variants=variants,
    )
