"""Check the whole video pipeline works on this machine, without any accounts or network."""
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw

from vyral import motion, photos, render, scenes, tools, voice


def main() -> None:
    missing = [name for name, ready in tools.status().items() if not ready and name != "claude"]
    assert not missing, f"Missing: {missing}"
    text = "This simple red cup keeps your coffee warm. Try it today."
    with tempfile.TemporaryDirectory() as temp:
        folder = Path(temp)
        photo = folder / "product.jpg"
        image = Image.new("RGB", (1000, 1000), (238, 236, 230))
        ImageDraw.Draw(image).ellipse((280, 220, 720, 780), fill=(190, 40, 40))
        image.save(photo, quality=92)

        audio = folder / "voice.wav"
        voice.synthesize(text, "af_heart", 1.0, audio)
        heard = voice.transcribe(audio)
        assert heard, "No speech was detected in the test voiceover."
        words = voice.align_script(text, heard, render.probe_duration(audio))
        assert photos.make_cutout(photo, folder / "cutout.png"), "Background removal failed."

        layouts = ["product", "fit", "full"]
        scene_list = [{"text": line, "photo_id": "a", "layout": layouts[index % 3], "motion": scenes.MOTIONS[index % 6],
                       "headline": " ".join(line.split()[:3])} for index, line in enumerate(scenes.split_lines(text))]
        out = folder / "video.mp4"
        seconds = motion.render_photo_video(scene_list, {"a": photo}, audio, words, words, out)
        assert seconds > 1 and out.stat().st_size > 10_000, "The rendered video looks wrong."

        clip = {"path": str(out), "duration": seconds}
        clip_out = folder / "clip.mp4"
        assert render.render_variant([clip], audio, words, clip_out) > 1, "Clip rendering failed."
    print(f"Smoke test passed: {seconds:.1f}s video, encoder {render.video_args()[1]}.")


if __name__ == "__main__":
    main()
