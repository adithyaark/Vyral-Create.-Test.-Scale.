"""Local voiceover (Kokoro) and word timings for captions (faster-whisper)."""
from __future__ import annotations

import difflib
import re
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path

from .tools import KOKORO_MODEL, KOKORO_VOICES, find_program

# English voices shipped in the Kokoro voice file, verified against it.
VOICES = {
    "af_heart": "Heart (US, female)",
    "af_bella": "Bella (US, female)",
    "af_nicole": "Nicole (US, female)",
    "am_michael": "Michael (US, male)",
    "am_adam": "Adam (US, male)",
    "am_eric": "Eric (US, male)",
    "bf_emma": "Emma (UK, female)",
    "bf_lily": "Lily (UK, female)",
    "bm_george": "George (UK, male)",
    "bm_daniel": "Daniel (UK, male)",
}
WHISPER_MODEL = "base.en"
SAMPLE_RATE = 16000
MIN_MATCH_FRACTION = 0.6

_lock = threading.Lock()
_kokoro = None
_whisper = None


@dataclass
class Word:
    text: str
    start: float
    end: float


def synthesize(text: str, voice: str, speed: float, out_path: Path) -> None:
    global _kokoro
    if voice not in VOICES:
        raise ValueError(f"Unknown voice: {voice}")
    import soundfile
    from kokoro_onnx import Kokoro

    with _lock:
        if _kokoro is None:
            _kokoro = Kokoro(str(KOKORO_MODEL), str(KOKORO_VOICES))
        lang = "en-gb" if voice.startswith("b") else "en-us"
        samples, rate = _kokoro.create(text, voice=voice, speed=speed, lang=lang)
    soundfile.write(str(out_path), samples, rate)


def _load_audio(path: Path):
    """Decode audio with FFmpeg, avoiding faster-whisper's PyAV decoder.

    faster-whisper 1.2.1 passes an option that the installed PyAV 19 rejects.
    Whisper also accepts raw samples, which sidesteps that incompatibility.
    """
    import numpy as np

    result = subprocess.run(
        [find_program("ffmpeg"), "-v", "error", "-i", str(path), "-f", "f32le",
         "-ac", "1", "-ar", str(SAMPLE_RATE), "-"],
        capture_output=True,
        check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return np.frombuffer(result.stdout, dtype=np.float32)


def transcribe(path: Path, accurate: bool = False) -> list[Word]:
    """Return the words heard in a recording with their timings.

    The script is deliberately not given to Whisper as a prompt: on longer scripts
    that made it output only the final line, repeated, with broken timings.

    The fast settings suit clear AI voices, where only the timings are used. Own
    recordings use `accurate`, because their captions show the recognized words.
    """
    global _whisper
    from faster_whisper import WhisperModel

    audio = _load_audio(path)
    with _lock:
        if _whisper is None:
            _whisper = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
        options = {} if accurate else {"beam_size": 1, "best_of": 1, "temperature": 0}
        segments, _ = _whisper.transcribe(audio, word_timestamps=True, condition_on_previous_text=False, **options)
        return [
            Word(word.word.strip(), float(word.start), float(word.end))
            for segment in segments
            for word in segment.words or []
            if word.word.strip()
        ]


def _normalize(word: str) -> str:
    return re.sub(r"[^a-z0-9]", "", word.lower())


def align_script(script: str, heard: list[Word], duration: float) -> list[Word]:
    """Give each word of the known script a time taken from the transcription.

    Captions show the script's exact wording, so a product name Whisper mishears
    still appears correctly. Words with no match are spaced between their neighbours.
    If too little of the script was heard, the timings can't be trusted, so words
    are spaced evenly across the audio instead of bunching up or getting stuck.
    """
    tokens = script.split()
    if not tokens:
        return []
    matcher = difflib.SequenceMatcher(
        a=[_normalize(token) for token in tokens],
        b=[_normalize(word.text) for word in heard],
        autojunk=False,
    )
    starts: list[float | None] = [None] * len(tokens)
    ends: list[float | None] = [None] * len(tokens)
    blocks = matcher.get_matching_blocks()
    if sum(block.size for block in blocks) / len(tokens) >= MIN_MATCH_FRACTION:
        for block in blocks:
            for offset in range(block.size):
                starts[block.a + offset] = heard[block.b + offset].start
                ends[block.a + offset] = heard[block.b + offset].end

    index = 0
    while index < len(tokens):
        if starts[index] is not None:
            index += 1
            continue
        gap_end = index
        while gap_end < len(tokens) and starts[gap_end] is None:
            gap_end += 1
        left = ends[index - 1] if index > 0 else 0.0
        right = starts[gap_end] if gap_end < len(tokens) else max(duration, left)
        step = max(right - left, 0.0) / (gap_end - index)
        for position in range(index, gap_end):
            starts[position] = left + step * (position - index)
            ends[position] = left + step * (position - index + 1)
        index = gap_end
    return [Word(token, starts[i], ends[i]) for i, token in enumerate(tokens)]
