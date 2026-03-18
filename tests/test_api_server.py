"""Tests for the REST API server."""

import json
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))

from api_server import create_app, _redact_key


# ---------------------------------------------------------------------------
# Key redaction
# ---------------------------------------------------------------------------


class TestRedactKey:
    def test_redacts_key(self):
        assert _redact_key("00112233445566778899aabbccddeeff") == "****eeff"

    def test_none_returns_none(self):
        assert _redact_key(None) is None

    def test_empty_returns_none(self):
        assert _redact_key("") is None

    def test_short_key(self):
        assert _redact_key("abcd") == "****abcd"


# ---------------------------------------------------------------------------
# API endpoints
# ---------------------------------------------------------------------------


@pytest.fixture
def client(tmp_path):
    """Create a Flask test client with a temp config."""
    config_path = tmp_path / "pipeline.toml"
    config_path.write_text(
        '[source]\nurl = "http://localhost/test.m3u8"\n'
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


class TestHealthEndpoint:
    def test_health(self, client):
        resp = client.get("/api/v1/health")
        assert resp.status_code == 200
        assert resp.json["status"] == "ok"


class TestPipelinesEndpoints:
    def test_list_empty(self, client):
        resp = client.get("/api/v1/pipelines")
        assert resp.status_code == 200
        assert resp.json["pipelines"] == []

    def test_create_missing_source_url(self, client):
        resp = client.post(
            "/api/v1/pipelines",
            data=json.dumps({"output_mode": "file"}),
            content_type="application/json",
        )
        assert resp.status_code == 400
        assert "source_url" in resp.json["error"]

    @patch("api_server.subprocess.Popen")
    def test_create_pipeline(self, mock_popen, client):
        mock_proc = MagicMock()
        mock_proc.pid = 12345
        mock_proc.poll.return_value = None
        mock_popen.return_value = mock_proc

        resp = client.post(
            "/api/v1/pipelines",
            data=json.dumps({
                "source_url": "http://example.com/live.m3u8",
                "output_mode": "file",
            }),
            content_type="application/json",
        )
        assert resp.status_code == 201
        data = resp.json
        assert "id" in data
        assert data["state"] == "running"

    @patch("api_server.subprocess.Popen")
    def test_get_pipeline(self, mock_popen, client):
        mock_proc = MagicMock()
        mock_proc.pid = 12345
        mock_proc.poll.return_value = None
        mock_popen.return_value = mock_proc

        create_resp = client.post(
            "/api/v1/pipelines",
            data=json.dumps({"source_url": "http://example.com/live.m3u8"}),
            content_type="application/json",
        )
        pid = create_resp.json["id"]

        resp = client.get(f"/api/v1/pipelines/{pid}")
        assert resp.status_code == 200
        assert resp.json["id"] == pid

    def test_get_nonexistent_pipeline(self, client):
        resp = client.get("/api/v1/pipelines/nonexistent")
        assert resp.status_code == 404

    def test_delete_nonexistent_pipeline(self, client):
        resp = client.delete("/api/v1/pipelines/nonexistent")
        assert resp.status_code == 404

    @patch("api_server.subprocess.Popen")
    def test_delete_all(self, mock_popen, client):
        mock_proc = MagicMock()
        mock_proc.pid = 12345
        mock_proc.poll.return_value = None
        mock_popen.return_value = mock_proc

        client.post(
            "/api/v1/pipelines",
            data=json.dumps({"source_url": "http://example.com/a.m3u8"}),
            content_type="application/json",
        )
        client.post(
            "/api/v1/pipelines",
            data=json.dumps({"source_url": "http://example.com/b.m3u8"}),
            content_type="application/json",
        )

        resp = client.delete("/api/v1/pipelines")
        assert resp.status_code == 200

    @patch("api_server.subprocess.Popen")
    def test_drm_key_redacted_in_status(self, mock_popen, client):
        mock_proc = MagicMock()
        mock_proc.pid = 12345
        mock_proc.poll.return_value = None
        mock_popen.return_value = mock_proc

        client.post(
            "/api/v1/pipelines",
            data=json.dumps({
                "source_url": "http://example.com/drm.m3u8",
                "drm_mode": "aes128",
                "drm_key": "00112233445566778899aabbccddeeff",
            }),
            content_type="application/json",
        )

        resp = client.get("/api/v1/pipelines")
        pipeline = resp.json["pipelines"][0]
        # Key must be redacted
        assert pipeline["config"]["drm_key"] == "****eeff"


# ---------------------------------------------------------------------------
# Legacy endpoints
# ---------------------------------------------------------------------------


class TestLegacyEndpoints:
    def test_legacy_status_when_empty(self, client):
        resp = client.get("/api/v1/pipeline/status")
        assert resp.status_code == 200
        assert resp.json["state"] == "stopped"

    def test_legacy_delete_when_empty(self, client):
        resp = client.delete("/api/v1/pipeline")
        assert resp.status_code == 200
        assert resp.json["state"] == "already_stopped"
