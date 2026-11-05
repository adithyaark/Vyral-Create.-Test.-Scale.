"""Clean up uploaded product photos and cut products out of their backgrounds."""
from __future__ import annotations

import threading
from pathlib import Path

import numpy as np
from PIL import Image, ImageOps, UnidentifiedImageError

MAX_SIDE = 2400
THUMB_SIDE = 768
MIN_COVERAGE, MAX_COVERAGE = 0.03, 0.95
CUTOUT_MODEL = "isnet-general-use"

_lock = threading.Lock()
_session = None


class PhotoError(ValueError):
    """A photo problem with a message that is safe to show the user."""


def ingest(source: Path, target: Path) -> tuple[int, int]:
    """Save an upload as an upright, size-limited JPEG and return its size."""
    import pillow_heif

    # Registers iPhone HEIC support with Pillow.
    pillow_heif.register_heif_opener()
    try:
        with Image.open(source) as image:
            image = ImageOps.exif_transpose(image)
            image.thumbnail((MAX_SIDE, MAX_SIDE), Image.Resampling.LANCZOS)
            if image.mode in ("RGBA", "LA", "P"):
                # A transparent PNG keeps its shape when placed on white.
                rgba = image.convert("RGBA")
                flat = Image.new("RGB", rgba.size, (255, 255, 255))
                flat.paste(rgba, mask=rgba.getchannel("A"))
                image = flat
            image.convert("RGB").save(target, "JPEG", quality=93)
            return image.size
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError) as exc:
        raise PhotoError("That file could not be read as a photo.") from exc


def make_thumbnail(source: Path, target: Path) -> None:
    with Image.open(source) as image:
        image.thumbnail((THUMB_SIDE, THUMB_SIDE), Image.Resampling.LANCZOS)
        image.convert("RGB").save(target, "JPEG", quality=85)


def cutout_is_usable(alpha: np.ndarray) -> bool:
    """Reject masks that removed nearly everything or almost nothing."""
    coverage = float((alpha > 128).mean())
    return MIN_COVERAGE <= coverage <= MAX_COVERAGE


def _tighten_alpha(value: int) -> int:
    low, high = 90, 210
    return max(0, min(255, round((value - low) * 255 / (high - low))))


def remove_specks(alpha: Image.Image) -> Image.Image:
    """Erase small islands that are separate from the main product."""
    from scipy import ndimage

    values = np.asarray(alpha)
    labels, count = ndimage.label(values > 32)
    if count <= 1:
        return alpha
    sizes = ndimage.sum_labels(np.ones_like(labels), labels, index=range(1, count + 1))
    keep = np.flatnonzero(sizes >= sizes.max() * 0.05) + 1
    return Image.fromarray(np.where(np.isin(labels, keep), values, 0).astype(np.uint8), "L")


def make_cutout(source: Path, target: Path) -> bool:
    """Create a transparent product image. Returns False if the result is unusable."""
    global _session
    from rembg import new_session, remove

    with _lock:
        if _session is None:
            _session = new_session(CUTOUT_MODEL)
        with Image.open(source) as image:
            result = remove(image.convert("RGB"), session=_session)
    # Removes the faint leftover patches that the model leaves around the product.
    result.putalpha(remove_specks(result.getchannel("A").point(_tighten_alpha)))
    if not cutout_is_usable(np.asarray(result.getchannel("A"))):
        return False
    box = result.getchannel("A").point(lambda value: 255 if value > 16 else 0).getbbox()
    if not box:
        return False
    result.crop(box).save(target, "PNG")
    return True


def get_cutout(photo_path: Path) -> Path | None:
    """Return a cached cutout, creating it on first use. None means use the full photo."""
    cutout = photo_path.with_name(f"{photo_path.stem}_cutout.png")
    failed = photo_path.with_name(f"{photo_path.stem}_cutout.failed")
    if cutout.is_file():
        return cutout
    if failed.is_file():
        return None
    if make_cutout(photo_path, cutout):
        return cutout
    failed.touch()
    return None


def dominant_color(photo_path: Path) -> tuple[int, int, int]:
    """Choose a theme color from the photo's most common saturated color."""
    with Image.open(photo_path) as image:
        small = image.convert("RGB").resize((80, 80), Image.Resampling.BILINEAR)
    pixels = np.asarray(small.quantize(16).convert("RGB")).reshape(-1, 3).astype(float)
    peak, low = pixels.max(axis=1), pixels.min(axis=1)
    usable = pixels[(peak - low > 60) & (peak > 60) & (peak < 245)]
    if len(usable) < 40:
        return (58, 52, 92)  # Photos without strong color get a neutral indigo.
    values, counts = np.unique(usable, axis=0, return_counts=True)
    red, green, blue = values[counts.argmax()]
    return int(red), int(green), int(blue)
