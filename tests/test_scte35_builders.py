"""Tests for SCTE-35 XML builders and binary decoder."""

import base64
import struct
import sys
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

# Add bin/ to path so we can import the monitor module
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))

from manifest_monitor import (
    build_splice_insert_xml,
    build_time_signal_xml,
    decode_daterange_scte35,
    SCTE35_COMMAND_NAMES,
)


# ---------------------------------------------------------------------------
# build_splice_insert_xml
# ---------------------------------------------------------------------------


class TestBuildSpliceInsertXML:
    def _parse(self, xml_fragment: str) -> ET.Element:
        """Wrap fragment in <tsduck> and parse."""
        full = f'<?xml version="1.0"?>\n<tsduck>\n{xml_fragment}\n</tsduck>'
        return ET.fromstring(full)

    def test_splice_immediate_out(self):
        xml = build_splice_insert_xml(
            event_id=1,
            pts_time=None,
            duration_pts=270000,
            out_of_network=True,
            unique_program_id=1,
        )
        root = self._parse(xml)
        si = root.find(".//splice_insert")
        assert si is not None
        assert si.get("splice_event_id") == "1"
        assert si.get("splice_immediate") == "true"
        assert si.get("out_of_network") == "true"
        assert si.get("unique_program_id") == "1"
        # pts_time must NOT be present when splice_immediate=true
        assert si.get("pts_time") is None

    def test_splice_immediate_in(self):
        xml = build_splice_insert_xml(
            event_id=2,
            pts_time=None,
            duration_pts=None,
            out_of_network=False,
            unique_program_id=1,
        )
        root = self._parse(xml)
        si = root.find(".//splice_insert")
        assert si.get("out_of_network") == "false"
        assert si.get("splice_immediate") == "true"

    def test_timed_splice_with_pts(self):
        xml = build_splice_insert_xml(
            event_id=3,
            pts_time=8100000,
            duration_pts=2700000,
            out_of_network=True,
            unique_program_id=1,
        )
        root = self._parse(xml)
        si = root.find(".//splice_insert")
        assert si.get("splice_immediate") == "false"
        assert si.get("pts_time") == "8100000"

    def test_break_duration_present(self):
        xml = build_splice_insert_xml(
            event_id=4,
            pts_time=None,
            duration_pts=2700000,
            out_of_network=True,
            unique_program_id=1,
        )
        root = self._parse(xml)
        bd = root.find(".//break_duration")
        assert bd is not None
        assert bd.get("auto_return") == "true"
        assert bd.get("duration") == "2700000"

    def test_no_break_duration_when_none(self):
        xml = build_splice_insert_xml(
            event_id=5,
            pts_time=None,
            duration_pts=None,
            out_of_network=False,
            unique_program_id=1,
        )
        root = self._parse(xml)
        bd = root.find(".//break_duration")
        assert bd is None

    def test_cancel_event(self):
        xml = build_splice_insert_xml(
            event_id=6,
            pts_time=None,
            duration_pts=None,
            out_of_network=True,
            cancel=True,
            unique_program_id=1,
        )
        root = self._parse(xml)
        si = root.find(".//splice_insert")
        assert si.get("splice_event_cancel") == "true"
        # When cancel=true, out_of_network, pts_time, unique_program_id should be absent
        assert si.get("out_of_network") is None
        assert si.get("pts_time") is None

    def test_wrapped_in_splice_information_table(self):
        xml = build_splice_insert_xml(
            event_id=7, pts_time=None, duration_pts=None,
            out_of_network=True, unique_program_id=1,
        )
        root = self._parse(xml)
        sit = root.find("splice_information_table")
        assert sit is not None
        assert sit.find("splice_insert") is not None


# ---------------------------------------------------------------------------
# build_time_signal_xml
# ---------------------------------------------------------------------------


class TestBuildTimeSignalXML:
    def test_basic(self):
        xml = build_time_signal_xml(pts_time=900000)
        full = f'<?xml version="1.0"?>\n<tsduck>\n{xml}\n</tsduck>'
        root = ET.fromstring(full)
        ts = root.find(".//time_signal")
        assert ts is not None
        assert ts.get("pts_time") == "900000"


# ---------------------------------------------------------------------------
# decode_daterange_scte35
# ---------------------------------------------------------------------------


def _build_scte35_section(splice_command_type: int, extra: bytes = b"") -> bytes:
    """Build a minimal SCTE-35 section for testing."""
    # table_id(1) + section_syntax_indicator etc(2) + protocol_version(1)
    # + encrypted_packet(1) + pts_adjustment(4+1 bits packed into 5 bytes... simplified)
    # We just need: byte 0 = 0xFC, bytes 1-12 = filler, byte 13 = command type
    header = b"\xFC" + b"\x00" * 12 + bytes([splice_command_type])
    return header + extra


class TestDecodeDaterangeSCTE35:
    def test_splice_insert(self):
        raw = _build_scte35_section(0x05)
        b64 = base64.b64encode(raw).decode()
        result = decode_daterange_scte35(b64)
        assert result["table_id"] == 0xFC
        assert result["splice_command_type"] == 0x05
        assert result["command_name"] == "splice_insert"
        assert result["raw_bytes"] == raw

    def test_time_signal(self):
        raw = _build_scte35_section(0x06)
        b64 = base64.b64encode(raw).decode()
        result = decode_daterange_scte35(b64)
        assert result["splice_command_type"] == 0x06
        assert result["command_name"] == "time_signal"

    def test_splice_null(self):
        raw = _build_scte35_section(0x00)
        b64 = base64.b64encode(raw).decode()
        result = decode_daterange_scte35(b64)
        assert result["command_name"] == "splice_null"

    def test_private_command(self):
        raw = _build_scte35_section(0xFF)
        b64 = base64.b64encode(raw).decode()
        result = decode_daterange_scte35(b64)
        assert result["command_name"] == "private_command"

    def test_unknown_command_type(self):
        raw = _build_scte35_section(0x42)
        b64 = base64.b64encode(raw).decode()
        result = decode_daterange_scte35(b64)
        assert result["command_name"] == "unknown_0x42"

    def test_too_short(self):
        raw = b"\xFC" * 5
        b64 = base64.b64encode(raw).decode()
        result = decode_daterange_scte35(b64)
        assert "error" in result

    def test_raw_bytes_preserved(self):
        extra = b"\x01\x02\x03\x04\x05"
        raw = _build_scte35_section(0x06, extra)
        b64 = base64.b64encode(raw).decode()
        result = decode_daterange_scte35(b64)
        assert result["raw_bytes"] == raw
        assert len(result["raw_bytes"]) == 14 + len(extra)


# ---------------------------------------------------------------------------
# SCTE35_COMMAND_NAMES
# ---------------------------------------------------------------------------


class TestCommandNames:
    def test_all_known_types(self):
        assert SCTE35_COMMAND_NAMES[0x00] == "splice_null"
        assert SCTE35_COMMAND_NAMES[0x04] == "splice_schedule"
        assert SCTE35_COMMAND_NAMES[0x05] == "splice_insert"
        assert SCTE35_COMMAND_NAMES[0x06] == "time_signal"
        assert SCTE35_COMMAND_NAMES[0x07] == "bandwidth_reservation"
        assert SCTE35_COMMAND_NAMES[0xFF] == "private_command"
