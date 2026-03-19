#!/usr/bin/env python3
"""
Prometheus-compatible metrics for hls-scte35 pipeline.

Provides thread-safe counters, gauges, and histograms with Prometheus
text exposition format output. No external dependencies required.

Usage:
    from prometheus_metrics import metrics, render_prometheus

    metrics.inc("scte35_events_detected_total", labels={"type": "cue_out"})
    metrics.observe("manifest_poll_duration_seconds", 0.234)
    metrics.set("pipeline_state", 1.0, labels={"id": "a1b2c3d4"})

    text = render_prometheus()
"""

import json
import os
import threading
import time
from collections import defaultdict
from pathlib import Path


class _MetricStore:
    """Thread-safe metric storage with Prometheus-compatible types."""

    def __init__(self):
        self._lock = threading.Lock()
        self._counters: dict[str, float] = defaultdict(float)
        self._gauges: dict[str, float] = {}
        self._histograms: dict[str, list[float]] = defaultdict(list)
        self._meta: dict[str, tuple[str, str]] = {}  # name -> (type, help)
        self._start_time = time.time()

        # Register built-in metrics
        self._register_defaults()

    def _register_defaults(self):
        """Register all metric metadata (type and help text)."""
        self.register("scte35_events_detected_total", "counter",
                       "Total SCTE-35 events detected from manifest")
        self.register("scte35_events_injected_total", "counter",
                       "Total SCTE-35 events written to inject files")
        self.register("manifest_poll_total", "counter",
                       "Total manifest poll attempts")
        self.register("manifest_poll_errors_total", "counter",
                       "Total manifest poll failures (HTTP errors, timeouts)")
        self.register("manifest_poll_duration_seconds", "histogram",
                       "Duration of manifest poll in seconds")
        self.register("pts_calibration_status", "gauge",
                       "PTS calibration state: 0=pending, 1=calibrated, -1=disabled")
        self.register("pts_calibration_attempts_total", "counter",
                       "Total PTS calibration probe attempts")
        self.register("drm_key_rotations_total", "counter",
                       "Total DRM key rotation events detected")
        self.register("drm_detected", "gauge",
                       "DRM detected in manifest: 0=no, 1=yes")
        self.register("pipeline_state", "gauge",
                       "Pipeline state: 1=running, 0.5=degraded, 0=stopped")
        self.register("pipeline_restarts_total", "counter",
                       "Total process restarts per pipeline")
        self.register("pipelines_created_total", "counter",
                       "Total pipelines created")
        self.register("pipelines_stopped_total", "counter",
                       "Total pipelines stopped")
        self.register("pipelines_failed_total", "counter",
                       "Total pipeline creation failures")
        self.register("seen_cues_size", "gauge",
                       "Current size of the CUE deduplication set")
        self.register("seen_raw_hashes_size", "gauge",
                       "Current size of the raw section deduplication set")
        self.register("process_start_time_seconds", "gauge",
                       "Unix timestamp when the process started")

    def register(self, name: str, metric_type: str, help_text: str):
        """Register metric metadata."""
        with self._lock:
            self._meta[name] = (metric_type, help_text)

    def inc(self, name: str, amount: float = 1.0, labels: dict | None = None):
        """Increment a counter."""
        key = self._key(name, labels)
        with self._lock:
            self._counters[key] += amount

    def set(self, name: str, value: float, labels: dict | None = None):
        """Set a gauge value."""
        key = self._key(name, labels)
        with self._lock:
            self._gauges[key] = value

    def observe(self, name: str, value: float, labels: dict | None = None):
        """Record a histogram observation."""
        key = self._key(name, labels)
        with self._lock:
            self._histograms[key].append(value)
            # Keep only last 1000 observations to bound memory
            if len(self._histograms[key]) > 1000:
                self._histograms[key] = self._histograms[key][-1000:]

    def get(self, name: str, labels: dict | None = None) -> float:
        """Get current value of a counter or gauge."""
        key = self._key(name, labels)
        with self._lock:
            if key in self._counters:
                return self._counters[key]
            if key in self._gauges:
                return self._gauges[key]
            return 0.0

    def snapshot(self) -> dict:
        """Return a JSON-friendly snapshot of all metrics."""
        with self._lock:
            result = {}
            for key, val in self._counters.items():
                result[key] = val
            for key, val in self._gauges.items():
                result[key] = val
            for key, observations in self._histograms.items():
                if observations:
                    result[f"{key}_count"] = len(observations)
                    result[f"{key}_sum"] = sum(observations)
                    result[f"{key}_avg"] = sum(observations) / len(observations)
            result["process_start_time_seconds"] = self._start_time
            return result

    def export_to_file(self, path: str):
        """Write metrics snapshot to a JSON file for cross-process sharing."""
        data = self.snapshot()
        tmp = path + ".tmp"
        try:
            with open(tmp, "w") as f:
                json.dump(data, f)
            os.replace(tmp, path)
        except OSError:
            pass

    def import_from_directory(self, directory: str):
        """
        Read and merge metrics from all .metrics.json files in a directory.
        Used by the API server to aggregate metrics from monitor subprocesses.
        """
        metrics_dir = Path(directory)
        if not metrics_dir.is_dir():
            return

        # Clear previously imported metrics to prevent stale data
        # Only clear keys that look like they came from monitor imports
        # (they have pipeline ID labels or are known monitor-exported metrics)
        monitor_metrics = {
            "manifest_poll_total", "scte35_events_detected_total",
            "scte35_events_injected_total", "pts_calibration_attempts_total",
            "pts_calibration_status", "seen_cues_size", "seen_raw_hashes_size",
            "manifest_poll_duration_seconds", "drm_detected",
        }
        with self._lock:
            for store in (self._counters, self._gauges):
                stale = [k for k in store
                         if k.split("{")[0].split("_count")[0].split("_sum")[0].split("_avg")[0]
                         in monitor_metrics]
                for k in stale:
                    del store[k]
        for metrics_file in metrics_dir.glob("**/*.metrics.json"):
            try:
                # Extract pipeline ID from path: inject/<id>/monitor.metrics.json
                pipeline_id = metrics_file.parent.name

                with open(metrics_file) as f:
                    data = json.load(f)
                with self._lock:
                    for key, val in data.items():
                        if not isinstance(val, (int, float)):
                            continue
                        # Inject pipeline ID label into the key
                        if "{" in key:
                            # Already has labels: add id
                            labeled_key = key.replace("{", f'{{id="{pipeline_id}",', 1)
                        else:
                            labeled_key = f'{key}{{id="{pipeline_id}"}}'

                        # Merge: for counters use max (they only go up),
                        # for gauges use latest file value
                        name = key.split("{")[0].rstrip("_count").rstrip("_sum").rstrip("_avg")
                        if name in [n for n, (t, _) in self._meta.items() if t == "gauge"]:
                            self._gauges[labeled_key] = val
                        else:
                            # Counter: take the max of current and imported
                            if labeled_key in self._counters:
                                self._counters[labeled_key] = max(self._counters[labeled_key], val)
                            else:
                                self._counters[labeled_key] = val
            except (OSError, json.JSONDecodeError):
                pass

    def _key(self, name: str, labels: dict | None) -> str:
        """Build a metric key with optional labels."""
        if not labels:
            return name
        label_str = ",".join(f'{k}="{v}"' for k, v in sorted(labels.items()))
        return f"{name}{{{label_str}}}"

    def _parse_key(self, key: str) -> tuple[str, str]:
        """Split a key into base name and label suffix."""
        if "{" in key:
            name, rest = key.split("{", 1)
            return name, "{" + rest
        return key, ""


def render_prometheus(store: "_MetricStore | None" = None) -> str:
    """
    Render all metrics in Prometheus text exposition format.

    Format:
        # HELP metric_name Description
        # TYPE metric_name type
        metric_name{label="value"} 123.0
    """
    if store is None:
        store = metrics

    lines = []
    rendered_meta: set[str] = set()

    with store._lock:
        # Collect all keys and group by base metric name
        all_keys: dict[str, list[tuple[str, float]]] = defaultdict(list)

        for key, val in store._counters.items():
            name, labels = store._parse_key(key)
            all_keys[name].append((labels, val))

        for key, val in store._gauges.items():
            name, labels = store._parse_key(key)
            all_keys[name].append((labels, val))

        for key, observations in store._histograms.items():
            name, labels = store._parse_key(key)
            if observations:
                all_keys[f"{name}_count"].append((labels, len(observations)))
                all_keys[f"{name}_sum"].append((labels, sum(observations)))

        # Add process start time
        all_keys["process_start_time_seconds"].append(("", store._start_time))

        # Render in sorted order
        for name in sorted(all_keys.keys()):
            # Strip _count/_sum suffix for metadata lookup
            base_name = name
            for suffix in ("_count", "_sum"):
                if base_name.endswith(suffix):
                    base_name = base_name[: -len(suffix)]
                    break

            if base_name in store._meta and base_name not in rendered_meta:
                metric_type, help_text = store._meta[base_name]
                lines.append(f"# HELP {base_name} {help_text}")
                lines.append(f"# TYPE {base_name} {metric_type}")
                rendered_meta.add(base_name)

            for labels, val in all_keys[name]:
                if isinstance(val, float) and val == int(val):
                    val_str = f"{int(val)}"
                else:
                    val_str = f"{val}"
                lines.append(f"{name}{labels} {val_str}")

    lines.append("")  # trailing newline
    return "\n".join(lines)


# Global singleton
metrics = _MetricStore()
