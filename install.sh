#!/usr/bin/env bash
#
# install.sh - Install hls-scte35 pipeline and dependencies
#
# Usage:
#   sudo ./install.sh              # Full install (deps + pipeline)
#   sudo ./install.sh --deps-only  # Install system dependencies only
#   ./install.sh --no-deps         # Pipeline setup only (skip apt/TSDuck)
#
# Tested on: Ubuntu 22.04, 24.04 (amd64)
# Requires: root for dependency installation

set -euo pipefail

# --- Defaults ---
INSTALL_DIR="/opt/hls-scte35"
DEPS=true
PIPELINE=true
TSDUCK_VERSION="3.42"

# --- Parse args ---
for arg in "$@"; do
    case "$arg" in
        --deps-only)  PIPELINE=false ;;
        --no-deps)    DEPS=false ;;
        --help|-h)
            sed -n '3,8p' "$0"
            exit 0
            ;;
    esac
done

log()  { echo -e "\033[1;32m==>\033[0m $*"; }
warn() { echo -e "\033[1;33m==> WARNING:\033[0m $*"; }
err()  { echo -e "\033[1;31m==> ERROR:\033[0m $*" >&2; exit 1; }

# --- Preflight ---
if [ "$DEPS" = true ] && [ "$(id -u)" -ne 0 ]; then
    err "Dependency installation requires root. Run with sudo or use --no-deps."
fi

# --- System dependencies ---
if [ "$DEPS" = true ]; then
    log "Installing system dependencies..."

    apt-get update -qq
    apt-get install -y -qq \
        python3 \
        python3-venv \
        python3-pip \
        ffmpeg \
        curl \
        git \
        jq

    # TSDuck
    if command -v tsp &>/dev/null; then
        INSTALLED_VER=$(tsp --version 2>&1 | grep -oP 'version \K[0-9]+\.[0-9]+' || echo "unknown")
        log "TSDuck already installed (v${INSTALLED_VER})"
    else
        log "Installing TSDuck..."
        ARCH=$(dpkg --print-architecture)
        CODENAME=$(lsb_release -cs 2>/dev/null || source /etc/os-release && echo "$VERSION_CODENAME")

        TSDUCK_DEB="tsduck_${TSDUCK_VERSION}-4421.${CODENAME}_${ARCH}.deb"
        TSDUCK_URL="https://github.com/tsduck/tsduck/releases/download/v${TSDUCK_VERSION}/${TSDUCK_DEB}"

        log "Downloading ${TSDUCK_DEB}..."
        TMPDIR=$(mktemp -d)
        curl -fsSL -o "${TMPDIR}/${TSDUCK_DEB}" "$TSDUCK_URL" \
            || err "Failed to download TSDuck. Check https://github.com/tsduck/tsduck/releases for your platform."

        dpkg -i "${TMPDIR}/${TSDUCK_DEB}" || apt-get install -f -y -qq
        rm -rf "$TMPDIR"

        tsp --version 2>&1 | head -1
        log "TSDuck installed."
    fi

    # Verify
    log "Checking installed versions..."
    echo "  python3:  $(python3 --version 2>&1)"
    echo "  ffmpeg:   $(ffmpeg -version 2>&1 | head -1)"
    echo "  ffprobe:  $(ffprobe -version 2>&1 | head -1)"
    echo "  tsp:      $(tsp --version 2>&1 | head -1)"
fi

# --- Pipeline setup ---
if [ "$PIPELINE" = true ]; then
    log "Setting up pipeline in ${INSTALL_DIR}..."

    # Create directory structure
    mkdir -p "${INSTALL_DIR}"/{bin,config,inject,logs,output}

    # Copy files if running from a different directory (e.g., git clone)
    SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    if [ "$SCRIPT_DIR" != "$INSTALL_DIR" ]; then
        log "Copying files from ${SCRIPT_DIR} to ${INSTALL_DIR}..."
        cp -v "$SCRIPT_DIR"/bin/*.py "$INSTALL_DIR/bin/"
        cp -v "$SCRIPT_DIR"/bin/*.sh "$INSTALL_DIR/bin/"
        cp -v "$SCRIPT_DIR"/config/pipeline.toml "$INSTALL_DIR/config/pipeline.toml"
        cp -v "$SCRIPT_DIR"/requirements.txt "$INSTALL_DIR/"
    fi

    # Make scripts executable
    chmod +x "${INSTALL_DIR}"/bin/*.sh

    # Python virtual environment
    if [ ! -d "${INSTALL_DIR}/venv" ]; then
        log "Creating Python virtual environment..."
        python3 -m venv "${INSTALL_DIR}/venv"
    fi

    log "Installing Python dependencies..."
    "${INSTALL_DIR}/venv/bin/pip" install --quiet --upgrade pip
    "${INSTALL_DIR}/venv/bin/pip" install --quiet -r "${INSTALL_DIR}/requirements.txt"

    # Seed inject file
    if [ ! -f "${INSTALL_DIR}/inject/splice.xml" ]; then
        cat > "${INSTALL_DIR}/inject/splice.xml" <<'SEED'
<?xml version="1.0" encoding="UTF-8"?>
<tsduck>
</tsduck>
SEED
    fi

    log "Installation complete."
    echo ""
    echo "  Install dir:  ${INSTALL_DIR}"
    echo "  Config:       ${INSTALL_DIR}/config/pipeline.toml"
    echo "  Python venv:  ${INSTALL_DIR}/venv"
    echo ""
    echo "Next steps:"
    echo "  1. Edit config:    vi ${INSTALL_DIR}/config/pipeline.toml"
    echo "  2. Start TSDuck:   ${INSTALL_DIR}/bin/launch_tsp.sh"
    echo "  3. Start monitor:  ${INSTALL_DIR}/venv/bin/python3 ${INSTALL_DIR}/bin/manifest_monitor.py"
    echo "  4. Validate:       ${INSTALL_DIR}/bin/validate_scte35.sh file ${INSTALL_DIR}/output/live.ts"
fi
