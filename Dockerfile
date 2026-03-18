# ---------------------------------------------------------------------------
# hls-scte35 Docker image
#
# Multi-stage build:
#   Stage 1: Debian — install TSDuck from official .deb package
#   Stage 2: Alpine — lightweight runtime with gcompat for TSDuck binaries
#
# Usage:
#   docker build -t hls-scte35 .
#   docker run -p 8080:8080 hls-scte35
#
# With API key:
#   docker run -e API_KEY=mysecret -p 8080:8080 hls-scte35
#
# With DRM key:
#   docker run -e DRM_KEY=00112233445566778899aabbccddeeff hls-scte35 \
#     ./bin/launch_tsp.sh --source-url http://origin/drm.m3u8 --drm-mode aes128 ...
# ---------------------------------------------------------------------------

ARG TSDUCK_VERSION=3.42
ARG TSDUCK_BUILD=4421

# ========================== Stage 1: TSDuck from Debian ==========================
FROM debian:bookworm-slim AS tsduck-builder

ARG TSDUCK_VERSION
ARG TSDUCK_BUILD
ARG TARGETARCH

RUN apt-get update -qq && apt-get install -y -qq curl

# Download and extract TSDuck .deb (don't install — just unpack the binaries)
RUN set -e; \
    case "${TARGETARCH}" in \
        amd64) DEB_ARCH="amd64" ;; \
        arm64) DEB_ARCH="arm64" ;; \
        *) echo "Unsupported arch: ${TARGETARCH}" && exit 1 ;; \
    esac; \
    PKG="tsduck_${TSDUCK_VERSION}-${TSDUCK_BUILD}.bookworm_${DEB_ARCH}.deb"; \
    curl -fsSL -o /tmp/tsduck.deb \
        "https://github.com/tsduck/tsduck/releases/download/v${TSDUCK_VERSION}/${PKG}"; \
    mkdir -p /tsduck-extract; \
    dpkg-deb -x /tmp/tsduck.deb /tsduck-extract; \
    rm /tmp/tsduck.deb

# ========================== Stage 2: Alpine runtime ==========================
FROM alpine:3.20

# gcompat provides glibc compatibility layer for TSDuck binaries
# libstdc++ is needed by TSDuck C++ runtime
RUN apk add --no-cache \
    gcompat \
    libstdc++ \
    python3 \
    py3-pip \
    ffmpeg \
    curl \
    bash \
    jq

# Copy TSDuck binaries and libraries from builder stage
COPY --from=tsduck-builder /tsduck-extract/usr/bin/ /usr/bin/
COPY --from=tsduck-builder /tsduck-extract/usr/lib/ /usr/lib/
COPY --from=tsduck-builder /tsduck-extract/usr/share/tsduck/ /usr/share/tsduck/

# Verify TSDuck works under gcompat
RUN tsp --version || (echo "TSDuck failed under gcompat — see README for alternatives" && exit 1)

# Create app directory structure
WORKDIR /opt/hls-scte35
RUN mkdir -p bin config inject logs output tests

# Install Python dependencies (no venv needed in container)
COPY requirements.txt .
RUN pip3 install --no-cache-dir --break-system-packages -r requirements.txt

# Copy application code
COPY bin/ bin/
COPY config/pipeline.toml config/pipeline.toml
COPY tests/ tests/
COPY openapi.yaml .

# Make scripts executable
RUN chmod +x bin/*.sh

# Create non-root user
RUN adduser -D -H -s /sbin/nologin hls-scte35 && \
    chown -R hls-scte35:hls-scte35 inject logs output

# Run tests to verify the image is correct
RUN python3 -m pytest tests/ -q --tb=short

# Runtime config
ENV PIPELINE_CONFIG=/opt/hls-scte35/config/pipeline.toml
ENV ALLOW_LOCALHOST_SOURCES=1

EXPOSE 8080

USER hls-scte35

# Default: start the API server
CMD ["python3", "bin/api_server.py", "--port", "8080", "--host", "0.0.0.0"]
