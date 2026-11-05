"""Turn clips, a voiceover and word timings into a captioned 9:16 video."""
from __future__ import annotations

import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .tools import find_program
from .voice import Word

WIDTH, HEIGHT, FPS = 1080, 1920, 30
SEGMENT_SECONDS = 2.5
MIN_SEGMENT_SECONDS = 0.5
TAIL_SECONDS = 0.3
MAX_WORDS_PER_CAPTION = 4
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


CPU_VIDEO_ARGS = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20"]
GPU_VIDEO_ARGS = ["-c:v", "h264_nvenc", "-preset", "p4", "-rc", "vbr", "-cq", "23", "-b:v", "0"]
_gpu_works: bool | None = None


class RenderError(RuntimeError):
    pass


@dataclass
class Segment:
    path: str
    start: float
    length: float


def _run(args: list[str], cwd: Path | None = None) -> str:
    result = subprocess.run(
        args, cwd=cwd, capture_output=True, text=True, encoding="utf-8",
        errors="replace", creationflags=NO_WINDOW,
    )
    if result.returncode != 0:
        tail = "\n".join(result.stderr.strip().splitlines()[-5:])
        raise RenderError(f"Video processing failed: {tail}")
    return result.stdout


def video_args() -> list[str]:
    """Use the NVIDIA encoder when this computer has a working one."""
    global _gpu_works
    if _gpu_works is None:
        test = subprocess.run(
            [find_program("ffmpeg"), "-v", "error", "-f", "lavfi", "-i", "color=s=320x240:d=0.1",
             *GPU_VIDEO_ARGS, "-f", "null", "-"],
            capture_output=True, creationflags=NO_WINDOW,
        )
        _gpu_works = test.returncode == 0
    return GPU_VIDEO_ARGS if _gpu_works else CPU_VIDEO_ARGS


def with_best_encoder(work):
    """Run a render, retrying on the CPU if the GPU encoder fails partway."""
    global _gpu_works
    args = video_args()
    try:
        return work(args)
    except RenderError:
        if args is CPU_VIDEO_ARGS:
            raise
        _gpu_works = False
        return work(CPU_VIDEO_ARGS)


def probe_duration(path: Path) -> float:
    output = _run([find_program("ffprobe"), "-v", "error", "-show_entries", "format=duration",
                   "-of", "default=noprint_wrappers=1:nokey=1", str(path)])
    try:
        return float(output.strip())
    except ValueError as exc:
        raise RenderError(f"Could not read the length of {path.name}.") from exc


def plan_segments(clips: list[dict], total: float, rotation: int = 0) -> list[Segment]:
    """Cycle through clips, moving forward in each so repeats show new footage.

    `rotation` starts each variant on a different clip so they don't all look alike.
    """
    usable = [clip for clip in clips if clip["duration"] >= MIN_SEGMENT_SECONDS]
    if not usable:
        raise RenderError("Add at least one clip that is 0.5 seconds or longer.")
    cursors = {clip["path"]: 0.0 for clip in usable}
    segments: list[Segment] = []
    covered = 0.0
    turn = rotation
    while covered < total:
        clip = usable[turn % len(usable)]
        turn += 1
        cursor = cursors[clip["path"]]
        if clip["duration"] - cursor < MIN_SEGMENT_SECONDS:
            cursor = 0.0
        length = min(SEGMENT_SECONDS, clip["duration"] - cursor, total - covered + 0.1)
        length = max(length, MIN_SEGMENT_SECONDS)
        segments.append(Segment(clip["path"], cursor, length))
        cursors[clip["path"]] = cursor + length
        covered += length
    return segments


def _ass_time(seconds: float) -> str:
    centis = max(0, round(seconds * 100))
    hours, centis = divmod(centis, 360000)
    minutes, centis = divmod(centis, 6000)
    secs, centis = divmod(centis, 100)
    return f"{hours}:{minutes:02d}:{secs:02d}.{centis:02d}"


def _ass_escape(text: str) -> str:
    return text.replace("\\", "").replace("{", "(").replace("}", ")").replace("\n", " ")


def group_words(words: list[Word]) -> list[list[Word]]:
    groups: list[list[Word]] = []
    current: list[Word] = []
    for word in words:
        current.append(word)
        if len(current) >= MAX_WORDS_PER_CAPTION or word.text[-1:] in ".!?,:;":
            groups.append(current)
            current = []
    if current:
        groups.append(current)
    return groups


def build_ass(words: list[Word]) -> str:
    """Build captions showing a few words at a time with the spoken word highlighted."""
    header = (
        "[Script Info]\nScriptType: v4.00+\n"
        f"PlayResX: {WIDTH}\nPlayResY: {HEIGHT}\nWrapStyle: 0\nScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
        "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
        "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        "Style: Caption,Arial,82,&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,-1,0,0,0,"
        "100,100,0,0,1,7,2,2,90,90,460,1\n\n"
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    lines = []
    for group in group_words(words):
        for index, word in enumerate(group):
            end = group[index + 1].start if index + 1 < len(group) else word.end
            if end <= word.start:
                end = word.start + 0.05
            parts = [
                "{\\c&H2BF0FF&}" + _ass_escape(item.text) + "{\\c&HFFFFFF&}"
                if position == index else _ass_escape(item.text)
                for position, item in enumerate(group)
            ]
            lines.append(
                f"Dialogue: 0,{_ass_time(word.start)},{_ass_time(end)},Caption,,0,0,0,,{' '.join(parts)}"
            )
    return header + "\n".join(lines) + "\n"


def render_variant(clips: list[dict], audio: Path, words: list[Word], out_path: Path,
                   rotation: int = 0) -> float:
    """Render one video and return its length in seconds."""
    return with_best_encoder(lambda args: _render_variant(clips, audio, words, out_path, rotation, args))


def _render_variant(clips: list[dict], audio: Path, words: list[Word], out_path: Path, rotation: int,
                    encoder_args: list[str]) -> float:
    ffmpeg = find_program("ffmpeg")
    voice_seconds = probe_duration(audio)
    total = voice_seconds + TAIL_SECONDS
    segments = plan_segments(clips, total, rotation)
    framing = (
        f"scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=increase,"
        f"crop={WIDTH}:{HEIGHT},setsar=1,fps={FPS},format=yuv420p"
    )
    with tempfile.TemporaryDirectory(prefix="vyral-render-") as temp:
        workdir = Path(temp)
        names = []
        for index, segment in enumerate(segments):
            name = f"seg{index:03d}.mp4"
            _run([ffmpeg, "-y", "-v", "error", "-ss", f"{segment.start:.3f}", "-t", f"{segment.length:.3f}",
                  "-i", segment.path, "-an", "-vf", framing, *encoder_args, str(workdir / name)])
            names.append(name)
        (workdir / "list.txt").write_text("".join(f"file '{name}'\n" for name in names), encoding="utf-8")
        (workdir / "captions.ass").write_text(build_ass(words), encoding="utf-8")
        # Running inside the temp folder keeps Windows drive colons out of the
        # subtitle filter's arguments, where they would need awkward escaping.
        _run([ffmpeg, "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", "list.txt",
              "-i", str(audio), "-vf", "ass=captions.ass", "-map", "0:v:0", "-map", "1:a:0",
              "-t", f"{total:.3f}", *encoder_args,
              "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart",
              str(out_path)], cwd=workdir)
    return probe_duration(out_path)
