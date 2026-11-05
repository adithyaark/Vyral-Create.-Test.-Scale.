"""Locate the external programs and model files Vyral depends on."""
from __future__ import annotations

import glob
import os
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MODELS = ROOT / "models"
# fp16 measured about 1.5x faster than int8 on a laptop CPU, so it's preferred when present.
KOKORO_MODEL = next(
    (path for path in (MODELS / "kokoro-v1.0.fp16.onnx", MODELS / "kokoro-v1.0.int8.onnx") if path.is_file()),
    MODELS / "kokoro-v1.0.fp16.onnx",
)
KOKORO_VOICES = MODELS / "voices-v1.0.bin"


def find_program(name: str) -> str | None:
    """Find a program on PATH, then in the folders installers commonly use.

    The extra folders matter because a shell opened before an install keeps a
    stale PATH, which would otherwise make a working install look missing.
    """
    found = shutil.which(name)
    if found:
        return found
    local = os.environ.get("LOCALAPPDATA", "")
    home = Path.home()
    candidates = [
        str(home / ".local" / "bin" / f"{name}.exe"),
        *glob.glob(os.path.join(local, "Microsoft", "WinGet", "Packages", "*", "*", "bin", f"{name}.exe")),
    ]
    return next((path for path in candidates if os.path.isfile(path)), None)


def status() -> dict[str, bool]:
    return {
        "ffmpeg": find_program("ffmpeg") is not None,
        "ffprobe": find_program("ffprobe") is not None,
        "claude": find_program("claude") is not None,
        "voice_model": KOKORO_MODEL.is_file() and KOKORO_VOICES.is_file(),
    }
