#!/usr/bin/env python3
"""
test_hls_server.py - Synthetic HLS server with SCTE-35 CUE tags

Serves a minimal HLS playlist that includes EXT-X-CUE-OUT and CUE-IN
tags for testing the manifest monitor without a real live source.

Run: python3 test_hls_server.py --port 8899
Then: set source.url = "http://192.168.1.42:8899/live/playlist.m3u8"

The server generates a rolling-window live playlist with a CUE-OUT/CUE-IN
break every --break-interval segments.
"""

import argparse
import math
import time
from datetime import datetime, timezone, timedelta
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
import struct
import base64
import io


class HLSHandler(SimpleHTTPRequestHandler):
    """Serves dynamic HLS manifest with SCTE-35 CUE tags."""

    # Class-level config (set in main())
    segment_duration = 6.0
    window_size = 6        # segments in the window
    break_interval = 10    # segments between CUE-OUT events
    break_duration = 30.0  # seconds of ad break
    signal_style = "cue_tags"  # "cue_tags" or "daterange"

    def do_GET(self):
        if self.path == "/live/playlist.m3u8":
            self._serve_playlist()
        elif self.path.startswith("/live/seg_"):
            self._serve_segment()
        else:
            self.send_error(404)

    def _serve_playlist(self):
        now = time.time()
        start_time = 1700000000.0  # fixed epoch for reproducibility
        elapsed = now - start_time
        current_seq = int(elapsed / self.segment_duration)
        window_start = max(0, current_seq - self.window_size + 1)

        pdt_base = datetime(2024, 1, 1, tzinfo=timezone.utc)

        lines = [
            "#EXTM3U",
            "#EXT-X-VERSION:6",
            f"#EXT-X-TARGETDURATION:{int(math.ceil(self.segment_duration))}",
            f"#EXT-X-MEDIA-SEQUENCE:{window_start}",
        ]

        # Break math: ad break every N segments
        break_segs = int(self.break_duration / self.segment_duration)

        for i in range(self.window_size):
            seq = window_start + i
            seg_pdt = pdt_base + timedelta(
                seconds=seq * self.segment_duration
            )
            lines.append(
                f"#EXT-X-PROGRAM-DATE-TIME:"
                f"{seg_pdt.strftime('%Y-%m-%dT%H:%M:%S.000Z')}"
            )

            cycle_pos = seq % self.break_interval

            if self.signal_style == "cue_tags":
                if cycle_pos == 0 and seq > 0:
                    lines.append(
                        f"#EXT-X-CUE-OUT:DURATION={self.break_duration}"
                    )
                elif cycle_pos == break_segs:
                    lines.append("#EXT-X-CUE-IN")
                elif 0 < cycle_pos < break_segs:
                    remaining = (break_segs - cycle_pos) * self.segment_duration
                    lines.append(
                        f"#EXT-X-CUE-OUT-CONT:"
                        f"ElapsedTime={cycle_pos * self.segment_duration},"
                        f"Duration={self.break_duration}"
                    )

            elif self.signal_style == "daterange":
                if cycle_pos == 0 and seq > 0:
                    # Build a minimal SCTE-35 splice_insert in base64
                    cmd_b64 = self._build_scte35_b64(event_id=seq)
                    lines.append(
                        f'#EXT-X-DATERANGE:ID="splice-{seq}",'
                        f'START-DATE="{seg_pdt.strftime("%Y-%m-%dT%H:%M:%S.000Z")}",'
                        f"PLANNED-DURATION={self.break_duration},"
                        f'SCTE35-OUT=0x{base64.b64decode(cmd_b64).hex().upper()}'
                    )

            lines.append(f"#EXTINF:{self.segment_duration:.3f},")
            lines.append(f"seg_{seq}.ts")

        body = "\n".join(lines) + "\n"
        self.send_response(200)
        self.send_header("Content-Type", "application/vnd.apple.mpegurl")
        self.send_header("Content-Length", len(body))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body.encode())

    def _serve_segment(self):
        """Serve a minimal valid TS segment (null packets)."""
        # 10 null TS packets (188 bytes each)
        null_pkt = b"\x47\x1F\xFF\x10" + b"\xFF" * 184
        body = null_pkt * 10
        self.send_response(200)
        self.send_header("Content-Type", "video/mp2t")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    @staticmethod
    def _build_scte35_b64(event_id: int = 1) -> str:
        """Build a minimal splice_insert section and return base64."""
        # Simplified splice_insert — enough to validate parsing
        buf = io.BytesIO()
        # table_id = 0xFC
        buf.write(b"\xFC")
        # section_syntax_indicator=0, private=0, reserved=11, section_length
        # We'll fill length later
        buf.write(b"\x30\x00")  # placeholder
        # protocol_version=0
        buf.write(b"\x00")
        # encrypted=0, encryption_algo=0, pts_adjustment=0 (5 bytes)
        buf.write(b"\x00\x00\x00\x00\x00")
        # cw_index=0
        buf.write(b"\x00")
        # tier = 0xFFF (12 bits), splice_command_length (12 bits)
        buf.write(b"\xFF\xF0\x0E")
        # splice_command_type = 0x05 (splice_insert)
        buf.write(b"\x05")
        # splice_insert() body
        buf.write(struct.pack(">I", event_id))  # splice_event_id
        buf.write(b"\x00")  # cancel=0
        buf.write(b"\x40")  # out_of_network=1, program_splice=0, duration=0, immediate=1
        # descriptor_loop_length = 0
        buf.write(b"\x00\x00")

        data = buf.getvalue()
        # Fix section_length
        section_len = len(data) - 3 + 4  # +4 for CRC
        data = data[:1] + struct.pack(">H", 0x3000 | section_len) + data[3:]
        # CRC32 placeholder (not computed — good enough for testing)
        data += b"\x00\x00\x00\x00"

        return base64.b64encode(data).decode()

    def log_message(self, format, *args):
        # Quiet down request logging
        pass


def main():
    parser = argparse.ArgumentParser(
        description="Test HLS server with SCTE-35 CUE tags"
    )
    parser.add_argument("--port", type=int, default=8899)
    parser.add_argument("--segment-duration", type=float, default=6.0)
    parser.add_argument("--break-interval", type=int, default=10,
                        help="Segments between CUE-OUT events")
    parser.add_argument("--break-duration", type=float, default=30.0)
    parser.add_argument("--signal-style", choices=["cue_tags", "daterange"],
                        default="cue_tags")
    args = parser.parse_args()

    HLSHandler.segment_duration = args.segment_duration
    HLSHandler.break_interval = args.break_interval
    HLSHandler.break_duration = args.break_duration
    HLSHandler.signal_style = args.signal_style

    server = HTTPServer(("0.0.0.0", args.port), HLSHandler)
    print(
        f"Test HLS server on http://0.0.0.0:{args.port}/live/playlist.m3u8\n"
        f"  Segment duration: {args.segment_duration}s\n"
        f"  Break every:      {args.break_interval} segments\n"
        f"  Break duration:   {args.break_duration}s\n"
        f"  Signal style:     {args.signal_style}"
    )
    server.serve_forever()


if __name__ == "__main__":
    main()
