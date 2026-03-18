# hls-scte35

HLS-to-MPEG-TS pipeline with SCTE-35 ad marker injection using TSDuck.

Converts live HLS streams (TS or fMP4 segments) to MPEG Transport Stream with proper SCTE-35 splice_insert commands on a dedicated PID, registered in the PMT as stream_type 0x86.

## Architecture

```
HLS Source             manifest_monitor.py        launch_tsp.sh

+--------------+      +--------------------+     +----------------------+
| master.m3u8  |----->| Poll manifest      |     |                      |
| media.m3u8   |----->| Detect CUE-OUT/IN  |     | tsp -I hls (TS)      |
|              |      | Detect DATERANGE   |     |   or ffmpeg | tsp    |
| segments     |      | PTS calibration    |     |     (fMP4)           |
| (.ts/.m4s)   |      |                    |     | -P continuity        |
|              |      | Write splice.xml   |---->| -P pmt (SCTE-35 reg) |
+--------------+      +--------------------+     | -P inject (PID 500)  |
                                                 | -P regulate          |
                                                 | -O file/udp/srt      |
                                                 +----------------------+
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
# Clone and install
git clone https://github.com/bokelleher/hls-scte35.git
cd hls-scte35
sudo ./install.sh

# Or manually
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### Option 1: Config file

```bash
cp config/pipeline.toml config/pipeline.local.toml
vi config/pipeline.local.toml   # set source.url, output mode, etc.

./bin/launch_tsp.sh config/pipeline.local.toml
python3 ./bin/manifest_monitor.py config/pipeline.local.toml
```

### Option 2: CLI arguments

```bash
# All settings can be passed as CLI flags (override config file values)
./bin/launch_tsp.sh --source-url http://example.com/live/index.m3u8 \
    --output-mode file --output-file /tmp/output.ts

python3 ./bin/manifest_monitor.py --source-url http://example.com/live/index.m3u8 \
    --scte35-pid 500 --mode auto_detect --log-level DEBUG
```

### Option 3: REST API

```bash
# Start the API server
python3 ./bin/api_server.py --port 8080

# Start a pipeline via HTTP
curl -X POST http://localhost:8080/api/v1/pipeline \
  -H "Content-Type: application/json" \
  -d '{
    "source_url": "http://example.com/live/index.m3u8",
    "output_mode": "file",
    "output_file": "/tmp/output.ts"
  }'

# Check status
curl http://localhost:8080/api/v1/pipeline/status

# Stop the pipeline
curl -X DELETE http://localhost:8080/api/v1/pipeline
```

## REST API Reference

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/api/v1/health` | Health check |
| `POST` | `/api/v1/pipeline` | Start a pipeline |
| `DELETE` | `/api/v1/pipeline` | Stop the running pipeline |
| `GET` | `/api/v1/pipeline/status` | Get pipeline status |

### POST /api/v1/pipeline

Request body (JSON):

| Field | Type | Default | Description |
|---|---|---|---|
| `source_url` | string | *required* | HLS manifest URL |
| `output_mode` | string | `"file"` | `file`, `udp`, or `srt` |
| `output_file` | string | `output/live.ts` | Output path (file mode) |
| `scte35_pid` | int | `500` | SCTE-35 PID |
| `output_bitrate` | int | `40000000` | Output bitrate (bps) |
| `mode` | string | `"auto_detect"` | `auto_detect`, `manifest_only`, `inband_only` |
| `poll_interval` | float | `6.0` | Manifest poll interval (seconds) |
| `udp_address` | string | | Multicast address (udp mode) |
| `udp_port` | int | | Multicast port (udp mode) |
| `log_level` | string | `"INFO"` | `DEBUG`, `INFO`, `WARN`, `ERROR` |

## CLI Reference

### launch_tsp.sh

```
./bin/launch_tsp.sh [config_file] [options]

Options:
  --source-url URL        HLS manifest URL
  --output-mode MODE      Output: file, udp, srt
  --output-file PATH      Output file path (when mode=file)
  --scte35-pid PID        SCTE-35 PID number
  --output-bitrate BPS    Output bitrate
  --udp-address ADDR      UDP multicast address
  --udp-port PORT         UDP port
  --inject-dir DIR        Splice XML directory
```

### manifest_monitor.py

```
python3 ./bin/manifest_monitor.py [config_file] [options]

Options:
  --source-url URL        HLS manifest URL
  --poll-interval SEC     Poll interval in seconds
  --scte35-pid PID        SCTE-35 PID number
  --mode MODE             auto_detect, manifest_only, inband_only
  --inject-dir DIR        Splice XML output directory
  --log-level LEVEL       DEBUG, INFO, WARN, ERROR
```

## Examples

Each example shows both CLI and REST API usage. The monitor and tsp always run as a pair.

### HLS (TS segments) to local file

Record a live stream with SCTE-35 markers to a local TS file.

**CLI:**
```bash
./bin/launch_tsp.sh --source-url http://origin.example.com/live/index.m3u8 \
    --output-mode file --output-file /recordings/live.ts

python3 ./bin/manifest_monitor.py --source-url http://origin.example.com/live/index.m3u8
```

**API:**
```bash
curl -X POST http://localhost:8080/api/v1/pipeline \
  -H "Content-Type: application/json" \
  -d '{
    "source_url": "http://origin.example.com/live/index.m3u8",
    "output_mode": "file",
    "output_file": "/recordings/live.ts"
  }'
```

### HLS (TS segments) to UDP multicast

Ingest HLS and output to a multicast group for downstream ad splicers.

**CLI:**
```bash
./bin/launch_tsp.sh --source-url http://origin.example.com/live/index.m3u8 \
    --output-mode udp --udp-address 239.1.1.1 --udp-port 5000 \
    --output-bitrate 20000000

python3 ./bin/manifest_monitor.py --source-url http://origin.example.com/live/index.m3u8
```

**API:**
```bash
curl -X POST http://localhost:8080/api/v1/pipeline \
  -H "Content-Type: application/json" \
  -d '{
    "source_url": "http://origin.example.com/live/index.m3u8",
    "output_mode": "udp",
    "udp_address": "239.1.1.1",
    "udp_port": 5000,
    "output_bitrate": 20000000
  }'
```

### HLS (TS segments) to SRT

Feed a remote site over SRT with SCTE-35 signaling intact.

**CLI:**
```bash
./bin/launch_tsp.sh --source-url http://origin.example.com/live/index.m3u8 \
    --output-mode srt

python3 ./bin/manifest_monitor.py --source-url http://origin.example.com/live/index.m3u8
```

SRT address, port, mode, and latency are set in `pipeline.toml` under `[tsduck]`:
```toml
srt_address = "192.168.1.50"
srt_port = 9000
srt_mode = "caller"
srt_latency = 200
```

**API:**
```bash
curl -X POST http://localhost:8080/api/v1/pipeline \
  -H "Content-Type: application/json" \
  -d '{
    "source_url": "http://origin.example.com/live/index.m3u8",
    "output_mode": "srt"
  }'
```

### HLS (fMP4/CMAF segments) to UDP multicast

Ingest an fMP4 HLS source (auto-detected via `EXT-X-MAP` in the playlist). The pipeline routes through ffmpeg for transmux and uses `EXT-X-DATERANGE` tags for SCTE-35 detection.

**CLI:**
```bash
./bin/launch_tsp.sh --source-url http://origin.example.com/cmaf/master.m3u8 \
    --output-mode udp --udp-address 239.2.2.2 --udp-port 5001

python3 ./bin/manifest_monitor.py --source-url http://origin.example.com/cmaf/master.m3u8 \
    --mode manifest_only
```

**API:**
```bash
curl -X POST http://localhost:8080/api/v1/pipeline \
  -H "Content-Type: application/json" \
  -d '{
    "source_url": "http://origin.example.com/cmaf/master.m3u8",
    "output_mode": "udp",
    "udp_address": "239.2.2.2",
    "udp_port": 5001,
    "mode": "manifest_only"
  }'
```

### Manifest-only detection with custom PID

Force manifest-based detection (ignore in-band) and use a non-default SCTE-35 PID.

**CLI:**
```bash
./bin/launch_tsp.sh --source-url http://origin.example.com/live/index.m3u8 \
    --output-mode file --output-file /tmp/out.ts --scte35-pid 600

python3 ./bin/manifest_monitor.py --source-url http://origin.example.com/live/index.m3u8 \
    --scte35-pid 600 --mode manifest_only --log-level DEBUG
```

**API:**
```bash
curl -X POST http://localhost:8080/api/v1/pipeline \
  -H "Content-Type: application/json" \
  -d '{
    "source_url": "http://origin.example.com/live/index.m3u8",
    "output_mode": "file",
    "output_file": "/tmp/out.ts",
    "scte35_pid": 600,
    "mode": "manifest_only",
    "log_level": "DEBUG"
  }'
```

### Managing a running pipeline (API)

```bash
# Check if a pipeline is running
curl http://localhost:8080/api/v1/pipeline/status

# Stop the current pipeline
curl -X DELETE http://localhost:8080/api/v1/pipeline

# Start a new one with different settings
curl -X POST http://localhost:8080/api/v1/pipeline \
  -H "Content-Type: application/json" \
  -d '{"source_url": "http://backup.example.com/live/index.m3u8", "output_mode": "file"}'
```

## Configuration

All settings are in `config/pipeline.toml`. CLI arguments and API parameters override config file values.

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
