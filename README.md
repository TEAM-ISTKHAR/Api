# HellAPI

**FastAPI YouTube stream API + Telegram API-key bot**  
Built for music bots, lightweight deployments, and simple plan-based access control.

[![Python](https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![Deploy on Railway](https://railway.app/button.svg)](https://railway.app/new)

HellAPI resolves YouTube URLs or search queries and returns metadata plus a direct stream URL. The companion Telegram bot creates API keys, tracks usage, handles plan upgrades, and provides a small admin workflow.

> **Important:** direct media URLs are temporary and can expire. Use them immediately instead of storing them. Only use this project for content you are allowed to access and process.

## What is included

- FastAPI server with OpenAPI docs at `/docs`
- `/stream`, `/song`, `/api/stream`, `/ytdl`, `/search`, `/info`, and `/formats`
- API-key authentication with daily and per-minute plan limits
- SQLite usage, payment-request, ticket, audit, and metadata-cache tables
- Telegram bot for free keys, renewals, upgrades, support, payments, and admin actions
- YouTube client fallback, proxy rotation, cookies, PO token, and TTL caching support
- Heroku `Procfile` + `app.json` with always-on Basic `web` and `worker` processes
- Railway health checks and restart policy
- Docker Compose setup with persistent SQLite volume
- VPS systemd units and Nginx reverse-proxy config

## Architecture

| Process | Command | Purpose |
| --- | --- | --- |
| `web` / `api` | `python -m uvicorn main:app ...` | HTTP API |
| `worker` / `bot` | `python tgbot.py` | Telegram polling bot |

Run the API and bot as separate processes. This keeps Telegram polling independent from HTTP health checks and lets either process restart without taking the other down.

## Quick start

```bash
git clone https://github.com/TEAM-ISTKHAR/Api.git
cd Api

python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# Fill .env before starting

python -m uvicorn main:app --host 0.0.0.0 --port 8000
```

Open `http://localhost:8000/docs`. The API root and `/healthz` do not require a key; stream and account endpoints do.

Start the Telegram bot in a second terminal:

```bash
source .venv/bin/activate
python tgbot.py
```

## Configuration

Copy `.env.example` to `.env`. Never commit `.env`, Telegram tokens, cookies, proxy credentials, UPI/bank details, or API keys.

Required for a complete deployment:

```dotenv
APP_URL=https://your-public-api.example.com
ADMIN_KEY=generate-a-long-random-value
TELEGRAM_BOT_TOKEN=123456:replace-me
ADMIN_IDS=123456789
```

`API_BASE_URL` is the URL used by the bot to call the API. For a public deployment it can be the same as `APP_URL`; in Docker Compose and the VPS systemd setup it is overridden to the local API address.

Useful optional settings:

- `PROXY_LIST`: comma-separated HTTP proxies for YouTube extraction
- `YTDLP_COOKIE_FILE`: path to a cookies file (keep it outside Git)
- `YTDLP_PO_TOKEN`: YouTube PO token, if required by the extractor
- `DB_PATH`: SQLite file location; use persistent storage in production
- `YTDLP_WORKERS`: extraction thread count; start with `4`
- `BF_MAX_ATTEMPTS`, `BF_WINDOW_SEC`, `BF_BLOCK_SEC`: invalid-key protection

Generate a strong admin key without putting it in shell history:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

## API examples

Use the header form in production:

```bash
curl -H "x-api-key: HellAPIxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx" \
  "https://your-api.example.com/stream?url=https%3A%2F%2Fwww.youtube.com%2Fwatch%3Fv%3DVIDEO_ID"
```

Search and resolve a song:

```bash
curl -H "x-api-key: YOUR_KEY" \
  "https://your-api.example.com/song?query=Alan%20Walker%20Faded"
```

Useful endpoints:

| Endpoint | Auth | Description |
| --- | --- | --- |
| `GET /` | No | Service information |
| `GET /healthz` | No | Hosting-platform health probe |
| `GET /docs` | No | Swagger UI |
| `GET /plans` | No | Plan and pricing metadata |
| `GET /stream` | Yes | Universal audio stream |
| `GET /song` | Yes | Music-bot-compatible response |
| `GET /api/stream` | Yes | Audio or video via `type` |
| `GET /ytdl` | Yes | Raw yt-dlp-style response |
| `GET /search` | Yes | YouTube search |
| `GET /info` | Yes | Video metadata |
| `GET /formats` | Yes | Available formats |
| `GET /my/stats` | Yes | Current key usage |

Admin endpoints use the same `x-api-key` header with `ADMIN_KEY`.

## Heroku: Basic dynos, API + bot

The repository includes a `Procfile` with `web` and `worker` processes. `app.json` requests one **Basic** dyno for each process so the API and Telegram bot stay on; Basic dynos are paid and do not use Eco sleeping behavior.

### Deploy from the Heroku Dashboard

1. Create a Heroku app and connect this GitHub repository.
2. In **Settings → Config Vars**, add the values from `.env.example`.
3. Deploy the `main` branch.
4. In **Resources**, verify `web` and `worker` are both set to `Basic` and quantity `1`.

### Deploy with Heroku CLI

```bash
heroku create your-hellapi-name
heroku stack:set heroku-24 -a your-hellapi-name

heroku config:set \
  APP_NAME=HellAPI \
  APP_URL=https://your-hellapi-name.herokuapp.com \
  API_BASE_URL=https://your-hellapi-name.herokuapp.com \
  ADMIN_KEY="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')" \
  ADMIN_IDS="123456789" \
  -a your-hellapi-name

git push heroku main
heroku ps:scale web=1:basic worker=1:basic -a your-hellapi-name
heroku ps -a your-hellapi-name
```

Add `TELEGRAM_BOT_TOKEN` separately in the Heroku dashboard or with `heroku config:set` from a secure terminal.

Do not paste real tokens into GitHub. Heroku cannot provide durable SQLite storage: a dyno restart or redeploy can remove `bot_data.db`. Use the Docker/VPS option or migrate the database before relying on persistent users, payments, and usage history.

## Railway: API + bot

Railway reads `railway.json`, installs the Python requirements, exposes the injected `$PORT`, checks `/healthz`, and restarts failed API processes.

1. Create a Railway project from this GitHub repository.
2. Add the environment variables from `.env.example`.
3. Deploy the first service as the API. Generate a public domain and set `APP_URL` and `API_BASE_URL` to it.
4. Add a second service from the same repository for the bot.
5. Set its start command to `python tgbot.py`.
6. Give both services the same `DB_PATH` only if the database is on persistent shared storage; otherwise use an external database for multi-service production.

For the bot service, disable the HTTP health check or configure its start command separately; the bot is a long-running Telegram polling process, not an HTTP server.

## VPS: recommended Docker deployment

Docker Compose keeps the API and bot alive with `restart: unless-stopped` and stores SQLite in a named volume.

```bash
git clone https://github.com/TEAM-ISTKHAR/Api.git /opt/hellapi
cd /opt/hellapi
cp .env.example .env
nano .env

docker compose up -d --build
docker compose ps
curl http://127.0.0.1:8000/healthz
```

Put `deploy/nginx.conf` in `/etc/nginx/sites-available/hellapi`, replace `api.example.com`, enable it, and issue TLS with Certbot:

```bash
sudo ln -s /etc/nginx/sites-available/hellapi /etc/nginx/sites-enabled/hellapi
sudo nginx -t && sudo systemctl reload nginx
sudo certbot --nginx -d api.example.com
```

## VPS: systemd deployment

For a non-Docker install:

```bash
sudo useradd --system --home /opt/hellapi --shell /usr/sbin/nologin hellapi
sudo mkdir -p /opt/hellapi /var/lib/hellapi
sudo chown -R hellapi:hellapi /opt/hellapi /var/lib/hellapi

cd /opt/hellapi
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
# Edit .env and set TELEGRAM_BOT_TOKEN, ADMIN_IDS, and ADMIN_KEY

sudo cp deploy/hellapi-api.service deploy/hellapi-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now hellapi-api hellapi-bot
sudo systemctl status hellapi-api hellapi-bot
```

The service files expect the checkout at `/opt/hellapi` and persistent SQLite at `/var/lib/hellapi/bot_data.db`.

## Telegram bot commands

- `/start` — create or show a free key
- `/mykey` — show key and plan
- `/getfile` — download the music-bot `youtube.py` helper
- `/revokekey` — revoke a leaked key and issue a replacement
- `/status` — check API health
- `/admin` — admin panel
- `/approve`, `/reject`, `/upgrade`, `/payments`, `/reply` — admin-only workflows

The included `youtube.py` helper is intended to be copied into another music bot. Its consumer project needs `aiohttp` and the dependencies used by that bot (including Pyrogram if the `YouTubeAPI` class is used).

## Production notes

- Keep one Uvicorn worker when using the bundled SQLite database.
- Use persistent disk or an external database for more than one service instance.
- Stream URLs are not permanent download links.
- Add `PROXY_LIST` only with proxies you own or are authorized to use.
- Review YouTube's terms and local copyright law before operating the service commercially.
- Rotate `ADMIN_KEY` and Telegram credentials if they are ever exposed.

## License

No license is currently declared. Add a license before distributing this project publicly.