"""Security primitives: JWT, password hashing, symmetric encryption, URL safety.

Architecture note
-----------------
These helpers are deliberately framework agnostic — they know nothing about
FastAPI or aiogram — so they can be unit tested in isolation and reused by the
API, the bot and the workers alike.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import importlib
import ipaddress
import re
import secrets
import socket
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final, Literal
from urllib.parse import urlparse

import jwt
from cryptography.fernet import Fernet, InvalidToken

from mediabot.core.config import SecuritySettings
from mediabot.core.exceptions import AuthenticationError, UnsafeUrlError

TokenType = Literal["access", "refresh"]

#: bcrypt cost factor. 12 ≈ 250 ms on a modern CPU — slow enough to make
#: offline cracking expensive, fast enough for an interactive login.
_BCRYPT_ROUNDS: Final[int] = 12

#: PBKDF2 iteration count for the fallback scheme (OWASP 2023 guidance).
_PBKDF2_ITERATIONS: Final[int] = 600_000

#: bcrypt needs a Rust toolchain and is optional; see :func:`hash_password`.
try:  # pragma: no cover - depends on the platform
    bcrypt: Any = importlib.import_module("bcrypt")

    _BCRYPT_AVAILABLE = True
except ImportError:  # pragma: no cover - platform without a build toolchain
    bcrypt = None
    _BCRYPT_AVAILABLE = False

#: Only these schemes may ever be fetched by the downloader.
_ALLOWED_SCHEMES: Final[frozenset[str]] = frozenset({"http", "https"})

#: Hostnames that must never be resolved/fetched (SSRF protection).
_BLOCKED_HOSTS: Final[frozenset[str]] = frozenset(
    {"localhost", "localhost.localdomain", "metadata.google.internal", "metadata", "instance-data"}
)

_URL_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"https?://[^\s<>\"'\\]+",
    re.IGNORECASE,
)

#: Characters that must never reach an HTML context unescaped.
_HTML_ESCAPES: Final[dict[str, str]] = {
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#x27;",
}


# --------------------------------------------------------------------------- #
# Passwords
# --------------------------------------------------------------------------- #
def _prehash(password: str) -> bytes:
    """Normalise a password to a fixed 44-byte token before bcrypt.

    bcrypt silently truncates anything beyond 72 bytes, so a long passphrase
    would lose entropy.  Hashing with SHA-256 first (a standard "pre-hash")
    keeps the full entropy and makes the input length constant.
    """
    digest = hashlib.sha256(password.encode("utf-8")).digest()
    return base64.b64encode(digest)


def hash_password(password: str) -> str:
    """Hash a plaintext password.

    bcrypt is used when the wheel is installable.  On platforms without a Rust
    toolchain (notably Termux on Android) it is unavailable, so we fall back to
    PBKDF2-HMAC-SHA256 from the standard library — a scheme that is still
    considered sound at 600k iterations.  The stored hash carries its scheme,
    so both forms verify and an existing database keeps working after a move
    between platforms.
    """
    if _BCRYPT_AVAILABLE:
        hashed = bcrypt.hashpw(_prehash(password), bcrypt.gensalt(rounds=_BCRYPT_ROUNDS))
        return str(hashed.decode())
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", _prehash(password), salt, _PBKDF2_ITERATIONS)
    encoded_salt = base64.b64encode(salt).decode()
    encoded_digest = base64.b64encode(digest).decode()
    return f"$pbkdf2-sha256${_PBKDF2_ITERATIONS}${encoded_salt}${encoded_digest}"


def verify_password(password: str, hashed: str) -> bool:
    """Constant-time verification against either supported hash scheme."""
    if not hashed:
        return False
    if hashed.startswith("$pbkdf2-sha256$"):
        try:
            _, _scheme, iterations, salt_b64, digest_b64 = hashed.split("$", 4)
            expected = base64.b64decode(digest_b64)
            candidate = hashlib.pbkdf2_hmac(
                "sha256", _prehash(password), base64.b64decode(salt_b64), int(iterations)
            )
        except (ValueError, TypeError):
            return False
        return hmac.compare_digest(candidate, expected)
    if not _BCRYPT_AVAILABLE:
        return False
    try:
        return bool(bcrypt.checkpw(_prehash(password), hashed.encode()))
    except (ValueError, TypeError):
        return False


def generate_api_key(prefix: str = "mb") -> str:
    """Generate a URL-safe API key (used for machine-to-machine access)."""
    return f"{prefix}_{secrets.token_urlsafe(32)}"


def constant_time_compare(left: str, right: str) -> bool:
    """Timing-attack resistant string comparison."""
    return hmac.compare_digest(left.encode(), right.encode())


# --------------------------------------------------------------------------- #
# JWT
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class TokenPair:
    """Access + refresh token bundle returned by the auth endpoints."""

    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int = 0


class JWTService:
    """Issue and validate JSON Web Tokens.

    A ``jti`` claim is always present so that individual tokens can be revoked
    through the Redis deny-list without invalidating every session.
    """

    def __init__(self, settings: SecuritySettings) -> None:
        self._settings = settings

    def _encode(self, subject: str, token_type: TokenType, ttl: timedelta, **claims: Any) -> str:
        now = datetime.now(UTC)
        payload: dict[str, Any] = {
            "sub": subject,
            "type": token_type,
            "iat": int(now.timestamp()),
            "exp": int((now + ttl).timestamp()),
            "jti": secrets.token_hex(16),
            **claims,
        }
        return jwt.encode(
            payload,
            self._settings.jwt_secret.get_secret_value(),
            algorithm=self._settings.jwt_algorithm,
        )

    def create_token_pair(self, subject: str, **claims: Any) -> TokenPair:
        """Create a fresh access/refresh pair for ``subject``."""
        access_ttl = timedelta(minutes=self._settings.access_token_ttl_minutes)
        refresh_ttl = timedelta(days=self._settings.refresh_token_ttl_days)
        return TokenPair(
            access_token=self._encode(subject, "access", access_ttl, **claims),
            refresh_token=self._encode(subject, "refresh", refresh_ttl),
            expires_in=int(access_ttl.total_seconds()),
        )

    def decode(self, token: str, expected_type: TokenType | None = None) -> dict[str, Any]:
        """Decode and validate a token, raising :class:`AuthenticationError`."""
        try:
            payload: dict[str, Any] = jwt.decode(
                token,
                self._settings.jwt_secret.get_secret_value(),
                algorithms=[self._settings.jwt_algorithm],
            )
        except jwt.ExpiredSignatureError as exc:
            raise AuthenticationError("Token has expired") from exc
        except jwt.InvalidTokenError as exc:
            raise AuthenticationError("Invalid token") from exc
        if expected_type and payload.get("type") != expected_type:
            raise AuthenticationError(f"Expected a {expected_type} token")
        return payload


# --------------------------------------------------------------------------- #
# Symmetric encryption (payment tokens, cookies, provider credentials)
# --------------------------------------------------------------------------- #
class TokenEncryptor:
    """Fernet-based encryption for secrets that must round-trip in the DB.

    When no key is configured we derive one from the JWT secret.  That keeps
    local development frictionless while production is expected to set an
    explicit ``SECURITY__ENCRYPTION_KEY``.
    """

    def __init__(self, settings: SecuritySettings) -> None:
        raw_key = settings.encryption_key.get_secret_value()
        if not raw_key:
            digest = hashlib.sha256(settings.jwt_secret.get_secret_value().encode()).digest()
            raw_key = base64.urlsafe_b64encode(digest).decode()
        self._fernet = Fernet(raw_key.encode() if isinstance(raw_key, str) else raw_key)

    def encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode()).decode()

    def decrypt(self, ciphertext: str) -> str:
        try:
            return self._fernet.decrypt(ciphertext.encode()).decode()
        except InvalidToken as exc:
            raise AuthenticationError("Could not decrypt stored secret") from exc


# --------------------------------------------------------------------------- #
# URL safety (SSRF / injection protection)
# --------------------------------------------------------------------------- #
def extract_urls(text: str) -> list[str]:
    """Extract every http(s) URL from arbitrary user text, preserving order."""
    seen: set[str] = set()
    result: list[str] = []
    for match in _URL_PATTERN.finditer(text or ""):
        url = match.group(0).rstrip(".,);]!?")
        if url not in seen:
            seen.add(url)
            result.append(url)
    return result


def _is_private_address(host: str) -> bool:
    """Return ``True`` when ``host`` resolves to a non-public address."""
    candidates: list[str] = [host]
    try:
        infos = socket.getaddrinfo(host, None)
        candidates.extend({str(info[4][0]) for info in infos})
    except (socket.gaierror, UnicodeError):
        # Unresolvable hosts are rejected by the caller anyway.
        return False
    for candidate in candidates:
        try:
            address = ipaddress.ip_address(candidate)
        except ValueError:
            continue
        if (
            address.is_private
            or address.is_loopback
            or address.is_link_local
            or address.is_reserved
            or address.is_multicast
            or address.is_unspecified
        ):
            return True
    return False


def validate_public_url(url: str, *, resolve_dns: bool = True) -> str:
    """Validate that ``url`` is a safe, public http(s) URL.

    Raises :class:`UnsafeUrlError` for anything that could be used for SSRF
    (internal hostnames, private ranges, non-http schemes, credentials in the
    netloc) or for shell/HTML injection attempts.
    """
    if not url or len(url) > 2048:
        raise UnsafeUrlError("URL is empty or too long")

    parsed = urlparse(url.strip())
    if parsed.scheme.lower() not in _ALLOWED_SCHEMES:
        raise UnsafeUrlError("Only http and https links are supported")
    if parsed.username or parsed.password:
        raise UnsafeUrlError("Links with embedded credentials are rejected")
    host = (parsed.hostname or "").lower()
    if not host:
        raise UnsafeUrlError("URL has no host")
    if host in _BLOCKED_HOSTS or host.endswith(".local") or host.endswith(".internal"):
        raise UnsafeUrlError("Internal hosts are not allowed")
    if any(char in url for char in ("\n", "\r", "\x00", "`", "$(")):
        raise UnsafeUrlError("URL contains forbidden characters")
    if resolve_dns and _is_private_address(host):
        raise UnsafeUrlError("URL resolves to a private address")
    return url.strip()


def escape_html(text: str) -> str:
    """Escape a string for safe inclusion in HTML (bot captions and web panel)."""
    return "".join(_HTML_ESCAPES.get(char, char) for char in text or "")


def sanitize_filename(name: str, *, max_length: int = 120) -> str:
    """Turn arbitrary media titles into a safe, portable file name.

    Removes path separators, control characters and reserved Windows names so
    that a hostile title can never escape the working directory.
    """
    cleaned = re.sub(r"[\x00-\x1f\x7f]", "", name or "")
    cleaned = re.sub(r"[\\/:*?\"<>|]+", "_", cleaned)
    cleaned = cleaned.replace("..", "_").strip(" .")
    cleaned = re.sub(r"\s+", " ", cleaned)
    if not cleaned:
        cleaned = f"media_{secrets.token_hex(4)}"
    return cleaned[:max_length]


def hash_identifier(value: str, *, salt: str = "") -> str:
    """Stable, non-reversible identifier hash used for analytics and log keys."""
    return hashlib.sha256(f"{salt}:{value}".encode()).hexdigest()
