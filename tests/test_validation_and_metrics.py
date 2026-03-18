"""Tests for input validation, API auth, and metrics."""

import json
import os
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))

from api_server import (
    create_app,
    validate_source_url,
    validate_output_path,
    Metrics,
)


# ---------------------------------------------------------------------------
# URL validation
# ---------------------------------------------------------------------------


class TestValidateSourceURL:
    def test_valid_https(self):
        assert validate_source_url("https://origin.example.com/live.m3u8") is None

    def test_valid_http(self):
        assert validate_source_url("http://origin.example.com/live.m3u8") is None

    def test_rejects_file_scheme(self):
        err = validate_source_url("file:///etc/passwd")
        assert err is not None
        assert "scheme" in err.lower()

    def test_rejects_ftp_scheme(self):
        err = validate_source_url("ftp://example.com/file")
        assert err is not None

    def test_rejects_no_hostname(self):
        err = validate_source_url("http://")
        assert err is not None

    def test_blocks_localhost_by_default(self):
        err = validate_source_url("http://localhost:8899/index.m3u8")
        assert err is not None
        assert "SSRF" in err or "localhost" in err.lower()

    def test_allows_localhost_with_env_var(self):
        with patch.dict(os.environ, {"ALLOW_LOCALHOST_SOURCES": "1"}):
            assert validate_source_url("http://localhost:8899/index.m3u8") is None

    def test_blocks_127_0_0_1(self):
        err = validate_source_url("http://127.0.0.1:8899/index.m3u8")
        assert err is not None

    def test_blocks_metadata_endpoint(self):
        err = validate_source_url("http://169.254.169.254/latest/meta-data/")
        assert err is not None
        assert "metadata" in err.lower() or "SSRF" in err


# ---------------------------------------------------------------------------
# Output path validation
# ---------------------------------------------------------------------------


class TestValidateOutputPath:
    def test_valid_output_path(self):
        assert validate_output_path("/opt/hls-scte35/output/live.ts") is None

    def test_valid_tmp_path(self):
        assert validate_output_path("/tmp/output.ts") is None

    def test_rejects_etc(self):
        err = validate_output_path("/etc/output.ts")
        assert err is not None

    def test_rejects_root(self):
        err = validate_output_path("/output.ts")
        assert err is not None


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


class TestMetrics:
    def test_initial_values(self):
        m = Metrics()
        snap = m.snapshot()
        assert snap["pipelines_created"] == 0
        assert snap["pipelines_stopped"] == 0

    def test_increment(self):
        m = Metrics()
        m.inc("pipelines_created")
        m.inc("pipelines_created")
        assert m.snapshot()["pipelines_created"] == 2

    def test_uptime(self):
        m = Metrics()
        snap = m.snapshot()
        assert snap["uptime_seconds"] >= 0


# ---------------------------------------------------------------------------
# API Key Auth
# ---------------------------------------------------------------------------


@pytest.fixture
def authed_client(tmp_path):
    """Client with API_KEY set."""
    config_path = tmp_path / "pipeline.toml"
    config_path.write_text(
        '[source]\nurl = "http://example.com/test.m3u8"\n'
        '[scte35]\npid = 500\nmode = "auto_detect"\n'
        '[tsduck]\noutput_mode = "file"\nfile_path = "/tmp/test.ts"\n'
        'inject_dir = "/tmp/inject"\noutput_bitrate = 6000000\n'
        '[logging]\nlevel = "INFO"\nlog_dir = "/tmp/logs"\n'
        '[drm]\nmode = "none"\n'
    )
    with patch.dict(os.environ, {"API_KEY": "secret123"}):
        app = create_app(str(config_path))
        app.config["TESTING"] = True
        with app.test_client() as client:
            yield client


class TestAPIKeyAuth:
    def test_health_no_auth_needed(self, authed_client):
        resp = authed_client.get("/api/v1/health")
        assert resp.status_code == 200

    def test_create_without_key_rejected(self, authed_client):
        resp = authed_client.post(
            "/api/v1/pipelines",
            data=json.dumps({"source_url": "http://example.com/live.m3u8"}),
            content_type="application/json",
        )
        assert resp.status_code == 401

    def test_create_with_wrong_key_rejected(self, authed_client):
        resp = authed_client.post(
            "/api/v1/pipelines",
            data=json.dumps({"source_url": "http://example.com/live.m3u8"}),
            content_type="application/json",
            headers={"X-API-Key": "wrong"},
        )
        assert resp.status_code == 401

    @patch("api_server.subprocess.Popen")
    def test_create_with_correct_key(self, mock_popen, authed_client):
        mock_proc = MagicMock()
        mock_proc.pid = 12345
        mock_proc.poll.return_value = None
        mock_popen.return_value = mock_proc

        resp = authed_client.post(
            "/api/v1/pipelines",
            data=json.dumps({"source_url": "http://example.com/live.m3u8"}),
            content_type="application/json",
            headers={"X-API-Key": "secret123"},
        )
        assert resp.status_code == 201

    def test_metrics_endpoint_requires_auth(self, authed_client):
        resp = authed_client.get("/api/v1/metrics")
        assert resp.status_code == 401

    def test_metrics_with_auth(self, authed_client):
        resp = authed_client.get(
            "/api/v1/metrics",
            headers={"X-API-Key": "secret123"},
        )
        assert resp.status_code == 200
        assert "pipelines_created" in resp.json


# ---------------------------------------------------------------------------
# Input validation via API
# ---------------------------------------------------------------------------


@pytest.fixture
def client(tmp_path):
    config_path = tmp_path / "pipeline.toml"
    config_path.write_text(
        '[source]\nurl = "http://example.com/test.m3u8"\n'
        '[scte35]\npid = 500\nmode = "auto_detect"\n'
        '[tsduck]\noutput_mode = "file"\nfile_path = "/tmp/test.ts"\n'
        'inject_dir = "/tmp/inject"\noutput_bitrate = 6000000\n'
        '[logging]\nlevel = "INFO"\nlog_dir = "/tmp/logs"\n'
        '[drm]\nmode = "none"\n'
    )
    app = create_app(str(config_path))
    app.config["TESTING"] = True
    with app.test_client() as client:
        yield client


class TestInputValidation:
    def test_rejects_file_url(self, client):
        resp = client.post(
            "/api/v1/pipelines",
            data=json.dumps({"source_url": "file:///etc/passwd"}),
            content_type="application/json",
        )
        assert resp.status_code == 400
        assert "scheme" in resp.json["error"].lower()

    def test_rejects_bad_output_path(self, client):
        resp = client.post(
            "/api/v1/pipelines",
            data=json.dumps({
                "source_url": "http://example.com/live.m3u8",
                "output_file": "/etc/evil.ts",
            }),
            content_type="application/json",
        )
        assert resp.status_code == 400

    def test_rejects_bad_drm_key(self, client):
        resp = client.post(
            "/api/v1/pipelines",
            data=json.dumps({
                "source_url": "http://example.com/live.m3u8",
                "drm_key": "not-hex",
            }),
            content_type="application/json",
        )
        assert resp.status_code == 400
        assert "hex" in resp.json["error"].lower()
