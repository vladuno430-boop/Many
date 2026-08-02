"""Prometheus metrics and system-health probes.

Every metric is defined once here and imported where it is incremented, which
keeps naming consistent and prevents duplicate-registration errors when a
module is imported by several entry points.
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

import psutil
from prometheus_client import (
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

REGISTRY = CollectorRegistry(auto_describe=True)

# --------------------------------------------------------------------------- #
# Counters
# --------------------------------------------------------------------------- #
DOWNLOADS_TOTAL = Counter(
    "mediabot_downloads_total",
    "Download jobs by platform, kind and outcome",
    ["platform", "kind", "status"],
    registry=REGISTRY,
)
BYTES_TRANSFERRED = Counter(
    "mediabot_bytes_transferred_total",
    "Bytes delivered to users",
    ["kind"],
    registry=REGISTRY,
)
BOT_UPDATES = Counter(
    "mediabot_bot_updates_total",
    "Telegram updates processed",
    ["type"],
    registry=REGISTRY,
)
ERRORS_TOTAL = Counter(
    "mediabot_errors_total",
    "Errors by code and component",
    ["code", "component"],
    registry=REGISTRY,
)
PAYMENTS_TOTAL = Counter(
    "mediabot_payments_total",
    "Payments by provider and status",
    ["provider", "status"],
    registry=REGISTRY,
)
RATE_LIMIT_HITS = Counter(
    "mediabot_rate_limit_hits_total",
    "Requests rejected by the rate limiter",
    ["scope"],
    registry=REGISTRY,
)

# --------------------------------------------------------------------------- #
# Gauges
# --------------------------------------------------------------------------- #
QUEUE_SIZE = Gauge(
    "mediabot_queue_size",
    "Jobs waiting for a worker",
    registry=REGISTRY,
)
ACTIVE_JOBS = Gauge(
    "mediabot_active_jobs",
    "Jobs currently being processed",
    registry=REGISTRY,
)
ACTIVE_USERS = Gauge(
    "mediabot_active_users",
    "Users seen in the last 24 hours",
    registry=REGISTRY,
)
SYSTEM_CPU = Gauge("mediabot_system_cpu_percent", "Process CPU usage", registry=REGISTRY)
SYSTEM_MEMORY = Gauge("mediabot_system_memory_bytes", "Process RSS", registry=REGISTRY)
DISK_FREE = Gauge("mediabot_disk_free_bytes", "Free space on the storage volume", registry=REGISTRY)

# --------------------------------------------------------------------------- #
# Histograms
# --------------------------------------------------------------------------- #
DOWNLOAD_DURATION = Histogram(
    "mediabot_download_duration_seconds",
    "End-to-end job duration",
    ["kind"],
    buckets=(1, 5, 10, 30, 60, 120, 300, 600, 1800),
    registry=REGISTRY,
)
HANDLER_LATENCY = Histogram(
    "mediabot_handler_latency_seconds",
    "Bot handler latency",
    ["handler"],
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5),
    registry=REGISTRY,
)
API_LATENCY = Histogram(
    "mediabot_api_latency_seconds",
    "REST API latency",
    ["method", "endpoint"],
    buckets=(0.005, 0.01, 0.05, 0.1, 0.5, 1, 2.5),
    registry=REGISTRY,
)


@contextmanager
def observe(histogram: Histogram, **labels: str) -> Iterator[None]:
    """Time a block and record it into ``histogram``."""
    started = time.perf_counter()
    try:
        yield
    finally:
        target = histogram.labels(**labels) if labels else histogram
        target.observe(time.perf_counter() - started)


def render_metrics() -> bytes:
    """Serialise the registry in the Prometheus text exposition format."""
    return generate_latest(REGISTRY)


@dataclass(frozen=True, slots=True)
class SystemHealth:
    """Point-in-time resource usage of the current process and host."""

    cpu_percent: float
    memory_bytes: int
    memory_percent: float
    disk_free_bytes: int
    disk_used_percent: float
    open_files: int
    threads: int
    load_average: tuple[float, float, float]

    @property
    def is_healthy(self) -> bool:
        return self.memory_percent < 92 and self.disk_used_percent < 95


def collect_system_health(storage_path: str = "/") -> SystemHealth:
    """Collect process and host metrics; also refreshes the gauges."""
    process = psutil.Process(os.getpid())
    with process.oneshot():
        memory = process.memory_info().rss
        cpu = process.cpu_percent(interval=None)
        try:
            open_files = len(process.open_files())
        except (psutil.AccessDenied, OSError):  # pragma: no cover - platform dependent
            open_files = 0
        threads = process.num_threads()

    disk = psutil.disk_usage(storage_path)
    health = SystemHealth(
        cpu_percent=cpu,
        memory_bytes=memory,
        memory_percent=psutil.virtual_memory().percent,
        disk_free_bytes=disk.free,
        disk_used_percent=disk.percent,
        open_files=open_files,
        threads=threads,
        load_average=tuple(os.getloadavg()),  # type: ignore[arg-type]
    )
    SYSTEM_CPU.set(health.cpu_percent)
    SYSTEM_MEMORY.set(health.memory_bytes)
    DISK_FREE.set(health.disk_free_bytes)
    return health
