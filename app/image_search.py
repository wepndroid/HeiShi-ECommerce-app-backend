"""Visual similarity helpers for catalog image search (average hash + Hamming distance)."""

from __future__ import annotations

import io
from functools import lru_cache

import httpx
from PIL import Image, UnidentifiedImageError

_HASH_SIZE = 8
_MAX_HAMMING = 24


def average_hash(image: Image.Image, size: int = _HASH_SIZE) -> str:
    gray = image.convert("L").resize((size, size), Image.Resampling.LANCZOS)
    pixels = list(gray.getdata())
    avg = sum(pixels) / len(pixels)
    return "".join("1" if px >= avg else "0" for px in pixels)


def hamming_distance(left: str, right: str) -> int:
    return sum(a != b for a, b in zip(left, right))


def hash_image_bytes(data: bytes) -> str:
    with Image.open(io.BytesIO(data)) as img:
        return average_hash(img)


@lru_cache(maxsize=256)
def hash_image_url(url: str) -> str | None:
    try:
        with httpx.Client(timeout=10.0, follow_redirects=True) as client:
            response = client.get(url)
            response.raise_for_status()
        return hash_image_bytes(response.content)
    except (httpx.HTTPError, UnidentifiedImageError, OSError, ValueError):
        return None


def is_similar_enough(distance: int) -> bool:
    return distance <= _MAX_HAMMING