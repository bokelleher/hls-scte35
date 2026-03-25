"""
End-to-end test: API → PipelineRegistry → subprocess launch → status → cleanup.

Uses mocked subprocesses (no real tsp/ffmpeg) but exercises the full
create → status → delete lifecycle through the Flask API.
"""

import json
import os
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))

from api_server import create_app, Pipeline


@pytest.fixture(autouse=True)
def _fast_monitor_ready():
    """Skip monitor ready wait in tests (mocked processes never write files)."""
    original = Pipeline.MONITOR_READY_TIMEOUT
    Pipeline.MONITOR_READY_TIMEOUT = 0.1
    yield
    Pipeline.MONITOR_READY_TIMEOUT = original


@pytest.fixture
def app_and_client(tmp_path):
    """Create a fully configured app with temp dirs and test client."""
    config_path = tmp_path / "pipeline.toml"
    config_path.write_text(
        '[source]\nurl = "http://example.com/test.m3u8"\n'
        '[scte35]\npid = 500\nmode = "auto_detect"\n'
        '[tsduck]\noutput_mode = "file"\nfile_path = "/tmp/test.ts"\n'
        f'inject_dir = "{tmp_path / "inject"}"\noutput_bitrate = 6000000\n'
        f'[logging]\nlevel = "INFO"\nlog_dir = "{tmp_path / "logs"}"\n'
        '[drm]\nmode = "none"\n'
    )
    app = create_app(str(config_path))
    app.config["TESTING"] = True
    with app.test_client() as client:
        yield app, client


def _mock_proc(pid=12345):
    proc = MagicMock()
    proc.pid = pid
    proc.poll.return_value = None  # running
    proc.returncode = None
    return proc


class TestEndToEnd:
    """Full lifecycle: create → list → get → metrics → delete → verify gone."""

    @patch("api_server.subprocess.Popen")
    def test_full_pipeline_lifecycle(self, mock_popen, app_and_client):
        app, client = app_and_client

        # Each Popen call returns a fresh mock (tsp, then monitor)
        mon_proc = _mock_proc(pid=1002)
        tsp_proc = _mock_proc(pid=1001)
        mock_popen.side_effect = [mon_proc, tsp_proc]

        # --- CREATE ---
        resp = client.post(
            "/api/v1/pipelines",
            data=json.dumps({"source_url": "http://example.com/live.m3u8"}),
            content_type="application/json",
        )
        assert resp.status_code == 201
        data = resp.json
        assert data["state"] == "running"
        pipeline_id = data["id"]
        assert pipeline_id  # non-empty

        # Verify two Popen calls (tsp + monitor)
        assert mock_popen.call_count == 2

        # --- LIST ---
        resp = client.get("/api/v1/pipelines")
        assert resp.status_code == 200
        pipelines = resp.json["pipelines"]
        assert len(pipelines) == 1
        assert pipelines[0]["id"] == pipeline_id
        assert pipelines[0]["state"] == "running"

        # --- GET ---
        resp = client.get(f"/api/v1/pipelines/{pipeline_id}")
        assert resp.status_code == 200
        detail = resp.json
        assert detail["id"] == pipeline_id
        assert detail["tsp"]["pid"] == 1001
        assert detail["monitor"]["pid"] == 1002
        assert detail["tsp"]["running"] is True
        assert detail["monitor"]["running"] is True
        assert "uptime_seconds" in detail

        # --- METRICS ---
        resp = client.get("/api/v1/metrics")
        assert resp.status_code == 200

        # --- DELETE ---
        # Simulate processes still running so stop() can SIGTERM them
        tsp_proc.poll.return_value = None
        mon_proc.poll.return_value = None
        tsp_proc.wait.return_value = 0
        mon_proc.wait.return_value = 0

        resp = client.delete(f"/api/v1/pipelines/{pipeline_id}")
        assert resp.status_code == 200
        assert resp.json["state"] == "stopped"

        # --- VERIFY GONE ---
        resp = client.get(f"/api/v1/pipelines/{pipeline_id}")
        assert resp.status_code == 404

        resp = client.get("/api/v1/pipelines")
        assert resp.json["pipelines"] == []

    @patch("api_server.subprocess.Popen")
    def test_multiple_pipelines(self, mock_popen, app_and_client):
        """Create multiple pipelines, verify isolation, delete all."""
        app, client = app_and_client

        ids = []
        for i in range(3):
            mock_popen.side_effect = [_mock_proc(pid=2000 + i * 2), _mock_proc(pid=2001 + i * 2)]
            resp = client.post(
                "/api/v1/pipelines",
                data=json.dumps({"source_url": f"http://example.com/stream{i}.m3u8"}),
                content_type="application/json",
            )
            assert resp.status_code == 201
            ids.append(resp.json["id"])

        # All three should be listed
        resp = client.get("/api/v1/pipelines")
        assert len(resp.json["pipelines"]) == 3

        # Each has a unique ID
        assert len(set(ids)) == 3

        # Delete all at once
        resp = client.delete("/api/v1/pipelines")
        assert resp.status_code == 200
        assert len(resp.json["stopped"]) == 3

        # Verify empty
        resp = client.get("/api/v1/pipelines")
        assert resp.json["pipelines"] == []

    @patch("api_server.subprocess.Popen")
    def test_legacy_endpoint_lifecycle(self, mock_popen, app_and_client):
        """Legacy single-pipeline endpoints still work."""
        app, client = app_and_client

        mock_popen.side_effect = [_mock_proc(), _mock_proc()]
        resp = client.post(
            "/api/v1/pipeline",
            data=json.dumps({"source_url": "http://example.com/live.m3u8"}),
            content_type="application/json",
        )
        assert resp.status_code == 201

        resp = client.get("/api/v1/pipeline/status")
        assert resp.status_code == 200
        assert resp.json["state"] == "running"

        resp = client.delete("/api/v1/pipeline")
        assert resp.status_code == 200

        resp = client.get("/api/v1/pipeline/status")
        assert resp.json["state"] == "stopped"

    def test_validation_rejects_bad_params(self, app_and_client):
        """Config validation rejects out-of-bounds parameters."""
        _, client = app_and_client

        # Bad poll_interval
        resp = client.post(
            "/api/v1/pipelines",
            data=json.dumps({
                "source_url": "http://example.com/live.m3u8",
                "poll_interval": 0.01,
            }),
            content_type="application/json",
        )
        assert resp.status_code == 400
        assert "poll_interval" in resp.json["error"]

        # Bad scte35_pid
        resp = client.post(
            "/api/v1/pipelines",
            data=json.dumps({
                "source_url": "http://example.com/live.m3u8",
                "scte35_pid": 99999,
            }),
            content_type="application/json",
        )
        assert resp.status_code == 400
        assert "scte35_pid" in resp.json["error"]

        # Bad output_bitrate
        resp = client.post(
            "/api/v1/pipelines",
            data=json.dumps({
                "source_url": "http://example.com/live.m3u8",
                "output_bitrate": 0,
            }),
            content_type="application/json",
        )
        assert resp.status_code == 400
        assert "output_bitrate" in resp.json["error"]

    @patch("api_server.subprocess.Popen")
    def test_degraded_state_when_one_process_dies(self, mock_popen, app_and_client):
        """Pipeline reports degraded when one process exits."""
        _, client = app_and_client

        mon_proc = _mock_proc(pid=3002)
        tsp_proc = _mock_proc(pid=3001)
        mock_popen.side_effect = [mon_proc, tsp_proc]

        resp = client.post(
            "/api/v1/pipelines",
            data=json.dumps({"source_url": "http://example.com/live.m3u8"}),
            content_type="application/json",
        )
        pipeline_id = resp.json["id"]

        # Simulate monitor dying
        mon_proc.poll.return_value = 1
        mon_proc.returncode = 1

        resp = client.get(f"/api/v1/pipelines/{pipeline_id}")
        assert resp.json["state"] == "degraded"
        assert resp.json["monitor"]["running"] is False
        assert resp.json["tsp"]["running"] is True
