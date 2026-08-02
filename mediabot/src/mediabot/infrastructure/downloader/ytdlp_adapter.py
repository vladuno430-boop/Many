"""yt-dlp adapter: metadata extraction and media downloading.

Architecture note
-----------------
yt-dlp is synchronous and CPU/IO heavy, so every call is executed in a worker
thread via ``asyncio.to_thread``.  The adapter maps yt-dlp's loose dictionaries
onto our typed :class:`MediaInfo`/:class:`MediaFormat` value objects and
translates its exceptions into the domain error hierarchy — nothing above this
module ever imports ``yt_dlp``.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError as YtDlpDownloadError
from yt_dlp.utils import ExtractorError
from yt_dlp.utils import GeoRestrictedError as YtGeoRestrictedError

from mediabot.core.config import AppSettings, DownloaderSettings
from mediabot.core.exceptions import (
    DownloadError,
    ExtractionError,
    GeoRestrictedError,
    PrivateContentError,
    UnsupportedUrlError,
)
from mediabot.core.logging import LogChannel, get_logger
from mediabot.core.security import sanitize_filename
from mediabot.domain.enums import MediaKind, VideoQuality
from mediabot.domain.platforms import detect_platform
from mediabot.domain.services.format_selection import FormatPlan
from mediabot.domain.value_objects import (
    DownloadArtifact,
    DownloadProgress,
    MediaFormat,
    MediaInfo,
)

log = get_logger(LogChannel.DOWNLOAD, component="ytdlp")

ProgressCallback = Callable[[DownloadProgress], None]

#: Patterns used to classify yt-dlp error messages into domain errors.
_PRIVATE_PATTERNS = (
    "private video",
    "login required",
    "sign in",
    "members-only",
    "this video is unavailable",
    "requested content is not available",
    "account is private",
)
_GEO_PATTERNS = ("not available in your country", "geo restricted", "geo-restricted")
_UNSUPPORTED_PATTERNS = ("unsupported url", "no video formats found", "is not a valid url")


class YtDlpAdapter:
    """Thin, typed, async wrapper around :class:`yt_dlp.YoutubeDL`."""

    def __init__(self, settings: DownloaderSettings, app_settings: AppSettings) -> None:
        self._settings = settings
        self._app = app_settings

    # ------------------------------------------------------------------ #
    # Options
    # ------------------------------------------------------------------ #
    def _base_options(self) -> dict[str, Any]:
        options: dict[str, Any] = {
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "noplaylist": True,
            "socket_timeout": 30,
            "retries": self._settings.max_retries,
            "fragment_retries": self._settings.max_retries,
            "extractor_retries": 2,
            "http_headers": {"User-Agent": self._settings.user_agent},
            "ffmpeg_location": self._settings.ffmpeg_path,
            "restrictfilenames": True,
            "windowsfilenames": True,
            "trim_file_name": 120,
            "overwrites": True,
            "consoletitle": False,
            "nocheckcertificate": False,
        }
        if self._settings.proxy:
            options["proxy"] = self._settings.proxy
        if self._settings.cookies_file and Path(self._settings.cookies_file).exists():
            options["cookiefile"] = self._settings.cookies_file
        return options

    # ------------------------------------------------------------------ #
    # Extraction
    # ------------------------------------------------------------------ #
    async def extract_info(self, url: str, *, process: bool = False) -> MediaInfo:
        """Fetch metadata for ``url`` without downloading anything."""
        raw = await asyncio.to_thread(self._extract_sync, url, process)
        return self._to_media_info(url, raw)

    def _extract_sync(self, url: str, process: bool) -> dict[str, Any]:
        options = self._base_options() | {"skip_download": True}
        try:
            with YoutubeDL(options) as ydl:
                info = ydl.extract_info(url, download=False, process=process)
        except YtGeoRestrictedError as exc:
            raise GeoRestrictedError(str(exc)) from exc
        except (YtDlpDownloadError, ExtractorError) as exc:
            raise self._classify(str(exc)) from exc
        except Exception as exc:  # pragma: no cover - unexpected extractor crash
            raise ExtractionError(str(exc)) from exc
        if not info:
            raise ExtractionError("Extractor returned no data")
        return dict(info)

    @staticmethod
    def _classify(message: str) -> DownloadError:
        lowered = message.lower()
        if any(pattern in lowered for pattern in _GEO_PATTERNS):
            return GeoRestrictedError(message[:300])
        if any(pattern in lowered for pattern in _PRIVATE_PATTERNS):
            return PrivateContentError(message[:300])
        if any(pattern in lowered for pattern in _UNSUPPORTED_PATTERNS):
            return UnsupportedUrlError(message[:300])  # type: ignore[return-value]
        return ExtractionError(message[:300])

    def _to_media_info(self, url: str, raw: dict[str, Any]) -> MediaInfo:
        """Map a yt-dlp info dict onto :class:`MediaInfo`."""
        entries = raw.get("entries")
        if entries:
            # Playlist: use the first entry for display, remember the count.
            items = [entry for entry in entries if entry]
            first = items[0] if items else {}
            info = self._to_media_info(url, dict(first))
            return MediaInfo(
                source_url=url,
                platform=info.platform,
                title=raw.get("title") or info.title,
                duration=info.duration,
                uploader=raw.get("uploader") or info.uploader,
                thumbnail=info.thumbnail,
                description=raw.get("description"),
                webpage_url=raw.get("webpage_url") or url,
                extractor=raw.get("extractor"),
                formats=info.formats,
                entries=len(items),
                raw_id=str(raw.get("id") or ""),
            )

        parsed = (self._to_format(item) for item in raw.get("formats") or [])
        formats: tuple[MediaFormat, ...] = tuple(fmt for fmt in parsed if fmt is not None)
        return MediaInfo(
            source_url=url,
            platform=detect_platform(raw.get("webpage_url") or url),
            title=(raw.get("title") or "Untitled").strip(),
            duration=int(raw["duration"]) if raw.get("duration") else None,
            uploader=raw.get("uploader") or raw.get("channel") or raw.get("creator"),
            uploader_url=raw.get("uploader_url") or raw.get("channel_url"),
            thumbnail=raw.get("thumbnail"),
            description=(raw.get("description") or "")[:1000] or None,
            view_count=raw.get("view_count"),
            like_count=raw.get("like_count"),
            upload_date=raw.get("upload_date"),
            is_live=bool(raw.get("is_live")),
            age_limit=int(raw.get("age_limit") or 0),
            webpage_url=raw.get("webpage_url") or url,
            extractor=raw.get("extractor"),
            formats=formats,
            entries=1,
            raw_id=str(raw.get("id") or ""),
        )

    @staticmethod
    def _to_format(item: dict[str, Any]) -> MediaFormat | None:
        format_id = item.get("format_id")
        if not format_id:
            return None
        vcodec = item.get("vcodec")
        acodec = item.get("acodec")
        has_video = bool(vcodec and vcodec != "none")
        kind = MediaKind.VIDEO if has_video else MediaKind.AUDIO
        return MediaFormat(
            format_id=str(format_id),
            extension=str(item.get("ext") or "bin"),
            kind=kind,
            height=item.get("height"),
            width=item.get("width"),
            fps=item.get("fps"),
            filesize=item.get("filesize") or item.get("filesize_approx"),
            tbr=item.get("tbr"),
            abr=item.get("abr"),
            vcodec=vcodec,
            acodec=acodec,
            note=item.get("format_note"),
        )

    # ------------------------------------------------------------------ #
    # Downloading
    # ------------------------------------------------------------------ #
    async def download(
        self,
        *,
        url: str,
        plan: FormatPlan,
        destination: Path,
        info: MediaInfo,
        progress_callback: ProgressCallback | None = None,
        speed_limit: int | None = None,
        max_filesize: int | None = None,
    ) -> DownloadArtifact:
        """Download ``url`` according to ``plan`` and return the artefact."""
        destination.mkdir(parents=True, exist_ok=True)
        return await asyncio.to_thread(
            self._download_sync,
            url,
            plan,
            destination,
            info,
            progress_callback,
            speed_limit,
            max_filesize,
        )

    def _build_download_options(
        self,
        *,
        plan: FormatPlan,
        outtmpl: str,
        progress_hook: Callable[[dict[str, Any]], None],
        speed_limit: int | None,
        max_filesize: int | None,
    ) -> dict[str, Any]:
        postprocessors: list[dict[str, Any]] = []
        if plan.is_audio:
            postprocessors.append(
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": plan.audio_codec,
                    "preferredquality": (
                        str(plan.audio_bitrate_kbps) if plan.audio_bitrate_kbps else "0"
                    ),
                }
            )
            if plan.embed_thumbnail:
                postprocessors.append({"key": "EmbedThumbnail", "already_have_thumbnail": False})
        elif plan.remux_video_to:
            postprocessors.append(
                {"key": "FFmpegVideoRemuxer", "preferedformat": plan.remux_video_to}
            )
        if plan.embed_metadata:
            postprocessors.append({"key": "FFmpegMetadata", "add_metadata": True})

        options = self._base_options() | {
            "format": plan.selector,
            "outtmpl": outtmpl,
            "progress_hooks": [progress_hook],
            "postprocessor_hooks": [progress_hook],
            "postprocessors": postprocessors,
            "writethumbnail": plan.embed_thumbnail,
            "skip_download": False,
            "concurrent_fragment_downloads": 4,
        }
        if plan.merge_output_format:
            options["merge_output_format"] = plan.merge_output_format
        if speed_limit:
            options["ratelimit"] = speed_limit
        if max_filesize:
            options["max_filesize"] = max_filesize
        return options

    def _download_sync(
        self,
        url: str,
        plan: FormatPlan,
        destination: Path,
        info: MediaInfo,
        progress_callback: ProgressCallback | None,
        speed_limit: int | None,
        max_filesize: int | None,
    ) -> DownloadArtifact:
        safe_title = sanitize_filename(info.title)
        outtmpl = str(destination / f"{safe_title}.%(ext)s")

        def hook(status: dict[str, Any]) -> None:
            if progress_callback is None:
                return
            progress_callback(self._to_progress(status))

        options = self._build_download_options(
            plan=plan,
            outtmpl=outtmpl,
            progress_hook=hook,
            speed_limit=speed_limit,
            max_filesize=max_filesize,
        )

        try:
            with YoutubeDL(options) as ydl:
                result = ydl.extract_info(url, download=True)
                if result is None:
                    raise DownloadError("Downloader returned no result")
                prepared = ydl.prepare_filename(result)
        except YtGeoRestrictedError as exc:
            raise GeoRestrictedError(str(exc)) from exc
        except (YtDlpDownloadError, ExtractorError) as exc:
            raise self._classify(str(exc)) from exc

        final_path = self._resolve_output(Path(prepared), plan, destination, safe_title)
        if not final_path.exists():
            raise DownloadError("Downloaded file is missing on disk")

        stat = final_path.stat()
        kind = MediaKind.AUDIO if plan.is_audio else MediaKind.VIDEO
        thumbnail = self._find_thumbnail(destination, safe_title)
        return DownloadArtifact(
            file_path=str(final_path),
            file_name=final_path.name,
            file_size=stat.st_size,
            mime_type=self._guess_mime(final_path.suffix, kind),
            kind=kind,
            duration=info.duration,
            width=None if plan.is_audio else self._best_dimension(info, "width"),
            height=None if plan.is_audio else self._best_dimension(info, "height"),
            thumbnail_path=str(thumbnail) if thumbnail else None,
        )

    @staticmethod
    def _best_dimension(info: MediaInfo, attribute: str) -> int | None:
        values = [
            getattr(fmt, attribute)
            for fmt in info.formats
            if fmt.has_video and getattr(fmt, attribute)
        ]
        return max(values) if values else None

    @staticmethod
    def _resolve_output(
        prepared: Path,
        plan: FormatPlan,
        destination: Path,
        safe_title: str,
    ) -> Path:
        """Locate the final file after post-processing changed its extension."""
        if plan.is_audio and plan.audio_codec:
            candidate = prepared.with_suffix(f".{plan.audio_codec}")
        elif plan.remux_video_to:
            candidate = prepared.with_suffix(f".{plan.remux_video_to}")
        else:
            candidate = prepared
        if candidate.exists():
            return candidate
        if prepared.exists():
            return prepared
        matches = sorted(
            (
                path
                for path in destination.glob(f"{safe_title}.*")
                if path.suffix not in {".part", ".ytdl", ".webp", ".jpg", ".png"}
            ),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        return matches[0] if matches else candidate

    @staticmethod
    def _find_thumbnail(destination: Path, safe_title: str) -> Path | None:
        for suffix in (".jpg", ".png", ".webp"):
            candidate = destination / f"{safe_title}{suffix}"
            if candidate.exists():
                return candidate
        return None

    @staticmethod
    def _guess_mime(suffix: str, kind: MediaKind) -> str:
        mapping = {
            ".mp4": "video/mp4",
            ".mkv": "video/x-matroska",
            ".webm": "video/webm",
            ".mp3": "audio/mpeg",
            ".m4a": "audio/mp4",
            ".flac": "audio/flac",
            ".wav": "audio/wav",
            ".ogg": "audio/ogg",
            ".opus": "audio/ogg",
        }
        default = "video/mp4" if kind is MediaKind.VIDEO else "audio/mpeg"
        return mapping.get(suffix.lower(), default)

    @staticmethod
    def _to_progress(status: dict[str, Any]) -> DownloadProgress:
        stage = str(status.get("status") or "downloading")
        downloaded = int(status.get("downloaded_bytes") or 0)
        total = status.get("total_bytes") or status.get("total_bytes_estimate")
        total_int = int(total) if total else None
        if total_int:
            percent = min(downloaded / total_int * 100, 100.0)
        else:
            percent = float(re.sub(r"[^\d.]", "", str(status.get("_percent_str") or "0")) or 0.0)
        return DownloadProgress(
            percent=round(percent, 1),
            downloaded_bytes=downloaded,
            total_bytes=total_int,
            speed_bytes_per_sec=status.get("speed"),
            eta_seconds=int(status["eta"]) if status.get("eta") else None,
            stage="processing" if stage in {"finished", "processing"} else stage,
        )

    # ------------------------------------------------------------------ #
    # Helpers used by the application layer
    # ------------------------------------------------------------------ #
    async def probe_supported(self, url: str) -> bool:
        """Cheap check whether *any* extractor claims ``url``."""

        def _check() -> bool:
            with YoutubeDL(self._base_options()) as ydl:
                extractor = ydl._ies
                return any(
                    ie.suitable(url) and ie.IE_NAME != "generic" for ie in extractor.values()
                )

        try:
            return await asyncio.to_thread(_check)
        except Exception:  # pragma: no cover - defensive
            return False

    def quality_ceiling(self, info: MediaInfo) -> VideoQuality:
        """Highest quality actually offered by the source."""
        qualities = info.available_video_qualities()
        return qualities[-1] if qualities else VideoQuality.P360
