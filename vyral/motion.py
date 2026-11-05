"""Render animated photo scenes directly into a captioned vertical video."""
from __future__ import annotations

import math
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

from . import photos
from .render import (
    FPS, HEIGHT, NO_WINDOW, TAIL_SECONDS, WIDTH, RenderError, build_ass, probe_duration, with_best_encoder,
)
from .tools import find_program
from .voice import Word

PRODUCT_BOX = (880, 820)
PRODUCT_CENTER_Y = 890
FIT_BOX = (960, 860)
HEADLINE_TOP = 215
HEADLINE_MAX_WIDTH = 940
ENTRANCE_SECONDS = 0.28
FONT_PATHS = [
    r"C:\Windows\Fonts\seguibl.ttf",
    r"C:\Windows\Fonts\arialbd.ttf",
]


@dataclass
class TimedScene:
    scene: dict
    start: float
    end: float


def scene_times(scenes: list[dict], words: list[Word], total: float) -> list[TimedScene]:
    """Start each scene when its first word is spoken."""
    timed = []
    index = 0
    previous = 0.0
    for number, scene in enumerate(scenes):
        start = 0.0 if number == 0 else max(words[min(index, len(words) - 1)].start, previous)
        timed.append(TimedScene(scene, start, total))
        if number:
            timed[number - 1].end = start
        previous = start
        index += len(scene["text"].split())
    return timed


def ease_out(value: float) -> float:
    return 1 - (1 - value) ** 3


def ease_in_out(value: float) -> float:
    return value * value * (3 - 2 * value)


def pop_in(value: float) -> float:
    """Scale from small to slightly oversized, then settle."""
    overshoot = 1.70158
    value -= 1
    return 1 + (overshoot + 1) * value ** 3 + overshoot * value ** 2


def motion_values(motion: str, progress: float, elapsed: float) -> tuple[float, float, float]:
    """Return scale, x offset and y offset for a moment within a scene."""
    smooth = ease_in_out(min(max(progress, 0.0), 1.0))
    if motion == "zoom_in":
        return 1.0 + 0.10 * smooth, 0.0, 0.0
    if motion == "zoom_out":
        return 1.10 - 0.10 * smooth, 0.0, 0.0
    if motion == "pan_left":
        return 1.08, 40 - 80 * smooth, 0.0
    if motion == "pan_right":
        return 1.08, -40 + 80 * smooth, 0.0
    if motion == "float":
        return 1.0 + 0.03 * smooth, 0.0, 14 * math.sin(elapsed * 1.8)
    # "pop"
    entrance = min(elapsed / ENTRANCE_SECONDS, 1.0)
    return (0.8 + 0.2 * pop_in(entrance)) * (1.0 + 0.04 * smooth), 0.0, 0.0


def _font(size: int) -> ImageFont.FreeTypeFont:
    for path in FONT_PATHS:
        if Path(path).is_file():
            return ImageFont.truetype(path, size)
    raise RenderError("No suitable headline font was found on this computer.")


def _luminance(color: tuple[int, int, int]) -> float:
    channels = [(value / 255) for value in color]
    linear = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def readable_ink(color: tuple[int, int, int]) -> tuple[int, int, int]:
    """Choose black or white text, whichever contrasts more with the color."""
    light = 1.05 / (_luminance(color) + 0.05)
    dark = (_luminance(color) + 0.05) / 0.05
    return (255, 255, 255) if light >= dark else (15, 15, 20)


def wrap_text(text: str, font: ImageFont.FreeTypeFont, width: int) -> list[str]:
    lines: list[str] = []
    current = ""
    for word in text.split():
        trial = f"{current} {word}".strip()
        if font.getlength(trial) <= width or not current:
            current = trial
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def build_headline(text: str, color: tuple[int, int, int]) -> Image.Image | None:
    """Draw the headline on a colored panel, shrinking it to fit two lines."""
    if not text:
        return None
    padding = 34
    font = _font(50)
    lines = [text]
    for size in (88, 78, 68, 60, 50):
        font = _font(size)
        lines = wrap_text(text, font, HEADLINE_MAX_WIDTH - padding * 2)
        if len(lines) <= 2 and all(font.getlength(line) <= HEADLINE_MAX_WIDTH - padding * 2 for line in lines):
            break
    ascent, descent = font.getmetrics()
    line_height = ascent + descent
    width = int(max(font.getlength(line) for line in lines)) + padding * 2
    height = line_height * len(lines) + padding
    panel = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(panel)
    draw.rounded_rectangle((0, 0, width - 1, height - 1), radius=22, fill=(*color, 255))
    ink = readable_ink(color)
    y = padding // 2
    for line in lines:
        draw.text((width / 2, y), line, font=font, fill=(*ink, 255), anchor="ma")
        y += line_height
    return panel


def gradient_background(color: tuple[int, int, int]) -> Image.Image:
    """Create a dark tinted backdrop with a soft light behind the product."""
    base = np.array(color, dtype=np.float32)
    deep = base * 0.16 + 6
    glow = base * 0.62 + 30
    ys, xs = np.mgrid[0:HEIGHT // 4, 0:WIDTH // 4].astype(np.float32)
    distance = np.sqrt(((xs - WIDTH / 8) / (WIDTH / 4)) ** 2 + ((ys - PRODUCT_CENTER_Y / 4) / (HEIGHT / 4)) ** 2)
    mix = np.clip(1 - distance / 0.62, 0, 1)[..., None] ** 1.6
    pixels = np.clip(deep + (glow - deep) * mix, 0, 255).astype(np.uint8)
    return Image.fromarray(pixels, "RGB").resize((WIDTH, HEIGHT), Image.Resampling.BICUBIC)


def _cover(image: Image.Image, width: int, height: int) -> Image.Image:
    scale = max(width / image.width, height / image.height)
    resized = image.resize((math.ceil(image.width * scale), math.ceil(image.height * scale)), Image.Resampling.LANCZOS)
    left = (resized.width - width) // 2
    top = (resized.height - height) // 2
    return resized.crop((left, top, left + width, top + height))


def _contain(image: Image.Image, box: tuple[int, int]) -> Image.Image:
    scale = min(box[0] / image.width, box[1] / image.height)
    return image.resize((max(1, round(image.width * scale)), max(1, round(image.height * scale))),
                        Image.Resampling.LANCZOS)


def _with_shadow(layer: Image.Image) -> Image.Image:
    margin = 70
    canvas = Image.new("RGBA", (layer.width + margin * 2, layer.height + margin * 2), (0, 0, 0, 0))
    shadow = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    dark = Image.new("RGBA", layer.size, (0, 0, 0, 150))
    shadow.paste(dark, (margin, margin + 26), layer.getchannel("A"))
    canvas.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(28)))
    canvas.alpha_composite(layer, (margin, margin))
    return canvas


class SceneArt:
    """The prepared images for one scene, which each frame then animates."""

    def __init__(self, scene: dict, photo_path: Path, color: tuple[int, int, int], accent: tuple[int, int, int]):
        self.motion = scene["motion"]
        self.layer: Image.Image | None = None
        self.moving_background = False
        self.headline = build_headline(scene["headline"], accent)
        with Image.open(photo_path) as opened:
            photo = opened.convert("RGB")

        layout = scene["layout"]
        cutout = photos.get_cutout(photo_path) if layout == "product" else None
        if layout == "full":
            self.background = _cover(photo, WIDTH, HEIGHT)
            self.moving_background = True
        elif cutout:
            with Image.open(cutout) as opened:
                self.layer = _with_shadow(_contain(opened.convert("RGBA"), PRODUCT_BOX))
            self.background = gradient_background(color)
        else:
            # Used for "fit", and for "product" photos that couldn't be cut out.
            soft = _cover(photo, WIDTH // 4, HEIGHT // 4).filter(ImageFilter.GaussianBlur(14))
            soft = soft.resize((WIDTH, HEIGHT), Image.Resampling.BICUBIC)
            self.background = ImageChops.multiply(soft, Image.new("RGB", (WIDTH, HEIGHT), (112, 112, 112)))
            fitted = _contain(photo, FIT_BOX).convert("RGBA")
            mask = Image.new("L", fitted.size, 0)
            ImageDraw.Draw(mask).rounded_rectangle((0, 0, fitted.width - 1, fitted.height - 1), radius=28, fill=255)
            fitted.putalpha(mask)
            self.layer = _with_shadow(fitted)
        if self.layer:
            # Premultiplied alpha stops dark fringes when edges are resampled.
            self.layer = self.layer.convert("RGBa")

    def _draw_layer(self, frame: Image.Image, scale: float, dx: float, dy: float) -> None:
        width = min(WIDTH, math.ceil(self.layer.width * 1.3))
        height = min(HEIGHT, math.ceil(self.layer.height * 1.3))
        left = (WIDTH - width) // 2
        top = PRODUCT_CENTER_Y - height // 2
        inverse = 1 / scale
        offset_x = self.layer.width / 2 - inverse * (width / 2 + dx)
        offset_y = self.layer.height / 2 - inverse * (height / 2 + dy)
        moved = self.layer.transform((width, height), Image.Transform.AFFINE,
                                     (inverse, 0, offset_x, 0, inverse, offset_y),
                                     Image.Resampling.BILINEAR).convert("RGBA")
        frame.paste(moved, (left, top), moved)

    def _draw_headline(self, frame: Image.Image, elapsed: float) -> None:
        if not self.headline:
            return
        progress = ease_out(min(elapsed / 0.22, 1.0))
        x = (WIDTH - self.headline.width) // 2
        y = HEADLINE_TOP + round((1 - progress) * 40)
        panel = self.headline
        if progress < 1:
            panel = panel.copy()
            panel.putalpha(panel.getchannel("A").point(lambda value: int(value * progress)))
        frame.paste(panel, (x, y), panel)

    def frame(self, progress: float, elapsed: float) -> Image.Image:
        scale, dx, dy = motion_values(self.motion, progress, elapsed)
        if self.moving_background:
            # Full-screen photos must always cover the frame, so they never go below 1.1x.
            scale = max(scale, 1.0) * 1.1
            inverse = 1 / scale
            frame = self.background.transform(
                (WIDTH, HEIGHT), Image.Transform.AFFINE,
                (inverse, 0, WIDTH / 2 - inverse * (WIDTH / 2 + dx), 0, inverse, HEIGHT / 2 - inverse * (HEIGHT / 2 + dy)),
                Image.Resampling.BILINEAR)
        else:
            frame = self.background.copy()
            self._draw_layer(frame, scale, dx, dy)
        self._draw_headline(frame, elapsed)
        return frame


def resolve_colors(color_hex: str | None, first_photo: Path) -> tuple[tuple[int, int, int], tuple[int, int, int]]:
    """Return the backdrop color and a brighter version for headline panels."""
    if color_hex:
        color = tuple(int(color_hex[i:i + 2], 16) for i in (1, 3, 5))
    else:
        color = photos.dominant_color(first_photo)
    return color, color


def render_photo_video(scenes: list[dict], photo_paths: dict[str, Path], audio: Path, script_words: list[Word],
                       caption_words: list[Word], out_path: Path, color_hex: str | None = None,
                       on_progress=None) -> float:
    """Render a video from scene plans and return its length in seconds."""
    return with_best_encoder(lambda args: _render_photo_video(
        scenes, photo_paths, audio, script_words, caption_words, out_path, color_hex, on_progress, args))


def _render_photo_video(scenes: list[dict], photo_paths: dict[str, Path], audio: Path, script_words: list[Word],
                        caption_words: list[Word], out_path: Path, color_hex: str | None, on_progress,
                        encoder_args: list[str]) -> float:
    ffmpeg = find_program("ffmpeg")
    total = probe_duration(audio) + TAIL_SECONDS
    timed = scene_times(scenes, script_words, total)
    color, accent = resolve_colors(color_hex, photo_paths[scenes[0]["photo_id"]])
    frame_total = round(total * FPS)

    with tempfile.TemporaryDirectory(prefix="vyral-motion-") as temp:
        workdir = Path(temp)
        (workdir / "captions.ass").write_text(build_ass(caption_words), encoding="utf-8")
        with (workdir / "ffmpeg.log").open("w+", encoding="utf-8", errors="replace") as log:
            # Running inside the temp folder keeps drive colons out of the subtitle filter.
            encoder = subprocess.Popen(
                [ffmpeg, "-y", "-v", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{WIDTH}x{HEIGHT}",
                 "-r", str(FPS), "-i", "pipe:0", "-i", str(audio), "-vf", "ass=captions.ass", "-map", "0:v:0",
                 "-map", "1:a:0", "-t", f"{total:.3f}", *encoder_args,
                 "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(out_path)],
                cwd=workdir, stdin=subprocess.PIPE, stderr=log, creationflags=NO_WINDOW,
            )
            try:
                written = 0
                for number, item in enumerate(timed):
                    art = SceneArt(item.scene, photo_paths[item.scene["photo_id"]], color, accent)
                    first = round(item.start * FPS)
                    last = frame_total if number == len(timed) - 1 else round(item.end * FPS)
                    count = max(last - first, 0)
                    for index in range(count):
                        progress = index / max(count - 1, 1)
                        encoder.stdin.write(art.frame(progress, index / FPS).tobytes())
                        written += 1
                    if on_progress:
                        on_progress(written / max(frame_total, 1))
                encoder.stdin.close()
            except (BrokenPipeError, OSError):
                pass  # The exit code check below reports what FFmpeg said.
            code = encoder.wait()
            if code != 0:
                log.seek(0)
                tail = "\n".join(log.read().strip().splitlines()[-5:])
                raise RenderError(f"Video processing failed: {tail}")
    return probe_duration(out_path)
