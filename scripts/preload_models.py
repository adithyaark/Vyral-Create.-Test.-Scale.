"""Download the caption and background-removal models while building the server image."""
from faster_whisper import WhisperModel
from rembg import new_session

from vyral.photos import CUTOUT_MODEL
from vyral.voice import WHISPER_MODEL

WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
new_session(CUTOUT_MODEL)
print("Models ready.")
