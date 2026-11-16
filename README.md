---
title: Clipy AI Auto Clipper
emoji: ✂️
colorFrom: purple
colorTo: pink
sdk: docker
app_port: 7860
pinned: false
---

# Clipy - AI Video Editing Platform

Upload a video, add a TikTok / Reel / Short as **editing inspiration**, and Clipy's AI
studies the reference's style (cuts, captions, zooms, pacing) and applies it to your
footage — then renders real, playable clips with FFmpeg and publishes them on schedule.

## What Clipy does

1. **Reference-based AI editing**: paste a public TikTok / Instagram Reel / YouTube
   Shorts URL (or upload the file). Clipy retrieves the video, samples its frames and
   audio, and Gemini builds a structured editing profile that drives your edit.
2. **Highlight detection**: local Whisper transcription + Gemini find the strongest
   moments; long videos are cut into multiple styled clips, short videos get polished.
3. **Real rendering**: FFmpeg engine with 9:16 / 16:9 / 1:1 / 4:5 output, karaoke /
   bold / minimal captions, zoom punches, silence removal, -14 LUFS audio.
4. **Publishing**: schedule or post now to Buffer, Instagram, TikTok and YouTube.
   Success is only reported when the platform confirms it.
5. **Permanent workspace**: users, projects, clips, history, API keys and connected
   accounts all live in a database (PostgreSQL on Render) and survive restarts.

## Deployment (Render)

1. Create a **PostgreSQL** instance (free tier works) and a Web Service from this repo
   (Docker runtime — build/start are automatic).
2. Set environment variables on the Web Service:
   - `DATABASE_URL` — internal Postgres connection string (**required** for persistence)
   - `SECRET_KEY` — any long random string; encrypts API keys & tokens at rest (**required**)
   - `GEMINI_API_KEY` — optional server-wide fallback; each user adds their own key in Settings
   - Optional OAuth: `INSTAGRAM_CLIENT_ID/SECRET`, `TIKTOK_CLIENT_KEY/SECRET`,
     `YOUTUBE_CLIENT_ID/SECRET`, `TIKTOK_AUDITED=true` only after TikTok approval
   - Optional: `BUFFER_ACCESS_TOKEN`, `CLIPY_MAX_UPLOAD_MB`, `CLIPY_PERSIST_FILES`
3. Deploy. First boot creates the schema and migrates any legacy data automatically.
4. Health check: `/health`.

## Run locally

1. `pip install -r requirements.txt` (ffmpeg must be on PATH).
2. `python -m uvicorn main:app --host 0.0.0.0 --port 8000` or double-click `start.bat`.
3. Open `http://localhost:8000`, register, complete onboarding, add your Gemini key.

Without `DATABASE_URL`, Clipy uses a local SQLite database (`data/clipy.db`) — handy
for development; production should always use PostgreSQL.

## Security notes

- Passwords: PBKDF2-SHA256 (240k iterations, per-user salt); sessions are DB-backed
  and survive restarts; reset tokens are single-use, hashed and expire in 1 hour.
- Secrets (Gemini key, OAuth tokens) are encrypted with Fernet before storage and are
  never returned to the browser in plaintext (masked indicator only).
- All clips/projects/history are ownership-checked per request; anonymous API access
  returns 401.
- Reference retrieval only uses publicly accessible videos with standard clients.
  If a platform refuses access, users upload the file instead — no access-control
  bypass.
