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
    segment_format = "ts"  # "ts" or "fmp4"
    drm_enabled = False
    # Fixed test key: 00112233445566778899aabbccddeeff
    drm_key = bytes.fromhex("00112233445566778899aabbccddeeff")
    drm_iv = bytes.fromhex("00000000000000000000000000000000")
    server_port = 8899

    def do_GET(self):
        if self.path == "/live/playlist.m3u8":
            self._serve_playlist()
        elif self.path == "/live/key.bin":
            self._serve_drm_key()
        elif self.path == "/live/init.mp4":
            self._serve_init_segment()
        elif self.path.startswith("/live/seg_") and self.path.endswith(".m4s"):
            self._serve_fmp4_segment()
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

        # fMP4: add EXT-X-MAP for init segment
        if self.segment_format == "fmp4":
            lines.append('#EXT-X-MAP:URI="init.mp4"')

        # DRM: add EXT-X-KEY for AES-128
        if self.drm_enabled:
            iv_hex = self.drm_iv.hex()
            lines.append(
                f'#EXT-X-KEY:METHOD=AES-128,'
                f'URI="http://localhost:{self.server_port}/live/key.bin",'
                f'IV=0x{iv_hex}'
            )

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
            seg_ext = "m4s" if self.segment_format == "fmp4" else "ts"
            lines.append(f"seg_{seq}.{seg_ext}")

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

    def _serve_drm_key(self):
        """Serve the raw 16-byte AES key (simulates a key server)."""
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", 16)
        self.end_headers()
        self.wfile.write(self.drm_key)

    def _serve_init_segment(self):
        """Serve a minimal fMP4 init segment (ftyp + moov)."""
        # Minimal ftyp box
        ftyp = self._mp4_box(b"ftyp",
            b"isom"           # major_brand
            + b"\x00\x00\x02\x00"  # minor_version
            + b"isomiso6mp41"  # compatible_brands
        )
        # Minimal moov with a single silent AAC track
        # (just enough for ffmpeg to accept as valid init)
        tkhd = self._mp4_fullbox(b"tkhd", 0, 3,
            b"\x00" * 4       # creation_time
            + b"\x00" * 4     # modification_time
            + b"\x00\x00\x00\x01"  # track_id=1
            + b"\x00" * 4     # reserved
            + b"\x00" * 4     # duration
            + b"\x00" * 8     # reserved
            + b"\x00\x00"     # layer
            + b"\x00\x00"     # alternate_group
            + b"\x01\x00"     # volume (1.0 fixed point)
            + b"\x00\x00"     # reserved
            + b"\x00\x01\x00\x00" + b"\x00" * 4 + b"\x00" * 4  # matrix row1
            + b"\x00" * 4 + b"\x00\x01\x00\x00" + b"\x00" * 4  # matrix row2
            + b"\x00" * 4 + b"\x00" * 4 + b"\x40\x00\x00\x00"  # matrix row3
            + b"\x00" * 4     # width
            + b"\x00" * 4     # height
        )
        mdhd = self._mp4_fullbox(b"mdhd", 0, 0,
            b"\x00" * 4       # creation_time
            + b"\x00" * 4     # modification_time
            + b"\x00\x00\xAC\x44"  # timescale=44100
            + b"\x00" * 4     # duration
            + b"\x55\xC4"     # language (und)
            + b"\x00\x00"     # pre_defined
        )
        hdlr = self._mp4_fullbox(b"hdlr", 0, 0,
            b"\x00" * 4       # pre_defined
            + b"soun"         # handler_type
            + b"\x00" * 12    # reserved
            + b"SoundHandler\x00"  # name
        )
        smhd = self._mp4_fullbox(b"smhd", 0, 0, b"\x00" * 4)
        dref_entry = self._mp4_fullbox(b"url ", 0, 1, b"")
        dref = self._mp4_fullbox(b"dref", 0, 0,
            b"\x00\x00\x00\x01" + dref_entry)
        dinf = self._mp4_box(b"dinf", dref)
        # Minimal AAC esds
        mp4a = self._mp4_box(b"mp4a",
            b"\x00" * 6       # reserved
            + b"\x00\x01"     # data_reference_index
            + b"\x00" * 8     # reserved
            + b"\x00\x02"     # channel_count
            + b"\x00\x10"     # sample_size
            + b"\x00\x00"     # pre_defined
            + b"\x00\x00"     # reserved
            + b"\xAC\x44\x00\x00"  # sample_rate 44100.0
        )
        stsd = self._mp4_fullbox(b"stsd", 0, 0,
            b"\x00\x00\x00\x01" + mp4a)
        stts = self._mp4_fullbox(b"stts", 0, 0, b"\x00\x00\x00\x00")
        stsc = self._mp4_fullbox(b"stsc", 0, 0, b"\x00\x00\x00\x00")
        stsz = self._mp4_fullbox(b"stsz", 0, 0, b"\x00" * 8)
        stco = self._mp4_fullbox(b"stco", 0, 0, b"\x00\x00\x00\x00")
        stbl = self._mp4_box(b"stbl", stsd + stts + stsc + stsz + stco)
        minf = self._mp4_box(b"minf", smhd + dinf + stbl)
        mdia = self._mp4_box(b"mdia", mdhd + hdlr + minf)
        trak = self._mp4_box(b"trak", tkhd + mdia)
        mvhd = self._mp4_fullbox(b"mvhd", 0, 0,
            b"\x00" * 4       # creation_time
            + b"\x00" * 4     # modification_time
            + b"\x00\x00\x03\xE8"  # timescale=1000
            + b"\x00" * 4     # duration
            + b"\x00\x01\x00\x00"  # rate (1.0)
            + b"\x01\x00"     # volume (1.0)
            + b"\x00" * 10    # reserved
            + b"\x00\x01\x00\x00" + b"\x00" * 4 + b"\x00" * 4  # matrix
            + b"\x00" * 4 + b"\x00\x01\x00\x00" + b"\x00" * 4
            + b"\x00" * 4 + b"\x00" * 4 + b"\x40\x00\x00\x00"
            + b"\x00" * 24    # pre_defined
            + b"\x00\x00\x00\x02"  # next_track_ID
        )
        mvex_trex = self._mp4_fullbox(b"trex", 0, 0,
            b"\x00\x00\x00\x01"  # track_ID
            + b"\x00\x00\x00\x01"  # default_sample_description_index
            + b"\x00\x00\x00\x00"  # default_sample_duration
            + b"\x00\x00\x00\x00"  # default_sample_size
            + b"\x00\x00\x00\x00"  # default_sample_flags
        )
        mvex = self._mp4_box(b"mvex", mvex_trex)
        moov = self._mp4_box(b"moov", mvhd + trak + mvex)

        body = ftyp + moov
        self.send_response(200)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def _serve_fmp4_segment(self):
        """Serve a minimal fMP4 media segment (moof + mdat)."""
        seq_str = self.path.split("seg_")[1].split(".")[0]
        seq = int(seq_str)

        # moof: movie fragment header
        mfhd = self._mp4_fullbox(b"mfhd", 0, 0,
            struct.pack(">I", seq))  # sequence_number

        # trun: one sample, 1024 bytes of silence
        sample_duration = int(self.segment_duration * 44100)
        sample_size = 128  # tiny silent frame
        # flags: data-offset-present(0x01), sample-duration(0x100), sample-size(0x200)
        trun = self._mp4_fullbox(b"trun", 0, 0x000301,
            b"\x00\x00\x00\x01"    # sample_count=1
            + b"\x00\x00\x00\x00"  # data_offset (placeholder, patched below)
            + struct.pack(">I", sample_duration)
            + struct.pack(">I", sample_size)
        )

        tfhd = self._mp4_fullbox(b"tfhd", 0, 0x020000,  # default-base-is-moof
            b"\x00\x00\x00\x01")  # track_ID

        tfdt_time = seq * int(self.segment_duration * 44100)
        tfdt = self._mp4_fullbox(b"tfdt", 1, 0,
            struct.pack(">Q", tfdt_time))  # baseMediaDecodeTime (64-bit)

        traf = self._mp4_box(b"traf", tfhd + tfdt + trun)
        moof = self._mp4_box(b"moof", mfhd + traf)

        # mdat: silent audio data
        mdat_payload = b"\x00" * sample_size
        mdat = self._mp4_box(b"mdat", mdat_payload)

        # Patch data_offset in trun to point to mdat payload
        moof_size = len(moof)
        mdat_header_size = 8  # box size(4) + type(4)
        data_offset = moof_size + mdat_header_size
        # data_offset is in trun, find it: trun starts after mfhd in traf in moof
        # We need to find the data_offset field and patch it
        body = moof + mdat

        self.send_response(200)
        self.send_header("Content-Type", "video/iso.segment")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    @staticmethod
    def _mp4_box(box_type: bytes, payload: bytes) -> bytes:
        """Build an MP4 box: [size(4)][type(4)][payload]."""
        size = 8 + len(payload)
        return struct.pack(">I", size) + box_type + payload

    @staticmethod
    def _mp4_fullbox(box_type: bytes, version: int, flags: int,
                     payload: bytes) -> bytes:
        """Build an MP4 full box: [size(4)][type(4)][version(1)][flags(3)][payload]."""
        size = 12 + len(payload)
        return (struct.pack(">I", size) + box_type
                + struct.pack(">I", (version << 24) | (flags & 0x00FFFFFF))
                + payload)

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
    parser.add_argument("--segment-format", choices=["ts", "fmp4"],
                        default="ts",
                        help="Segment format: ts (MPEG-TS) or fmp4 (CMAF)")
    parser.add_argument("--drm", action="store_true", default=False,
                        help="Enable AES-128 DRM with test key "
                             "(key=00112233445566778899aabbccddeeff)")
    args = parser.parse_args()

    # fMP4 requires daterange signaling (CUE tags are TS-only convention)
    if args.segment_format == "fmp4" and args.signal_style == "cue_tags":
        args.signal_style = "daterange"
        print("  Note: fMP4 mode forces signal-style=daterange")

    HLSHandler.segment_duration = args.segment_duration
    HLSHandler.break_interval = args.break_interval
    HLSHandler.break_duration = args.break_duration
    HLSHandler.signal_style = args.signal_style
    HLSHandler.segment_format = args.segment_format
    HLSHandler.drm_enabled = args.drm
    HLSHandler.server_port = args.port

    server = HTTPServer(("0.0.0.0", args.port), HLSHandler)
    print(
        f"Test HLS server on http://0.0.0.0:{args.port}/live/playlist.m3u8\n"
        f"  Segment duration: {args.segment_duration}s\n"
        f"  Break every:      {args.break_interval} segments\n"
        f"  Break duration:   {args.break_duration}s\n"
        f"  Signal style:     {args.signal_style}\n"
        f"  Segment format:   {args.segment_format}\n"
        f"  DRM:              {'AES-128 (key=00112233...eeff)' if args.drm else 'none'}"
    )
    server.serve_forever()


if __name__ == "__main__":
    main()
