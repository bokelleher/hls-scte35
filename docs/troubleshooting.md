# Troubleshooting

### No SCTE-35 detected in output

**Symptom**: `tsanalyze` shows no PID 500, or splice.log shows only "Starting manifest monitor" with zero CUE detections.

**Check the manifest for CUE tags**:
```bash
curl -s http://your-source/index.m3u8 | grep -i "CUE\|DATERANGE\|SCTE"
```
If nothing appears, the source isn't signaling SCTE-35 in the manifest. Check if signals are in-band instead (`--mode inband_only`).

**Check for blank lines in the manifest** (common with x9k3):
```bash
curl -s http://your-source/index.m3u8 | cat -A | head -40
```
Blank lines between `#EXT-X-CUE-OUT` and `#EXTINF` are handled by the regex parser, but if the manifest structure is unusual, run with `--log-level DEBUG` and check `splice.log`.

**Check the inject file is being written**:
```bash
watch cat inject/splice.xml
```
If it stays empty (`<tsduck></tsduck>`), the monitor isn't detecting signals.

### PID 500 appears as "Unreferenced" in tsanalyze

The PMT plugin isn't running or isn't matching the service. Verify the tsp command includes:
```
-P pmt --add-pid 500/0x86 --add-registration 0x43554549 --add-pid-registration 500/0x43554549 --set-cue-type 500/0
```
If your source has multiple services, you may need `--service <id>` on the pmt plugin.

### TSDuck reports "loaded 0 sections" from splice.xml

The XML doesn't conform to TSDuck v3.42 schema. Common causes:
- `pts_time` present when `splice_immediate="true"` (schema violation)
- Missing `unique_program_id` when `splice_event_cancel="false"`
- Missing `<splice_information_table>` wrapper

Check the generated XML:
```bash
cat inject/splice.xml
```
Compare against the schema at `/usr/share/tsduck/tsduck.tables.model.xml`.

### fMP4 source not working

**Symptom**: tsp exits immediately or produces no output from an fMP4/CMAF HLS source.

Verify auto-detection found the fMP4 segments:
```bash
curl -s http://your-source/index.m3u8 | grep "EXT-X-MAP"
```
If `EXT-X-MAP` is in the master playlist but not the media playlist, auto-detection may fail. Try forcing ffmpeg mode with `--drm-mode auto` (which routes through ffmpeg regardless of DRM).

Check ffmpeg can read the source directly:
```bash
ffmpeg -i http://your-source/index.m3u8 -c copy -f mpegts -t 10 /tmp/test.ts
```

### DRM decryption fails

**`aes128` mode — "ERROR: DRM key must be exactly 32 hex digits"**:
The key must be exactly 16 bytes represented as 32 hex characters. No spaces, no `0x` prefix.

**`auto` mode — ffmpeg exits with "403 Forbidden" or "401 Unauthorized"**:
The key server requires authentication. Add headers in `pipeline.toml`:
```toml
[drm]
mode = "auto"
key_server_headers = { Authorization = "Bearer your-token-here" }
```

**`auto` mode — ffmpeg exits with "Protocol not found" or hangs**:
The `EXT-X-KEY` URI may use a scheme ffmpeg doesn't support (e.g., `skd://` for FairPlay). FairPlay is not supported — see the DRM architecture notes. Use `aes128` mode with a pre-shared key instead.

### API returns 401 Unauthorized

API key auth is enabled when the `API_KEY` environment variable is set on the server. All endpoints except `/api/v1/health` require the key. Pass it via header:
```bash
curl -H "X-API-Key: your-key" http://localhost:8080/api/v1/pipelines
```

### API returns 400 "Invalid URL scheme" or "localhost blocked"

SSRF protection blocks `file://`, `ftp://`, and localhost URLs by default. For local development:
```bash
ALLOW_LOCALHOST_SOURCES=1 python3 ./bin/api_server.py --port 8080
```

### Pipeline shows "degraded" state

One of the two processes (tsp or monitor) has crashed. The supervisor will attempt automatic restart (up to 5 times in 5 minutes). Check the logs:
```bash
cat logs/<pipeline-id>/tsp.log
cat logs/<pipeline-id>/monitor.log
```
If the supervisor gives up, you'll see "max restarts exceeded" in the API server output. Stop and recreate the pipeline.

### High latency on fMP4 sources

ffmpeg's transmux adds 1-2 segment durations of buffering. For live sources, this is expected. To minimize:
- Use shorter segment durations at the source
- Set `ffmpeg_buffer_mode = "low_latency"` in the `[tuning]` config section
- If you don't need DRM decryption and the source is TS (not fMP4), remove `--drm-mode` to bypass ffmpeg entirely

### Monitor stops detecting after hours of running

This was a known issue where `seen_cues` grew unbounded. As of v0.7.0, seen cues expire after 1 hour automatically. If you're on an older version, upgrade. Verify with the metrics endpoint:
```bash
curl -s http://localhost:8080/api/v1/metrics | grep seen_cues_size
```

### Config changes not taking effect

If using the systemd service, restart after editing `pipeline.toml`:
```bash
sudo systemctl restart hls-scte35.target
```
CLI arguments and API parameters always override config file values. Check which values are active in the tsp startup log:
```bash
head -10 logs/tsp.log
```
