#!/usr/bin/env bash
#
# launch_tsp.sh - Start the TSDuck HLS-to-TS pipeline
#
# Reads pipeline.toml and constructs the tsp command line.
# Designed to be called by systemd or run standalone.
#
# Tested against TSDuck v3.42
#

set -uo pipefail

CONFIG="${1:-/opt/hls-scte35/config/pipeline.toml}"
LOG_DIR="/opt/hls-scte35/logs"
INJECT_DIR="/opt/hls-scte35/inject"
INJECT_FILE="${INJECT_DIR}/splice.xml"

# --- Parse config (lightweight TOML extraction) ---
# Strips inline comments, unquotes values, trims whitespace
get_val() {
    local key="$1"
    local result
    result=$(grep "^${key}" "$CONFIG" 2>/dev/null | head -1 | sed 's/#.*//' | sed 's/.*= *"\{0,1\}\([^"]*\)"\{0,1\}/\1/' | tr -d ' ')
    echo "$result"
}

SOURCE_URL=$(get_val "url")
SCTE35_PID=$(get_val "pid")
OUTPUT_MODE=$(get_val "output_mode")
OUTPUT_BITRATE=$(get_val "output_bitrate")

UDP_ADDR=$(get_val "udp_address")
UDP_PORT=$(get_val "udp_port")
UDP_LOCAL=$(get_val "udp_local")

SRT_ADDR=$(get_val "srt_address")
SRT_PORT=$(get_val "srt_port")
SRT_MODE=$(get_val "srt_mode")
SRT_LATENCY=$(get_val "srt_latency")

FILE_PATH=$(get_val "file_path")

# --- Defaults ---
SCTE35_PID="${SCTE35_PID:-500}"
OUTPUT_BITRATE="${OUTPUT_BITRATE:-6000000}"
OUTPUT_MODE="${OUTPUT_MODE:-udp}"

# --- Validate required values ---
if [ -z "$SOURCE_URL" ]; then
    echo "ERROR: source url not set in $CONFIG"
    exit 1
fi

# --- Setup directories ---
mkdir -p "$LOG_DIR" "$INJECT_DIR"

# --- Create seed inject file so tsp has something to watch ---
# The monitor will overwrite this with real splice commands.
if [ ! -f "$INJECT_FILE" ]; then
    cat > "$INJECT_FILE" <<'SEED'
<?xml version="1.0" encoding="UTF-8"?>
<tsduck>
</tsduck>
SEED
fi

echo "$(date -Iseconds) Starting TSDuck pipeline"
echo "  Source:      $SOURCE_URL"
echo "  SCTE-35 PID: $SCTE35_PID"
echo "  Output:      $OUTPUT_MODE"
echo "  Bitrate:     $OUTPUT_BITRATE bps"
echo "  Inject file: $INJECT_FILE"

# --- Build tsp command ---
# Note: Each plugin's options must come BEFORE the next -P or -O flag.
# TSDuck v3.42 option reference:
#   tables:  --log, --log-hexa-line[=prefix], --log-size <n>, --pid <pid>
#   inject:  <file> (positional), --poll-files, --pid, --bitrate, --stuffing, --repeat, --xml
#   continuity: (no required options)
#   regulate: --bitrate

TSP_CMD=(
    tsp --verbose --add-input-stuffing 1/10
    -I hls "$SOURCE_URL"
    -P continuity
    -P pmt --add-pid "$SCTE35_PID"/0x86 --add-registration 0x43554549 --add-pid-registration "$SCTE35_PID"/0x43554549 --set-cue-type "$SCTE35_PID"/0
    -P inject --pid "$SCTE35_PID" --xml --poll-files --stuffing --inter-packet 400 --repeat 3 "$INJECT_FILE"
    -P tables --pid "$SCTE35_PID" --log --log-hexa-line --log-size 80
    -P regulate --bitrate "$OUTPUT_BITRATE"
)

# --- Output plugin ---
case "$OUTPUT_MODE" in
    udp)
        TSP_CMD+=( -O ip "${UDP_ADDR}:${UDP_PORT}" --local-address "${UDP_LOCAL:-0.0.0.0}" --ttl 5 --packet-burst 7 )
        echo "  Multicast:   ${UDP_ADDR}:${UDP_PORT} (from ${UDP_LOCAL:-0.0.0.0})"
        ;;
    srt)
        TSP_CMD+=( -O srt "${SRT_ADDR}:${SRT_PORT}" --mode "${SRT_MODE:-caller}" --transtype live --latency "${SRT_LATENCY:-200}" )
        echo "  SRT:         ${SRT_ADDR}:${SRT_PORT} (${SRT_MODE:-caller})"
        ;;
    file)
        TSP_CMD+=( -O file "${FILE_PATH:-/opt/hls-scte35/output/live.ts}" )
        echo "  File:        ${FILE_PATH:-/opt/hls-scte35/output/live.ts}"
        ;;
    *)
        echo "ERROR: Unknown output_mode: $OUTPUT_MODE"
        exit 1
        ;;
esac

echo "$(date -Iseconds) Executing: ${TSP_CMD[*]}"
echo "---"

exec "${TSP_CMD[@]}" 2>&1 | tee -a "${LOG_DIR}/tsp.log"