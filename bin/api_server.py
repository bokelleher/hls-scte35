#!/usr/bin/env python3
"""
REST API server for hls-scte35 pipeline management.

Supports multiple concurrent pipelines, each with isolated inject dirs,
log files, and output paths.

Usage:
    python3 api_server.py [--port 8080] [--host 0.0.0.0] [--config pipeline.toml]

Endpoints:
    POST   /api/v1/pipelines              Create and start a new pipeline
    GET    /api/v1/pipelines              List all pipelines
    GET    /api/v1/pipelines/<id>         Get pipeline status
    DELETE /api/v1/pipelines/<id>         Stop and remove a pipeline
    DELETE /api/v1/pipelines              Stop all pipelines
    GET    /api/v1/health                 Health check
"""

import argparse
import logging
import os
import signal
import subprocess
import sys
import threading
import time
import uuid
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
# Pipeline (single instance)
# ---------------------------------------------------------------------------

class Pipeline:
    """Manages the lifecycle of one tsp + monitor subprocess pair."""

    def __init__(self, pipeline_id: str, config_path: str, params: dict):
        self.id = pipeline_id
        self.config_path = config_path
        self.params = params
        self.tsp_proc: subprocess.Popen | None = None
        self.monitor_proc: subprocess.Popen | None = None
        self._start_time: float | None = None

        # Per-pipeline isolated directories
        self.inject_dir = INSTALL_DIR / "inject" / self.id
        self.log_dir = INSTALL_DIR / "logs" / self.id
        self.inject_dir.mkdir(parents=True, exist_ok=True)
        self.log_dir.mkdir(parents=True, exist_ok=True)

        # Resolve output file with pipeline ID to avoid collisions
        if params.get("output_mode", "file") == "file":
            output_file = params.get("output_file")
            if not output_file:
                output_dir = INSTALL_DIR / "output"
                output_dir.mkdir(parents=True, exist_ok=True)
                params["output_file"] = str(output_dir / f"{self.id}.ts")

    @property
    def is_running(self) -> bool:
        return (
            self.tsp_proc is not None
            and self.tsp_proc.poll() is None
            and self.monitor_proc is not None
            and self.monitor_proc.poll() is None
        )

    def status(self) -> dict:
        tsp_running = self.tsp_proc and self.tsp_proc.poll() is None
        mon_running = self.monitor_proc and self.monitor_proc.poll() is None

        if tsp_running and mon_running:
            state = "running"
        elif tsp_running or mon_running:
            state = "degraded"
        elif self.tsp_proc is None and self.monitor_proc is None:
            state = "stopped"
        else:
            state = "stopped"

        result = {
            "id": self.id,
            "state": state,
            "config": {
                "source_url": self.params.get("source_url"),
                "output_mode": self.params.get("output_mode", "file"),
                "output_file": self.params.get("output_file"),
                "scte35_pid": self.params.get("scte35_pid", 500),
                "mode": self.params.get("mode", "auto_detect"),
            },
            "tsp": {
                "pid": self.tsp_proc.pid if self.tsp_proc else None,
                "running": bool(tsp_running),
                "returncode": self.tsp_proc.returncode if self.tsp_proc and not tsp_running else None,
            },
            "monitor": {
                "pid": self.monitor_proc.pid if self.monitor_proc else None,
                "running": bool(mon_running),
                "returncode": self.monitor_proc.returncode if self.monitor_proc and not mon_running else None,
            },
            "inject_dir": str(self.inject_dir),
            "log_dir": str(self.log_dir),
        }

        if self._start_time:
            result["uptime_seconds"] = round(time.monotonic() - self._start_time, 1)

        return result

    def start(self) -> dict:
        params = self.params
        source_url = params["source_url"]
        output_mode = params.get("output_mode", "file")
        output_file = params.get("output_file", str(INSTALL_DIR / "output" / f"{self.id}.ts"))
        scte35_pid = str(params.get("scte35_pid", 500))
        output_bitrate = str(params.get("output_bitrate", 40000000))
        inject_dir = str(self.inject_dir)
        poll_interval = params.get("poll_interval", 6.0)
        mode = params.get("mode", "auto_detect")
        log_level = params.get("log_level", "INFO")

        # Seed the inject file
        inject_file = self.inject_dir / "splice.xml"
        if not inject_file.exists():
            inject_file.write_text(
                '<?xml version="1.0" encoding="UTF-8"?>\n<tsduck>\n</tsduck>\n'
            )

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

        try:
            self.tsp_proc = subprocess.Popen(
                tsp_cmd,
                stdout=open(self.log_dir / "tsp.log", "a"),
                stderr=subprocess.STDOUT,
                preexec_fn=os.setsid,
            )
            self.monitor_proc = subprocess.Popen(
                monitor_cmd,
                stdout=open(self.log_dir / "monitor.log", "a"),
                stderr=subprocess.STDOUT,
                preexec_fn=os.setsid,
            )
        except Exception as e:
            self.stop()
            raise RuntimeError(f"Failed to start pipeline: {e}")

        self._start_time = time.monotonic()
        return self.status()

    def stop(self) -> list[str]:
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

        self.tsp_proc = None
        self.monitor_proc = None
        self._start_time = None
        return stopped


# ---------------------------------------------------------------------------
# Pipeline Registry
# ---------------------------------------------------------------------------

class PipelineRegistry:
    """Thread-safe registry of multiple Pipeline instances."""

    def __init__(self, config_path: str):
        self.config_path = config_path
        self._pipelines: dict[str, Pipeline] = {}
        self._lock = threading.Lock()

    def _generate_id(self) -> str:
        return uuid.uuid4().hex[:8]

    def create(self, params: dict) -> dict:
        source_url = params.get("source_url")
        if not source_url:
            return {"error": "source_url is required", "status": 400}

        with self._lock:
            pipeline_id = self._generate_id()
            pipeline = Pipeline(pipeline_id, self.config_path, params)

            try:
                result = pipeline.start()
            except RuntimeError as e:
                return {"error": str(e), "status": 500}

            self._pipelines[pipeline_id] = pipeline
            return result

    def get(self, pipeline_id: str) -> dict | None:
        with self._lock:
            pipeline = self._pipelines.get(pipeline_id)
            if not pipeline:
                return None
            return pipeline.status()

    def list_all(self) -> list[dict]:
        with self._lock:
            return [p.status() for p in self._pipelines.values()]

    def remove(self, pipeline_id: str) -> dict | None:
        with self._lock:
            pipeline = self._pipelines.pop(pipeline_id, None)
            if not pipeline:
                return None
            stopped = pipeline.stop()
            return {"id": pipeline_id, "state": "stopped", "stopped": stopped}

    def remove_all(self) -> list[dict]:
        with self._lock:
            results = []
            for pid in list(self._pipelines.keys()):
                pipeline = self._pipelines.pop(pid)
                stopped = pipeline.stop()
                results.append({"id": pid, "state": "stopped", "stopped": stopped})
            return results


# ---------------------------------------------------------------------------
# Flask App
# ---------------------------------------------------------------------------

def create_app(config_path: str) -> Flask:
    app = Flask(__name__)
    app.logger.setLevel(logging.INFO)
    registry = PipelineRegistry(config_path)

    @app.route("/api/v1/health", methods=["GET"])
    def health():
        return jsonify({"status": "ok"})

    # --- Multi-pipeline endpoints ---

    @app.route("/api/v1/pipelines", methods=["POST"])
    def create_pipeline():
        """
        Create and start a new pipeline.

        JSON body:
            source_url (required): HLS manifest URL
            output_mode: "file" | "udp" | "srt" (default: "file")
            output_file: path for file output (auto-generated if omitted)
            scte35_pid: SCTE-35 PID (default: 500)
            output_bitrate: bps (default: 40000000)
            mode: "auto_detect" | "manifest_only" | "inband_only"
            poll_interval: seconds (default: 6.0)
            inject_dir: splice XML directory (auto-generated if omitted)
            udp_address: multicast address (for udp mode)
            udp_port: multicast port (for udp mode)
            log_level: "DEBUG" | "INFO" | "WARN" | "ERROR"
        """
        params = request.get_json(force=True, silent=True) or {}
        result = registry.create(params)

        if "error" in result:
            return jsonify(result), result.pop("status", 400)
        return jsonify(result), 201

    @app.route("/api/v1/pipelines", methods=["GET"])
    def list_pipelines():
        """List all pipelines."""
        return jsonify({"pipelines": registry.list_all()})

    @app.route("/api/v1/pipelines/<pipeline_id>", methods=["GET"])
    def get_pipeline(pipeline_id):
        """Get a specific pipeline's status."""
        result = registry.get(pipeline_id)
        if result is None:
            return jsonify({"error": "Pipeline not found"}), 404
        return jsonify(result)

    @app.route("/api/v1/pipelines/<pipeline_id>", methods=["DELETE"])
    def delete_pipeline(pipeline_id):
        """Stop and remove a specific pipeline."""
        result = registry.remove(pipeline_id)
        if result is None:
            return jsonify({"error": "Pipeline not found"}), 404
        return jsonify(result)

    @app.route("/api/v1/pipelines", methods=["DELETE"])
    def delete_all_pipelines():
        """Stop and remove all pipelines."""
        results = registry.remove_all()
        return jsonify({"stopped": results})

    # --- Legacy single-pipeline endpoints (backwards compat) ---

    @app.route("/api/v1/pipeline", methods=["POST"])
    def legacy_start():
        """Backwards-compatible single pipeline start. Stops any existing pipelines first."""
        registry.remove_all()
        params = request.get_json(force=True, silent=True) or {}
        result = registry.create(params)

        if "error" in result:
            return jsonify(result), result.pop("status", 400)
        return jsonify(result), 201

    @app.route("/api/v1/pipeline", methods=["DELETE"])
    def legacy_stop():
        """Backwards-compatible: stop all pipelines."""
        results = registry.remove_all()
        if not results:
            return jsonify({"state": "already_stopped"})
        return jsonify(results[0] if len(results) == 1 else {"stopped": results})

    @app.route("/api/v1/pipeline/status", methods=["GET"])
    def legacy_status():
        """Backwards-compatible: return status of first pipeline."""
        pipelines = registry.list_all()
        if not pipelines:
            return jsonify({"state": "stopped"})
        return jsonify(pipelines[0])

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
    print(f"  Endpoints:")
    print(f"    POST   /api/v1/pipelines          Create a new pipeline")
    print(f"    GET    /api/v1/pipelines           List all pipelines")
    print(f"    GET    /api/v1/pipelines/<id>      Get pipeline status")
    print(f"    DELETE /api/v1/pipelines/<id>      Stop a pipeline")
    print(f"    DELETE /api/v1/pipelines           Stop all pipelines")

    app.run(host=args.host, port=args.port, debug=False)


if __name__ == "__main__":
    main()
