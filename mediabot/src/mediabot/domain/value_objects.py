"""Immutable value objects of the domain.

Value objects have no identity: two instances with equal fields are equal.
They are frozen dataclasses (cheap, hashable, typed) and never touch I/O.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Self

from mediabot.domain.enums import (
    AudioFormat,
    AudioQuality,
    MediaKind,
    Platform,
    VideoFormat,
    VideoQuality,
)

_BYTES_UNITS = ("B", "KB", "MB", "GB", "TB")


def humanize_bytes(size: int | float | None) -> str:
    """Render a byte count as a compact human readable string."""
    if not size or size < 0:
        return "—"
    value = float(size)
    for unit in _BYTES_UNITS:
        if value < 1024 or unit == _BYTES_UNITS[-1]:
            precision = 0 if unit == "B" else 1
            return f"{value:.{precision}f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


def humanize_duration(seconds: int | float | None) -> str:
    """Render a duration as ``H:MM:SS`` / ``M:SS``."""
    if not seconds or seconds < 0:
        return "—"
    total = int(seconds)
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


@dataclass(frozen=True, slots=True)
class MediaFormat:
    """A single downloadable stream advertised by the extractor."""

    format_id: str
    extension: str
    kind: MediaKind
    height: int | None = None
    width: int | None = None
    fps: float | None = None
    filesize: int | None = None
    tbr: float | None = None
    abr: float | None = None
    vcodec: str | None = None
    acodec: str | None = None
    note: str | None = None

    @property
    def has_video(self) -> bool:
        return bool(self.vcodec and self.vcodec != "none")

    @property
    def has_audio(self) -> bool:
        return bool(self.acodec and self.acodec != "none")

    @property
    def quality(self) -> VideoQuality:
        return VideoQuality.from_height(self.height)

    @property
    def label(self) -> str:
        if self.kind is MediaKind.AUDIO:
            bitrate = f"{int(self.abr)} kbps" if self.abr else self.extension.upper()
            return f"{bitrate} · {humanize_bytes(self.filesize)}"
        resolution = f"{self.height}p" if self.height else self.extension.upper()
        fps = f"{int(self.fps)}fps" if self.fps and self.fps > 30 else ""
        return " · ".join(filter(None, [resolution, fps, humanize_bytes(self.filesize)]))


@dataclass(frozen=True, slots=True)
class MediaInfo:
    """Normalised metadata for a piece of media, independent of the extractor."""

    source_url: str
    platform: Platform
    title: str
    duration: int | None = None
    uploader: str | None = None
    uploader_url: str | None = None
    thumbnail: str | None = None
    description: str | None = None
    view_count: int | None = None
    like_count: int | None = None
    upload_date: str | None = None
    is_live: bool = False
    age_limit: int = 0
    webpage_url: str | None = None
    extractor: str | None = None
    formats: tuple[MediaFormat, ...] = ()
    entries: int = 1
    raw_id: str | None = None

    @property
    def is_playlist(self) -> bool:
        return self.entries > 1

    @property
    def has_video_stream(self) -> bool:
        return any(fmt.has_video for fmt in self.formats)

    @property
    def best_filesize(self) -> int | None:
        sizes = [fmt.filesize for fmt in self.formats if fmt.filesize]
        return max(sizes) if sizes else None

    @property
    def duration_label(self) -> str:
        return humanize_duration(self.duration)

    def available_video_qualities(self) -> tuple[VideoQuality, ...]:
        """Distinct video qualities that the source actually offers."""
        heights = {fmt.height for fmt in self.formats if fmt.has_video and fmt.height}
        qualities = sorted(
            {VideoQuality.from_height(height) for height in heights},
            key=lambda quality: quality.height,
        )
        return tuple(qualities)

    def estimated_size(self, quality: VideoQuality) -> int | None:
        """Estimate the resulting file size for ``quality``.

        Falls back to a bitrate-based estimate when the extractor does not
        report ``filesize`` (common for DASH manifests).
        """
        if quality is VideoQuality.AUDIO_ONLY:
            audio = [fmt for fmt in self.formats if fmt.has_audio and not fmt.has_video]
            sizes = [fmt.filesize for fmt in audio if fmt.filesize]
            if sizes:
                return max(sizes)
            if self.duration:
                return int(self.duration * 192 * 1000 / 8)
            return None
        candidates = [
            fmt
            for fmt in self.formats
            if fmt.has_video
            and (quality is VideoQuality.BEST or (fmt.height or 0) <= quality.height)
        ]
        if not candidates:
            return self.best_filesize
        best = max(candidates, key=lambda fmt: (fmt.height or 0, fmt.tbr or 0))
        if best.filesize:
            return best.filesize
        if best.tbr and self.duration:
            return int(best.tbr * 1000 * self.duration / 8)
        return None


@dataclass(frozen=True, slots=True)
class DownloadRequest:
    """A fully resolved user intent, ready to be queued."""

    user_id: int
    url: str
    platform: Platform
    kind: MediaKind
    video_quality: VideoQuality | None = None
    audio_quality: AudioQuality | None = None
    video_format: VideoFormat | None = None
    audio_format: AudioFormat | None = None
    embed_thumbnail: bool = True
    embed_metadata: bool = True
    chat_id: int | None = None
    reply_to_message_id: int | None = None

    def __post_init__(self) -> None:
        if self.kind is MediaKind.VIDEO and self.video_quality is None:
            raise ValueError("video downloads require a video_quality")
        if self.kind is MediaKind.AUDIO and self.audio_quality is None:
            raise ValueError("audio downloads require an audio_quality")

    @property
    def target_extension(self) -> str:
        if self.kind is MediaKind.AUDIO:
            return (self.audio_format or AudioFormat.MP3).value
        return (self.video_format or VideoFormat.MP4).value

    def cache_key(self) -> str:
        """Deterministic key used to reuse an already produced artefact."""
        parts = [
            self.url,
            self.kind.value,
            self.video_quality.value if self.video_quality else "-",
            self.audio_quality.value if self.audio_quality else "-",
            self.target_extension,
        ]
        return "|".join(parts)


@dataclass(frozen=True, slots=True)
class DownloadArtifact:
    """The output of a completed download job."""

    file_path: str
    file_name: str
    file_size: int
    mime_type: str
    kind: MediaKind
    duration: int | None = None
    width: int | None = None
    height: int | None = None
    thumbnail_path: str | None = None
    checksum: str | None = None

    @property
    def size_label(self) -> str:
        return humanize_bytes(self.file_size)


@dataclass(frozen=True, slots=True)
class TierPolicy:
    """The complete rule-set attached to a subscription tier.

    Centralising the limits here means a single edit changes behaviour in the
    bot, the API, the queue and the admin panel simultaneously.
    ``None`` always means *unlimited*.
    """

    tier: str
    daily_downloads: int | None
    max_file_size_bytes: int | None
    max_duration_seconds: int | None
    max_concurrent_jobs: int
    speed_limit_bytes_per_sec: int | None
    max_video_quality: VideoQuality
    max_audio_quality: AudioQuality
    allow_lossless: bool
    allow_playlists: bool
    playlist_max_items: int
    allowed_platforms: frozenset[Platform] | None
    ads_enabled: bool
    priority: int
    monthly_price: int
    features: tuple[str, ...] = ()

    def allows_platform(self, platform: Platform) -> bool:
        return self.allowed_platforms is None or platform in self.allowed_platforms

    def allows_video_quality(self, quality: VideoQuality) -> bool:
        if quality is VideoQuality.AUDIO_ONLY:
            return True
        if quality is VideoQuality.BEST:
            return self.max_video_quality is VideoQuality.BEST
        return quality.height <= self.max_video_quality.height

    def allows_audio_quality(self, quality: AudioQuality) -> bool:
        if quality is AudioQuality.LOSSLESS:
            return self.allow_lossless
        return quality.bitrate <= self.max_audio_quality.bitrate


@dataclass(frozen=True, slots=True)
class UsageSnapshot:
    """Aggregated consumption of a user within the current quota window."""

    downloads_today: int
    bytes_today: int
    active_jobs: int
    queued_jobs: int
    window_started_at: datetime

    @property
    def window_resets_at(self) -> datetime:
        return self.window_started_at + timedelta(days=1)

    def remaining(self, policy: TierPolicy) -> int | None:
        if policy.daily_downloads is None:
            return None
        return max(policy.daily_downloads - self.downloads_today, 0)


@dataclass(frozen=True, slots=True)
class QueuePosition:
    """A user-facing view of where a job sits in the queue."""

    position: int
    total: int
    estimated_wait_seconds: int
    running_jobs: int

    @property
    def wait_label(self) -> str:
        return humanize_duration(self.estimated_wait_seconds)


@dataclass(frozen=True, slots=True)
class DownloadProgress:
    """Progress snapshot emitted by the worker and mirrored into Redis."""

    percent: float
    downloaded_bytes: int
    total_bytes: int | None
    speed_bytes_per_sec: float | None
    eta_seconds: int | None
    stage: str

    def bar(self, width: int = 12) -> str:
        """Render a text progress bar for Telegram messages."""
        filled = int(max(0.0, min(self.percent, 100.0)) / 100 * width)
        return "▰" * filled + "▱" * (width - filled)


@dataclass(frozen=True, slots=True)
class DateRange:
    """Inclusive-exclusive date window used by analytics queries."""

    start: datetime
    end: datetime

    @classmethod
    def last_days(cls, days: int) -> Self:
        now = datetime.now(UTC)
        return cls(start=now - timedelta(days=days), end=now)

    @property
    def days(self) -> int:
        return max((self.end - self.start).days, 1)


@dataclass(frozen=True, slots=True)
class AchievementDefinition:
    """Static description of an achievement and its unlock condition."""

    code: str
    icon: str
    reward_coins: int
    threshold: int = 0
    metric: str = "downloads"
    hidden: bool = False
    payload: dict[str, Any] = field(default_factory=dict)
