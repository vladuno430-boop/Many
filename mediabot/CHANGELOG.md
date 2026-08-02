# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and
the project adheres to [Semantic Versioning](https://semver.org/).

## [1.0.0] — 2026-07-31

First production release.

### Added

**Downloading**
- Automatic link detection with platform recognition for 35+ named platforms and
  a generic fallback covering every other site supported by yt-dlp.
- Media preview before downloading: title, thumbnail, author, duration,
  estimated size, available formats and qualities.
- Video output in MP4, MKV and WEBM from 144p to 2160p, plus "best" and
  audio-only.
- Audio output in MP3, M4A, FLAC, WAV and OGG at 128–320 kbps or lossless.
- Instant re-send of previously produced files through Telegram `file_id`
  caching.
- Thumbnail extraction, metadata embedding and MIME validation of every artefact.

**Queue and workers**
- Persistent, priority-ordered download queue with position and ETA.
- Celery workers on three queues (downloads, notifications, maintenance) with
  late acknowledgement, retries with exponential back-off and worker recycling.
- Live progress published from the worker to Redis and rendered in the chat.
- Automatic reclamation of jobs abandoned by a crashed worker.

**Monetisation**
- Free, Premium, VIP and Lifetime tiers, each with its own limits, priority,
  quality ceiling and platform access.
- Payments via Telegram Stars, cards (any Telegram payment provider), a
  pluggable crypto gateway and the internal coin balance.
- Promo codes: one-shot, multi-use, timed, per-user limited, percentage and
  fixed discounts, premium/VIP days, lifetime, coins and limit boosts.
- Referral programme with coin payouts for both sides and premium days every
  five invites.
- Coin economy: daily bonus with streaks and a reward calendar, coin shop,
  achievements with rewards.

**User experience**
- Profile with full statistics, rank and streaks.
- Searchable, filterable download history with one-tap re-download.
- Favourites organised in collections.
- Five interface languages: Russian, English, German, Spanish, French.
- Notification outbox with per-category opt-outs and resumable broadcasts.

**Administration**
- In-chat admin panel: statistics, user search, bans/mutes, subscription grants,
  balance adjustments, promo management, queue control, error and audit logs.
- Server-rendered dark-themed web panel with inline SVG charts and no external
  assets.
- Append-only audit log of every privileged action.

**Platform**
- Clean Architecture layering with a single composition root and full type
  annotations.
- PostgreSQL schema with 22 tables, deterministic constraint naming and an
  initial Alembic migration.
- Redis caching for media metadata, users and limits; distributed locks for
  periodic jobs.
- REST API with JWT and API-key authentication.
- Prometheus metrics, Grafana dashboard, alert rules and health checks.
- Per-channel structured logging (info, warning, error, download, payments,
  security, admin, queue, api).
- Docker multi-stage build, Docker Compose topology, nginx with TLS and rate
  limiting.
- 170+ unit and integration tests running fully offline against SQLite and
  fakeredis.

### Security
- SSRF-safe URL validation, filename sanitisation and HTML escaping.
- Flood control, rate limiting and CAPTCHA after repeated violations.
- bcrypt password hashing with a SHA-256 pre-hash, JWT sessions and account
  lockout.
- Fernet encryption for stored provider payloads.
- Strict security headers, CSP without third-party sources, `SameSite=Strict`
  session cookie.
- Refusal to start in production with placeholder secrets.
