# ⚡ MediaBot

Production-ready Telegram bot that downloads video and audio from **YouTube,
TikTok, Instagram, VK, Rutube, Twitter/X, SoundCloud, Twitch, Bilibili, Reddit,
Vimeo, Dailymotion** and every other site supported by `yt-dlp` — with
subscriptions, promo codes, a referral programme, an internal coin economy, a
REST API and a dark-themed admin panel.

```
Telegram ──► aiogram bot ──► admission control ──► Redis queue ──► Celery workers
                  │                  │                                   │
                  ▼                  ▼                                   ▼
             PostgreSQL         limits/quotas                    yt-dlp + FFmpeg
                  ▲                                                      │
                  └──────────────── REST API + admin panel ◄─────────────┘
```

---

## Feature overview

| Area | What it does |
| --- | --- |
| **Downloading** | Automatic link detection, 1000+ sites via yt-dlp, video (MP4/MKV/WEBM) and audio (MP3/M4A/FLAC/WAV/OGG) |
| **Quality** | 144p → 4K, "best" and audio-only; 128–320 kbps and lossless audio |
| **Preview** | Title, thumbnail, duration, author, estimated size and available formats before downloading |
| **Queue** | Persistent, priority-ordered queue with position, ETA and live progress |
| **Limits** | Per-tier daily quota, file size, duration, concurrency, speed and platform access |
| **Subscriptions** | Free · Premium · VIP · Lifetime, each with its own policy |
| **Payments** | Telegram Stars, cards (any Telegram payment provider), crypto gateway, internal balance |
| **Promo codes** | One-shot/multi-use, timed, percentage or fixed discounts, premium/VIP days, coins, limit boosts |
| **Referrals** | Personal link, coin payouts for both sides, premium days every 5 invites |
| **Economy** | Coins, daily bonus with streaks and a reward calendar, coin shop |
| **Gamification** | 16 achievements with rewards, leaderboard and user rank |
| **History** | Searchable, filterable, one-tap re-download from Telegram's own storage |
| **Favourites** | Saved links organised in collections |
| **i18n** | Russian, English, German, Spanish, French |
| **Admin** | In-chat panel + web panel: users, bans, grants, limits, promo codes, broadcasts, queue, logs |
| **Analytics** | DAU/MAU, new users, downloads by day/hour/platform, revenue, average size, server load |
| **API** | JWT-secured REST API for users, stats, downloads, history, promo codes and subscriptions |
| **Security** | Rate limiting, flood control, CAPTCHA, SSRF-safe URL validation, MIME checks, JWT, encrypted secrets |
| **Ops** | Docker Compose, nginx, Prometheus, Grafana, health checks, structured logs, automatic clean-up |

---

## Architecture

Clean Architecture with dependencies pointing inwards only:

```
src/mediabot/
├── core/             configuration, logging, security, exceptions, DI container
├── domain/           enums, value objects, policies and pure business rules
│   └── services/     limits, economy, promo, queue, format selection  (no I/O)
├── application/      use-case orchestration
│   └── services/     user, media, download, economy, payment, admin, analytics …
├── infrastructure/   adapters
│   ├── db/           SQLAlchemy models, repositories, Unit of Work
│   ├── cache/        Redis cache, rate limiter, locks, progress publisher
│   ├── downloader/   yt-dlp adapter, FFmpeg service, MIME validation
│   ├── payments/     Stars / card / crypto / balance gateways
│   ├── queue/        Celery app, tasks, dispatcher abstraction
│   └── notifications/ Telegram delivery and notification sender
└── presentation/     delivery mechanisms
    ├── bot/          aiogram routers, keyboards, middlewares, i18n
    ├── api/          FastAPI routers, schemas, dependencies
    └── web/          server-rendered admin panel (Jinja2, no external assets)
```

Design principles applied throughout: **SOLID**, **Repository Pattern**,
**Service Layer**, **Dependency Injection** (one composition root in
`core/container.py`), full type annotations and `mypy --strict`.

---

## Two ways to run it

| Mode | Topology | Use it for |
| --- | --- | --- |
| `distributed` (default) | PostgreSQL + Redis + Celery workers + nginx, via Docker Compose | Production on a VPS |
| `standalone` | One process: SQLite, in-memory cache, inline download worker | A phone (Termux), a Raspberry Pi, or local development |

The mode is a single setting (`APP__RUNTIME_MODE`); the application code is the
same in both — the container swaps the infrastructure adapters behind the
interfaces the services already depend on.

## Quick start — server (Docker)

```bash
git clone <your-repo> && cd mediabot
cp .env.example .env          # fill in TELEGRAM__BOT_TOKEN and the secrets
docker compose up -d --build
docker compose logs -f bot
```

The admin panel is then available at `https://<your-domain>/admin` and the API
at `https://<your-domain>/api/v1`.

## Quick start — Android (Termux)

```bash
pkg install -y git
git clone <your-repo> && cd mediabot
bash termux/install.sh        # installs everything and asks for the bot token
bash termux/run.sh            # starts the bot
```

No PostgreSQL, no Redis, no Celery and no compiler required — the phone runs
the same code as the server. Full guide: [TERMUX.md](TERMUX.md) (in Russian).

## Local development without Docker

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"       # server profile + test tooling
alembic upgrade head
python -m mediabot bot        # or: api | scheduler
celery -A mediabot.infrastructure.queue worker -l info
```

Install profiles (`pip install -e ".[<profile>]"`):

| Profile | Adds | When |
| --- | --- | --- |
| *(none)* | bot core only | Minimal bot |
| `standalone` | FastAPI + admin panel | Phone / single-process host |
| `server` | PostgreSQL, Celery, API, bcrypt, psutil | Production |
| `dev` | `server` + pytest, mypy, ruff | Development |

See [INSTALL.md](INSTALL.md) for the full setup and [DEPLOY.md](DEPLOY.md) for
production deployment, scaling and backups.

---

## Documentation

| Document | Contents |
| --- | --- |
| [INSTALL.md](INSTALL.md) | Requirements, local setup, configuration reference |
| [TERMUX.md](TERMUX.md) | Running the bot on an Android phone (Russian) |
| [DEPLOY.md](DEPLOY.md) | VPS deployment, TLS, scaling, backups, upgrades, troubleshooting |
| [API.md](API.md) | REST API reference with request/response examples |
| [DATABASE.md](DATABASE.md) | Schema, relationships, indexes and migration workflow |
| [SECURITY.md](SECURITY.md) | Threat model, controls, hardening checklist, incident response |
| [CHANGELOG.md](CHANGELOG.md) | Release history |

---

## Testing and quality

```bash
pytest                      # 220+ unit and integration tests (SQLite + fakeredis)
pytest -m unit              # fast, pure-domain tests only
ruff check src tests        # linting
ruff format --check src     # formatting
mypy src                    # strict type checking
```

The whole suite is hermetic: no PostgreSQL, no Redis and no network access are
required, because the test-suite substitutes SQLite, `fakeredis` and the
in-memory task dispatcher through the same interfaces production uses.

---

## Legal note

MediaBot downloads only publicly accessible media and never circumvents DRM.
Streaming catalogues (Spotify, Deezer, Yandex Music) are treated as
**metadata-only**: the bot resolves the track information and refuses to
produce media bytes for them. Operators are responsible for complying with the
terms of service of the platforms they enable and with local copyright law.

## License

MIT — see the license header in `pyproject.toml`.
