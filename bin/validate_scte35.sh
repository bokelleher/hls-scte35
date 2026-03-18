#!/usr/bin/env bash
#
# validate_scte35.sh - Validate SCTE-35 presence and conformance in output
#
# Usage:
#   ./validate_scte35.sh udp 239.1.1.1:5000      # multicast input
#   ./validate_scte35.sh file /path/to/output.ts   # file input
#   ./validate_scte35.sh srt 192.168.1.42:9000     # SRT input
#

set -euo pipefail

MODE="${1:-udp}"
SOURCE="${2:-239.1.1.1:5000}"
SCTE35_PID="${3:-500}"
DURATION="${4:-60}"   # seconds to capture

echo "============================================"
echo " SCTE-35 Validation"
echo " Mode:       $MODE"
echo " Source:     $SOURCE"
echo " SCTE35 PID: $SCTE35_PID (0x$(printf '%X' "$SCTE35_PID"))"
echo " Duration:   ${DURATION}s"
echo "============================================"
echo ""

# --- Step 1: Quick TS analysis ---
echo "--- Step 1: Stream Analysis ---"
case "$MODE" in
    udp)
        timeout "$DURATION" tsp \
            -I ip "$SOURCE" \
            -P until --seconds "$DURATION" \
            -P analyze -o /dev/stdout \
            -O drop 2>/dev/null || true
        ;;
    file)
        tsp \
            -I file "$SOURCE" \
            -P analyze -o /dev/stdout \
            -O drop 2>/dev/null
        ;;
    srt)
        timeout "$DURATION" tsp \
            -I srt "$SOURCE" --mode listener --transtype live \
            -P until --seconds "$DURATION" \
            -P analyze -o /dev/stdout \
            -O drop 2>/dev/null || true
        ;;
esac

echo ""
echo "--- Step 2: SCTE-35 Section Dump ---"
case "$MODE" in
    udp)
        timeout "$DURATION" tsp \
            -I ip "$SOURCE" \
            -P until --seconds "$DURATION" \
            -P tables --pid "$SCTE35_PID" --log --log-hexa-line 80 \
            -O drop 2>&1 | head -100 || true
        ;;
    file)
        tsp \
            -I file "$SOURCE" \
            -P tables --pid "$SCTE35_PID" --log --log-hexa-line 80 \
            -O drop 2>&1 | head -100
        ;;
    srt)
        timeout "$DURATION" tsp \
            -I srt "$SOURCE" --mode listener --transtype live \
            -P until --seconds "$DURATION" \
            -P tables --pid "$SCTE35_PID" --log --log-hexa-line 80 \
            -O drop 2>&1 | head -100 || true
        ;;
esac

echo ""
echo "--- Step 3: Continuity Counter Check ---"
case "$MODE" in
    udp)
        timeout "$DURATION" tsp \
            -I ip "$SOURCE" \
            -P until --seconds "$DURATION" \
            -P continuity --log \
            -O drop 2>&1 | grep -i "error\|discontinuity" | head -20 || echo "  No CC errors detected"
        ;;
    file)
        tsp \
            -I file "$SOURCE" \
            -P continuity --log \
            -O drop 2>&1 | grep -i "error\|discontinuity" | head -20 || echo "  No CC errors detected"
        ;;
    srt)
        timeout "$DURATION" tsp \
            -I srt "$SOURCE" --mode listener --transtype live \
            -P until --seconds "$DURATION" \
            -P continuity --log \
            -O drop 2>&1 | grep -i "error\|discontinuity" | head -20 || echo "  No CC errors detected"
        ;;
esac

echo ""
echo "--- Validation complete ---"
