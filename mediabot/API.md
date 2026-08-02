# REST API

Base URL: `https://<host>/api/v1` · Interactive docs: `/docs` (when
`API__DOCS_ENABLED=true`).

## Authentication

Two mechanisms:

| Method | Header | Use case |
| --- | --- | --- |
| JWT | `Authorization: Bearer <access_token>` | Interactive/staff sessions |
| API key | `X-API-Key: <key>` | Machine-to-machine (maps to the `admin` role) |

Roles are ordered `support < moderator < admin < owner`; each endpoint states
its minimum role.

### `POST /auth/login`

```json
{ "username": "admin", "password": "secret" }
```

```json
{
  "access_token": "eyJhbGciOi…",
  "refresh_token": "eyJhbGciOi…",
  "token_type": "bearer",
  "expires_in": 1800
}
```

Five consecutive failures lock the account for 15 minutes; every attempt is
written to the audit log.

### `POST /auth/refresh`

```json
{ "refresh_token": "eyJhbGciOi…" }
```

Returns a fresh pair. Refresh tokens are rejected where an access token is
expected and vice versa.

---

## Users

### `GET /users` — *support*

Query: `query` (id, `@username` or name), `limit` (≤100), `offset`.

```json
{
  "items": [
    {
      "id": 123456789,
      "username": "alice",
      "full_name": "Alice",
      "language": "en",
      "tier": "premium",
      "status": "active",
      "balance": 340,
      "total_downloads": 128,
      "total_bytes": 8123456789,
      "referrals": 4,
      "created_at": "2026-05-02T10:11:12Z",
      "last_seen_at": "2026-07-31T08:00:00Z"
    }
  ],
  "total": 1,
  "limit": 25,
  "offset": 0
}
```

### `GET /users/{user_id}` — *support*

Single user, same shape as an item above.

### `GET /users/{user_id}/limits` — *support*

Effective limits, i.e. the tier policy merged with admin/promo/shop overrides.

```json
{
  "tier": "free",
  "daily_downloads": 15,
  "used_today": 3,
  "remaining": 12,
  "max_file_size_bytes": 262144000,
  "max_duration_seconds": 1200,
  "max_concurrent_jobs": 1,
  "max_video_quality": "720p",
  "max_audio_quality": "192"
}
```

### `GET /users/{user_id}/subscription` — *support*

```json
{
  "tier": "vip",
  "active": true,
  "expires_at": "2026-08-30T12:00:00Z",
  "auto_renew": false,
  "features": ["all_platforms", "no_ads", "4k_quality", "lossless_audio"]
}
```

### `POST /users/{user_id}/subscription` — *admin*

```json
{ "user_id": 123456789, "tier": "premium", "days": 30 }
```

Grants or extends a subscription; the action is audit-logged. Extending the
same tier moves the expiry forward instead of creating an overlapping period.

### `GET /users/{user_id}/statistics` — *support*

Profile aggregates: downloads, traffic, rank, streak, referrals, achievements,
favourite platform.

---

## Statistics

### `GET /statistics/dashboard` — *support*

DAU/WAU/MAU, new users, downloads (today/week/failed), traffic, average file
size, revenue (today/month), queue size, active jobs, `by_platform`, `by_hour`
and live CPU/RAM/disk usage.

### `GET /statistics/history?days=30` — *support*

One entry per day: `new_users`, `active_users`, `downloads`,
`downloads_failed`, `bytes`, `revenue`.

---

## Downloads

### `POST /downloads` — *support*

```json
{
  "user_id": 123456789,
  "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
  "kind": "video",
  "video_quality": "1080p",
  "container": "mp4"
}
```

```json
{
  "download_id": 4242,
  "status": "queued",
  "queued": true,
  "position": 3,
  "estimated_wait_seconds": 135,
  "cached": false
}
```

The same admission control as the bot applies: quota, platform access, quality
ceiling, duration and size. `cached: true` means the artefact already existed
and was re-sent by `file_id` at no cost.

### `GET /downloads/{download_id}` — *support*

### `DELETE /downloads/{download_id}` — *support*

Revokes the Celery task, marks the job cancelled and removes its files.

### `GET /users/{user_id}/history` — *support*

Query: `page`, `page_size` (≤100), `query`, `kind` (`video`/`audio`).

### `GET /media/info?url=…` — *support*

Inspects a link without downloading: title, uploader, duration, thumbnail,
available qualities and estimated sizes.

---

## Promo codes

### `GET /promo` — *support*

Query: `status`, `limit`, `offset`.

### `POST /promo` — *admin*

```json
{
  "promo_type": "premium_days",
  "value": 7,
  "code": "SUMMER7",
  "max_activations": 500,
  "per_user_limit": 1,
  "expires_in_days": 30,
  "new_users_only": true,
  "campaign": "summer-2026"
}
```

Types: `percent_discount`, `fixed_discount`, `premium_days`, `vip_days`,
`lifetime`, `coins`, `limit_boost`. Omitting `code` generates a random one.

### `DELETE /promo/{code}` — *admin*

Disables a code (it is never deleted, so activations stay auditable).

### `GET /promo/{code}/statistics` — *support*

Activation count, coins/days granted and the 20 most recent activations.

### `POST /promo/redeem` — *support*

```json
{ "user_id": 123456789, "code": "SUMMER7" }
```

Applies the reward (coins, limits) and grants any subscription days.

---

## Operations

### `GET /health`

Unauthenticated. Reports database, Redis and FFmpeg availability, queue size,
active jobs, free disk and memory usage. `status` is `ok` or `degraded`.

### `GET /metrics`

Prometheus exposition format. nginx restricts it to the internal network.

---

## Errors

Every error is a structured JSON body:

```json
{
  "error": "daily_quota_exceeded",
  "message": "Daily download limit reached",
  "details": {}
}
```

| HTTP | Codes |
| --- | --- |
| 401 | `unauthenticated` |
| 402 | `payment_failed`, `insufficient_funds` |
| 403 | `permission_denied`, `user_banned`, `quality_not_allowed`, `platform_not_allowed` |
| 404 | `not_found`, `promo_not_found` |
| 409 | `conflict`, `job_cancelled` |
| 413 | `file_too_large` |
| 422 | `validation_error`, `unsupported_url`, `unsafe_url` |
| 429 | `rate_limited`, `flood_detected`, `daily_quota_exceeded` |
| 502 | `download_failed`, `extraction_failed`, `geo_restricted`, `private_content` |
| 503 | `queue_full`, `payment_provider_unavailable` |

## Rate limits

nginx applies 20 req/s (burst 40) to `/api/`, 5 req/s to `/admin` and 60 req/s
to the Telegram webhook. Per-user limits inside the application are enforced by
the security service and reported through the `mediabot_rate_limit_hits_total`
metric.

## Example

```bash
TOKEN=$(curl -sS -X POST https://bot.example.com/api/v1/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"secret"}' | jq -r .access_token)

curl -sS https://bot.example.com/api/v1/statistics/dashboard \
  -H "Authorization: Bearer $TOKEN" | jq
```
