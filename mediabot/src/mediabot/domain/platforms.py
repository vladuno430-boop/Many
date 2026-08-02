"""URL → :class:`Platform` detection rules.

Architecture note
-----------------
Detection is pure string logic with zero I/O, so it lives in the domain and is
covered by fast unit tests.  The infrastructure layer only decides *how* to
download once the platform is known.

Matching is host-based first (cheap, unambiguous) and then refined by path
patterns so that, e.g., ``youtube.com/shorts/...`` is reported as
``YOUTUBE_SHORTS`` and ``instagram.com/reel/...`` as ``INSTAGRAM_REELS``.
Anything unknown falls back to :attr:`Platform.GENERIC`, which yt-dlp may still
be able to handle — the bot therefore degrades gracefully instead of refusing.
"""

from __future__ import annotations

import re
from typing import Final
from urllib.parse import urlparse

from mediabot.domain.enums import Platform

#: host suffix -> platform.  Longest suffix wins.
_HOST_RULES: Final[tuple[tuple[str, Platform], ...]] = (
    ("music.youtube.com", Platform.YOUTUBE_MUSIC),
    ("youtube.com", Platform.YOUTUBE),
    ("youtu.be", Platform.YOUTUBE),
    ("m.youtube.com", Platform.YOUTUBE),
    ("tiktok.com", Platform.TIKTOK),
    ("vm.tiktok.com", Platform.TIKTOK),
    ("vt.tiktok.com", Platform.TIKTOK),
    ("rutube.ru", Platform.RUTUBE),
    ("vk.com", Platform.VK_VIDEO),
    ("vkvideo.ru", Platform.VK_VIDEO),
    ("m.vk.com", Platform.VK_VIDEO),
    ("instagram.com", Platform.INSTAGRAM),
    ("instagr.am", Platform.INSTAGRAM),
    ("facebook.com", Platform.FACEBOOK),
    ("fb.watch", Platform.FACEBOOK),
    ("fb.com", Platform.FACEBOOK),
    ("twitter.com", Platform.TWITTER),
    ("x.com", Platform.TWITTER),
    ("t.co", Platform.TWITTER),
    ("vimeo.com", Platform.VIMEO),
    ("dailymotion.com", Platform.DAILYMOTION),
    ("dai.ly", Platform.DAILYMOTION),
    ("twitch.tv", Platform.TWITCH),
    ("clips.twitch.tv", Platform.TWITCH),
    ("soundcloud.com", Platform.SOUNDCLOUD),
    ("on.soundcloud.com", Platform.SOUNDCLOUD),
    ("mixcloud.com", Platform.MIXCLOUD),
    ("bandcamp.com", Platform.BANDCAMP),
    ("bilibili.com", Platform.BILIBILI),
    ("b23.tv", Platform.BILIBILI),
    ("pinterest.com", Platform.PINTEREST),
    ("pin.it", Platform.PINTEREST),
    ("reddit.com", Platform.REDDIT),
    ("redd.it", Platform.REDDIT),
    ("t.me", Platform.TELEGRAM),
    ("telegram.me", Platform.TELEGRAM),
    ("ok.ru", Platform.ODNOKLASSNIKI),
    ("music.yandex.ru", Platform.YANDEX_MUSIC),
    ("music.yandex.com", Platform.YANDEX_MUSIC),
    ("open.spotify.com", Platform.SPOTIFY),
    ("spotify.com", Platform.SPOTIFY),
    ("deezer.com", Platform.DEEZER),
    ("likee.video", Platform.LIKEE),
    ("kuaishou.com", Platform.KUAISHOU),
    ("nicovideo.jp", Platform.NICONICO),
    ("streamable.com", Platform.STREAMABLE),
    ("imgur.com", Platform.IMGUR),
    ("tumblr.com", Platform.TUMBLR),
    ("linkedin.com", Platform.LINKEDIN),
    ("coub.com", Platform.COUB),
)

#: (platform, compiled path pattern) -> refined platform.
_PATH_REFINEMENTS: Final[tuple[tuple[Platform, re.Pattern[str], Platform], ...]] = (
    (Platform.YOUTUBE, re.compile(r"^/shorts/"), Platform.YOUTUBE_SHORTS),
    (Platform.INSTAGRAM, re.compile(r"^/reels?/"), Platform.INSTAGRAM_REELS),
    (Platform.VK_VIDEO, re.compile(r"^/clip"), Platform.VK_CLIPS),
    (Platform.VK_VIDEO, re.compile(r"^/(clips|video_ext)"), Platform.VK_CLIPS),
)

#: Hosts that are known aggregators/shorteners we refuse to follow blindly.
_SHORTENER_HOSTS: Final[frozenset[str]] = frozenset(
    {"bit.ly", "tinyurl.com", "goo.gl", "ow.ly", "is.gd", "cutt.ly", "shorturl.at"}
)


def normalize_host(url: str) -> str:
    """Return the lower-cased host of ``url`` without a ``www.`` prefix."""
    host = (urlparse(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def detect_platform(url: str) -> Platform:
    """Detect the source platform of ``url``.

    The function never raises: unknown hosts map to :attr:`Platform.GENERIC`
    so the extractor still gets a chance to handle them.
    """
    parsed = urlparse(url)
    host = normalize_host(url)
    if not host:
        return Platform.GENERIC

    matched: Platform | None = None
    matched_len = -1
    for suffix, platform in _HOST_RULES:
        if (host == suffix or host.endswith("." + suffix)) and len(suffix) > matched_len:
            matched, matched_len = platform, len(suffix)

    if matched is None:
        return Platform.GENERIC

    path = parsed.path or "/"
    for source, pattern, refined in _PATH_REFINEMENTS:
        if matched is source and pattern.search(path):
            return refined
    return matched


def is_url_shortener(url: str) -> bool:
    """Return ``True`` for generic link shorteners (handled with extra care)."""
    return normalize_host(url) in _SHORTENER_HOSTS


def supported_platforms() -> tuple[Platform, ...]:
    """Every platform we advertise in the UI, excluding the generic fallback."""
    return tuple(platform for platform in Platform if platform is not Platform.GENERIC)
