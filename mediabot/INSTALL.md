# Installation

## 1. Requirements

| Component | Minimum | Recommended |
| --- | --- | --- |
| CPU | 2 cores | 4+ cores (FFmpeg is CPU bound) |
| RAM | 2 GB | 8 GB |
| Disk | 20 GB SSD | 100 GB SSD (media is transient but bursty) |
| OS | any Linux with Docker | Ubuntu 22.04 / Debian 12 |

Software:

* Docker 24+ and the Compose plugin — the only requirement for the container path;
* or, for a bare-metal install: Python 3.12+, PostgreSQL 15+, Redis 7+, FFmpeg 6+.

## 2. Get a bot token

1. Talk to [@BotFather](https://t.me/BotFather), send `/newbot`.
2. Save the token into `TELEGRAM__BOT_TOKEN`.
3. Send `/setprivacy` → *Disable* if you want the bot to see links in groups.
4. Note your own numeric id (via [@userinfobot](https://t.me/userinfobot)) and
   put it into `TELEGRAM__ROOT_ADMIN_IDS` — that account becomes the owner.

## 3. Configure

```bash
cp .env.example .env
```

Generate the secrets:

```bash
openssl rand -hex 32                                    # SECURITY__JWT_SECRET
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
openssl rand -base64 24                                 # DB__PASSWORD, API__ADMIN_PASSWORD
```

Configuration reference (every key maps to a nested settings model; the
delimiter is a double underscore):

| Group | Key | Meaning |
| --- | --- | --- |
| `APP__` | `ENV` | `local` / `staging` / `production` — production refuses insecure secrets |
| | `STORAGE_DIR`, `TEMP_DIR` | Working directories, shared between bot and workers |
| | `ARTIFACT_TTL_MINUTES` | How long finished files stay on disk |
| `TELEGRAM__` | `BOT_TOKEN`, `BOT_USERNAME` | Credentials and the username used in referral links |
| | `ROOT_ADMIN_IDS` | Comma-separated owner ids |
| | `WEBHOOK_URL`, `WEBHOOK_SECRET` | Leave empty for long polling |
| | `MAX_UPLOAD_BYTES` | 50 MiB for the cloud API, 2 GiB with a local Bot API server |
| `DB__` | `HOST`…`PASSWORD` | PostgreSQL connection |
| | `POOL_SIZE`, `MAX_OVERFLOW` | Connection pool sizing |
| `REDIS__` | `HOST`, `PORT`, `DB_*` | Separate logical databases for cache, broker, results, FSM |
| `SECURITY__` | `JWT_SECRET`, `ENCRYPTION_KEY` | Token signing and secret encryption |
| | `RATE_LIMIT_PER_MINUTE`, `FLOOD_*` | Abuse controls |
| | `CAPTCHA_ENABLED`, `CAPTCHA_AFTER_VIOLATIONS` | Human verification |
| `DOWNLOADER__` | `MAX_CONCURRENT_JOBS` | Parallel jobs per worker |
| | `JOB_TIMEOUT_SECONDS`, `MAX_RETRIES` | Job execution envelope |
| | `PROXY`, `COOKIES_FILE` | Optional egress proxy and cookie jar |
| `PAYMENTS__` | `STARS_ENABLED`, `CARD_*`, `CRYPTO_*` | Enable the rails you actually use |
| `API__` | `PORT`, `DOCS_ENABLED`, `CORS_ORIGINS` | REST API |
| | `ADMIN_USERNAME`, `ADMIN_PASSWORD` | Bootstrap web-panel account |
| `OBSERVABILITY__` | `LOG_LEVEL`, `LOG_DIR`, `JSON_LOGS` | Logging |
| `ECONOMY__` | `REFERRAL_*`, `DAILY_*`, `COINS_*` | Economy tuning |

## 4. Start with Docker (recommended)

```bash
docker compose build
docker compose up -d
docker compose ps
docker compose logs -f bot worker
```

What starts:

| Service | Role |
| --- | --- |
| `postgres`, `redis` | Data stores |
| `migrate` | One-shot Alembic upgrade; every other service waits for it |
| `bot` | Telegram bot (polling or webhook) |
| `api` | REST API + admin panel on port 8000 (behind nginx) |
| `worker` | Celery workers — scale with `--scale worker=N` |
| `beat`, `scheduler` | Periodic tasks (statistics, expiry, clean-up) |
| `nginx` | TLS termination, rate limiting, routing |
| `prometheus`, `grafana` | Metrics and dashboards |
| `flower` | Celery monitoring |

## 5. Bare-metal installation

```bash
sudo apt update
sudo apt install -y python3.12 python3.12-venv postgresql redis-server ffmpeg libmagic1

sudo -u postgres psql -c "CREATE USER mediabot WITH PASSWORD 'change-me';"
sudo -u postgres psql -c "CREATE DATABASE mediabot OWNER mediabot;"

python3.12 -m venv /opt/mediabot/venv
/opt/mediabot/venv/bin/pip install -e ".[dev]"
/opt/mediabot/venv/bin/alembic upgrade head
```

Run the processes under systemd (unit files are listed in `DEPLOY.md`):

```bash
/opt/mediabot/venv/bin/python -m mediabot bot
/opt/mediabot/venv/bin/python -m mediabot api
/opt/mediabot/venv/bin/python -m mediabot scheduler
/opt/mediabot/venv/bin/celery -A mediabot.infrastructure.queue worker -l info
/opt/mediabot/venv/bin/celery -A mediabot.infrastructure.queue beat -l info
```

## 6. Verify

```bash
curl -fsS http://localhost:8000/api/v1/health | jq
```

Expected: `{"status": "ok", "database": true, "redis": true, "ffmpeg": true, …}`.

Then open Telegram, send `/start` to the bot and paste a YouTube link. Sign in
to the panel at `http://localhost:8000/admin` with `API__ADMIN_USERNAME` /
`API__ADMIN_PASSWORD`.

## 7. Optional: raise the upload limit

The Telegram cloud API caps bot uploads at 50 MiB. Running a
[local Bot API server](https://github.com/tdlib/telegram-bot-api) raises it to
2 GiB:

```env
TELEGRAM__API_SERVER=http://telegram-bot-api:8081
TELEGRAM__MAX_UPLOAD_BYTES=2147483648
```

## 8. Common problems

| Symptom | Cause | Fix |
| --- | --- | --- |
| `Insecure production configuration` on start | placeholder secrets with `APP__ENV=production` | generate real secrets |
| `ffmpeg: false` in `/health` | FFmpeg missing | install it or fix `DOWNLOADER__FFMPEG_PATH` |
| Jobs stay `queued` | no worker running | start `worker`, check the broker DSN |
| `file_too_large` on big videos | Telegram's 50 MiB cap | use a local Bot API server, or offer a lower quality |
| `private_content` / `geo_restricted` | source restriction | configure `DOWNLOADER__PROXY` or `COOKIES_FILE` |
