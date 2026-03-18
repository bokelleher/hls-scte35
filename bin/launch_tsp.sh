#!/usr/bin/env bash
#
# launch_tsp.sh - Start the TSDuck HLS-to-TS pipeline
#
# Reads pipeline.toml and constructs the tsp command line.
# CLI arguments override config file values.
# Designed to be called by systemd, the API server, or run standalone.
#
# Usage:
#   ./launch_tsp.sh [config_file] [options]
#
# Options:
#   --source-url URL        HLS manifest URL
#   --output-mode MODE      Output: file, udp, srt
#   --output-file PATH      Output file path (when mode=file)
#   --scte35-pid PID        SCTE-35 PID number
#   --output-bitrate BPS    Output bitrate
#   --udp-address ADDR      UDP multicast address
#   --udp-port PORT         UDP port
#   --inject-dir DIR        Splice XML directory
#   --drm-mode MODE         DRM: none, auto, aes128
#   --drm-key HEX           Pre-shared AES key (32 hex digits)
#   --drm-iv HEX            Pre-shared IV (32 hex digits)
#
# Tested against TSDuck v3.42
#

set -uo pipefail

# --- Defaults ---
CONFIG="/opt/hls-scte35/config/pipeline.toml"
LOG_DIR="/opt/hls-scte35/logs"

# CLI override variables (empty = use config/default)
CLI_SOURCE_URL=""
CLI_OUTPUT_MODE=""
CLI_OUTPUT_FILE=""
CLI_SCTE35_PID=""
CLI_OUTPUT_BITRATE=""
CLI_UDP_ADDR=""
CLI_UDP_PORT=""
CLI_INJECT_DIR=""
CLI_DRM_MODE=""
CLI_DRM_KEY=""
CLI_DRM_IV=""
CLI_SRT_ADDR=""
CLI_SRT_PORT=""
CLI_SRT_MODE=""
CLI_SRT_LATENCY=""

# --- Parse arguments ---
while [[ $# -gt 0 ]]; do
    case "$1" in
        --source-url)      CLI_SOURCE_URL="$2"; shift 2 ;;
        --output-mode)     CLI_OUTPUT_MODE="$2"; shift 2 ;;
        --output-file)     CLI_OUTPUT_FILE="$2"; shift 2 ;;
        --scte35-pid)      CLI_SCTE35_PID="$2"; shift 2 ;;
        --output-bitrate)  CLI_OUTPUT_BITRATE="$2"; shift 2 ;;
        --udp-address)     CLI_UDP_ADDR="$2"; shift 2 ;;
        --udp-port)        CLI_UDP_PORT="$2"; shift 2 ;;
        --inject-dir)      CLI_INJECT_DIR="$2"; shift 2 ;;
        --drm-mode)        CLI_DRM_MODE="$2"; shift 2 ;;
        --drm-key)         CLI_DRM_KEY="$2"; shift 2 ;;
        --drm-iv)          CLI_DRM_IV="$2"; shift 2 ;;
        --srt-address)     CLI_SRT_ADDR="$2"; shift 2 ;;
        --srt-port)        CLI_SRT_PORT="$2"; shift 2 ;;
        --srt-mode)        CLI_SRT_MODE="$2"; shift 2 ;;
        --srt-latency)     CLI_SRT_LATENCY="$2"; shift 2 ;;
        --help|-h)
            sed -n '10,24p' "$0"
            exit 0
            ;;
        -*)
            echo "Unknown option: $1" >&2
            exit 1
            ;;
        *)
            # Positional: config file path
            CONFIG="$1"; shift
            ;;
    esac
done

# --- Parse config via Python TOML helper (section-aware, no ambiguity) ---
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="${SCRIPT_DIR}/../venv/bin/python3"
[ ! -x "$PYTHON" ] && PYTHON="python3"

if [ -f "$CONFIG" ]; then
    eval "$("$PYTHON" "$SCRIPT_DIR/config_helper.py" "$CONFIG" 2>/dev/null)"
fi

# Apply CLI overrides (CLI > config)
SOURCE_URL="${CLI_SOURCE_URL:-${CFG_SOURCE_URL:-}}"
SCTE35_PID="${CLI_SCTE35_PID:-${CFG_SCTE35_PID:-}}"
OUTPUT_MODE="${CLI_OUTPUT_MODE:-${CFG_OUTPUT_MODE:-}}"
OUTPUT_BITRATE="${CLI_OUTPUT_BITRATE:-${CFG_OUTPUT_BITRATE:-}}"

UDP_ADDR="${CLI_UDP_ADDR:-${CFG_UDP_ADDR:-}}"
UDP_PORT="${CLI_UDP_PORT:-${CFG_UDP_PORT:-}}"
UDP_LOCAL="${CFG_UDP_LOCAL:-}"

SRT_ADDR="${CLI_SRT_ADDR:-${CFG_SRT_ADDR:-}}"
SRT_PORT="${CLI_SRT_PORT:-${CFG_SRT_PORT:-}}"
SRT_MODE="${CLI_SRT_MODE:-${CFG_SRT_MODE:-}}"
SRT_LATENCY="${CLI_SRT_LATENCY:-${CFG_SRT_LATENCY:-}}"

FILE_PATH="${CLI_OUTPUT_FILE:-${CFG_FILE_PATH:-}}"
INJECT_DIR="${CLI_INJECT_DIR:-${CFG_INJECT_DIR:-}}"

# DRM config: env var > CLI > config
DRM_MODE="${CLI_DRM_MODE:-${CFG_DRM_MODE:-}}"
DRM_KEY="${DRM_KEY:-${CLI_DRM_KEY:-${CFG_DRM_KEY:-}}}"
DRM_IV="${CLI_DRM_IV:-${CFG_DRM_IV:-}}"

# --- Defaults ---
SCTE35_PID="${SCTE35_PID:-500}"
OUTPUT_BITRATE="${OUTPUT_BITRATE:-6000000}"
OUTPUT_MODE="${OUTPUT_MODE:-udp}"
INJECT_DIR="${INJECT_DIR:-/opt/hls-scte35/inject}"
INJECT_FILE="${INJECT_DIR}/splice.xml"
INJECT_BIN="${INJECT_DIR}/splice.bin"
DRM_MODE="${DRM_MODE:-none}"

# --- Validate required values ---
if [ -z "$SOURCE_URL" ]; then
    echo "ERROR: source url not set. Use --source-url or set url in $CONFIG"
    exit 1
fi

# --- Validate DRM config ---
if [ "$DRM_MODE" = "aes128" ] && [ -z "$DRM_KEY" ]; then
    echo "ERROR: drm mode is aes128 but no key provided."
    echo "  Use --drm-key, DRM_KEY env var, or set key in [drm] section of $CONFIG"
    exit 1
fi

if [ -n "$DRM_KEY" ]; then
    # Validate key is 32 hex digits
    if ! echo "$DRM_KEY" | grep -qE '^[0-9a-fA-F]{32}$'; then
        echo "ERROR: DRM key must be exactly 32 hex digits (16 bytes)"
        exit 1
    fi
fi

# --- Setup directories ---
mkdir -p "$LOG_DIR" "$INJECT_DIR"

# --- Create seed inject files so tsp has something to watch ---
# The monitor will overwrite these with real splice commands.
if [ ! -f "$INJECT_FILE" ]; then
    cat > "$INJECT_FILE" <<'SEED'
<?xml version="1.0" encoding="UTF-8"?>
<tsduck>
</tsduck>
SEED
fi

# Empty binary seed (tsp --poll-files needs the file to exist)
if [ ! -f "$INJECT_BIN" ]; then
    : > "$INJECT_BIN"
fi

echo "$(date -Iseconds) Starting TSDuck pipeline"
echo "  Source:      $SOURCE_URL"
echo "  SCTE-35 PID: $SCTE35_PID"
echo "  Output:      $OUTPUT_MODE"
echo "  Bitrate:     $OUTPUT_BITRATE bps"
echo "  Inject file: $INJECT_FILE"
echo "  DRM mode:    $DRM_MODE"

# --- Detect fMP4 segments (EXT-X-MAP in media playlist) ---
detect_fmp4() {
    local url="$1"
    local manifest
    manifest=$(curl -sf --max-time 10 "$url" 2>/dev/null) || return 1

    # If master playlist, follow the first rendition
    if echo "$manifest" | grep -q "^#EXT-X-STREAM-INF"; then
        local rendition
        rendition=$(echo "$manifest" | grep -v "^#" | grep -v "^$" | head -1)
        # Resolve relative URL
        if [[ "$rendition" != http* ]]; then
            local base
            base="${url%/*}"
            rendition="${base}/${rendition}"
        fi
        manifest=$(curl -sf --max-time 10 "$rendition" 2>/dev/null) || return 1
    fi

    # Check for EXT-X-MAP (indicates fMP4/CMAF segments)
    echo "$manifest" | grep -q "^#EXT-X-MAP"
}

# --- Determine input mode: direct HLS or ffmpeg ---
USE_FFMPEG=false
FFMPEG_ARGS=()

# Auto-detect fMP4 when DRM is not already forcing ffmpeg
if [ "$DRM_MODE" = "none" ] || [ -z "$DRM_MODE" ]; then
    if detect_fmp4 "$SOURCE_URL"; then
        USE_FFMPEG=true
        echo "  Segments:    fMP4/CMAF (auto-detected via EXT-X-MAP)"
    fi
fi

case "$DRM_MODE" in
    aes128)
        # Pre-shared key: route through ffmpeg with explicit decryption key
        USE_FFMPEG=true
        FFMPEG_ARGS+=( -decryption_key "$DRM_KEY" )
        if [ -n "$DRM_IV" ]; then
            FFMPEG_ARGS+=( -decryption_iv "$DRM_IV" )
        fi
        echo "  DRM key:     ****${DRM_KEY: -4}"
        ;;
    auto)
        # Let ffmpeg handle key fetch from EXT-X-KEY URI automatically
        USE_FFMPEG=true
        # Parse key_server_headers from config for auth
        KS_HEADERS=$(sed -n '/^\[drm\]/,/^\[/{ /^key_server_headers/p }' "$CONFIG" 2>/dev/null | head -1)
        if [ -n "$KS_HEADERS" ]; then
            # Extract header values - simplified for common Authorization header
            AUTH_VAL=$(echo "$KS_HEADERS" | grep -oP 'Authorization\s*=\s*"\K[^"]+')
            if [ -n "$AUTH_VAL" ]; then
                FFMPEG_ARGS+=( -headers "Authorization: ${AUTH_VAL}\r\n" )
                echo "  DRM auth:    Authorization header configured"
            fi
        fi
        echo "  DRM:         auto (ffmpeg handles EXT-X-KEY)"
        ;;
    none|"")
        # No DRM, use direct TSDuck HLS input
        ;;
    *)
        echo "ERROR: Unknown drm mode: $DRM_MODE (valid: none, auto, aes128)"
        exit 1
        ;;
esac

# --- Build tsp command ---
# Note: Each plugin's options must come BEFORE the next -P or -O flag.
# TSDuck v3.42 option reference:
#   tables:  --log, --log-hexa-line[=prefix], --log-size <n>, --pid <pid>
#   inject:  <file> (positional), --poll-files, --pid, --bitrate, --stuffing, --repeat, --xml
#   continuity: (no required options)
#   regulate: --bitrate

if [ "$USE_FFMPEG" = true ]; then
    # Build tsp with stdin input (ffmpeg pipes to it)
    TSP_CMD=(
        tsp --verbose --add-input-stuffing 1/10
        -I file
        -P continuity
        -P pmt --add-pid "$SCTE35_PID"/0x86 --add-registration 0x43554549 --add-pid-registration "$SCTE35_PID"/0x43554549 --set-cue-type "$SCTE35_PID"/0
        -P inject --pid "$SCTE35_PID" --xml --poll-files --stuffing --inter-packet 400 --repeat 3 "$INJECT_FILE"
        -P inject --pid "$SCTE35_PID" --binary --poll-files --stuffing --inter-packet 400 --repeat 3 "$INJECT_BIN"
        -P tables --pid "$SCTE35_PID" --log --log-hexa-line --log-size 80
        -P regulate --bitrate "$OUTPUT_BITRATE"
    )
else
    # Direct HLS input (no DRM)
    TSP_CMD=(
        tsp --verbose --add-input-stuffing 1/10
        -I hls "$SOURCE_URL"
        -P continuity
        -P pmt --add-pid "$SCTE35_PID"/0x86 --add-registration 0x43554549 --add-pid-registration "$SCTE35_PID"/0x43554549 --set-cue-type "$SCTE35_PID"/0
        -P inject --pid "$SCTE35_PID" --xml --poll-files --stuffing --inter-packet 400 --repeat 3 "$INJECT_FILE"
        -P inject --pid "$SCTE35_PID" --binary --poll-files --stuffing --inter-packet 400 --repeat 3 "$INJECT_BIN"
        -P tables --pid "$SCTE35_PID" --log --log-hexa-line --log-size 80
        -P regulate --bitrate "$OUTPUT_BITRATE"
    )
fi

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

echo "---"

if [ "$USE_FFMPEG" = true ]; then
    # ffmpeg decrypts and transmuxes to MPEG-TS on stdout, piped to tsp stdin
    echo "$(date -Iseconds) Executing: ffmpeg [...] | ${TSP_CMD[*]}"
    ffmpeg -loglevel warning -re \
        "${FFMPEG_ARGS[@]}" \
        -i "$SOURCE_URL" \
        -c copy -f mpegts pipe:1 \
    | "${TSP_CMD[@]}" 2>&1 | tee -a "${LOG_DIR}/tsp.log"
else
    echo "$(date -Iseconds) Executing: ${TSP_CMD[*]}"
    exec "${TSP_CMD[@]}" 2>&1 | tee -a "${LOG_DIR}/tsp.log"
fi
