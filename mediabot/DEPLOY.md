# Deployment

Target: a single VPS running Docker Compose, able to scale horizontally later
without an architecture change.

## 1. Prepare the server

```bash
sudo apt update && sudo apt upgrade -y
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker "$USER"

sudo ufw default deny incoming
sudo ufw allow OpenSSH
sudo ufw allow 80,443/tcp
sudo ufw enable

# Swap protects against OOM during 4K merges.
sudo fallocate -l 4G /swapfile && sudo chmod 600 /swapfile
sudo mkswap /swapfile && sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```

Only ports 80/443 are exposed. PostgreSQL, Redis, the API, Prometheus and
Grafana stay on the internal Compose network.

## 2. Deploy

```bash
git clone <your-repo> /opt/mediabot && cd /opt/mediabot
cp .env.example .env && $EDITOR .env      # real secrets, APP__ENV=production
docker compose build
docker compose up -d
docker compose ps
```

## 3. TLS

```bash
sudo apt install -y certbot
sudo certbot certonly --standalone -d bot.example.com
sudo cp /etc/letsencrypt/live/bot.example.com/fullchain.pem deploy/nginx/certs/
sudo cp /etc/letsencrypt/live/bot.example.com/privkey.pem   deploy/nginx/certs/
sudo sed -i 's/${MEDIABOT_DOMAIN}/bot.example.com/' deploy/nginx/conf.d/mediabot.conf
docker compose restart nginx
```

Renewal (cron, twice a day):

```cron
0 3,15 * * * certbot renew --quiet --deploy-hook 'cp /etc/letsencrypt/live/bot.example.com/*.pem /opt/mediabot/deploy/nginx/certs/ && docker compose -f /opt/mediabot/docker-compose.yml restart nginx'
```

## 4. Switch to webhooks

Polling is fine up to a few thousand users; webhooks scale further and reduce
latency.

```env
TELEGRAM__WEBHOOK_URL=https://bot.example.com
TELEGRAM__WEBHOOK_PATH=/telegram/webhook
TELEGRAM__WEBHOOK_SECRET=<openssl rand -hex 32>
```

`nginx` already proxies `/telegram/webhook` to the API container. Restart the
bot; it registers the webhook on start-up and removes it on shutdown.

## 5. Scaling

```bash
# More download throughput:
docker compose up -d --scale worker=6

# Dedicate a worker pool to notifications so broadcasts never block downloads:
docker compose run -d --name notify-worker \
  -e CELERY_QUEUES=notifications mediabot worker
```

Guidelines:

* **Workers** are stateless — scale them first; ~1 worker per 2 CPU cores.
* **PostgreSQL** — add PgBouncer once you exceed ~150 connections.
* **Redis** — a single instance handles far more than one VPS can download;
  move it to a managed service before sharding.
* **Storage** — files are transient (`APP__ARTIFACT_TTL_MINUTES`); the janitor
  removes them and the completed-artefact cache means popular links are re-sent
  by `file_id` at zero cost.
* **Multiple hosts** — point every node at the same PostgreSQL and Redis. The
  distributed lock in `core/container.py` ensures periodic jobs run once.

## 6. Backups

```bash
# Nightly database dump, 14-day retention.
cat >/etc/cron.daily/mediabot-backup <<'SH'
#!/bin/sh
set -e
STAMP=$(date +%F)
cd /opt/mediabot
docker compose exec -T postgres pg_dump -U mediabot mediabot | gzip > /backup/db-$STAMP.sql.gz
find /backup -name 'db-*.sql.gz' -mtime +14 -delete
SH
chmod +x /etc/cron.daily/mediabot-backup
```

Restore:

```bash
gunzip -c /backup/db-2026-07-31.sql.gz | docker compose exec -T postgres psql -U mediabot mediabot
```

Back up `.env` separately and offline — it holds every secret.

## 7. Upgrades

```bash
cd /opt/mediabot
git pull
docker compose build
docker compose up -d migrate      # migrations first
docker compose up -d              # then the services
docker compose logs -f --tail=100
```

Zero-downtime notes: enum columns are `VARCHAR + CHECK`, so adding a value
never rewrites a table; migrations are additive by convention. Roll back with
`docker compose run --rm migrate alembic downgrade -1`.

## 8. Monitoring

* `GET /api/v1/health` — used by Docker health checks and uptime probes.
* `GET /api/v1/metrics` — Prometheus (restricted to the internal network by nginx).
* Grafana dashboard **MediaBot — Overview** is provisioned automatically.
* Alerts in `deploy/prometheus/alerts.yml`: API down, failure rate > 25 %,
  queue backlog, disk below 5 GB.

Logs are written per channel to `/var/log/mediabot`
(`info`, `warning`, `error`, `download`, `payments`, `security`, `admin`,
`queue`, `api`) and to stdout as JSON for Loki/ELK.

## 9. systemd (bare metal)

```ini
# /etc/systemd/system/mediabot-bot.service
[Unit]
Description=MediaBot — Telegram bot
After=network.target postgresql.service redis-server.service

[Service]
Type=simple
User=mediabot
WorkingDirectory=/opt/mediabot
EnvironmentFile=/opt/mediabot/.env
ExecStart=/opt/mediabot/venv/bin/python -m mediabot bot
Restart=always
RestartSec=5
# Hardening
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/var/lib/mediabot /var/log/mediabot

[Install]
WantedBy=multi-user.target
```

Duplicate for `api`, `scheduler` and `worker`
(`ExecStart=/opt/mediabot/venv/bin/celery -A mediabot.infrastructure.queue worker -l info`).

## 10. Troubleshooting

| Symptom | Diagnosis | Action |
| --- | --- | --- |
| Queue grows, workers idle | broker unreachable | `docker compose logs worker`, check `REDIS__` |
| `worker_lost` errors | worker OOM-killed | raise the memory limit or lower `CELERY_CONCURRENCY` |
| Disk fills up | janitor not running | check `scheduler`/`beat`, lower `ARTIFACT_TTL_MINUTES` |
| Many `extraction_failed` | yt-dlp is out of date | rebuild the image (yt-dlp is version-pinned) |
| Slow API | connection pool exhausted | raise `DB__POOL_SIZE` or add PgBouncer |
| Users report "banned" wrongly | flood control too strict | raise `SECURITY__FLOOD_THRESHOLD` |
