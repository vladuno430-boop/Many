"""Translation of a user's choice into a yt-dlp format selector.

Architecture note
-----------------
This is deliberately *not* in the infrastructure layer: building the selector
string is pure business logic (which stream do we want for this tier and this
request?) and deserves fast unit tests.  The infrastructure adapter only feeds
the resulting string to yt-dlp.
"""

from __future__ import annotations

from dataclasses import dataclass

from mediabot.domain.enums import (
    AudioFormat,
    AudioQuality,
    MediaKind,
    VideoFormat,
    VideoQuality,
)
from mediabot.domain.value_objects import DownloadRequest

#: Formats that must be produced by an FFmpeg post-processor rather than by a
#: direct stream copy.
_TRANSCODE_AUDIO: frozenset[AudioFormat] = frozenset(
    {AudioFormat.MP3, AudioFormat.FLAC, AudioFormat.WAV, AudioFormat.OGG}
)


@dataclass(frozen=True, slots=True)
class FormatPlan:
    """Everything the downloader needs to fetch and post-process a request."""

    selector: str
    merge_output_format: str | None
    audio_codec: str | None
    audio_bitrate_kbps: int | None
    needs_conversion: bool
    remux_video_to: str | None
    embed_thumbnail: bool
    embed_metadata: bool

    @property
    def is_audio(self) -> bool:
        return self.audio_codec is not None


class FormatSelector:
    """Build a :class:`FormatPlan` from a :class:`DownloadRequest`."""

    def build(self, request: DownloadRequest) -> FormatPlan:
        if request.kind is MediaKind.AUDIO:
            return self._audio_plan(request)
        return self._video_plan(request)

    # ------------------------------------------------------------------ #
    # Video
    # ------------------------------------------------------------------ #
    def _video_plan(self, request: DownloadRequest) -> FormatPlan:
        quality = request.video_quality or VideoQuality.P720
        container = request.video_format or VideoFormat.MP4

        if quality is VideoQuality.AUDIO_ONLY:
            return self._audio_plan(request)

        height_clause = "" if quality is VideoQuality.BEST else f"[height<={quality.height}]"

        # Prefer a pre-muxed stream when one exists (no FFmpeg pass needed),
        # otherwise merge the best video+audio pair for the requested ceiling.
        if container is VideoFormat.WEBM:
            selector = (
                f"bestvideo{height_clause}[ext=webm]+bestaudio[ext=webm]/"
                f"bestvideo{height_clause}+bestaudio/best{height_clause}"
            )
        elif container is VideoFormat.MP4:
            selector = (
                f"bestvideo{height_clause}[ext=mp4]+bestaudio[ext=m4a]/"
                f"best{height_clause}[ext=mp4]/"
                f"bestvideo{height_clause}+bestaudio/best{height_clause}"
            )
        else:  # MKV — any codec combination is legal inside Matroska.
            selector = f"bestvideo{height_clause}+bestaudio/best{height_clause}"

        return FormatPlan(
            selector=selector,
            merge_output_format=container.value,
            audio_codec=None,
            audio_bitrate_kbps=None,
            needs_conversion=container is not VideoFormat.MP4,
            remux_video_to=container.value,
            embed_thumbnail=False,
            embed_metadata=request.embed_metadata,
        )

    # ------------------------------------------------------------------ #
    # Audio
    # ------------------------------------------------------------------ #
    def _audio_plan(self, request: DownloadRequest) -> FormatPlan:
        audio_format = request.audio_format or AudioFormat.MP3
        quality = request.audio_quality or AudioQuality.KBPS_192

        if quality is AudioQuality.LOSSLESS and not audio_format.is_lossless:
            # A lossless request forces a lossless container.
            audio_format = AudioFormat.FLAC

        if audio_format is AudioFormat.M4A:
            selector = "bestaudio[ext=m4a]/bestaudio/best"
        elif audio_format is AudioFormat.OGG:
            selector = "bestaudio[ext=webm]/bestaudio/best"
        else:
            selector = "bestaudio/best"

        bitrate = None if quality is AudioQuality.LOSSLESS else quality.bitrate
        return FormatPlan(
            selector=selector,
            merge_output_format=None,
            audio_codec=audio_format.value,
            audio_bitrate_kbps=bitrate,
            needs_conversion=audio_format in _TRANSCODE_AUDIO,
            remux_video_to=None,
            embed_thumbnail=request.embed_thumbnail and audio_format is not AudioFormat.WAV,
            embed_metadata=request.embed_metadata,
        )
