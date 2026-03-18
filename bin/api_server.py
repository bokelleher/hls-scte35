#!/usr/bin/env python3
"""
REST API server for hls-scte35 pipeline management.

Provides endpoints to start, stop, and query pipeline instances.
Each pipeline consists of a TSDuck process (launch_tsp.sh) and
a manifest monitor process.

Usage:
    python3 api_server.py [--port 8080] [--host 0.0.0.0] [--config pipeline.toml]

Endpoints:
    POST   /api/v1/pipeline          Start a new pipeline
    DELETE /api/v1/pipeline           Stop the running pipeline
    GET    /api/v1/pipeline/status    Get pipeline status
    GET    /api/v1/health             Health check
"""

import argparse
import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

try:
    import tomllib
except ImportError:
    import tomli as tomllib

from flask import Flask, jsonify, request

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DEFAULT_CONFIG = "/opt/hls-scte35/config/pipeline.toml"
BIN_DIR = Path(__file__).resolve().parent
INSTALL_DIR = BIN_DIR.parent


def load_config(path: str = None) -> dict:
    config_path = path or os.environ.get("PIPELINE_CONFIG", DEFAULT_CONFIG)
    with open(config_path, "rb") as f:
        return tomllib.load(f)


# ---------------------------------------------------------------------------
# Pipeline Manager
# ---------------------------------------------------------------------------

class PipelineManager:
    """Manages the lifecycle of tsp and monitor subprocesses."""

    def __init__(self, config_path: str):
        self.config_path = config_path
        self.tsp_proc: subprocess.Popen | None = None
        self.monitor_proc: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._pipeline_config: dict = {}
        self._start_time: float | None = None

    @property
    def is_running(self) -> bool:
        return (
            self.tsp_proc is not None
            and self.tsp_proc.poll() is None
            and self.monitor_proc is not None
            and self.monitor_proc.poll() is None
        )

    def status(self) -> dict:
        with self._lock:
            if not self.tsp_proc and not self.monitor_proc:
                return {"state": "stopped"}

            tsp_running = self.tsp_proc and self.tsp_proc.poll() is None
            mon_running = self.monitor_proc and self.monitor_proc.poll() is None

            if tsp_running and mon_running:
                state = "running"
            elif tsp_running or mon_running:
                state = "degraded"
            else:
                state = "stopped"

            result = {
                "state": state,
                "config": self._pipeline_config,
                "tsp": {
                    "pid": self.tsp_proc.pid if self.tsp_proc else None,
                    "running": tsp_running,
                    "returncode": self.tsp_proc.returncode if self.tsp_proc and not tsp_running else None,
                },
                "monitor": {
                    "pid": self.monitor_proc.pid if self.monitor_proc else None,
                    "running": mon_running,
                    "returncode": self.monitor_proc.returncode if self.monitor_proc and not mon_running else None,
                },
            }

            if self._start_time:
                result["uptime_seconds"] = round(time.monotonic() - self._start_time, 1)

            return result

    def start(self, params: dict) -> dict:
        with self._lock:
            if self.is_running:
                return {"error": "Pipeline already running. Stop it first.", "status": 409}

            # Clean up dead processes
            self._cleanup()

            source_url = params.get("source_url")
            if not source_url:
                return {"error": "source_url is required", "status": 400}

            output_mode = params.get("output_mode", "file")
            output_file = params.get("output_file", str(INSTALL_DIR / "output" / "live.ts"))
            scte35_pid = str(params.get("scte35_pid", 500))
            output_bitrate = str(params.get("output_bitrate", 40000000))
            inject_dir = params.get("inject_dir", str(INSTALL_DIR / "inject"))
            poll_interval = params.get("poll_interval", 6.0)
            mode = params.get("mode", "auto_detect")
            log_level = params.get("log_level", "INFO")

            # Store config for status reporting
            self._pipeline_config = {
                "source_url": source_url,
                "output_mode": output_mode,
                "output_file": output_file if output_mode == "file" else None,
                "scte35_pid": int(scte35_pid),
                "mode": mode,
            }

            # Build tsp command
            tsp_cmd = [
                str(BIN_DIR / "launch_tsp.sh"),
                self.config_path,
                "--source-url", source_url,
                "--output-mode", output_mode,
                "--scte35-pid", scte35_pid,
                "--output-bitrate", output_bitrate,
                "--inject-dir", inject_dir,
            ]
            if output_mode == "file":
                tsp_cmd += ["--output-file", output_file]
            if output_mode == "udp":
                if params.get("udp_address"):
                    tsp_cmd += ["--udp-address", params["udp_address"]]
                if params.get("udp_port"):
                    tsp_cmd += ["--udp-port", str(params["udp_port"])]

            # Build monitor command
            venv_python = str(INSTALL_DIR / "venv" / "bin" / "python3")
            if not Path(venv_python).exists():
                venv_python = sys.executable

            monitor_cmd = [
                venv_python,
                str(BIN_DIR / "manifest_monitor.py"),
                self.config_path,
                "--source-url", source_url,
                "--scte35-pid", scte35_pid,
                "--mode", mode,
                "--inject-dir", inject_dir,
                "--log-level", log_level,
            ]
            if poll_interval:
                monitor_cmd += ["--poll-interval", str(poll_interval)]

            log_dir = INSTALL_DIR / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)

            try:
                self.tsp_proc = subprocess.Popen(
                    tsp_cmd,
                    stdout=open(log_dir / "tsp.log", "a"),
                    stderr=subprocess.STDOUT,
                    preexec_fn=os.setsid,
                )
                self.monitor_proc = subprocess.Popen(
                    monitor_cmd,
                    stdout=open(log_dir / "monitor.log", "a"),
                    stderr=subprocess.STDOUT,
                    preexec_fn=os.setsid,
                )
            except Exception as e:
                self._cleanup()
                return {"error": f"Failed to start pipeline: {e}", "status": 500}

            self._start_time = time.monotonic()

            return {
                "state": "started",
                "tsp_pid": self.tsp_proc.pid,
                "monitor_pid": self.monitor_proc.pid,
                "config": self._pipeline_config,
            }

    def stop(self) -> dict:
        with self._lock:
            if not self.tsp_proc and not self.monitor_proc:
                return {"state": "already_stopped"}

            stopped = []

            for name, proc in [("monitor", self.monitor_proc), ("tsp", self.tsp_proc)]:
                if proc and proc.poll() is None:
                    try:
                        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                        proc.wait(timeout=10)
                        stopped.append(name)
                    except subprocess.TimeoutExpired:
                        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                        proc.wait(timeout=5)
                        stopped.append(f"{name} (killed)")
                    except ProcessLookupError:
                        stopped.append(f"{name} (already exited)")

            self._cleanup()
            return {"state": "stopped", "stopped": stopped}

    def _cleanup(self):
        self.tsp_proc = None
        self.monitor_proc = None
        self._pipeline_config = {}
        self._start_time = None


# ---------------------------------------------------------------------------
# Flask App
# ---------------------------------------------------------------------------

def create_app(config_path: str) -> Flask:
    app = Flask(__name__)
    app.logger.setLevel(logging.INFO)
    manager = PipelineManager(config_path)

    @app.route("/api/v1/health", methods=["GET"])
    def health():
        return jsonify({"status": "ok"})

    @app.route("/api/v1/pipeline", methods=["POST"])
    def start_pipeline():
        """
        Start a pipeline.

        JSON body:
            source_url (required): HLS manifest URL
            output_mode: "file" | "udp" | "srt" (default: "file")
            output_file: path for file output
            scte35_pid: SCTE-35 PID (default: 500)
            output_bitrate: bps (default: 40000000)
            mode: "auto_detect" | "manifest_only" | "inband_only"
            poll_interval: seconds (default: 6.0)
            inject_dir: splice XML directory
            udp_address: multicast address (for udp mode)
            udp_port: multicast port (for udp mode)
            log_level: "DEBUG" | "INFO" | "WARN" | "ERROR"
        """
        params = request.get_json(force=True, silent=True) or {}
        result = manager.start(params)

        if "error" in result:
            return jsonify(result), result.pop("status", 400)
        return jsonify(result), 201

    @app.route("/api/v1/pipeline", methods=["DELETE"])
    def stop_pipeline():
        """Stop the running pipeline."""
        result = manager.stop()
        return jsonify(result)

    @app.route("/api/v1/pipeline/status", methods=["GET"])
    def pipeline_status():
        """Get current pipeline status."""
        return jsonify(manager.status())

    return app


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="hls-scte35 REST API server")
    parser.add_argument("--port", type=int, default=8080, help="Listen port")
    parser.add_argument("--host", default="0.0.0.0", help="Listen address")
    parser.add_argument(
        "--config", default=DEFAULT_CONFIG,
        help="Path to pipeline.toml config file",
    )
    args = parser.parse_args()

    app = create_app(args.config)

    print(f"hls-scte35 API server starting on http://{args.host}:{args.port}")
    print(f"  Config: {args.config}")
    print(f"  Docs:   POST /api/v1/pipeline, DELETE /api/v1/pipeline, GET /api/v1/pipeline/status")

    app.run(host=args.host, port=args.port, debug=False)


if __name__ == "__main__":
    main()
