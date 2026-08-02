# Database

PostgreSQL 15+, SQLAlchemy 2.0 (async, fully typed), Alembic migrations.

## Conventions

* **Naming** — every constraint is named through a metadata naming convention
  (`pk_`, `fk_`, `uq_`, `ix_`, `ck_`), so migrations are reproducible and
  down-migrations reliable.
* **Timestamps** — `created_at` / `updated_at` on every table, maintained by the
  database; all datetime columns use a `UtcDateTime` type that guarantees
  timezone-aware UTC values in Python.
* **Enums** — stored as `VARCHAR` with a `CHECK` constraint, not native
  PostgreSQL enums: adding a value never rewrites a table. Values (not names)
  are persisted, so the database matches the API representation.
* **Keys** — surrogate `BIGINT` identity keys, except `users.id`, which *is* the
  Telegram user id (globally unique and stable).
* **Deletes** — `ON DELETE CASCADE` for owned data, `SET NULL` for references
  that must survive (history keeps working after a job is purged).

## Tables

| Table | Purpose | Key relationships |
| --- | --- | --- |
| `users` | Telegram users, preferences, moderation state, denormalised counters | self-FK `referrer_id` |
| `user_limits` | Per-user overrides on top of the tier policy | 1:1 `users` |
| `wallets` | Coin balance | 1:1 `users` |
| `transactions` | Immutable coin ledger | N:1 `wallets`, `users` |
| `referrals` | Confirmed invitations and payouts | N:1 `users` (inviter/invitee) |
| `achievements` | Unlocked achievements | N:1 `users`, unique `(user_id, code)` |
| `languages` | Enabled interface locales | — |
| `downloads` | Download jobs, the operational core | N:1 `users` |
| `download_queue` | Persistent waiting queue | 1:1 `downloads` |
| `download_history` | Denormalised, searchable user history | N:1 `users`, `downloads` |
| `favorites` | Saved links | N:1 `users`, `favorite_collections` |
| `favorite_collections` | Folders for favourites | N:1 `users` |
| `subscriptions` | Granted subscription periods | N:1 `users`, `payments` |
| `payments` | Payment attempts across all rails | N:1 `users` |
| `promo_codes` | Redeemable codes | — |
| `promo_activations` | Redemption audit trail | N:1 `promo_codes`, `users` |
| `notifications` | Outbox for messages and broadcasts | N:1 `users` |
| `admins` | Staff accounts (bot + web panel) | — |
| `audit_logs` | Append-only record of privileged actions | — |
| `error_logs` | Searchable failures for the admin panel | — |
| `settings` | Runtime-editable JSON settings | — |
| `statistics` | Pre-aggregated daily analytics | unique `day` |

## Relationships

```
users 1─1 wallets ──1─N transactions
users 1─1 user_limits
users 1─N downloads ──1─1 download_queue
users 1─N download_history
users 1─N favorites ──N─1 favorite_collections
users 1─N subscriptions ──N─1 payments
users 1─N promo_activations ──N─1 promo_codes
users 1─N achievements
users 1─N notifications
users 1─N referrals (as inviter and as invitee)
```

## Why `downloads` and `download_history` are separate

`downloads` is operational: it carries worker state, file paths, task ids and
error details, and its rows are pruned once the artefacts expire.
`download_history` is user-facing: every display field is inlined so search and
paging never join, rows survive job clean-up, and free-tier history can be
pruned without touching operational data.

## Indexes

| Index | Table | Why |
| --- | --- | --- |
| `ix_downloads_user_created` | `downloads` | Quota counting and per-user listings |
| `ix_downloads_status_created` | `downloads` | Reclaiming stale jobs, analytics |
| `ix_downloads_platform_created` | `downloads` | Platform breakdown |
| `ix_downloads_cache_key` | `downloads` | Instant-cache lookup before every job |
| `ix_download_queue_priority_enqueued` | `download_queue` | Queue ordering |
| `ix_history_user_created`, `ix_history_user_kind` | `download_history` | History paging and filtering |
| `ix_history_title` | `download_history` | Title search |
| `ix_users_status_last_seen` | `users` | DAU/MAU and broadcast targeting |
| `ix_subscriptions_expires_at` | `subscriptions` | Expiry sweep and reminders |
| `ix_payments_status_created` | `payments` | Revenue reporting, pending polling |
| `ix_transactions_wallet_created` | `transactions` | Wallet statements |
| `ix_notifications_status_scheduled` | `notifications` | Outbox dispatch |
| `ix_audit_logs_action_created` | `audit_logs` | Audit filtering |

## Consistency guarantees

* **Wallet** — the balance and the ledger row are written inside one
  transaction; they cannot diverge.
* **Promo codes** — activation, counter increment and status recomputation
  happen in one transaction, so a code can never exceed its activation limit.
* **Payments** — `invoice_payload` is unique per attempt and confirmation is
  idempotent, so a retried `successful_payment` update never grants twice.
* **Referrals** — `invitee_id` is unique, which makes the payout idempotent.
* **Queue** — `download_queue.download_id` is unique, so a job cannot be queued
  twice.

## Migrations

```bash
alembic upgrade head              # apply
alembic downgrade -1              # roll back one revision
alembic revision --autogenerate -m "add table x"
alembic history --verbose
alembic upgrade head --sql        # emit SQL for a review/DBA workflow
```

`migrations/env.py` reads the DSN from the application settings, so migrations
always target the same database the application uses. In Docker Compose the
one-shot `migrate` service applies them once, and every other service waits for
it — replicas never race on Alembic.

## Maintenance

```sql
-- Largest tables
SELECT relname, pg_size_pretty(pg_total_relation_size(relid)) AS size
FROM pg_catalog.pg_statio_user_tables ORDER BY pg_total_relation_size(relid) DESC LIMIT 10;

-- Unused indexes
SELECT relname, indexrelname, idx_scan FROM pg_stat_user_indexes WHERE idx_scan = 0;

-- Purge download rows older than a year (history is kept)
DELETE FROM downloads WHERE created_at < now() - interval '1 year';
```

For a busy deployment, partition `downloads` and `download_history` by month and
schedule `VACUUM (ANALYZE)` outside peak hours.
