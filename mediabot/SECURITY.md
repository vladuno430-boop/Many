# Security

## Threat model

| Threat | Vector | Control |
| --- | --- | --- |
| SSRF | User pastes `http://169.254.169.254/…` or an internal hostname | `validate_public_url` rejects non-http(s) schemes, credentials in the netloc, blocked hostnames and anything resolving to a private/loopback/link-local address |
| Command injection | Malicious media title reaching a shell | FFmpeg is invoked with an argument list (never a shell string); filenames pass through `sanitize_filename` |
| Path traversal | Title containing `../` | `sanitize_filename` strips separators and `..`; each job writes into its own directory |
| XSS | Media title rendered in a caption or the panel | `escape_html` on every user-controlled string; Jinja2 autoescaping; strict CSP |
| SQL injection | Search queries, admin filters | SQLAlchemy parameter binding everywhere; no string-built SQL |
| Spam / flooding | Scripted request bursts | Flood detection with temporary bans, per-minute rate limits, CAPTCHA after repeated violations |
| Quota abuse | Free users bypassing limits | Server-side admission control in the application layer — the bot UI is never the enforcement point |
| Payment fraud | Replayed `successful_payment` | Unique per-attempt payload, idempotent confirmation, amount verification |
| Privilege escalation | Non-staff calling admin endpoints | Role hierarchy checked in the service layer *and* the API dependency |
| Credential theft | Stolen panel cookie | `HttpOnly`, `SameSite=Strict`, `Secure` in production, short-lived JWT, account lockout |
| Malicious payload | Server returning HTML labelled `.mp4` | MIME/magic-byte validation and size checks before delivery |
| Resource exhaustion | Huge files or endless streams | Absolute size ceiling, job timeouts, memory-capped workers, disk janitor |

## Controls

### Authentication and authorisation

* JWT (HS256) with `jti`, typed access/refresh tokens and TTLs from settings.
* bcrypt password hashing (cost 12) with a SHA-256 pre-hash, so long
  passphrases keep their entropy instead of being silently truncated at 72 bytes.
* Account lockout after 5 failed logins for 15 minutes.
* Static API keys compared in constant time, mapped to the `admin` role.
* Roles: `support < moderator < admin < owner`; every privileged action is
  checked in `AdminService` and recorded in `audit_logs`.

### Input validation

* Pydantic models validate every API request.
* URLs pass `validate_public_url` before any network access.
* Callback data uses aiogram's typed `CallbackData` factories — no raw string
  parsing, no trust in client-supplied payloads.
* Uploaded artefacts are validated by size and magic bytes before delivery.

### Abuse prevention

| Layer | Default | Setting |
| --- | --- | --- |
| Flood control | 8 events / 5 s → 5-minute ban | `SECURITY__FLOOD_*` |
| Rate limit | 30 requests / minute | `SECURITY__RATE_LIMIT_PER_MINUTE` |
| CAPTCHA | after 3 violations, trusted 24 h | `SECURITY__CAPTCHA_*` |
| nginx | 20 r/s API, 5 r/s admin, 60 r/s webhook | `deploy/nginx/nginx.conf` |

### Secrets

* Configuration is the only place that reads the environment.
* Provider payloads are encrypted at rest with Fernet
  (`SECURITY__ENCRYPTION_KEY`).
* `SecretStr` prevents secrets from appearing in logs or tracebacks.
* Booting with `APP__ENV=production` and placeholder secrets is refused
  outright — a misconfigured deployment fails fast instead of running exposed.

### Web panel

* Server-rendered, **no external assets** — charts are inline SVG, so the CSP
  can forbid third-party scripts entirely.
* Headers on every response: `X-Frame-Options: DENY`, `X-Content-Type-Options`,
  `Referrer-Policy`, `Permissions-Policy`, strict `Content-Security-Policy`
  with `frame-ancestors 'none'`, plus HSTS in production.
* Session cookie is `HttpOnly`, `SameSite=Strict`, `Secure` in production and
  scoped to `/admin`; `SameSite=Strict` is what blocks cross-site form posts
  (CSRF) against the panel's POST endpoints.
* CORS is disabled unless `API__CORS_ORIGINS` is set explicitly.

### Container hardening

* Runs as an unprivileged user (uid 1000) — media processing never has root.
* Multi-stage build: compilers stay in the builder stage.
* `tini` reaps FFmpeg zombies; workers recycle every 50 tasks to bound memory.
* Only nginx publishes ports; everything else stays on the internal network.

### Data protection

* Only the data needed for the product is stored: Telegram id, profile fields,
  download metadata. No message content, no media is retained after delivery.
* Artefacts are deleted once Telegram holds the file (`file_id` re-send).
* Passwords, tokens and provider payloads are never logged.

## Hardening checklist

- [ ] `APP__ENV=production`, `APP__DEBUG=false`
- [ ] `SECURITY__JWT_SECRET` ≥ 32 random characters
- [ ] `SECURITY__ENCRYPTION_KEY` set to a generated Fernet key
- [ ] `API__ADMIN_PASSWORD` changed from the default
- [ ] `DB__PASSWORD` and Redis password set
- [ ] TLS certificate installed, HTTP redirects to HTTPS
- [ ] `/admin` restricted by IP allow-list or VPN
- [ ] `API__DOCS_ENABLED=false` if the API is not public
- [ ] Firewall allows only 22/80/443
- [ ] Automated database backups verified by a test restore
- [ ] Prometheus alerts routed to a channel someone reads
- [ ] Log retention configured (`OBSERVABILITY__LOG_RETENTION`)

## Logging and audit

Separate channels — `security.log` (flood, CAPTCHA, auth failures),
`admin.log` (staff actions), `payments.log`, `download.log` — plus the
append-only `audit_logs` table with actor, action, target, IP and user agent.

## Incident response

1. **Contain** — `docker compose stop bot api` halts user-facing traffic while
   workers finish; ban the offending accounts through the panel.
2. **Assess** — check `security.log`, `audit_logs` and `error_logs`; the
   dashboard shows the failure-rate and queue anomalies.
3. **Rotate** — regenerate `SECURITY__JWT_SECRET` (invalidates every session),
   the encryption key, database and provider credentials; restart.
4. **Recover** — restore from the latest verified backup if data was altered.
5. **Learn** — add a regression test; the suite already covers every URL-safety
   and limit rule, so a fix belongs there.

## Reporting a vulnerability

Do not open a public issue. Contact the maintainers privately with a
description, reproduction steps and impact assessment. Expect an
acknowledgement within 72 hours.
