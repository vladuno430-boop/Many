"""FFmpeg/FFprobe helpers and MIME validation.

Architecture note
-----------------
FFmpeg is invoked through ``asyncio.create_subprocess_exec`` with an argument
*list* — never a shell string — so a hostile media title can not inject shell
commands.  Every call is wrapped in a timeout so a malformed file can not pin a
worker forever (part of the "sandboxed processing" requirement).
"""

from __future__ import annotations

import asyncio
import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mediabot.core.config import DownloaderSettings
from mediabot.core.exceptions import ConversionError
from mediabot.core.logging import LogChannel, get_logger

log = get_logger(LogChannel.DOWNLOAD, component="ffmpeg")

#: Extension -> expected MIME prefix, used to reject mislabelled artefacts.
_EXPECTED_MIME_PREFIX: dict[str, str] = {
    ".mp4": "video/",
    ".mkv": "video/",
    ".webm": "video/",
    ".mp3": "audio/",
    ".m4a": "audio/",
    ".flac": "audio/",
    ".wav": "audio/",
    ".ogg": "audio/",
}


@dataclass(frozen=True, slots=True)
class MediaProbe:
    """Result of an ``ffprobe`` inspection."""

    duration: float | None
    width: int | None
    height: int | None
    bitrate: int | None
    video_codec: str | None
    audio_codec: str | None
    format_name: str | None

    @property
    def has_video(self) -> bool:
        return bool(self.video_codec)


class FFmpegService:
    """Async wrapper around the ffmpeg/ffprobe binaries."""

    def __init__(self, settings: DownloaderSettings) -> None:
        self._settings = settings
        self._ffmpeg = settings.ffmpeg_path if Path(settings.ffmpeg_path).exists() else "ffmpeg"
        self._ffprobe = settings.ffprobe_path if Path(settings.ffprobe_path).exists() else "ffprobe"

    @property
    def available(self) -> bool:
        """``True`` when both binaries can be located on this host."""
        return bool(shutil.which(self._ffmpeg) and shutil.which(self._ffprobe))

    async def _run(self, *args: str, timeout: int = 300) -> tuple[int, bytes, bytes]:
        process = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
        except TimeoutError as exc:
            process.kill()
            await process.wait()
            raise ConversionError(f"ffmpeg timed out after {timeout}s") from exc
        return process.returncode or 0, stdout, stderr

    async def probe(self, path: Path | str) -> MediaProbe:
        """Inspect a media file with ffprobe."""
        code, stdout, stderr = await self._run(
            self._ffprobe,
            "-v",
            "quiet",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(path),
            timeout=60,
        )
        if code != 0:
            raise ConversionError(f"ffprobe failed: {stderr.decode(errors='ignore')[:200]}")
        try:
            payload: dict[str, Any] = json.loads(stdout or b"{}")
        except json.JSONDecodeError as exc:
            raise ConversionError("ffprobe returned malformed JSON") from exc

        streams = payload.get("streams") or []
        video = next((s for s in streams if s.get("codec_type") == "video"), None)
        audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
        fmt = payload.get("format") or {}
        return MediaProbe(
            duration=float(fmt["duration"]) if fmt.get("duration") else None,
            width=int(video["width"]) if video and video.get("width") else None,
            height=int(video["height"]) if video and video.get("height") else None,
            bitrate=int(fmt["bit_rate"]) if fmt.get("bit_rate") else None,
            video_codec=video.get("codec_name") if video else None,
            audio_codec=audio.get("codec_name") if audio else None,
            format_name=fmt.get("format_name"),
        )

    async def make_thumbnail(
        self,
        source: Path | str,
        destination: Path | str,
        *,
        timestamp: float = 1.0,
        width: int = 320,
    ) -> Path | None:
        """Extract a single frame to use as a Telegram video thumbnail."""
        code, _, stderr = await self._run(
            self._ffmpeg,
            "-y",
            "-ss",
            str(max(timestamp, 0)),
            "-i",
            str(source),
            "-vframes",
            "1",
            "-vf",
            f"scale={width}:-2",
            str(destination),
            timeout=60,
        )
        if code != 0:
            log.debug("thumbnail extraction failed: {}", stderr.decode(errors="ignore")[:160])
            return None
        return Path(destination)

    async def transcode(
        self,
        source: Path | str,
        destination: Path | str,
        *,
        audio_bitrate_kbps: int | None = None,
        video_codec: str | None = None,
        audio_codec: str | None = None,
        timeout: int | None = None,
    ) -> Path:
        """Transcode/remux a file, copying streams whenever possible."""
        args: list[str] = [self._ffmpeg, "-y", "-i", str(source)]
        args += ["-c:v", video_codec or "copy"]
        args += ["-c:a", audio_codec or "copy"]
        if audio_bitrate_kbps:
            args += ["-b:a", f"{audio_bitrate_kbps}k"]
        args.append(str(destination))
        code, _, stderr = await self._run(
            *args, timeout=timeout or self._settings.job_timeout_seconds
        )
        if code != 0:
            raise ConversionError(f"ffmpeg failed: {stderr.decode(errors='ignore')[:300]}")
        return Path(destination)

    async def split(
        self,
        source: Path | str,
        destination_dir: Path | str,
        *,
        chunk_seconds: int,
    ) -> list[Path]:
        """Split a long file into chunks so each part fits Telegram's limit."""
        destination = Path(destination_dir)
        destination.mkdir(parents=True, exist_ok=True)
        pattern = destination / f"{Path(source).stem}_part%03d{Path(source).suffix}"
        code, _, stderr = await self._run(
            self._ffmpeg,
            "-y",
            "-i",
            str(source),
            "-c",
            "copy",
            "-f",
            "segment",
            "-segment_time",
            str(chunk_seconds),
            "-reset_timestamps",
            "1",
            str(pattern),
            timeout=self._settings.job_timeout_seconds,
        )
        if code != 0:
            raise ConversionError(f"ffmpeg split failed: {stderr.decode(errors='ignore')[:200]}")
        return sorted(destination.glob(f"{Path(source).stem}_part*"))


def detect_mime(path: Path | str) -> str:
    """Detect the MIME type of a file by its magic bytes.

    ``python-magic`` is optional at runtime (it needs libmagic): when it is not
    importable we fall back to the extension, which is still enough for the
    sanity check performed by :func:`validate_media_file`.
    """
    try:
        import magic

        return str(magic.from_file(str(path), mime=True))
    except Exception:  # pragma: no cover - libmagic missing
        suffix = Path(path).suffix.lower()
        prefix = _EXPECTED_MIME_PREFIX.get(suffix, "application/")
        return f"{prefix}{suffix.lstrip('.') or 'octet-stream'}"


def validate_media_file(path: Path | str, *, max_bytes: int | None = None) -> str:
    """Validate that a produced file really is audio/video and fits the limit.

    Protects against extractor bugs or hostile servers returning an HTML error
    page with a ``.mp4`` name, which would otherwise be forwarded to the user.
    """
    file_path = Path(path)
    if not file_path.exists() or not file_path.is_file():
        raise ConversionError("Produced file does not exist")
    size = file_path.stat().st_size
    if size == 0:
        raise ConversionError("Produced file is empty")
    if max_bytes is not None and size > max_bytes:
        raise ConversionError("Produced file exceeds the allowed size")

    mime = detect_mime(file_path)
    expected_prefix = _EXPECTED_MIME_PREFIX.get(file_path.suffix.lower())
    if expected_prefix and not mime.startswith(("video/", "audio/", "application/octet-stream")):
        raise ConversionError(f"Unexpected MIME type: {mime}")
    return mime
