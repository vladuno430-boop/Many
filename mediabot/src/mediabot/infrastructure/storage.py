"""Local artefact storage: working directories, disk usage and clean-up.

Downloads are written into a per-job directory so that a failed job can be
removed atomically and two jobs can never overwrite each other's files.
"""

from __future__ import annotations

import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from mediabot.core.config import AppSettings
from mediabot.core.logging import LogChannel, get_logger

log = get_logger(LogChannel.DOWNLOAD, component="storage")


@dataclass(frozen=True, slots=True)
class DiskUsage:
    """Snapshot of the storage volume."""

    total_bytes: int
    used_bytes: int
    free_bytes: int

    @property
    def used_percent(self) -> float:
        return round(self.used_bytes / self.total_bytes * 100, 1) if self.total_bytes else 0.0


class StorageService:
    """Owns every path the workers write to."""

    def __init__(self, settings: AppSettings) -> None:
        self._settings = settings
        self.root = Path(settings.storage_dir)
        self.temp = Path(settings.temp_dir)

    def ensure(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.temp.mkdir(parents=True, exist_ok=True)

    def job_dir(self, download_id: int) -> Path:
        """Isolated working directory for a single job."""
        path = self.root / f"job_{download_id}"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def remove_job_dir(self, download_id: int) -> None:
        shutil.rmtree(self.root / f"job_{download_id}", ignore_errors=True)

    def remove_file(self, path: str | Path | None) -> None:
        if not path:
            return
        try:
            target = Path(path)
            if target.is_file():
                target.unlink(missing_ok=True)
        except OSError as exc:  # pragma: no cover - permission issues
            log.warning("could not delete {}: {}", path, exc)

    def cleanup_older_than(self, minutes: int) -> tuple[int, int]:
        """Delete job directories older than ``minutes``.

        Returns ``(directories_removed, bytes_freed)``.  This is the safety net
        that keeps a long-running VPS from filling its disk even if a worker
        crashes before its own clean-up runs.
        """
        threshold = time.time() - minutes * 60
        removed = 0
        freed = 0
        for directory in list(self.root.glob("job_*")) + list(self.temp.glob("*")):
            try:
                if directory.stat().st_mtime > threshold:
                    continue
                size = self._directory_size(directory)
                if directory.is_dir():
                    shutil.rmtree(directory, ignore_errors=True)
                else:
                    directory.unlink(missing_ok=True)
                removed += 1
                freed += size
            except OSError:  # pragma: no cover - race with an active worker
                continue
        if removed:
            log.info("janitor removed {} artefacts ({} bytes)", removed, freed)
        return removed, freed

    @staticmethod
    def _directory_size(path: Path) -> int:
        if path.is_file():
            return path.stat().st_size
        return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())

    def usage(self) -> DiskUsage:
        usage = shutil.disk_usage(self.root if self.root.exists() else Path("/"))
        return DiskUsage(
            total_bytes=usage.total,
            used_bytes=usage.used,
            free_bytes=usage.free,
        )

    def has_free_space(self, required_bytes: int, *, reserve_bytes: int = 2 * 1024**3) -> bool:
        """Refuse a job when finishing it would leave the volume nearly full."""
        return self.usage().free_bytes - required_bytes > reserve_bytes
