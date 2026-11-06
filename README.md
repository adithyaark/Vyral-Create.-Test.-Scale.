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
| Scripts | Your installed Claude Code, the Claude API, or Groq | Claude Code counts toward your plan's usage; the API is billed to your key; Groq depends on your Groq account |
| Scene planning | The Claude API if a key is saved, otherwise your installed Claude Code | API use is billed to your key; Claude Code counts toward your plan's usage |
| Background removal | rembg `isnet-general-use`, on your PC | Free |
| AI voice | Kokoro, on your PC | Free |
| Caption timing | faster-whisper `base.en`, on your PC | Free |
| Video editing | FFmpeg, on your PC, using your NVIDIA graphics card when available | Free |

Your product details and reviews go to Claude or Groq when scripts are written. Small copies of your photos go to Claude when scenes are planned. Clips, recordings and videos stay on your PC.

## Hosted mode with accounts

Set `VYRAL_HOSTED=1` to run Vyral for many users, with Supabase handling sign-in, data and files.

1. Fill in the Supabase values in `.env`, using `.env.example` as a guide.
2. Run `uv run python -m vyral.migrate` to create the tables and private file storage.
3. In Supabase, under Authentication's URL settings, set the Site URL and add your `VYRAL_PUBLIC_URL` to the redirect URLs, so email links return to Vyral.
4. Start Vyral and create your account. List your email in `VYRAL_ADMIN_EMAILS` to change app-wide settings.

To copy projects from `data/` into an account, sign up first, then run `uv run python -m vyral.import_local your@email.com` to preview and add `--apply` to copy them. Local copies aren't changed.

Without `VYRAL_HOSTED=1`, Vyral stays single-user on your PC, with no sign-in and everything in `data/`.

To run the isolation tests against a real project, set `VYRAL_TEST_SUPABASE=1` and run `uv run pytest tests/test_hosted_integration.py`. They create two temporary users and delete them afterwards.

## Keeping API keys in a .env file

Copy `.env.example` to `.env` and fill in your keys. Keys there override keys saved in Settings, and `.env` is ignored by Git. On a hosted server (`VYRAL_HOSTED=1`), keys must come from `.env` or the server's environment, and they can't be viewed or changed in the app.

## Using Groq for scripts

Open **Settings**, paste a key from https://console.groq.com/keys, and choose a model. Vyral tests the key before saving it in `data/settings.json`, which isn't encrypted. Scene planning with photos still uses Claude, or you can choose **Plan automatically**.

## Using the Claude API

Open **Settings**, paste a key from the Anthropic Console, and choose a model and monthly budget. The API is billed separately from a Claude plan. Vyral estimates spend from token counts and stops at your budget, so also set a limit in the Anthropic Console.

To stop a hosted server from using a personal Claude plan, set the environment variable `VYRAL_HOSTED=1`.

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
