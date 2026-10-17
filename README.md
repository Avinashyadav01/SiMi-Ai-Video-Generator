# SiMi — AI Content Automation

SiMi turns a topic into a short video with Gemini, Edge TTS, local licensed visuals, burned-in subtitles, FFmpeg rendering, and quality validation. The browser app has separate accounts, a responsive workspace, private job/media access, durable generation jobs, topic queues, and per-user creation schedules.

## Current capabilities and limits

- Topic-to-video creation runs asynchronously and tracks each generation stage.
- Accounts use scrypt password hashes and opaque, HTTP-only database-backed sessions.
- Jobs, preferences, schedules, topic ideas, connections, and publishing-history records are related to a user. API job reads and video downloads are owner-scoped.
- Scheduling uses each user's IANA timezone. Editable default post times are 08:00, 14:00, and 20:00. The scheduler queues generation 30 minutes ahead (configurable), then tries to publish validated jobs at the chosen time.
- YouTube, Instagram, and Facebook OAuth entry points and official API adapter modules are implemented. OAuth tokens are encrypted per user. Publishing is disabled by default; once enabled, a user can explicitly publish a validated video, and scheduled jobs publish automatically at their chosen time to connected, selected platforms. Real platform uploads still need external credentials, a public HTTPS deployment for Instagram, and live account verification.
- Add licensed local videos under `data/assets/<keyword>/` or `data/assets/fallback/`. The app stops clearly if it cannot find an asset; it does not download arbitrary footage.

This is a single-machine pilot application, not a horizontally scalable public service. Production mode requires HTTPS, secure cookies, and an exact `APP_ORIGIN`; unsafe API requests are rejected unless their browser `Origin` matches it, and requests with an unrecognized `Host` are rejected. Login and signup throttling is in process memory, so keep one application process and put shared rate limiting at the reverse proxy before accepting public signups. Set up managed secrets, backup and restore, account recovery, monitoring, and an operational deployment plan. Do not expose the development server directly to the internet.

## Install and run

- Python 3.11+
- FFmpeg and ffprobe with H.264, AAC, and libass support
- Either a Gemini API key or local Ollama; Edge TTS also needs network access

```powershell
python -m pip install -r requirement.txt
Copy-Item .env.example .env
# Edit .env and choose AI_PROVIDER=gemini or AI_PROVIDER=ollama
python main.py
```

Open `http://127.0.0.1:8000`, create an account, and enter a topic. Use a password with at least 12 characters. The app creates SQLite state under `data/database/` and private job artifacts under `output/users/<user-id>/<job-id>/`.

For local development with reload:

```powershell
python main.py --reload
```

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `AI_PROVIDER` | `ollama` | local `ollama`, `gemini`, or `auto` (try Ollama, then Gemini) |
| `GEMINI_API_KEY` | required for Gemini | Gemini credential; never log or commit it |
| `GEMINI_MODEL` | `gemini-2.5-flash` | Script/content model |
| `OLLAMA_BASE_URL` | `http://127.0.0.1:11434` | Local Ollama server URL |
| `OLLAMA_MODEL` | `qwen3.5:4b` | Installed local Ollama model |
| `OLLAMA_TIMEOUT` | `240` | Local generation timeout in seconds |
| `TTS_VOICE` | `en-IN-NeerjaNeural` | Default Edge TTS voice |
| `TTS_RATE` / `TTS_VOLUME` | `+0%` | Default speech rate and volume |
| `DATABASE_PATH` | `data/database/jobs.sqlite3` | SQLite database path |
| `OUTPUT_DIR` | `output` | Private generated-media root |
| `COOKIE_SECURE` | `false` locally | Set `true` when deployed behind HTTPS |
| `APP_ENV` | `development` | Set `production` to enforce HTTPS origin, host, secure-cookie, and mutation-origin checks |
| `APP_ORIGIN` | unset | Exact public HTTPS origin in production, for example `https://studio.example.com` (no path) |
| `OAUTH_ENCRYPTION_KEY` | required for OAuth | Fernet key used to encrypt user platform tokens at rest |
| `GOOGLE_OAUTH_CLIENT_ID` / `GOOGLE_OAUTH_CLIENT_SECRET` | required for YouTube OAuth | Google Cloud web OAuth client credentials |
| `GOOGLE_OAUTH_REDIRECT_URI` | local callback URL | Must exactly match the Google OAuth web client callback |
| `META_APP_ID` / `META_APP_SECRET` | required for Meta OAuth | Meta developer app credentials |
| `META_GRAPH_VERSION` | required for Meta | Supported Graph API version enabled for your app |
| `META_INSTAGRAM_REDIRECT_URI` | local callback URL | Exact Instagram OAuth callback URI registered in Meta |
| `META_FACEBOOK_REDIRECT_URI` | local callback URL | Exact Facebook OAuth callback URI registered in Meta |
| `PUBLISHING_ENABLED` | `false` | Explicit global guard for manual platform uploads; keep off until approved and tested |
| `PUBLIC_BASE_URL` | unset | Public HTTPS origin used for signed Instagram media fetches |
| `MEDIA_SIGNING_KEY` | unset | Private random key for short-lived signed video URLs |
| `PREPUBLISH_LEAD_MINUTES` | `30` | How far before each user slot to start preparing a scheduled video |
| `AUTO_PUBLISH_LATE_GRACE_MINUTES` | `60` | Do not auto-publish a video more than this long after its slot |
| `HOST` / `PORT` | `127.0.0.1` / `8000` | Development bind address and port |

Do not use `--host 0.0.0.0` on an untrusted network. A production pilot needs TLS at a trusted reverse proxy, `APP_ENV=production`, `APP_ORIGIN=https://your-domain`, `COOKIE_SECURE=true`, a persistent database/media volume, backup and restore procedures, shared edge rate limiting, monitoring, and an operational plan for workers and secrets. Keep one application process: the generation worker and scheduler are started in-process and SQLite is the durable queue. Configure the reverse proxy to forward the original public `Host` header; the app intentionally does not trust arbitrary forwarded headers. This configuration adds browser-origin and host checks, but does not provide account recovery, horizontal scaling, or managed queue/database services.

### No Gemini API quota: use a local model

Install Ollama from the [official Windows installer](https://ollama.com/download/windows), then in PowerShell download and start the configured local model:

From the project folder, run `.\setup_ollama.ps1` in PowerShell. It starts the local Ollama service if needed and downloads/verifies the configured model. Ollama runs the script model on your own computer; it needs no Gemini key and makes no per-request API charge. The Qwen 3.5 4B download uses about 3.3–4.0 GB; generation speed and quality depend on your RAM and GPU. Edge TTS still needs internet. This only replaces script generation; FFmpeg and a suitable licensed local background video are still required.

## User workflow

1. Sign up or sign in.
2. Enter one topic in **Create video**, or add several newline-separated ideas under **Schedule**.
3. The persistent worker generates the script, voiceover, subtitles, and validated MP4. The UI polls job progress without waiting on the render request.
4. Preview or download a ready video from **Content library**.
5. Configure posts per day, time slots, timezone, and platform preferences. Active schedules dispatch queued ideas to video generation in that timezone.

If the process restarts, interrupted jobs are returned to the durable queue and existing stage artifacts are reused. The SQLite scheduler uses a unique per-user/day/time tick to avoid duplicate dispatch.

## Verification

```powershell
python -m py_compile (Get-ChildItem -Filter *.py | ForEach-Object FullName)
python -c "import app; print(len(app.app.routes), 'API routes loaded')"
ffmpeg -version
ffprobe -version
```

The video download route performs a media quality check before showing the preview. A failed job shows its error in the library and can be retried. Real account OAuth and uploads require your official developer apps and credentials; `PUBLISHING_ENABLED` defaults to false.

## External setup required for the full product

These steps require accounts, approvals, or media that must come from you; the application cannot safely invent or bypass them:

1. **Visual assets:** add video clips that you own or have licensed under `data/assets/<matching-keyword>/` (or `data/assets/fallback/`). No production assets are currently included, so video generation stays blocked until at least one usable clip is added. Do not use clips downloaded from random sites.
2. **Social publishing:** follow the OAuth setup steps below. Developer app credentials, app review and permissions belong to your accounts. Upload adapters and a manual publish endpoint are present but unverified against real accounts. Automatic scheduled publishing still needs orchestration and end-to-end testing. Do not enable public posting before testing with private/test accounts.
3. **Voice network access:** Edge TTS is a remote service and needs outbound internet. If it is unavailable, voice generation cannot complete with the current implementation.
4. **Public deployment:** choose a host/domain, configure TLS and `COOKIE_SECURE=true`, persistent storage and database/media backups, secret management, monitoring, and account recovery. The built-in SQLite and single local worker are suitable for a single-machine pilot, not a horizontally scaled public service.

Local scripts, account isolation, schedules, and job processing can be exercised without social credentials. Use `python main.py` for the local app; stop it with Ctrl+C.

### Connect YouTube / Meta accounts (step by step)

Before connecting an account, generate a token encryption key and put it in the local `.env` file. Keep it private and back it up securely; losing it means saved platform tokens must be reconnected.

```powershell
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

**YouTube:** create a project in Google Cloud Console, enable YouTube Data API v3, configure the OAuth consent screen (add your own email as a test user while testing), and create an OAuth client of type **Web application**. Register `http://localhost:8000/api/oauth/youtube/callback` as an authorized redirect URI. Put the client ID and secret in `GOOGLE_OAUTH_CLIENT_ID` and `GOOGLE_OAUTH_CLIENT_SECRET` in `.env`.

**Instagram and Facebook:** create a Meta developer app, add the required Facebook Login/Instagram and Pages products, configure the app's OAuth redirect allow-list with the exact callback URIs from `.env.example`, and request the publishing/Page permissions shown in `oauth_service.py`. The selected Facebook Page must have publishing access; Instagram publishing requires a professional account linked to that Page. Put the App ID, App Secret and a currently supported Graph API version in `.env`.

Set `OAUTH_ENCRYPTION_KEY` to the generated Fernet key, restart SiMi, sign in, and use **Connected accounts → Connect account**. The browser will show the provider's consent screen. Never send client secrets, generated encryption keys, authorization codes, or access tokens in chat or screenshots. Meta/Google may restrict testing to app roles until review. OAuth connection alone does not activate automatic publishing; do not enable public posting until scheduling, job orchestration and test-account publishing have been verified.

For Meta Graph calls, also set `META_GRAPH_VERSION` to a version currently supported by your app. For Instagram, deploy SiMi on a public HTTPS origin, set `PUBLIC_BASE_URL` to that origin, and generate a separate random `MEDIA_SIGNING_KEY` (at least 32 characters). The signed media route allows Meta to fetch only the specific validated user's video for a short time. Facebook and YouTube upload from the local worker. Keep `PUBLISHING_ENABLED=false` until OAuth is connected and you have reviewed the platform requirements. When ready for a deliberate manual test, enable the flag, restart SiMi, then use **Publish to ...** on a validated video. Test first with a private/test account; YouTube API projects may restrict uploaded videos to private pending audit.
