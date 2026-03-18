#!/usr/bin/env bash
#
# install.sh - Install hls-scte35 pipeline and dependencies
#
# Usage:
#   sudo ./install.sh              # Full install (deps + pipeline)
#   sudo ./install.sh --deps-only  # Install system dependencies only
#   ./install.sh --no-deps         # Pipeline setup only (skip apt/dnf)
#   sudo ./install.sh --service    # Full install + systemd services
#
# Supported: Ubuntu 22.04/24.04, Debian 12, Rocky/Alma/RHEL 8/9
# Requires: root for dependency installation and service setup

set -euo pipefail

# --- Defaults ---
INSTALL_DIR="/opt/hls-scte35"
DEPS=true
PIPELINE=true
SERVICE=false
TSDUCK_VERSION="3.42"
TSDUCK_BUILD="4421"

# --- Parse args ---
for arg in "$@"; do
    case "$arg" in
        --deps-only)  PIPELINE=false ;;
        --no-deps)    DEPS=false ;;
        --service)    SERVICE=true ;;
        --help|-h)
            sed -n '3,11p' "$0"
            exit 0
            ;;
    esac
done

log()  { echo -e "\033[1;32m==>\033[0m $*"; }
warn() { echo -e "\033[1;33m==> WARNING:\033[0m $*"; }
err()  { echo -e "\033[1;31m==> ERROR:\033[0m $*" >&2; exit 1; }

# --- OS Detection ---
detect_os() {
    if [ ! -f /etc/os-release ]; then
        err "Cannot detect OS: /etc/os-release not found"
    fi
    . /etc/os-release

    OS_ID="${ID}"
    OS_VERSION="${VERSION_ID}"
    OS_MAJOR="${VERSION_ID%%.*}"
    OS_CODENAME="${VERSION_CODENAME:-}"

    case "$OS_ID" in
        ubuntu|debian)
            PKG_MANAGER="apt"
            ;;
        rocky|almalinux|rhel|centos)
            PKG_MANAGER="dnf"
            ;;
        fedora)
            PKG_MANAGER="dnf"
            ;;
        *)
            err "Unsupported OS: ${OS_ID} ${OS_VERSION}. Supported: Ubuntu, Debian, Rocky, Alma, RHEL, Fedora."
            ;;
    esac

    log "Detected ${PRETTY_NAME} (package manager: ${PKG_MANAGER})"
}

# --- Dependency installation: apt (Debian/Ubuntu) ---
install_deps_apt() {
    apt-get update -qq
    apt-get install -y -qq \
        python3 \
        python3-venv \
        python3-pip \
        ffmpeg \
        curl \
        git \
        jq
}

# --- Dependency installation: dnf (Rocky/Alma/RHEL/Fedora) ---
install_deps_dnf() {
    # EPEL is needed for ffmpeg on RHEL-family
    if [[ "$OS_ID" =~ ^(rocky|almalinux|rhel|centos)$ ]]; then
        if ! rpm -q epel-release &>/dev/null; then
            log "Installing EPEL repository..."
            dnf install -y -q epel-release
        fi

        # RPM Fusion for ffmpeg (not in EPEL)
        if ! rpm -q rpmfusion-free-release &>/dev/null; then
            log "Installing RPM Fusion (free) for ffmpeg..."
            dnf install -y -q \
                "https://mirrors.rpmfusion.org/free/el/rpmfusion-free-release-${OS_MAJOR}.noarch.rpm" \
                || warn "RPM Fusion install failed — ffmpeg may need manual installation"
        fi

        # Enable CRB/PowerTools for build deps
        if [ "$OS_MAJOR" -ge 9 ]; then
            dnf config-manager --set-enabled crb 2>/dev/null || true
        elif [ "$OS_MAJOR" -eq 8 ]; then
            dnf config-manager --set-enabled powertools 2>/dev/null \
                || dnf config-manager --set-enabled PowerTools 2>/dev/null || true
        fi
    fi

    dnf install -y -q \
        python3 \
        python3-pip \
        ffmpeg \
        curl \
        git \
        jq

    # python3-venv isn't a separate package on RHEL-family; venv is included with python3
    # But we need pip's ensurepip for venv to work
    python3 -m ensurepip --default-pip 2>/dev/null || true
}

# --- TSDuck installation ---
install_tsduck() {
    if command -v tsp &>/dev/null; then
        INSTALLED_VER=$(tsp --version 2>&1 | grep -oP 'version \K[0-9]+\.[0-9]+' || echo "unknown")
        log "TSDuck already installed (v${INSTALLED_VER})"
        return
    fi

    log "Installing TSDuck v${TSDUCK_VERSION}..."
    ARCH=$(uname -m)
    TMPDIR=$(mktemp -d)
    trap "rm -rf '$TMPDIR'" EXIT

    case "$PKG_MANAGER" in
        apt)
            # Map arch: x86_64 -> amd64, aarch64 -> arm64
            case "$ARCH" in
                x86_64)  DEB_ARCH="amd64" ;;
                aarch64) DEB_ARCH="arm64" ;;
                *)       err "Unsupported architecture: ${ARCH}" ;;
            esac

            CODENAME="${OS_CODENAME}"
            [ -z "$CODENAME" ] && err "Cannot determine OS codename for TSDuck package"

            PKG_FILE="tsduck_${TSDUCK_VERSION}-${TSDUCK_BUILD}.${CODENAME}_${DEB_ARCH}.deb"
            PKG_URL="https://github.com/tsduck/tsduck/releases/download/v${TSDUCK_VERSION}/${PKG_FILE}"

            log "Downloading ${PKG_FILE}..."
            curl -fsSL -o "${TMPDIR}/${PKG_FILE}" "$PKG_URL" \
                || err "Failed to download TSDuck. Check https://github.com/tsduck/tsduck/releases"

            dpkg -i "${TMPDIR}/${PKG_FILE}" || apt-get install -f -y -qq
            ;;

        dnf)
            # TSDuck RPM naming: tsduck-3.42-4421.el9.x86_64.rpm
            case "$OS_ID" in
                fedora)   DIST_TAG="fc${OS_VERSION}" ;;
                *)        DIST_TAG="el${OS_MAJOR}" ;;
            esac

            PKG_FILE="tsduck-${TSDUCK_VERSION}-${TSDUCK_BUILD}.${DIST_TAG}.${ARCH}.rpm"
            PKG_URL="https://github.com/tsduck/tsduck/releases/download/v${TSDUCK_VERSION}/${PKG_FILE}"

            log "Downloading ${PKG_FILE}..."
            curl -fsSL -o "${TMPDIR}/${PKG_FILE}" "$PKG_URL" \
                || err "Failed to download TSDuck. Check https://github.com/tsduck/tsduck/releases"

            dnf install -y "$TMPDIR/${PKG_FILE}"
            ;;
    esac

    tsp --version 2>&1 | head -1
    log "TSDuck installed."
}

# --- Systemd service installation ---
install_services() {
    if [ "$(id -u)" -ne 0 ]; then
        err "Service installation requires root."
    fi

    # Create a dedicated service user if it doesn't exist
    if ! id hls-scte35 &>/dev/null; then
        log "Creating service user: hls-scte35"
        useradd --system --no-create-home --shell /usr/sbin/nologin hls-scte35
    fi

    # Set ownership
    chown -R hls-scte35:hls-scte35 "${INSTALL_DIR}"/{inject,logs,output}

    log "Installing systemd unit: hls-scte35-tsp.service"
    cat > /etc/systemd/system/hls-scte35-tsp.service <<EOF
[Unit]
Description=HLS-to-TS SCTE-35 Pipeline (TSDuck)
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=300
StartLimitBurst=5

[Service]
Type=simple
User=hls-scte35
Group=hls-scte35
ExecStart=${INSTALL_DIR}/bin/launch_tsp.sh ${INSTALL_DIR}/config/pipeline.toml
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal
SyslogIdentifier=hls-scte35-tsp

# Hardening
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=${INSTALL_DIR}/logs ${INSTALL_DIR}/output ${INSTALL_DIR}/inject
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF

    log "Installing systemd unit: hls-scte35-monitor.service"
    cat > /etc/systemd/system/hls-scte35-monitor.service <<EOF
[Unit]
Description=HLS Manifest Monitor for SCTE-35 Signal Detection
After=network-online.target hls-scte35-tsp.service
Wants=network-online.target
Requires=hls-scte35-tsp.service
StartLimitIntervalSec=300
StartLimitBurst=5

[Service]
Type=simple
User=hls-scte35
Group=hls-scte35
ExecStart=${INSTALL_DIR}/venv/bin/python3 ${INSTALL_DIR}/bin/manifest_monitor.py ${INSTALL_DIR}/config/pipeline.toml
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal
SyslogIdentifier=hls-scte35-monitor

# Hardening
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=${INSTALL_DIR}/logs ${INSTALL_DIR}/inject
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF

    log "Installing systemd unit: hls-scte35-api.service"
    cat > /etc/systemd/system/hls-scte35-api.service <<EOF
[Unit]
Description=HLS SCTE-35 REST API Server
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=300
StartLimitBurst=5

[Service]
Type=simple
User=hls-scte35
Group=hls-scte35
ExecStart=${INSTALL_DIR}/venv/bin/python3 ${INSTALL_DIR}/bin/api_server.py --config ${INSTALL_DIR}/config/pipeline.toml
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal
SyslogIdentifier=hls-scte35-api

# Hardening
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=${INSTALL_DIR}/logs ${INSTALL_DIR}/output ${INSTALL_DIR}/inject
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF

    log "Installing systemd target: hls-scte35.target"
    cat > /etc/systemd/system/hls-scte35.target <<EOF
[Unit]
Description=HLS SCTE-35 Pipeline (all components)
Wants=hls-scte35-tsp.service hls-scte35-monitor.service hls-scte35-api.service
After=hls-scte35-tsp.service hls-scte35-monitor.service hls-scte35-api.service

[Install]
WantedBy=multi-user.target
EOF

    systemctl daemon-reload
    systemctl enable hls-scte35.target

    log "Systemd services installed."
    echo ""
    echo "  Manage the full pipeline:"
    echo "    sudo systemctl start hls-scte35.target    # start all services"
    echo "    sudo systemctl stop hls-scte35.target     # stop all"
    echo "    sudo systemctl status hls-scte35-tsp      # TSDuck status"
    echo "    sudo systemctl status hls-scte35-monitor   # monitor status"
    echo "    sudo systemctl status hls-scte35-api       # API server status"
    echo ""
    echo "  View logs:"
    echo "    journalctl -u hls-scte35-tsp -f"
    echo "    journalctl -u hls-scte35-monitor -f"
    echo "    journalctl -u hls-scte35-api -f"
    echo ""
    echo "  API server:  http://localhost:8080/api/v1/pipeline"
    echo ""
    echo "  NOTE: Edit ${INSTALL_DIR}/config/pipeline.toml before starting."
}

# --- Preflight ---
if [ "$DEPS" = true ] && [ "$(id -u)" -ne 0 ]; then
    err "Dependency installation requires root. Run with sudo or use --no-deps."
fi

detect_os

# --- System dependencies ---
if [ "$DEPS" = true ]; then
    log "Installing system dependencies..."

    case "$PKG_MANAGER" in
        apt) install_deps_apt ;;
        dnf) install_deps_dnf ;;
    esac

    install_tsduck

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

    # Prompt for service install unless already decided via flags
    if [ "$SERVICE" = false ] && [ "$(id -u)" -eq 0 ]; then
        echo ""
        read -rp "$(echo -e '\033[1;32m==>\033[0m') Install as a systemd service? [Y/n] " REPLY
        case "${REPLY:-Y}" in
            [Yy]*|"") SERVICE=true ;;
        esac
    fi

    if [ "$SERVICE" = true ]; then
        install_services
    else
        echo "Next steps:"
        echo "  1. Edit config:    vi ${INSTALL_DIR}/config/pipeline.toml"
        echo "  2. Start TSDuck:   ${INSTALL_DIR}/bin/launch_tsp.sh"
        echo "  3. Start monitor:  ${INSTALL_DIR}/venv/bin/python3 ${INSTALL_DIR}/bin/manifest_monitor.py"
        echo "  4. Validate:       ${INSTALL_DIR}/bin/validate_scte35.sh file ${INSTALL_DIR}/output/live.ts"
    fi
fi
