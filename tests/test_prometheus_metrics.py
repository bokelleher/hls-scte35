"""Tests for Prometheus metrics module."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))

from prometheus_metrics import _MetricStore, render_prometheus


@pytest.fixture
def store():
    """Fresh metric store for each test."""
    return _MetricStore()


class TestCounters:
    def test_increment(self, store):
        store.inc("test_counter")
        assert store.get("test_counter") == 1.0

    def test_increment_by(self, store):
        store.inc("test_counter", 5.0)
        assert store.get("test_counter") == 5.0

    def test_increment_accumulates(self, store):
        store.inc("test_counter")
        store.inc("test_counter")
        store.inc("test_counter")
        assert store.get("test_counter") == 3.0

    def test_labeled_counter(self, store):
        store.inc("events", labels={"type": "cue_out"})
        store.inc("events", labels={"type": "cue_in"})
        store.inc("events", labels={"type": "cue_out"})
        assert store.get("events", labels={"type": "cue_out"}) == 2.0
        assert store.get("events", labels={"type": "cue_in"}) == 1.0


class TestGauges:
    def test_set(self, store):
        store.set("test_gauge", 42.0)
        assert store.get("test_gauge") == 42.0

    def test_overwrite(self, store):
        store.set("test_gauge", 1.0)
        store.set("test_gauge", 2.0)
        assert store.get("test_gauge") == 2.0

    def test_labeled_gauge(self, store):
        store.set("state", 1.0, labels={"id": "abc"})
        store.set("state", 0.5, labels={"id": "def"})
        assert store.get("state", labels={"id": "abc"}) == 1.0
        assert store.get("state", labels={"id": "def"}) == 0.5


class TestHistograms:
    def test_observe(self, store):
        store.observe("duration", 0.1)
        store.observe("duration", 0.2)
        store.observe("duration", 0.3)
        snap = store.snapshot()
        assert snap["duration_count"] == 3
        assert abs(snap["duration_sum"] - 0.6) < 0.001
        assert abs(snap["duration_avg"] - 0.2) < 0.001

    def test_bounded_observations(self, store):
        for i in range(1500):
            store.observe("big", float(i))
        snap = store.snapshot()
        assert snap["big_count"] == 1000  # capped at 1000


class TestPrometheusRendering:
    def test_empty_store(self, store):
        text = render_prometheus(store)
        assert isinstance(text, str)
        # Should have HELP/TYPE lines for registered metrics
        assert "# HELP" in text

    def test_counter_renders(self, store):
        store.inc("scte35_events_detected_total", labels={"type": "cue_out"})
        text = render_prometheus(store)
        assert '# TYPE scte35_events_detected_total counter' in text
        assert 'scte35_events_detected_total{type="cue_out"} 1' in text

    def test_gauge_renders(self, store):
        store.set("pipeline_state", 1.0, labels={"id": "abc123"})
        text = render_prometheus(store)
        assert '# TYPE pipeline_state gauge' in text
        assert 'pipeline_state{id="abc123"} 1' in text

    def test_histogram_renders_count_and_sum(self, store):
        store.observe("manifest_poll_duration_seconds", 0.5)
        store.observe("manifest_poll_duration_seconds", 1.5)
        text = render_prometheus(store)
        assert "manifest_poll_duration_seconds_count 2" in text
        assert "manifest_poll_duration_seconds_sum 2" in text

    def test_help_text_renders(self, store):
        store.inc("scte35_events_detected_total", labels={"type": "test"})
        text = render_prometheus(store)
        assert "# HELP scte35_events_detected_total Total SCTE-35 events detected" in text

    def test_multiple_labels(self, store):
        store.inc("pipeline_restarts_total", labels={"id": "abc", "process": "tsp"})
        text = render_prometheus(store)
        assert 'pipeline_restarts_total{id="abc",process="tsp"} 1' in text


class TestSnapshot:
    def test_snapshot_includes_counters_and_gauges(self, store):
        store.inc("scte35_events_detected_total")
        store.set("pipeline_state", 1.0)
        snap = store.snapshot()
        assert "scte35_events_detected_total" in snap
        assert "pipeline_state" in snap

    def test_snapshot_includes_start_time(self, store):
        snap = store.snapshot()
        assert "process_start_time_seconds" in snap
        assert snap["process_start_time_seconds"] > 0


class TestGetDefault:
    def test_missing_key_returns_zero(self, store):
        assert store.get("nonexistent") == 0.0

    def test_missing_labeled_key_returns_zero(self, store):
        assert store.get("nonexistent", labels={"foo": "bar"}) == 0.0
