"""Tests for DRM key provider module."""

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))

from drm_key_provider import (
    StaticKeyProvider,
    HLSKeyProvider,
    create_provider,
)


# ---------------------------------------------------------------------------
# StaticKeyProvider
# ---------------------------------------------------------------------------


class TestStaticKeyProvider:
    def test_valid_key(self):
        provider = StaticKeyProvider("00112233445566778899aabbccddeeff")
        key, iv = provider.get_key()
        assert key == bytes.fromhex("00112233445566778899aabbccddeeff")
        assert iv is None

    def test_valid_key_with_iv(self):
        provider = StaticKeyProvider(
            "00112233445566778899aabbccddeeff",
            iv_hex="aabbccdd11223344aabbccdd11223344",
        )
        key, iv = provider.get_key()
        assert len(key) == 16
        assert len(iv) == 16

    def test_invalid_key_length(self):
        with pytest.raises(ValueError, match="32 hex digits"):
            StaticKeyProvider("0011223344")

    def test_invalid_iv_length(self):
        with pytest.raises(ValueError, match="32 hex digits"):
            StaticKeyProvider(
                "00112233445566778899aabbccddeeff",
                iv_hex="short",
            )

    def test_empty_key(self):
        with pytest.raises(ValueError, match="No DRM key"):
            StaticKeyProvider("")

    def test_env_var_override(self):
        with patch.dict(os.environ, {"DRM_KEY": "aabbccddaabbccddaabbccddaabbccdd"}):
            provider = StaticKeyProvider("00112233445566778899aabbccddeeff")
            key, _ = provider.get_key()
            assert key == bytes.fromhex("aabbccddaabbccddaabbccddaabbccdd")

    def test_key_uri_ignored(self):
        """StaticKeyProvider ignores key_uri — always returns the static key."""
        provider = StaticKeyProvider("00112233445566778899aabbccddeeff")
        key, _ = provider.get_key(key_uri="http://example.com/key")
        assert len(key) == 16


# ---------------------------------------------------------------------------
# HLSKeyProvider
# ---------------------------------------------------------------------------


class TestHLSKeyProvider:
    def test_requires_key_uri(self):
        provider = HLSKeyProvider()
        with pytest.raises(ValueError, match="requires a key_uri"):
            provider.get_key()

    @patch("drm_key_provider.requests.Session")
    def test_fetch_key(self, mock_session_class):
        mock_session = MagicMock()
        mock_resp = MagicMock()
        mock_resp.content = b"\x00" * 16
        mock_session.get.return_value = mock_resp
        mock_session_class.return_value = mock_session

        provider = HLSKeyProvider()
        provider._session = mock_session

        key, iv = provider.get_key(key_uri="https://keyserver.example.com/key/123")
        assert len(key) == 16
        assert iv is None
        mock_session.get.assert_called_once()

    @patch("drm_key_provider.requests.Session")
    def test_invalid_key_length_from_server(self, mock_session_class):
        mock_session = MagicMock()
        mock_resp = MagicMock()
        mock_resp.content = b"\x00" * 10  # Wrong length
        mock_session.get.return_value = mock_resp
        mock_session_class.return_value = mock_session

        provider = HLSKeyProvider()
        provider._session = mock_session

        with pytest.raises(ValueError, match="expected 16"):
            provider.get_key(key_uri="https://keyserver.example.com/key/123")

    @patch("drm_key_provider.requests.Session")
    def test_caching(self, mock_session_class):
        mock_session = MagicMock()
        mock_resp = MagicMock()
        mock_resp.content = b"\x00" * 16
        mock_session.get.return_value = mock_resp
        mock_session_class.return_value = mock_session

        provider = HLSKeyProvider(cache_ttl=300.0)
        provider._session = mock_session

        uri = "https://keyserver.example.com/key/456"
        provider.get_key(key_uri=uri)
        provider.get_key(key_uri=uri)

        # Should only fetch once due to caching
        assert mock_session.get.call_count == 1

    def test_auth_headers_passed(self):
        provider = HLSKeyProvider(headers={"Authorization": "Bearer token123"})
        assert provider._session.headers["Authorization"] == "Bearer token123"


# ---------------------------------------------------------------------------
# create_provider factory
# ---------------------------------------------------------------------------


class TestCreateProvider:
    def test_none_mode(self):
        assert create_provider({"drm": {"mode": "none"}}) is None

    def test_no_drm_section(self):
        assert create_provider({}) is None

    def test_aes128_mode(self):
        provider = create_provider({
            "drm": {"mode": "aes128", "key": "00112233445566778899aabbccddeeff"}
        })
        assert isinstance(provider, StaticKeyProvider)

    def test_auto_mode(self):
        provider = create_provider({"drm": {"mode": "auto"}})
        assert isinstance(provider, HLSKeyProvider)

    def test_unknown_mode(self):
        with pytest.raises(ValueError, match="Unknown DRM mode"):
            create_provider({"drm": {"mode": "widevine"}})
