# hls-scte35

HLS-to-MPEG-TS pipeline with SCTE-35 ad marker injection using TSDuck.

Converts live HLS streams (TS or fMP4 segments) to MPEG Transport Stream with proper SCTE-35 splice_insert commands on a dedicated PID, registered in the PMT as stream_type 0x86.

## Architecture

```
HLS Source                manifest_monitor.py              launch_tsp.sh
┌─────────────┐         ┌───────────────────┐         ┌──────────────────────┐
│ master.m3u8 │────────>│ Poll manifest     │         │                      │
│ media.m3u8  │────────>│ Detect CUE-OUT/IN │         │ tsp -I hls (TS)      │
│             │         │ Detect DATERANGE   │         │  or ffmpeg | tsp     │
│ segments    │         │ PTS calibration   │         │   (fMP4)             │
│ (.ts/.m4s)  │         │                   │         │ -P continuity        │
│             │         │ Write splice.xml  │────────>│ -P pmt (SCTE-35 reg) │
└─────────────┘         └───────────────────┘         │ -P inject (PID 500)  │
                                                      │ -P regulate          │
                                                      │ -O file/udp/srt      │
                                                      └──────────────────────┘
```

### Signal Detection

The monitor detects SCTE-35 ad markers from three sources:

| Signal Type | HLS Tag | Segment Format |
|---|---|---|
| CUE tags | `EXT-X-CUE-OUT` / `EXT-X-CUE-IN` | TS |
| DATERANGE | `EXT-X-DATERANGE` with `SCTE35-CMD` | TS or fMP4 |
| In-band | Passthrough (TSDuck native) | TS |

### fMP4 Input

When the source uses fMP4/CMAF segments (`EXT-X-MAP` present in the playlist), the pipeline automatically routes through ffmpeg for transmux:

```
ffmpeg -i <hls_url> -c copy -f mpegts pipe:1 | tsp -I file ...
```

PTS calibration runs automatically to correct for timestamp rebasing during transmux.

## Prerequisites

- **TSDuck** v3.42+ ([tsduck.io](https://tsduck.io))
- **Python** 3.11+
- **ffmpeg** / **ffprobe** (required for fMP4 input)

## Quick Start

```bash
# Clone and install Python deps
git clone https://github.com/<you>/hls-scte35.git
cd hls-scte35
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Edit config
cp config/pipeline.toml config/pipeline.local.toml
# Set source.url, output mode, etc.

# Start the pipeline (two terminals)
./bin/launch_tsp.sh config/pipeline.local.toml
python3 ./bin/manifest_monitor.py config/pipeline.local.toml
```

## Configuration

All settings are in `config/pipeline.toml`:

```toml
[source]
url = "http://example.com/live/index.m3u8"
poll_interval = 6.0

[scte35]
pid = 500
default_duration = 30.0
mode = "auto_detect"           # auto_detect | manifest_only | inband_only
calibration_enabled = true     # PTS calibration for fMP4 sources

[tsduck]
output_mode = "udp"            # udp | srt | file
udp_address = "239.1.1.1"
udp_port = 5000
output_bitrate = 40000000

[logging]
level = "INFO"
```

## Output

The TSDuck pipeline produces a compliant MPEG-TS with:

- **PID 500** carrying SCTE-35 sections (`splice_insert` commands)
- **PMT** updated with stream_type `0x86` and CUEI registration descriptor
- **splice_immediate** mode for manifest-detected signals
- **PTS-timed splices** when PROGRAM-DATE-TIME is available and calibrated

## Validation

```bash
# Analyze output stream
./bin/validate_scte35.sh file output/live.ts

# Quick check with tsanalyze
tsanalyze output/live.ts
# Look for: PID 500 (0x01F4) — SCTE-35, stream_type 0x86
```

## Testing

A synthetic HLS server is included for development:

```bash
# Serve a live HLS playlist with CUE-OUT/CUE-IN every 10 segments
python3 bin/test_hls_server.py --port 8899

# Or with DATERANGE signaling (fMP4 style)
python3 bin/test_hls_server.py --port 8899 --signal-style daterange
```

## TSDuck XML Schema

Splice commands conform to TSDuck v3.42 XML schema (`/usr/share/tsduck/tsduck.tables.model.xml`):

- `unique_program_id` is required when `splice_event_cancel="false"`
- `pts_time` must NOT be present when `splice_immediate="true"`
- All commands are wrapped in `<splice_information_table>`

## License

MIT
