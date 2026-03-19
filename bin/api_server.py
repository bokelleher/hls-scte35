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
import functools
import logging
import os
import re
import signal
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

try:
    import tomllib
except ImportError:
    import tomli as tomllib

from flask import Flask, Response, jsonify, request

from prometheus_metrics import metrics as prom, render_prometheus

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DEFAULT_CONFIG = "/opt/hls-scte35/config/pipeline.toml"
BIN_DIR = Path(__file__).resolve().parent
INSTALL_DIR = BIN_DIR.parent


def _redact_key(key: str | None) -> str | None:
    """Redact a hex key for safe display — show only last 4 chars."""
    if not key:
        return None
    return "****" + key[-4:]


def load_config(path: str = None) -> dict:
    config_path = path or os.environ.get("PIPELINE_CONFIG", DEFAULT_CONFIG)
    with open(config_path, "rb") as f:
        return tomllib.load(f)


# ---------------------------------------------------------------------------
# Input Validation
# ---------------------------------------------------------------------------

ALLOWED_URL_SCHEMES = {"http", "https"}
SAFE_PATH_RE = re.compile(r"^[a-zA-Z0-9_./-]+$")


def validate_source_url(url: str) -> str | None:
    """Validate source URL. Returns error message or None if valid."""
    parsed = urlparse(url)
    if parsed.scheme not in ALLOWED_URL_SCHEMES:
        return f"Invalid URL scheme '{parsed.scheme}'. Only http/https allowed."
    if not parsed.hostname:
        return "URL must have a hostname."
    # Block common SSRF targets
    hostname = parsed.hostname.lower()
    if hostname in ("localhost", "127.0.0.1", "0.0.0.0", "::1", "[::1]"):
        if not os.environ.get("ALLOW_LOCALHOST_SOURCES"):
            return (
                "Localhost sources blocked (SSRF protection). "
                "Set ALLOW_LOCALHOST_SOURCES=1 to override."
            )
    if hostname.startswith("169.254.") or hostname.startswith("metadata"):
        return "Cloud metadata endpoints blocked (SSRF protection)."
    return None


def validate_output_path(path: str) -> str | None:
    """Validate output file path. Returns error message or None if valid."""
    resolved = Path(path).resolve()
    # Must be under a safe base directory
    allowed_bases = [
        Path("/opt/hls-scte35/output"),
        Path("/tmp"),
        Path("/var/tmp"),
    ]
    if not any(str(resolved).startswith(str(b)) for b in allowed_bases):
        return f"Output path must be under /opt/hls-scte35/output, /tmp, or /var/tmp."
    return None


# ---------------------------------------------------------------------------
# API Key Authentication
# ---------------------------------------------------------------------------

def require_api_key(f):
    """Decorator that enforces API key auth if API_KEY env var is set."""
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        api_key = os.environ.get("API_KEY")
        if not api_key:
            return f(*args, **kwargs)  # No key configured = no auth
        provided = request.headers.get("X-API-Key") or request.args.get("api_key")
        if provided != api_key:
            return jsonify({"error": "Unauthorized. Provide X-API-Key header."}), 401
        return f(*args, **kwargs)
    return decorated


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

class Metrics:
    """Bridges the old JSON metrics API with the shared Prometheus store."""

    def __init__(self):
        self.start_time = time.monotonic()

    def inc(self, counter: str, amount: int = 1):
        # Map old counter names to Prometheus names
        name_map = {
            "pipelines_created": "pipelines_created_total",
            "pipelines_stopped": "pipelines_stopped_total",
            "pipelines_failed": "pipelines_failed_total",
            "restarts_total": "pipeline_restarts_total",
        }
        prom_name = name_map.get(counter, counter)
        prom.inc(prom_name, amount)

    def snapshot(self) -> dict:
        return {
            "pipelines_created": int(prom.get("pipelines_created_total")),
            "pipelines_stopped": int(prom.get("pipelines_stopped_total")),
            "pipelines_failed": int(prom.get("pipelines_failed_total")),
            "restarts_total": int(prom.get("pipeline_restarts_total")),
            "uptime_seconds": round(time.monotonic() - self.start_time, 1),
        }


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

        # Process supervision
        self._max_restarts = 5
        self._restart_window = 300.0  # seconds
        self._restart_times: list[float] = []
        self._supervisor_thread: threading.Thread | None = None
        self._supervisor_stop = threading.Event()
        self._tsp_cmd: list[str] = []
        self._monitor_cmd: list[str] = []
        self._proc_env: dict = {}

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
                "drm_mode": self.params.get("drm_mode", "none"),
                "drm_key": _redact_key(self.params.get("drm_key")),
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
        drm_mode = params.get("drm_mode", "none")
        drm_key = params.get("drm_key")
        drm_iv = params.get("drm_iv")

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
        if output_mode == "srt":
            if params.get("srt_address"):
                tsp_cmd += ["--srt-address", params["srt_address"]]
            if params.get("srt_port"):
                tsp_cmd += ["--srt-port", str(params["srt_port"])]
            if params.get("srt_mode"):
                tsp_cmd += ["--srt-mode", params["srt_mode"]]
            if params.get("srt_latency"):
                tsp_cmd += ["--srt-latency", str(params["srt_latency"])]
        if drm_mode and drm_mode != "none":
            tsp_cmd += ["--drm-mode", drm_mode]
        if drm_iv:
            tsp_cmd += ["--drm-iv", drm_iv]
        # DRM key is passed via env var, not CLI, to avoid /proc/pid/cmdline exposure

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
        if drm_mode and drm_mode != "none":
            monitor_cmd += ["--drm-mode", drm_mode]
        if poll_interval:
            monitor_cmd += ["--poll-interval", str(poll_interval)]

        # Build env with DRM_KEY and tuning params passed via env vars
        proc_env = os.environ.copy()
        if drm_key:
            proc_env["DRM_KEY"] = drm_key

        # Tuning overrides via env (read by config_helper.py / launch_tsp.sh)
        tuning_map = {
            "ffmpeg_buffer_mode": "CFG_FFMPEG_BUFFER_MODE",
            "ffmpeg_realtime": "CFG_FFMPEG_REALTIME",
            "ffmpeg_analyzeduration": "CFG_FFMPEG_ANALYZEDURATION",
            "ffmpeg_probesize": "CFG_FFMPEG_PROBESIZE",
            "regulate_bitrate": "CFG_REGULATE_BITRATE",
        }
        for param_key, env_key in tuning_map.items():
            val = params.get(param_key)
            if val is not None:
                proc_env[env_key] = str(val)

        # Store commands for restart capability
        self._tsp_cmd = tsp_cmd
        self._monitor_cmd = monitor_cmd
        self._proc_env = proc_env

        try:
            self._launch_processes()
        except Exception as e:
            self.stop()
            raise RuntimeError(f"Failed to start pipeline: {e}")

        self._start_time = time.monotonic()

        # Start supervisor thread
        self._supervisor_stop.clear()
        self._supervisor_thread = threading.Thread(
            target=self._supervise, daemon=True,
            name=f"supervisor-{self.id}",
        )
        self._supervisor_thread.start()

        return self.status()

    def _launch_processes(self):
        """Launch tsp and monitor subprocesses."""
        self.tsp_proc = subprocess.Popen(
            self._tsp_cmd,
            stdout=open(self.log_dir / "tsp.log", "a"),
            stderr=subprocess.STDOUT,
            preexec_fn=os.setsid,
            env=self._proc_env,
        )
        self.monitor_proc = subprocess.Popen(
            self._monitor_cmd,
            stdout=open(self.log_dir / "monitor.log", "a"),
            stderr=subprocess.STDOUT,
            preexec_fn=os.setsid,
            env=self._proc_env,
        )

    def _supervise(self):
        """
        Background thread that checks process health every 5 seconds
        and restarts crashed processes.
        """
        logger = logging.getLogger(f"supervisor.{self.id}")

        while not self._supervisor_stop.wait(timeout=5.0):
            tsp_alive = self.tsp_proc and self.tsp_proc.poll() is None
            mon_alive = self.monitor_proc and self.monitor_proc.poll() is None

            # Update pipeline state gauge
            if tsp_alive and mon_alive:
                prom.set("pipeline_state", 1.0, labels={"id": self.id})
                continue  # both healthy
            elif tsp_alive or mon_alive:
                prom.set("pipeline_state", 0.5, labels={"id": self.id})
            else:
                prom.set("pipeline_state", 0.0, labels={"id": self.id})

            if self._supervisor_stop.is_set():
                break  # intentional shutdown

            # Check restart budget
            now = time.monotonic()
            self._restart_times = [
                t for t in self._restart_times
                if now - t < self._restart_window
            ]
            if len(self._restart_times) >= self._max_restarts:
                logger.error(
                    "Pipeline %s: max restarts (%d in %.0fs) exceeded, giving up",
                    self.id, self._max_restarts, self._restart_window,
                )
                break

            # Restart crashed processes
            if not tsp_alive and self.tsp_proc:
                rc = self.tsp_proc.returncode
                logger.warning(
                    "Pipeline %s: tsp exited (rc=%s), restarting", self.id, rc
                )
                try:
                    self.tsp_proc = subprocess.Popen(
                        self._tsp_cmd,
                        stdout=open(self.log_dir / "tsp.log", "a"),
                        stderr=subprocess.STDOUT,
                        preexec_fn=os.setsid,
                        env=self._proc_env,
                    )
                    self._restart_times.append(now)
                    prom.inc("pipeline_restarts_total", labels={"id": self.id, "process": "tsp"})
                except Exception as e:
                    logger.error("Pipeline %s: tsp restart failed: %s", self.id, e)

            if not mon_alive and self.monitor_proc:
                rc = self.monitor_proc.returncode
                logger.warning(
                    "Pipeline %s: monitor exited (rc=%s), restarting", self.id, rc
                )
                try:
                    self.monitor_proc = subprocess.Popen(
                        self._monitor_cmd,
                        stdout=open(self.log_dir / "monitor.log", "a"),
                        stderr=subprocess.STDOUT,
                        preexec_fn=os.setsid,
                        env=self._proc_env,
                    )
                    self._restart_times.append(now)
                    prom.inc("pipeline_restarts_total", labels={"id": self.id, "process": "monitor"})
                except Exception as e:
                    logger.error("Pipeline %s: monitor restart failed: %s", self.id, e)

    def stop(self) -> list[str]:
        # Signal supervisor to stop first
        self._supervisor_stop.set()
        if self._supervisor_thread and self._supervisor_thread.is_alive():
            self._supervisor_thread.join(timeout=10)

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

    def create(self, params: dict, metrics: Metrics | None = None) -> dict:
        source_url = params.get("source_url")
        if not source_url:
            return {"error": "source_url is required", "status": 400}

        # Validate source URL (SSRF protection)
        url_error = validate_source_url(source_url)
        if url_error:
            return {"error": url_error, "status": 400}

        # Validate output path if specified
        output_file = params.get("output_file")
        if output_file:
            path_error = validate_output_path(output_file)
            if path_error:
                return {"error": path_error, "status": 400}

        # Validate DRM key format
        drm_key = params.get("drm_key")
        if drm_key and not re.match(r'^[0-9a-fA-F]{32}$', drm_key):
            return {"error": "drm_key must be exactly 32 hex digits", "status": 400}

        with self._lock:
            pipeline_id = self._generate_id()
            pipeline = Pipeline(pipeline_id, self.config_path, params)

            try:
                result = pipeline.start()
            except RuntimeError as e:
                if metrics:
                    metrics.inc("pipelines_failed")
                return {"error": str(e), "status": 500}

            self._pipelines[pipeline_id] = pipeline
            if metrics:
                metrics.inc("pipelines_created")
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

    def remove(self, pipeline_id: str, metrics: Metrics | None = None) -> dict | None:
        with self._lock:
            pipeline = self._pipelines.pop(pipeline_id, None)
            if not pipeline:
                return None
            stopped = pipeline.stop()
            if metrics:
                metrics.inc("pipelines_stopped")
            return {"id": pipeline_id, "state": "stopped", "stopped": stopped}

    def remove_all(self, metrics: Metrics | None = None) -> list[dict]:
        with self._lock:
            results = []
            for pid in list(self._pipelines.keys()):
                pipeline = self._pipelines.pop(pid)
                stopped = pipeline.stop()
                results.append({"id": pid, "state": "stopped", "stopped": stopped})
            if metrics and results:
                metrics.inc("pipelines_stopped", len(results))
            return results


# ---------------------------------------------------------------------------
# Flask App
# ---------------------------------------------------------------------------

def create_app(config_path: str) -> Flask:
    app = Flask(__name__)
    app.logger.setLevel(logging.INFO)
    registry = PipelineRegistry(config_path)
    metrics = Metrics()

    @app.route("/api/v1/health", methods=["GET"])
    def health():
        return jsonify({"status": "ok"})

    def _collect_ffmpeg_metrics():
        """Scan running ffmpeg processes and export metrics by reason (drm/fmp4)."""
        ffmpeg_count = {"drm": 0, "fmp4": 0, "unknown": 0}
        ffmpeg_threads = {"drm": 0, "fmp4": 0, "unknown": 0}
        try:
            import glob as globmod
            for proc_dir in globmod.glob("/proc/[0-9]*/cmdline"):
                try:
                    with open(proc_dir, "rb") as f:
                        cmdline = f.read().decode("utf-8", errors="replace")
                    if "ffmpeg" not in cmdline:
                        continue
                    # Determine reason from cmdline
                    if "-decryption_key" in cmdline:
                        reason = "drm"
                    elif "mpegts" in cmdline:
                        reason = "fmp4"  # fMP4 transmux to TS
                    else:
                        reason = "unknown"
                    ffmpeg_count[reason] += 1
                    # Count threads
                    pid = proc_dir.split("/")[2]
                    try:
                        tasks = len(os.listdir(f"/proc/{pid}/task"))
                        ffmpeg_threads[reason] += tasks
                    except (OSError, FileNotFoundError):
                        pass
                except (OSError, FileNotFoundError):
                    continue
        except Exception:
            pass

        for reason, count in ffmpeg_count.items():
            prom.set("ffmpeg_processes", float(count),
                     labels={"reason": reason})
            prom.set("ffmpeg_threads", float(ffmpeg_threads[reason]),
                     labels={"reason": reason})

    @app.route("/api/v1/metrics", methods=["GET"])
    @require_api_key
    def get_metrics():
        """Return pipeline metrics. Prometheus format if Accept header requests it."""
        # Import metrics from monitor subprocesses (cross-process aggregation)
        prom.import_from_directory(str(INSTALL_DIR / "inject"))

        # Collect ffmpeg process/thread metrics
        _collect_ffmpeg_metrics()

        # Update active pipeline count gauge
        prom.set("active_pipelines", float(len(registry.list_all())))

        accept = request.headers.get("Accept", "")
        if "text/plain" in accept or "application/openmetrics" in accept:
            return Response(render_prometheus(), mimetype="text/plain; version=0.0.4")

        # JSON fallback for backwards compat
        data = metrics.snapshot()
        data["active_pipelines"] = len(registry.list_all())
        return jsonify(data)

    # --- Multi-pipeline endpoints ---

    @app.route("/api/v1/pipelines", methods=["POST"])
    @require_api_key
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
            drm_mode: "none" | "auto" | "aes128" (default: "none")
            drm_key: pre-shared AES key, 32 hex digits (for aes128 mode)
            drm_iv: pre-shared IV, 32 hex digits (optional)
        """
        params = request.get_json(force=True, silent=True) or {}
        result = registry.create(params, metrics=metrics)

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
    @require_api_key
    def delete_pipeline(pipeline_id):
        """Stop and remove a specific pipeline."""
        result = registry.remove(pipeline_id, metrics=metrics)
        if result is None:
            return jsonify({"error": "Pipeline not found"}), 404
        return jsonify(result)

    @app.route("/api/v1/pipelines", methods=["DELETE"])
    @require_api_key
    def delete_all_pipelines():
        """Stop and remove all pipelines."""
        results = registry.remove_all(metrics=metrics)
        return jsonify({"stopped": results})

    # --- Legacy single-pipeline endpoints (backwards compat) ---

    @app.route("/api/v1/pipeline", methods=["POST"])
    @require_api_key
    def legacy_start():
        """Backwards-compatible single pipeline start. Stops any existing pipelines first."""
        registry.remove_all(metrics=metrics)
        params = request.get_json(force=True, silent=True) or {}
        result = registry.create(params, metrics=metrics)

        if "error" in result:
            return jsonify(result), result.pop("status", 400)
        return jsonify(result), 201

    @app.route("/api/v1/pipeline", methods=["DELETE"])
    @require_api_key
    def legacy_stop():
        """Backwards-compatible: stop all pipelines."""
        results = registry.remove_all(metrics=metrics)
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
