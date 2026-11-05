# Vyral: Create. Test. Scale.

Vyral turns one product and either your photos or your phone footage into captioned 9:16 video ad variations. It runs on your own PC.

## Start it

Double-click **Start Vyral.bat**. Vyral opens in your browser at http://127.0.0.1:8765. Keep the black window open while you work, and close it to stop Vyral.

## How it works

1. **Product:** enter the product details and any real customer reviews.
2. **Scripts:** Claude writes a brief, 10 hooks and 3–5 scripts. You can also paste a reply from another AI or write your own. Edit and approve the ones you want.
3. **Visuals:** upload product photos for animated ads, or switch to your phone clips.
4. **Voice & render:** choose hook and script combinations and a voice. In photo mode, Claude can plan the scenes, or Vyral can assign your photos automatically. You can change them before rendering.
5. **Videos:** watch, download or delete the finished videos.

## What it uses

| Job | Tool | Cost |
|---|---|---|
| Scripts | Your installed Claude Code (Sonnet model), or Groq with your own API key | Claude counts toward your plan's usage; Groq depends on your Groq account |
| Scene planning | Your installed Claude Code (Sonnet model) | Counts toward your Claude plan's usage |
| Background removal | rembg `isnet-general-use`, on your PC | Free |
| AI voice | Kokoro, on your PC | Free |
| Caption timing | faster-whisper `base.en`, on your PC | Free |
| Video editing | FFmpeg, on your PC, using your NVIDIA graphics card when available | Free |

Your product details and reviews go to Claude or Groq when scripts are written. Small copies of your photos go to Claude when scenes are planned. Clips, recordings and videos stay on your PC.

## Using Groq for scripts

Open **Settings**, paste a key from https://console.groq.com/keys, and choose a model. Vyral tests the key before saving it in `data/settings.json`, which isn't encrypted. Scene planning with photos still uses Claude, or you can choose **Plan automatically**.

## If your Claude limit is reached

If you've added a Groq key, Vyral can switch to Groq for scripts automatically. Voiceovers, captions, background removal and rendering keep working. For scripts, open the Scripts step and use another AI or write your own. For scene planning, choose **Plan automatically**.

## Current limits

- English only.
- Photo mode animates your photos. It doesn't generate new footage, people or product shots, and there are no avatars.
- Vyral doesn't read product links, so paste the details in yourself.
- It doesn't publish ads or track their results.
- With your own recordings, captions come from speech recognition and can't yet be edited, so check them for mistakes.

## Before using an ad

- Watch the whole video and check the product, wording and captions.
- Only use photos you have the right to use. Brand website photos aren't automatically free for ads.
- Check the "Verify before publishing" notes. Vyral can't confirm claims are true.
- Check the current AI-disclosure rules for your ad platform.

## Where files are stored

- `data/projects/`: your projects, clips and videos
- `models/`: the AI voice model

## Developer commands

```bash
uv run pytest
```
