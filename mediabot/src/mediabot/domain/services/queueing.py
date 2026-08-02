"""Queue ordering and wait-time estimation (pure domain logic)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from mediabot.domain.enums import QueuePriority, SubscriptionTier
from mediabot.domain.value_objects import QueuePosition

#: Fallback processing time used before enough samples are collected.
DEFAULT_JOB_SECONDS = 45


@dataclass(frozen=True, slots=True)
class QueueEntry:
    """A queued job as seen by the scheduler."""

    job_id: int
    user_id: int
    priority: QueuePriority
    enqueued_at: datetime
    estimated_seconds: int = DEFAULT_JOB_SECONDS

    def sort_key(self) -> tuple[int, float]:
        """Higher priority first, then FIFO within the same priority."""
        return (-int(self.priority), self.enqueued_at.timestamp())


class QueueService:
    """Ordering, position lookup and ETA maths for the download queue."""

    def priority_for(
        self,
        tier: SubscriptionTier,
        *,
        boost: int = 0,
    ) -> QueuePriority:
        """Queue priority of a user, optionally boosted (e.g. by a promo)."""
        base = QueuePriority.for_tier(tier)
        if not boost:
            return base
        target = int(base) + boost
        for candidate in sorted(QueuePriority, key=int, reverse=True):
            if int(candidate) <= target:
                return candidate
        return QueuePriority.LOW

    def order(self, entries: list[QueueEntry]) -> list[QueueEntry]:
        """Return ``entries`` in execution order."""
        return sorted(entries, key=lambda entry: entry.sort_key())

    def position_of(
        self,
        entries: list[QueueEntry],
        job_id: int,
        *,
        workers: int,
        running_jobs: int = 0,
        average_job_seconds: int = DEFAULT_JOB_SECONDS,
    ) -> QueuePosition | None:
        """Compute the 1-based position and ETA of ``job_id``.

        The ETA models ``workers`` parallel consumers draining the ordered
        queue: the jobs ahead are spread across the workers, so the expected
        wait is ``ceil(ahead / workers) * average_job_seconds``.
        """
        ordered = self.order(entries)
        index = next((i for i, entry in enumerate(ordered) if entry.job_id == job_id), None)
        if index is None:
            return None

        workers = max(workers, 1)
        ahead = ordered[:index]
        ahead_seconds = sum(entry.estimated_seconds or average_job_seconds for entry in ahead)
        eta = int(ahead_seconds / workers) + average_job_seconds
        return QueuePosition(
            position=index + 1,
            total=len(ordered),
            estimated_wait_seconds=max(eta, 0),
            running_jobs=running_jobs,
        )

    def estimate_job_seconds(
        self,
        *,
        duration_seconds: int | None,
        size_bytes: int | None,
        needs_conversion: bool,
    ) -> int:
        """Heuristic estimate of how long a job will occupy a worker.

        Roughly: 1 second per 12 seconds of media (network bound) plus 1 second
        per 8 MiB (disk + upload bound), with a conversion surcharge.
        """
        seconds = DEFAULT_JOB_SECONDS
        if duration_seconds:
            seconds = max(seconds, int(duration_seconds / 12))
        if size_bytes:
            seconds = max(seconds, int(size_bytes / (8 * 1024 * 1024)))
        if needs_conversion:
            seconds = int(seconds * 1.6)
        return max(min(seconds, 3600), 10)

    def should_queue(self, *, running_jobs: int, capacity: int) -> bool:
        """``True`` when the pool is saturated and the job must wait."""
        return running_jobs >= max(capacity, 1)

    def stale_entries(
        self,
        entries: list[QueueEntry],
        *,
        max_age_seconds: int,
        now: datetime | None = None,
    ) -> list[QueueEntry]:
        """Entries that waited longer than ``max_age_seconds`` (to be expired)."""
        now = now or datetime.now(UTC)
        return [
            entry
            for entry in entries
            if (now - entry.enqueued_at).total_seconds() > max_age_seconds
        ]
