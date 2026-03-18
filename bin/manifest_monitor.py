#!/usr/bin/env python3
"""
HLS Manifest Monitor for SCTE-35 Signal Detection and Injection

Polls an HLS manifest and detects three signaling styles:
  1. EXT-X-CUE-OUT / EXT-X-CUE-IN tags
  2. EXT-X-DATERANGE with SCTE35-CMD attribute
  3. In-band SCTE-35 PID (passthrough, detected by TSDuck)

For manifest-level signals (1 & 2), writes a TSDuck XML table file
to a single known path (splice.xml) that the inject plugin watches
via --poll-files.

TSDuck v3.42 XML schema (from tsduck.tables.model.xml):
  <splice_information_table>
    <splice_insert
      splice_event_id="uint32, required"
      splice_event_cancel="bool, default=false"
      out_of_network="bool, required when cancel=false"
      splice_immediate="bool, default=false"
      pts_time="uint33, required when cancel=false and immediate=false"
      unique_program_id="uint16, required when cancel=false"
      avail_num="uint8, default=0"
      avails_expected="uint8, default=0">
      <break_duration auto_return="bool, required" duration="uint33, required" />
    </splice_insert>
  </splice_information_table>
"""

import argparse
import base64
import hashlib
import io
import logging
import os
import re
import struct
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    import tomllib
except ImportError:
    import tomli as tomllib  # Python < 3.11

import m3u8
import requests

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DEFAULT_CONFIG = "/opt/hls-scte35/config/pipeline.toml"


def load_config(path: str = None) -> dict:
    config_path = path or os.environ.get("PIPELINE_CONFIG", DEFAULT_CONFIG)
    with open(config_path, "rb") as f:
        return tomllib.load(f)


# ---------------------------------------------------------------------------
# SCTE-35 XML Builders (TSDuck v3.42 format)
# ---------------------------------------------------------------------------

def build_splice_insert_xml(
    event_id: int,
    pts_time: int | None,
    duration_pts: int | None,
    out_of_network: bool = True,
    cancel: bool = False,
    unique_program_id: int = 1,
) -> str:
    """
    Build a TSDuck XML splice_information_table containing a splice_insert.

    Rules from tsduck.tables.model.xml:
      - unique_program_id: required when cancel=false
      - pts_time: required when cancel=false AND splice_immediate=false
      - pts_time: must NOT be present when splice_immediate=true
      - splice_immediate: when true, splice happens now (no pts_time)
    """
    cancel_str = "true" if cancel else "false"
    oon_str = "true" if out_of_network else "false"

    # If we have a PTS time, use it; otherwise splice_immediate
    splice_immediate = pts_time is None and not cancel

    attrs = [
        f'splice_event_id="{event_id}"',
        f'splice_event_cancel="{cancel_str}"',
    ]

    if not cancel:
        attrs.append(f'out_of_network="{oon_str}"')
        attrs.append(f'splice_immediate="{"true" if splice_immediate else "false"}"')

        if not splice_immediate and pts_time is not None:
            attrs.append(f'pts_time="{pts_time}"')

        attrs.append(f'unique_program_id="{unique_program_id}"')
        attrs.append('avail_num="0"')
        attrs.append('avails_expected="0"')

    attr_str = " ".join(attrs)

    # Build inner elements
    inner = ""
    if duration_pts is not None and not cancel:
        inner += f'\n        <break_duration auto_return="true" duration="{duration_pts}" />'

    if inner:
        xml = f"""  <splice_information_table>
    <splice_insert {attr_str}>{inner}
    </splice_insert>
  </splice_information_table>"""
    else:
        xml = f"""  <splice_information_table>
    <splice_insert {attr_str} />
  </splice_information_table>"""

    return xml


def build_time_signal_xml(pts_time: int) -> str:
    """Build a TSDuck XML splice_information_table with time_signal."""
    return f"""  <splice_information_table>
    <time_signal pts_time="{pts_time}" />
  </splice_information_table>"""


def decode_daterange_scte35(scte35_cmd_b64: str) -> dict:
    """
    Decode the SCTE35-CMD base64 attribute from EXT-X-DATERANGE.
    Returns a dict with splice_command_type and relevant fields.
    """
    raw = base64.b64decode(scte35_cmd_b64)
    if len(raw) < 14:
        return {"raw": raw.hex(), "error": "section too short"}

    table_id = raw[0]
    splice_command_type = raw[13]

    return {
        "table_id": table_id,
        "splice_command_type": splice_command_type,
        "raw_hex": raw.hex(),
        "raw_bytes": raw,
    }


# ---------------------------------------------------------------------------
# Manifest Poller
# ---------------------------------------------------------------------------

class ManifestMonitor:
    def __init__(self, config: dict):
        self.source_url = config["source"]["url"]
        self.poll_interval = config["source"].get("poll_interval", 6.0)
        self.headers = config["source"].get("headers", {})

        self.scte35_pid = config["scte35"].get("pid", 500)
        self.default_duration = config["scte35"].get("default_duration", 30.0)
        self.preroll_ms = config["scte35"].get("preroll_ms", 4000)
        self.event_id_base = config["scte35"].get("event_id_base", 1)
        self.mode = config["scte35"].get("mode", "auto_detect")

        self.inject_dir = Path(config["tsduck"].get(
            "inject_dir", "/opt/hls-scte35/inject"
        ))
        self.inject_dir.mkdir(parents=True, exist_ok=True)
        self.inject_file = self.inject_dir / "splice.xml"

        log_dir = Path(config["logging"].get("log_dir", "/opt/hls-scte35/logs"))
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / config["logging"].get("splice_log", "splice.log")

        self.logger = logging.getLogger("manifest_monitor")
        self.logger.setLevel(
            getattr(logging, config["logging"].get("level", "INFO"))
        )
        fh = logging.FileHandler(log_file)
        fh.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s %(message)s"
        ))
        self.logger.addHandler(fh)
        self.logger.addHandler(logging.StreamHandler(sys.stdout))

        self.config = config
        self.event_id = self.event_id_base
        self.seen_cues: set[str] = set()
        self.session = requests.Session()
        self.session.headers.update(self.headers)

        # Pending splice commands to write as a batch
        self.pending_commands: list[str] = []

        # Track PTS baseline from PROGRAM-DATE-TIME
        self.pdt_base: datetime | None = None
        self.pts_base: int = 0

        # PTS calibration: measure actual output PTS to correct estimates
        self._pts_calibrated = False
        self._calibration_delay = config["scte35"].get(
            "calibration_delay_s", 10.0
        )
        self._calibration_retries = config["scte35"].get(
            "calibration_retries", 3
        )
        self._calibration_enabled = config["scte35"].get(
            "calibration_enabled", True
        )
        self._start_time: float | None = None

        # DRM detection state
        drm_config = config.get("drm", {})
        self.drm_mode = drm_config.get("mode", "none")
        self.drm_detected: dict | None = None
        self._drm_key_rotation_count = 0
        self._last_key_uri: str | None = None

    def _detect_drm(self, playlist) -> None:
        """Parse EXT-X-KEY tags from playlist and report DRM status."""
        if not hasattr(playlist, "keys") or not playlist.keys:
            return

        for key in playlist.keys:
            if key is None:
                continue

            method = getattr(key, "method", None)
            if not method or method == "NONE":
                continue

            uri = getattr(key, "uri", None)
            iv = getattr(key, "iv", None)
            keyformat = getattr(key, "keyformat", None) or "identity"

            drm_info = {
                "method": method,
                "keyformat": keyformat,
                "uri_present": uri is not None,
                "iv_present": iv is not None,
            }

            # Detect key rotation
            if uri and uri != self._last_key_uri:
                if self._last_key_uri is not None:
                    self._drm_key_rotation_count += 1
                    self.logger.info(
                        f"DRM key rotation detected (count={self._drm_key_rotation_count})"
                    )
                self._last_key_uri = uri

            if self.drm_detected is None:
                self.logger.info(
                    f"DRM detected: method={method} keyformat={keyformat}"
                )
                # Log URI domain only, never the full key URI (security)
                if uri:
                    from urllib.parse import urlparse
                    parsed = urlparse(uri)
                    self.logger.info(f"  Key server: {parsed.hostname}")

            self.drm_detected = drm_info
            break  # Only report the first active key

    @property
    def drm_status(self) -> dict:
        """Return current DRM status for API reporting."""
        if self.drm_detected is None:
            return {"state": "none"}
        return {
            "state": "detected",
            "method": self.drm_detected.get("method"),
            "keyformat": self.drm_detected.get("keyformat"),
            "key_rotation_count": self._drm_key_rotation_count,
        }

    def _next_event_id(self) -> int:
        eid = self.event_id
        self.event_id += 1
        return eid

    def _cue_key(self, tag_type: str, position: float | str) -> str:
        raw = f"{tag_type}:{position}"
        return hashlib.md5(raw.encode()).hexdigest()[:12]

    def _queue_command(self, xml_fragment: str, label: str):
        """Queue a splice command XML fragment. Written to file after full poll."""
        self.pending_commands.append(xml_fragment)
        self.logger.info(f"Queued splice command: {label}")

    def _flush_commands(self):
        """Write all pending commands to the single inject file."""
        if not self.pending_commands:
            return

        xml = '<?xml version="1.0" encoding="UTF-8"?>\n<tsduck>\n'
        for cmd in self.pending_commands:
            xml += cmd + "\n"
        xml += "</tsduck>\n"

        # Atomic write: write to temp file then rename
        tmp_path = self.inject_file.with_suffix(".tmp")
        tmp_path.write_text(xml)
        tmp_path.rename(self.inject_file)

        self.logger.info(
            f"Wrote {len(self.pending_commands)} command(s) to {self.inject_file.name}"
        )
        self.pending_commands.clear()

    def _duration_to_pts(self, seconds: float) -> int:
        return int(seconds * 90000)

    def _estimate_pts_from_pdt(self, pdt: datetime) -> int | None:
        if self.pdt_base is None:
            return None
        delta = (pdt - self.pdt_base).total_seconds()
        return (self.pts_base + self._duration_to_pts(delta)) % (1 << 33)

    # ------------------------------------------------------------------
    # PTS Calibration
    # ------------------------------------------------------------------

    def _probe_pts_from_file(self, path: str) -> int | None:
        """Read the first video PTS from a TS file using ffprobe."""
        try:
            result = subprocess.run(
                [
                    "ffprobe", "-v", "quiet",
                    "-select_streams", "v:0",
                    "-show_entries", "packet=pts",
                    "-of", "csv=p=0",
                    "-read_intervals", "%+#1",
                    path,
                ],
                capture_output=True, text=True, timeout=10,
            )
            for line in result.stdout.strip().splitlines():
                line = line.strip()
                if line and line != "N/A":
                    return int(line)
        except (subprocess.TimeoutExpired, FileNotFoundError, ValueError) as e:
            self.logger.debug(f"PTS probe (file) failed: {e}")
        return None

    def _probe_pts_from_segment(self, playlist) -> int | None:
        """Read the first video PTS from the first HLS segment via ffprobe."""
        if not playlist.segments:
            return None
        seg_url = playlist.segments[0].absolute_uri
        try:
            result = subprocess.run(
                [
                    "ffprobe", "-v", "quiet",
                    "-select_streams", "v:0",
                    "-show_entries", "packet=pts",
                    "-of", "csv=p=0",
                    "-read_intervals", "%+#1",
                    seg_url,
                ],
                capture_output=True, text=True, timeout=15,
            )
            for line in result.stdout.strip().splitlines():
                line = line.strip()
                if line and line != "N/A":
                    return int(line)
        except (subprocess.TimeoutExpired, FileNotFoundError, ValueError) as e:
            self.logger.debug(f"PTS probe (segment) failed: {e}")
        return None

    def _probe_actual_pts(self, playlist=None) -> int | None:
        """Probe the actual first video PTS from the pipeline output or source."""
        output_mode = self.config["tsduck"].get("output_mode", "file")

        # For file output, probe the output file directly
        if output_mode == "file":
            file_path = self.config["tsduck"].get(
                "file_path", "/opt/hls-scte35/output/live.ts"
            )
            if Path(file_path).exists() and Path(file_path).stat().st_size > 0:
                pts = self._probe_pts_from_file(file_path)
                if pts is not None:
                    return pts

        # Fallback: probe the first HLS source segment directly
        if playlist is not None:
            return self._probe_pts_from_segment(playlist)

        return None

    def _calibrate_pts(self, playlist=None):
        """
        Measure the actual PTS in the output and adjust pts_base so that
        _estimate_pts_from_pdt() produces correct values.

        Called from poll_once() until calibration succeeds.
        """
        if self._pts_calibrated or not self._calibration_enabled:
            return
        if self.pdt_base is None:
            return  # need PDT baseline first
        if self._start_time is None:
            return
        if time.monotonic() - self._start_time < self._calibration_delay:
            return  # wait for pipeline to produce output

        probed_pts = self._probe_actual_pts(playlist)
        if probed_pts is None:
            self.logger.debug("PTS calibration: probe returned None, will retry")
            return

        old_base = self.pts_base
        self.pts_base = probed_pts
        self._pts_calibrated = True
        offset = probed_pts - old_base
        self.logger.info(
            f"PTS calibrated: probed={probed_pts} "
            f"(old_base={old_base}, offset={offset:+d}, "
            f"{offset / 90000:+.3f}s)"
        )

    def _process_cue_out(self, duration: float | None, media_sequence: int):
        key = self._cue_key("CUE-OUT", media_sequence)
        if key in self.seen_cues:
            return
        self.seen_cues.add(key)

        dur = duration or self.default_duration
        event_id = self._next_event_id()
        dur_pts = self._duration_to_pts(dur)

        self.logger.info(
            f"CUE-OUT detected: event_id={event_id} "
            f"duration={dur}s media_seq={media_sequence}"
        )

        xml = build_splice_insert_xml(
            event_id=event_id,
            pts_time=None,  # splice_immediate
            duration_pts=dur_pts,
            out_of_network=True,
            unique_program_id=1,
        )
        self._queue_command(xml, f"CUE-OUT event_id={event_id}")

    def _process_cue_in(self, media_sequence: int):
        key = self._cue_key("CUE-IN", media_sequence)
        if key in self.seen_cues:
            return
        self.seen_cues.add(key)

        event_id = self._next_event_id()
        self.logger.info(
            f"CUE-IN detected: event_id={event_id} media_seq={media_sequence}"
        )

        xml = build_splice_insert_xml(
            event_id=event_id,
            pts_time=None,  # splice_immediate
            duration_pts=None,
            out_of_network=False,
            unique_program_id=1,
        )
        self._queue_command(xml, f"CUE-IN event_id={event_id}")

    def _process_daterange(self, daterange: dict):
        dr_id = daterange.get("id", "unknown")
        key = self._cue_key("DATERANGE", dr_id)
        if key in self.seen_cues:
            return
        self.seen_cues.add(key)

        scte35_cmd = daterange.get("scte35_cmd")
        scte35_out = daterange.get("scte35_out")
        scte35_in = daterange.get("scte35_in")

        cmd_b64 = scte35_cmd or scte35_out or scte35_in
        if not cmd_b64:
            self.logger.warning(
                f"DATERANGE {dr_id} has no SCTE35-CMD/OUT/IN attribute"
            )
            return

        decoded = decode_daterange_scte35(cmd_b64)
        self.logger.info(
            f"DATERANGE {dr_id}: command_type=0x{decoded.get('splice_command_type', 0):02X} "
            f"raw={decoded.get('raw_hex', 'N/A')}"
        )

        start_date_str = daterange.get("start_date")
        pts = None
        if start_date_str:
            try:
                start_date = datetime.fromisoformat(start_date_str)
                pts = self._estimate_pts_from_pdt(start_date)
            except (ValueError, TypeError):
                pass

        event_id = self._next_event_id()
        dur_str = daterange.get("duration") or daterange.get("planned_duration")
        dur_pts = self._duration_to_pts(float(dur_str)) if dur_str else None

        is_out = scte35_out is not None or (
            scte35_cmd is not None and decoded.get("splice_command_type") == 5
        )

        xml = build_splice_insert_xml(
            event_id=event_id,
            pts_time=pts,
            duration_pts=dur_pts,
            out_of_network=is_out,
            unique_program_id=1,
        )
        self._queue_command(xml, f"DATERANGE {dr_id} event_id={event_id}")

    # Patterns for CUE-OUT / CUE-IN tags (tolerant of blank lines)
    _RE_CUE_OUT = re.compile(
        r"#EXT-X-CUE-OUT(?::(?:DURATION=)?(\d+(?:\.\d+)?))?\s*$",
        re.MULTILINE | re.IGNORECASE,
    )
    _RE_CUE_OUT_CONT = re.compile(
        r"#EXT-X-CUE-OUT-CONT",
        re.IGNORECASE,
    )
    _RE_CUE_IN = re.compile(
        r"#EXT-X-CUE-IN",
        re.IGNORECASE,
    )
    _RE_EXTINF = re.compile(
        r"#EXTINF:\s*(\d+(?:\.\d+)?)",
    )

    def _parse_cue_tags(self, manifest_text: str, media_sequence: int):
        """
        Walk the raw manifest text and associate CUE-OUT/CUE-IN tags with the
        segment they precede, identified by media sequence number.

        This is resilient to blank lines between tags that trip up the m3u8
        library parser (common with x9k3 output).
        """
        seg_index = 0
        # Pending CUE signals that apply to the next segment URI
        pending_cue_out: float | None | bool = None
        pending_cue_in = False

        for line in manifest_text.splitlines():
            stripped = line.strip()
            if not stripped:
                continue  # skip blank lines without resetting state

            # CUE-OUT (may have duration)
            m = self._RE_CUE_OUT.match(stripped)
            if m:
                dur_str = m.group(1)
                pending_cue_out = float(dur_str) if dur_str else True
                continue

            # CUE-IN
            if self._RE_CUE_IN.match(stripped):
                pending_cue_in = True
                continue

            # Skip CUE-OUT-CONT (mid-break continuation, not actionable)
            if self._RE_CUE_OUT_CONT.match(stripped):
                continue

            # EXTINF marks the next segment; don't advance seg_index yet
            if self._RE_EXTINF.match(stripped):
                continue

            # Skip other HLS tags
            if stripped.startswith("#"):
                continue

            # Non-comment, non-blank line = segment URI
            media_seq = media_sequence + seg_index

            if pending_cue_out is not None:
                dur = pending_cue_out if isinstance(pending_cue_out, float) else None
                self._process_cue_out(dur, media_seq)
                pending_cue_out = None

            if pending_cue_in:
                self._process_cue_in(media_seq)
                pending_cue_in = False

            seg_index += 1

    def poll_once(self):
        """Fetch manifest and process SCTE-35 signals."""
        try:
            resp = self.session.get(self.source_url, timeout=10)
            resp.raise_for_status()
        except requests.RequestException as e:
            self.logger.error(f"Failed to fetch manifest: {e}")
            return

        playlist = m3u8.loads(resp.text, uri=self.source_url)

        # If master playlist, follow highest bandwidth rendition
        if playlist.is_variant:
            if not playlist.playlists:
                self.logger.warning("Master playlist has no renditions")
                return
            best = max(
                playlist.playlists,
                key=lambda p: p.stream_info.bandwidth or 0,
            )
            self.logger.debug(
                f"Following rendition: {best.uri} "
                f"({best.stream_info.bandwidth} bps)"
            )
            try:
                resp = self.session.get(best.absolute_uri, timeout=10)
                resp.raise_for_status()
                playlist = m3u8.loads(resp.text, uri=best.absolute_uri)
            except requests.RequestException as e:
                self.logger.error(f"Failed to fetch rendition: {e}")
                return

        # Detect DRM from EXT-X-KEY tags
        self._detect_drm(playlist)

        # Establish PDT baseline from first segment if available
        for seg in playlist.segments:
            if seg.program_date_time and self.pdt_base is None:
                self.pdt_base = seg.program_date_time
                self.logger.info(f"PDT baseline: {self.pdt_base.isoformat()}")
                break

        # Attempt PTS calibration if not yet done
        self._calibrate_pts(playlist)

        # Process segments for CUE-OUT / CUE-IN tags using regex on raw text.
        # The m3u8 library misses these when blank lines separate tags (x9k3).
        if self.mode in ("auto_detect", "manifest_only"):
            self._parse_cue_tags(resp.text, playlist.media_sequence or 0)

        # Process EXT-X-DATERANGE tags
        if self.mode in ("auto_detect", "manifest_only"):
            if hasattr(playlist, "dateranges"):
                for dr in playlist.dateranges:
                    dr_dict = {
                        "id": getattr(dr, "id", None),
                        "start_date": str(getattr(dr, "start_date", "")),
                        "duration": getattr(dr, "duration", None),
                        "planned_duration": getattr(dr, "planned_duration", None),
                        "scte35_cmd": getattr(dr, "scte35_cmd", None),
                        "scte35_out": getattr(dr, "scte35_out", None),
                        "scte35_in": getattr(dr, "scte35_in", None),
                    }
                    if dr_dict["scte35_cmd"] or dr_dict["scte35_out"] or dr_dict["scte35_in"]:
                        self._process_daterange(dr_dict)

        # Flush all queued commands to the inject file
        self._flush_commands()

    def run(self):
        """Main polling loop."""
        self.logger.info(
            f"Starting manifest monitor: {self.source_url} "
            f"(mode={self.mode}, poll={self.poll_interval}s)"
        )

        # Write empty seed file so tsp inject has something to watch
        if not self.inject_file.exists():
            self.inject_file.write_text(
                '<?xml version="1.0" encoding="UTF-8"?>\n<tsduck>\n</tsduck>\n'
            )

        self._start_time = time.monotonic()

        while True:
            try:
                self.poll_once()
            except Exception as e:
                self.logger.exception(f"Unhandled error in poll: {e}")
            time.sleep(self.poll_interval)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="HLS Manifest Monitor for SCTE-35 Signal Detection"
    )
    parser.add_argument(
        "config", nargs="?", default=None,
        help="Path to pipeline.toml config file",
    )
    parser.add_argument(
        "--source-url", dest="source_url",
        help="HLS manifest URL (overrides config)",
    )
    parser.add_argument(
        "--poll-interval", dest="poll_interval", type=float,
        help="Poll interval in seconds (overrides config)",
    )
    parser.add_argument(
        "--scte35-pid", dest="scte35_pid", type=int,
        help="SCTE-35 PID (overrides config)",
    )
    parser.add_argument(
        "--mode", choices=["auto_detect", "manifest_only", "inband_only"],
        help="Detection mode (overrides config)",
    )
    parser.add_argument(
        "--inject-dir", dest="inject_dir",
        help="Directory for splice XML output (overrides config)",
    )
    parser.add_argument(
        "--log-level", dest="log_level",
        choices=["DEBUG", "INFO", "WARN", "ERROR"],
        help="Log level (overrides config)",
    )
    parser.add_argument(
        "--drm-mode", dest="drm_mode",
        choices=["none", "auto", "aes128"],
        help="DRM decryption mode (overrides config)",
    )
    parser.add_argument(
        "--drm-key", dest="drm_key",
        help="Pre-shared AES key, 32 hex digits (overrides config)",
    )
    return parser.parse_args(argv)


def apply_cli_overrides(config: dict, args) -> dict:
    """Apply CLI argument overrides to the loaded config."""
    if args.source_url:
        config.setdefault("source", {})["url"] = args.source_url
    if args.poll_interval is not None:
        config.setdefault("source", {})["poll_interval"] = args.poll_interval
    if args.scte35_pid is not None:
        config.setdefault("scte35", {})["pid"] = args.scte35_pid
    if args.mode:
        config.setdefault("scte35", {})["mode"] = args.mode
    if args.inject_dir:
        config.setdefault("tsduck", {})["inject_dir"] = args.inject_dir
    if args.log_level:
        config.setdefault("logging", {})["level"] = args.log_level
    if args.drm_mode:
        config.setdefault("drm", {})["mode"] = args.drm_mode
    if args.drm_key:
        config.setdefault("drm", {})["key"] = args.drm_key
    return config


def main():
    args = parse_args()
    config = load_config(args.config)
    config = apply_cli_overrides(config, args)
    monitor = ManifestMonitor(config)
    monitor.run()


if __name__ == "__main__":
    main()