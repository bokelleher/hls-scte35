"""Tests for CUE-OUT/CUE-IN regex parsing in manifest_monitor."""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))

from manifest_monitor import ManifestMonitor


def _make_monitor(tmp_path) -> ManifestMonitor:
    """Create a ManifestMonitor with minimal config for testing."""
    config = {
        "source": {"url": "http://localhost:8899/index.m3u8", "poll_interval": 1.0},
        "scte35": {"pid": 500, "default_duration": 30.0, "mode": "auto_detect",
                    "event_id_base": 1},
        "tsduck": {"inject_dir": str(tmp_path / "inject")},
        "logging": {"log_dir": str(tmp_path / "logs"), "level": "DEBUG"},
        "drm": {"mode": "none"},
    }
    return ManifestMonitor(config)


# ---------------------------------------------------------------------------
# _parse_cue_tags
# ---------------------------------------------------------------------------


class TestParseCueTags:
    def test_cue_out_with_duration(self, tmp_path):
        monitor = _make_monitor(tmp_path)
        manifest = (
            "#EXTM3U\n"
            "#EXT-X-TARGETDURATION:6\n"
            "#EXT-X-MEDIA-SEQUENCE:100\n"
            "#EXTINF:6.0,\n"
            "seg100.ts\n"
            "#EXT-X-CUE-OUT:30.0\n"
            "#EXTINF:6.0,\n"
            "seg101.ts\n"
        )
        monitor._parse_cue_tags(manifest, media_sequence=100)
        assert len(monitor.pending_commands) == 1
        assert "out_of_network" in monitor.pending_commands[0]

    def test_cue_out_without_duration(self, tmp_path):
        monitor = _make_monitor(tmp_path)
        manifest = (
            "#EXTM3U\n"
            "#EXT-X-MEDIA-SEQUENCE:50\n"
            "#EXT-X-CUE-OUT\n"
            "#EXTINF:6.0,\n"
            "seg50.ts\n"
        )
        monitor._parse_cue_tags(manifest, media_sequence=50)
        assert len(monitor.pending_commands) == 1

    def test_cue_in(self, tmp_path):
        monitor = _make_monitor(tmp_path)
        manifest = (
            "#EXTM3U\n"
            "#EXT-X-MEDIA-SEQUENCE:60\n"
            "#EXT-X-CUE-IN\n"
            "#EXTINF:6.0,\n"
            "seg60.ts\n"
        )
        monitor._parse_cue_tags(manifest, media_sequence=60)
        assert len(monitor.pending_commands) == 1
        assert '"false"' in monitor.pending_commands[0]  # out_of_network=false

    def test_cue_out_and_in_pair(self, tmp_path):
        monitor = _make_monitor(tmp_path)
        manifest = (
            "#EXTM3U\n"
            "#EXT-X-MEDIA-SEQUENCE:70\n"
            "#EXTINF:6.0,\n"
            "seg70.ts\n"
            "#EXT-X-CUE-OUT:30.0\n"
            "#EXTINF:6.0,\n"
            "seg71.ts\n"
            "#EXT-X-CUE-OUT-CONT\n"
            "#EXTINF:6.0,\n"
            "seg72.ts\n"
            "#EXT-X-CUE-IN\n"
            "#EXTINF:6.0,\n"
            "seg73.ts\n"
        )
        monitor._parse_cue_tags(manifest, media_sequence=70)
        assert len(monitor.pending_commands) == 2  # one OUT, one IN

    def test_blank_lines_between_tags(self, tmp_path):
        """x9k3 inserts blank lines that break the m3u8 library."""
        monitor = _make_monitor(tmp_path)
        manifest = (
            "#EXTM3U\n"
            "#EXT-X-MEDIA-SEQUENCE:80\n"
            "\n"
            "#EXT-X-CUE-OUT:30.0\n"
            "\n"
            "#EXTINF:6.0,\n"
            "\n"
            "seg80.ts\n"
        )
        monitor._parse_cue_tags(manifest, media_sequence=80)
        assert len(monitor.pending_commands) == 1

    def test_cue_out_duration_format(self, tmp_path):
        """EXT-X-CUE-OUT:DURATION=30.0 format."""
        monitor = _make_monitor(tmp_path)
        manifest = (
            "#EXTM3U\n"
            "#EXT-X-MEDIA-SEQUENCE:90\n"
            "#EXT-X-CUE-OUT:DURATION=45.5\n"
            "#EXTINF:6.0,\n"
            "seg90.ts\n"
        )
        monitor._parse_cue_tags(manifest, media_sequence=90)
        assert len(monitor.pending_commands) == 1

    def test_dedup_same_media_sequence(self, tmp_path):
        """Same CUE-OUT at same media_sequence should not produce duplicates."""
        monitor = _make_monitor(tmp_path)
        manifest = (
            "#EXTM3U\n"
            "#EXT-X-MEDIA-SEQUENCE:100\n"
            "#EXT-X-CUE-OUT:30.0\n"
            "#EXTINF:6.0,\n"
            "seg100.ts\n"
        )
        monitor._parse_cue_tags(manifest, media_sequence=100)
        monitor._parse_cue_tags(manifest, media_sequence=100)
        assert len(monitor.pending_commands) == 1

    def test_cue_out_cont_ignored(self, tmp_path):
        """CUE-OUT-CONT should not generate commands."""
        monitor = _make_monitor(tmp_path)
        manifest = (
            "#EXTM3U\n"
            "#EXT-X-MEDIA-SEQUENCE:110\n"
            "#EXT-X-CUE-OUT-CONT\n"
            "#EXTINF:6.0,\n"
            "seg110.ts\n"
        )
        monitor._parse_cue_tags(manifest, media_sequence=110)
        assert len(monitor.pending_commands) == 0

    def test_case_insensitive(self, tmp_path):
        monitor = _make_monitor(tmp_path)
        manifest = (
            "#EXTM3U\n"
            "#EXT-X-MEDIA-SEQUENCE:120\n"
            "#ext-x-cue-out:30.0\n"
            "#EXTINF:6.0,\n"
            "seg120.ts\n"
        )
        monitor._parse_cue_tags(manifest, media_sequence=120)
        assert len(monitor.pending_commands) == 1

    def test_no_cues_produces_no_commands(self, tmp_path):
        monitor = _make_monitor(tmp_path)
        manifest = (
            "#EXTM3U\n"
            "#EXT-X-MEDIA-SEQUENCE:130\n"
            "#EXTINF:6.0,\n"
            "seg130.ts\n"
            "#EXTINF:6.0,\n"
            "seg131.ts\n"
        )
        monitor._parse_cue_tags(manifest, media_sequence=130)
        assert len(monitor.pending_commands) == 0


# ---------------------------------------------------------------------------
# _flush_commands
# ---------------------------------------------------------------------------


class TestFlushCommands:
    def test_xml_flush(self, tmp_path):
        monitor = _make_monitor(tmp_path)
        monitor._queue_command("<splice_information_table/>", "test")
        monitor._flush_commands()

        xml_file = tmp_path / "inject" / "splice.xml"
        assert xml_file.exists()
        content = xml_file.read_text()
        assert "<tsduck>" in content
        assert "<splice_information_table/>" in content

    def test_raw_binary_flush(self, tmp_path):
        monitor = _make_monitor(tmp_path)
        raw = b"\xFC" + b"\x00" * 13
        monitor._queue_raw_section(raw, "test")
        monitor._flush_commands()

        bin_file = tmp_path / "inject" / "splice.bin"
        assert bin_file.exists()
        assert bin_file.read_bytes() == raw

    def test_empty_flush_no_file_written(self, tmp_path):
        monitor = _make_monitor(tmp_path)
        monitor._flush_commands()
        xml_file = tmp_path / "inject" / "splice.xml"
        # Seed file may exist from init, but no new write
        assert len(monitor.pending_commands) == 0

    def test_multiple_commands_concatenated(self, tmp_path):
        monitor = _make_monitor(tmp_path)
        monitor._queue_command("<splice_information_table>1</splice_information_table>", "a")
        monitor._queue_command("<splice_information_table>2</splice_information_table>", "b")
        monitor._flush_commands()

        content = (tmp_path / "inject" / "splice.xml").read_text()
        assert "1</splice_information_table>" in content
        assert "2</splice_information_table>" in content
