#!/usr/bin/env python3
"""
Config helper: reads pipeline.toml and outputs key=value pairs for shell consumption.

Usage:
    eval $(python3 config_helper.py /path/to/pipeline.toml)

Outputs flat environment variables:
    CFG_SOURCE_URL="http://..."
    CFG_POLL_INTERVAL="6.0"
    CFG_SCTE35_PID="500"
    CFG_OUTPUT_MODE="file"
    etc.
"""

import os
import sys

try:
    import tomllib
except ImportError:
    import tomli as tomllib


# Map of config paths to flat variable names
CONFIG_MAP = {
    ("source", "url"): "CFG_SOURCE_URL",
    ("source", "poll_interval"): "CFG_POLL_INTERVAL",
    ("scte35", "pid"): "CFG_SCTE35_PID",
    ("scte35", "mode"): "CFG_SCTE35_MODE",
    ("scte35", "default_duration"): "CFG_DEFAULT_DURATION",
    ("tsduck", "output_mode"): "CFG_OUTPUT_MODE",
    ("tsduck", "output_bitrate"): "CFG_OUTPUT_BITRATE",
    ("tsduck", "file_path"): "CFG_FILE_PATH",
    ("tsduck", "inject_dir"): "CFG_INJECT_DIR",
    ("tsduck", "udp_address"): "CFG_UDP_ADDR",
    ("tsduck", "udp_port"): "CFG_UDP_PORT",
    ("tsduck", "udp_local"): "CFG_UDP_LOCAL",
    ("tsduck", "srt_address"): "CFG_SRT_ADDR",
    ("tsduck", "srt_port"): "CFG_SRT_PORT",
    ("tsduck", "srt_mode"): "CFG_SRT_MODE",
    ("tsduck", "srt_latency"): "CFG_SRT_LATENCY",
    ("drm", "mode"): "CFG_DRM_MODE",
    ("drm", "key"): "CFG_DRM_KEY",
    ("drm", "iv"): "CFG_DRM_IV",
    ("logging", "log_dir"): "CFG_LOG_DIR",
    ("logging", "level"): "CFG_LOG_LEVEL",
}


def main():
    if len(sys.argv) < 2:
        print("Usage: config_helper.py <pipeline.toml>", file=sys.stderr)
        sys.exit(1)

    config_path = sys.argv[1]
    with open(config_path, "rb") as f:
        config = tomllib.load(f)

    for (section, key), var_name in CONFIG_MAP.items():
        value = config.get(section, {}).get(key)
        if value is not None:
            # Shell-safe quoting
            value_str = str(value).replace("'", "'\\''")
            print(f"{var_name}='{value_str}'")


if __name__ == "__main__":
    main()
