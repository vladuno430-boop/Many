"""Media resolution: URL validation, extraction and caching."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from mediabot.application.dto import MediaPreview, UserContext
from mediabot.core.config import Settings
from mediabot.core.exceptions import ExtractionError
from mediabot.core.logging import LogChannel, get_logger
from mediabot.core.security import hash_identifier, validate_public_url
from mediabot.domain.enums import MediaKind, Platform, VideoQuality
from mediabot.domain.platforms import detect_platform
from mediabot.domain.services.limits import LimitService
from mediabot.domain.value_objects import MediaFormat, MediaInfo
from mediabot.infrastructure.cache.redis_cache import CacheService
from mediabot.infrastructure.downloader.ytdlp_adapter import YtDlpAdapter

log = get_logger(LogChannel.DOWNLOAD, component="media_service")


class MediaService:
    """Turn a raw user link into a validated, cached :class:`MediaPreview`.

    Extraction is by far the slowest part of the "paste a link" flow, so the
    normalised metadata is cached in Redis under a hash of the URL.  A second
    user pasting the same link gets an instant answer and the platform sees one
    request instead of two.
    """

    def __init__(
        self,
        adapter: YtDlpAdapter,
        cache: CacheService,
        settings: Settings,
        limit_service: LimitService,
    ) -> None:
        self._adapter = adapter
        self._cache = cache
        self._settings = settings
        self._limits = limit_service

    async def resolve(self, url: str, *, use_cache: bool = True) -> MediaInfo:
        """Validate and extract metadata for ``url``."""
        safe_url = validate_public_url(url)
        platform = detect_platform(safe_url)
        cache_key = hash_identifier(safe_url, salt="media")

        if use_cache:
            cached = await self._cache.get_media_info(cache_key)
            if cached is not None:
                return self._from_cache(cached)

        info = await self._adapter.extract_info(safe_url)
        if not info.formats and not info.is_playlist and not platform.metadata_only:
            raise ExtractionError("No downloadable formats were found for this link")

        ttl = self._settings.downloader.metadata_cache_ttl_seconds
        if ttl:
            await self._cache.cache_media_info(cache_key, self._to_cache(info), ttl)
        return info

    async def build_preview(self, url: str, context: UserContext) -> MediaPreview:
        """Resolve ``url`` and annotate it with what this user may download."""
        info = await self.resolve(url)
        platform = info.platform

        allowed_video = tuple(
            quality
            for quality in self._limits.allowed_video_qualities(context.policy)
            if quality is VideoQuality.BEST
            or quality.height
            <= max((q.height for q in info.available_video_qualities()), default=0)
        )
        if not allowed_video and info.has_video_stream:
            # The source only offers qualities above the tier ceiling: still
            # allow the lowest permitted step so the user is not stuck.
            allowed_video = self._limits.allowed_video_qualities(context.policy)[:1]

        estimates: dict[str, int | None] = {
            quality.value: info.estimated_size(quality) for quality in allowed_video
        }
        estimates[VideoQuality.AUDIO_ONLY.value] = info.estimated_size(VideoQuality.AUDIO_ONLY)

        warning: str | None = None
        if info.is_live:
            warning = "live_stream"
        elif info.is_playlist and not context.policy.allow_playlists:
            warning = "playlist_not_allowed"
        elif platform.metadata_only:
            warning = "metadata_only"

        return MediaPreview(
            info=info,
            platform=platform,
            allowed_video_qualities=allowed_video,
            allowed_audio_qualities=self._limits.allowed_audio_qualities(context.policy),
            estimated_sizes=estimates,
            metadata_only=platform.metadata_only,
            warning=warning,
        )

    async def detect(self, url: str) -> Platform:
        """Detect the platform without any network access."""
        return detect_platform(validate_public_url(url, resolve_dns=False))

    async def is_supported(self, url: str) -> bool:
        """Whether some extractor claims this URL (used for early feedback)."""
        try:
            safe_url = validate_public_url(url, resolve_dns=False)
        except Exception:
            return False
        if detect_platform(safe_url) is not Platform.GENERIC:
            return True
        return await self._adapter.probe_supported(safe_url)

    def default_kind(self, info: MediaInfo) -> MediaKind:
        """Audio-only sources should not offer video buttons."""
        if info.platform.is_audio_only or not info.has_video_stream:
            return MediaKind.AUDIO
        return MediaKind.VIDEO

    async def invalidate(self, url: str) -> None:
        await self._cache.delete(CacheService.key("media", hash_identifier(url, salt="media")))

    # ------------------------------------------------------------------ #
    # (de)serialisation of the cached payload
    # ------------------------------------------------------------------ #
    @staticmethod
    def _to_cache(info: MediaInfo) -> dict[str, Any]:
        payload = asdict(info)
        payload["platform"] = info.platform.value
        payload["formats"] = [{**asdict(fmt), "kind": fmt.kind.value} for fmt in info.formats]
        return payload

    @staticmethod
    def _from_cache(payload: dict[str, Any]) -> MediaInfo:
        formats = tuple(
            MediaFormat(
                format_id=item["format_id"],
                extension=item["extension"],
                kind=MediaKind(item["kind"]),
                height=item.get("height"),
                width=item.get("width"),
                fps=item.get("fps"),
                filesize=item.get("filesize"),
                tbr=item.get("tbr"),
                abr=item.get("abr"),
                vcodec=item.get("vcodec"),
                acodec=item.get("acodec"),
                note=item.get("note"),
            )
            for item in payload.get("formats", [])
        )
        try:
            platform = Platform(payload.get("platform", Platform.GENERIC.value))
        except ValueError:  # pragma: no cover - stale cache after an enum change
            platform = Platform.GENERIC
        return MediaInfo(
            source_url=payload["source_url"],
            platform=platform,
            title=payload.get("title") or "Untitled",
            duration=payload.get("duration"),
            uploader=payload.get("uploader"),
            uploader_url=payload.get("uploader_url"),
            thumbnail=payload.get("thumbnail"),
            description=payload.get("description"),
            view_count=payload.get("view_count"),
            like_count=payload.get("like_count"),
            upload_date=payload.get("upload_date"),
            is_live=bool(payload.get("is_live")),
            age_limit=int(payload.get("age_limit") or 0),
            webpage_url=payload.get("webpage_url"),
            extractor=payload.get("extractor"),
            formats=formats,
            entries=int(payload.get("entries") or 1),
            raw_id=payload.get("raw_id"),
        )
