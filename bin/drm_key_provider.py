#!/usr/bin/env python3
"""
DRM Key Provider for hls-scte35 pipeline.

Manages content key acquisition for decrypting DRM-protected HLS streams.
Keys are never logged — only key IDs and server hostnames appear in logs.

Supported providers:
  - StaticKeyProvider: pre-shared key from config or environment variable
  - HLSKeyProvider: fetches key from EXT-X-KEY URI (AES-128 identity format)

Future providers (not yet implemented):
  - WidevineKeyProvider: Widevine license proxy integration
  - SPEKEKeyProvider: CPIX/SPEKE key server
"""

import logging
import os
import time
from abc import ABC, abstractmethod
from urllib.parse import urlparse

import requests

logger = logging.getLogger("drm_key_provider")


class KeyProvider(ABC):
    """Abstract base for key acquisition."""

    @abstractmethod
    def get_key(
        self, key_uri: str | None = None, key_id: str | None = None
    ) -> tuple[bytes, bytes | None]:
        """
        Return (key_bytes_16, iv_bytes_or_None).

        Raises ValueError if the key cannot be obtained.
        """


class StaticKeyProvider(KeyProvider):
    """Pre-shared key from config or DRM_KEY environment variable."""

    def __init__(self, key_hex: str, iv_hex: str | None = None):
        # Env var takes precedence
        key_hex = os.environ.get("DRM_KEY", key_hex)

        if not key_hex:
            raise ValueError("No DRM key provided")

        key_hex = key_hex.strip()
        if len(key_hex) != 32:
            raise ValueError(
                f"DRM key must be 32 hex digits (got {len(key_hex)})"
            )

        self._key = bytes.fromhex(key_hex)

        self._iv: bytes | None = None
        if iv_hex:
            iv_hex = iv_hex.strip()
            if len(iv_hex) != 32:
                raise ValueError(
                    f"DRM IV must be 32 hex digits (got {len(iv_hex)})"
                )
            self._iv = bytes.fromhex(iv_hex)

        logger.info(
            "StaticKeyProvider initialized (key=****%s)", key_hex[-4:]
        )

    def get_key(
        self, key_uri: str | None = None, key_id: str | None = None
    ) -> tuple[bytes, bytes | None]:
        return self._key, self._iv


class HLSKeyProvider(KeyProvider):
    """
    Fetches key from the EXT-X-KEY URI (AES-128 identity keyformat).

    The URI returns the raw 16-byte key. Optional auth headers are sent
    with the request.
    """

    def __init__(
        self,
        headers: dict[str, str] | None = None,
        cache_ttl: float = 300.0,
    ):
        self._session = requests.Session()
        if headers:
            self._session.headers.update(headers)
        self._cache: dict[str, tuple[bytes, float]] = {}
        self._cache_ttl = cache_ttl

        logger.info(
            "HLSKeyProvider initialized (cache_ttl=%.0fs, auth=%s)",
            cache_ttl,
            "yes" if headers else "no",
        )

    def get_key(
        self, key_uri: str | None = None, key_id: str | None = None
    ) -> tuple[bytes, bytes | None]:
        if not key_uri:
            raise ValueError("HLSKeyProvider requires a key_uri")

        # Check cache
        cached = self._cache.get(key_uri)
        if cached:
            key_bytes, fetched_at = cached
            if time.monotonic() - fetched_at < self._cache_ttl:
                return key_bytes, None

        # Fetch key
        parsed = urlparse(key_uri)
        logger.info("Fetching key from %s", parsed.hostname)

        if parsed.scheme == "http":
            logger.warning(
                "Key server uses plaintext HTTP — keys may be intercepted"
            )

        resp = self._session.get(key_uri, timeout=10)
        resp.raise_for_status()

        key_bytes = resp.content
        if len(key_bytes) != 16:
            raise ValueError(
                f"Key server returned {len(key_bytes)} bytes, expected 16"
            )

        # Cache it
        self._cache[key_uri] = (key_bytes, time.monotonic())
        logger.info(
            "Key fetched successfully (****%s)", key_bytes.hex()[-4:]
        )

        return key_bytes, None


def create_provider(config: dict) -> KeyProvider | None:
    """
    Factory: create the appropriate KeyProvider from the [drm] config section.

    Returns None if DRM mode is "none" or not configured.
    """
    drm = config.get("drm", {})
    mode = drm.get("mode", "none")

    if mode == "none":
        return None

    if mode == "aes128":
        key_hex = os.environ.get("DRM_KEY") or drm.get("key", "")
        iv_hex = drm.get("iv")
        return StaticKeyProvider(key_hex, iv_hex)

    if mode == "auto":
        headers = drm.get("key_server_headers", {})
        cache_ttl = drm.get("cache_ttl", 300.0)
        return HLSKeyProvider(headers=headers, cache_ttl=cache_ttl)

    raise ValueError(f"Unknown DRM mode: {mode}")
