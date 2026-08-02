"""Unit tests for platform detection, URL safety and other security helpers."""

from __future__ import annotations

import pytest

from mediabot.core.exceptions import AuthenticationError, UnsafeUrlError
from mediabot.core.security import (
    JWTService,
    TokenEncryptor,
    escape_html,
    extract_urls,
    hash_password,
    sanitize_filename,
    validate_public_url,
    verify_password,
)
from mediabot.domain.enums import Platform
from mediabot.domain.platforms import detect_platform, is_url_shortener, normalize_host

pytestmark = pytest.mark.unit


class TestPlatformDetection:
    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", Platform.YOUTUBE),
            ("https://youtu.be/dQw4w9WgXcQ", Platform.YOUTUBE),
            ("https://youtube.com/shorts/abc123", Platform.YOUTUBE_SHORTS),
            ("https://music.youtube.com/watch?v=x", Platform.YOUTUBE_MUSIC),
            ("https://www.tiktok.com/@user/video/123", Platform.TIKTOK),
            ("https://vm.tiktok.com/ZM123/", Platform.TIKTOK),
            ("https://rutube.ru/video/abc/", Platform.RUTUBE),
            ("https://vk.com/video-1_2", Platform.VK_VIDEO),
            ("https://vk.com/clip-1_2", Platform.VK_CLIPS),
            ("https://www.instagram.com/p/Cabc/", Platform.INSTAGRAM),
            ("https://www.instagram.com/reel/Cabc/", Platform.INSTAGRAM_REELS),
            ("https://x.com/user/status/1", Platform.TWITTER),
            ("https://twitter.com/user/status/1", Platform.TWITTER),
            ("https://soundcloud.com/artist/track", Platform.SOUNDCLOUD),
            ("https://open.spotify.com/track/abc", Platform.SPOTIFY),
            ("https://music.yandex.ru/album/1/track/2", Platform.YANDEX_MUSIC),
            ("https://example.org/video.mp4", Platform.GENERIC),
        ],
    )
    def test_detects_the_right_platform(self, url: str, expected: Platform) -> None:
        assert detect_platform(url) is expected

    def test_unknown_hosts_fall_back_to_generic(self) -> None:
        assert detect_platform("https://some-random-site.tld/watch/1") is Platform.GENERIC

    def test_normalize_host_strips_www(self) -> None:
        assert normalize_host("https://WWW.Example.COM/path") == "example.com"

    def test_shorteners_are_recognised(self) -> None:
        assert is_url_shortener("https://bit.ly/abc")
        assert not is_url_shortener("https://youtube.com/watch?v=1")

    def test_streaming_catalogues_are_metadata_only(self) -> None:
        assert Platform.SPOTIFY.metadata_only
        assert Platform.YANDEX_MUSIC.metadata_only
        assert not Platform.YOUTUBE.metadata_only

    def test_audio_only_platforms(self) -> None:
        assert Platform.SOUNDCLOUD.is_audio_only
        assert not Platform.TIKTOK.is_audio_only


class TestUrlSafety:
    def test_accepts_a_public_https_url(self) -> None:
        url = "https://www.youtube.com/watch?v=1"
        assert validate_public_url(url, resolve_dns=False) == url

    @pytest.mark.parametrize(
        "url",
        [
            "ftp://example.com/file",
            "file:///etc/passwd",
            "javascript:alert(1)",
            "http://localhost/admin",
            "http://user:pass@example.com/",
            "http://service.internal/secret",
            "http://example.com/\nX-Injected: 1",
            "http://example.com/`whoami`",
            "",
        ],
    )
    def test_rejects_dangerous_urls(self, url: str) -> None:
        with pytest.raises(UnsafeUrlError):
            validate_public_url(url, resolve_dns=False)

    def test_rejects_private_addresses(self) -> None:
        with pytest.raises(UnsafeUrlError):
            validate_public_url("http://127.0.0.1:8000/", resolve_dns=True)
        with pytest.raises(UnsafeUrlError):
            validate_public_url("http://10.0.0.5/", resolve_dns=True)

    def test_rejects_overly_long_urls(self) -> None:
        with pytest.raises(UnsafeUrlError):
            validate_public_url("https://example.com/" + "a" * 3000, resolve_dns=False)

    def test_extracts_urls_in_order_without_duplicates(self) -> None:
        text = "see https://a.com/1 and https://b.com/2, also https://a.com/1"
        assert extract_urls(text) == ["https://a.com/1", "https://b.com/2"]

    def test_strips_trailing_punctuation(self) -> None:
        assert extract_urls("watch https://a.com/x.") == ["https://a.com/x"]


class TestSanitisation:
    def test_escapes_html_entities(self) -> None:
        assert escape_html("<b>x</b> & 'y'") == "&lt;b&gt;x&lt;/b&gt; &amp; &#x27;y&#x27;"

    @pytest.mark.parametrize(
        ("raw", "forbidden"),
        [
            ("../../etc/passwd", ".."),
            ("a/b\\c", "/"),
            ("na\x00me", "\x00"),
        ],
    )
    def test_filenames_cannot_escape_the_directory(self, raw: str, forbidden: str) -> None:
        assert forbidden not in sanitize_filename(raw)

    def test_filename_is_truncated(self) -> None:
        assert len(sanitize_filename("x" * 500)) <= 120

    def test_empty_filename_gets_a_random_name(self) -> None:
        assert sanitize_filename("   ").startswith("media_")


class TestPasswordsAndTokens:
    def test_password_round_trip(self) -> None:
        hashed = hash_password("s3cret-password")
        assert hashed != "s3cret-password"
        assert verify_password("s3cret-password", hashed)
        assert not verify_password("wrong", hashed)

    def test_jwt_round_trip(self, settings) -> None:
        service = JWTService(settings.security)
        pair = service.create_token_pair("42", role="admin")
        claims = service.decode(pair.access_token, expected_type="access")
        assert claims["sub"] == "42"
        assert claims["role"] == "admin"
        assert claims["jti"]

    def test_access_token_is_not_a_refresh_token(self, settings) -> None:
        service = JWTService(settings.security)
        pair = service.create_token_pair("42")
        with pytest.raises(AuthenticationError):
            service.decode(pair.access_token, expected_type="refresh")

    def test_tampered_token_is_rejected(self, settings) -> None:
        service = JWTService(settings.security)
        pair = service.create_token_pair("42")
        with pytest.raises(AuthenticationError):
            service.decode(pair.access_token + "x")

    def test_encryption_round_trip(self, settings) -> None:
        encryptor = TokenEncryptor(settings.security)
        ciphertext = encryptor.encrypt("provider-secret")
        assert ciphertext != "provider-secret"
        assert encryptor.decrypt(ciphertext) == "provider-secret"
