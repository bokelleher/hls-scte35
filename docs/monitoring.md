# Monitoring & Observability

This guide covers setting up Prometheus, Grafana, and InfluxDB/Telegraf to monitor hls-scte35 pipelines in production.

## Metrics Endpoint

The API server exposes metrics at `GET /api/v1/metrics`.

**Prometheus format** (for scraping):
```bash
curl -H "Accept: text/plain" http://localhost:8080/api/v1/metrics
```

**JSON format** (for debugging or custom integrations):
```bash
curl http://localhost:8080/api/v1/metrics
```

If `API_KEY` is set, include the auth header:
```bash
curl -H "Accept: text/plain" -H "X-API-Key: your-key" http://localhost:8080/api/v1/metrics
```

## Available Metrics

### SCTE-35 Signal Metrics

| Metric | Type | Labels | Description |
|---|---|---|---|
| `scte35_events_detected_total` | counter | `type` | Events detected from manifest. Types: `cue_out`, `cue_in`, `splice_insert`, `time_signal`, `splice_null`, `splice_schedule`, `bandwidth_reservation`, `private_command` |
| `scte35_events_injected_total` | counter | `format` | Events written to inject files. Formats: `xml` (CUE tags), `binary` (DATERANGE raw passthrough) |

### Manifest Polling

| Metric | Type | Labels | Description |
|---|---|---|---|
| `manifest_poll_total` | counter | | Total poll attempts |
| `manifest_poll_errors_total` | counter | | Failed polls (HTTP errors, timeouts) |
| `manifest_poll_duration_seconds` | histogram | | Time to fetch and process manifest |

### PTS Calibration

| Metric | Type | Labels | Description |
|---|---|---|---|
| `pts_calibration_status` | gauge | | `-1` = disabled, `0` = pending, `1` = calibrated |
| `pts_calibration_attempts_total` | counter | | Probe attempts (high count = pipeline startup issues) |

### DRM

| Metric | Type | Labels | Description |
|---|---|---|---|
| `drm_detected` | gauge | | `0` = no DRM in manifest, `1` = DRM detected |
| `drm_key_rotations_total` | counter | | Key rotation events (new `EXT-X-KEY` URI) |

### Pipeline Health

| Metric | Type | Labels | Description |
|---|---|---|---|
| `pipeline_state` | gauge | `id` | `1` = running, `0.5` = degraded, `0` = stopped |
| `pipeline_restarts_total` | counter | `id`, `process` | Automatic restarts. Process: `tsp` or `monitor` |
| `pipelines_created_total` | counter | | Total pipelines created via API |
| `pipelines_stopped_total` | counter | | Total pipelines stopped |
| `pipelines_failed_total` | counter | | Pipeline creation failures |

### Internal State

| Metric | Type | Labels | Description |
|---|---|---|---|
| `seen_cues_size` | gauge | | Current size of the CUE deduplication set |
| `seen_raw_hashes_size` | gauge | | Current size of the raw binary dedup set |
| `process_start_time_seconds` | gauge | | Unix timestamp when the API server started |

## Prometheus Setup

### prometheus.yml

```yaml
scrape_configs:
  - job_name: "hls-scte35"
    scrape_interval: 15s
    metrics_path: /api/v1/metrics
    scheme: http
    static_configs:
      - targets: ["localhost:8080"]
        labels:
          environment: "production"
    # If API_KEY is set:
    # headers:
    #   X-API-Key: ["your-api-key"]
```

For multiple instances:
```yaml
scrape_configs:
  - job_name: "hls-scte35"
    scrape_interval: 15s
    metrics_path: /api/v1/metrics
    static_configs:
      - targets:
          - "scte35-node1:8080"
          - "scte35-node2:8080"
          - "scte35-node3:8080"
```

### Docker Compose with Prometheus

```yaml
services:
  hls-scte35:
    build: .
    ports:
      - "8080:8080"

  prometheus:
    image: prom/prometheus:latest
    ports:
      - "9090:9090"
    volumes:
      - ./prometheus.yml:/etc/prometheus/prometheus.yml:ro
    depends_on:
      - hls-scte35
```

## Grafana Dashboard

### Recommended Panels

**Row 1 — Signal Detection**

| Panel | Query | Visualization |
|---|---|---|
| Events/min by type | `rate(scte35_events_detected_total[5m]) * 60` | Time series, stacked by `type` |
| Injection rate | `rate(scte35_events_injected_total[5m]) * 60` | Time series, by `format` |
| Detection vs injection gap | `sum(rate(scte35_events_detected_total[5m])) - sum(rate(scte35_events_injected_total[5m]))` | Stat (should be ~0) |

**Row 2 — Pipeline Health**

| Panel | Query | Visualization |
|---|---|---|
| Pipeline state | `pipeline_state` | State timeline, by `id` |
| Restarts | `increase(pipeline_restarts_total[1h])` | Bar gauge, by `id` and `process` |
| Active pipelines | `count(pipeline_state == 1)` | Stat |

**Row 3 — Polling & Latency**

| Panel | Query | Visualization |
|---|---|---|
| Poll duration (p99) | `histogram_quantile(0.99, manifest_poll_duration_seconds)` | Time series |
| Poll error rate | `rate(manifest_poll_errors_total[5m]) / rate(manifest_poll_total[5m])` | Gauge (red if >5%) |
| Poll success rate | `1 - (rate(manifest_poll_errors_total[5m]) / rate(manifest_poll_total[5m]))` | Stat (percentage) |

**Row 4 — DRM & Calibration**

| Panel | Query | Visualization |
|---|---|---|
| PTS calibration | `pts_calibration_status` | State timeline (-1/0/1) |
| Calibration attempts | `pts_calibration_attempts_total` | Stat |
| Key rotations | `increase(drm_key_rotations_total[1h])` | Time series |
| DRM detected | `drm_detected` | Stat (boolean) |

### Recommended Alerts

```yaml
groups:
  - name: hls-scte35
    rules:
      # Pipeline down
      - alert: PipelineDown
        expr: pipeline_state == 0
        for: 2m
        labels:
          severity: critical
        annotations:
          summary: "Pipeline {{ $labels.id }} is down"

      # Pipeline degraded (one process crashed)
      - alert: PipelineDegraded
        expr: pipeline_state == 0.5
        for: 1m
        labels:
          severity: warning
        annotations:
          summary: "Pipeline {{ $labels.id }} is degraded"

      # High poll error rate
      - alert: ManifestPollErrors
        expr: rate(manifest_poll_errors_total[5m]) / rate(manifest_poll_total[5m]) > 0.1
        for: 5m
        labels:
          severity: warning
        annotations:
          summary: "Manifest poll error rate > 10%"

      # No SCTE-35 events detected for 30 minutes
      - alert: NoEventsDetected
        expr: increase(scte35_events_detected_total[30m]) == 0
        for: 30m
        labels:
          severity: warning
        annotations:
          summary: "No SCTE-35 events detected in 30 minutes"

      # Excessive restarts
      - alert: ExcessiveRestarts
        expr: increase(pipeline_restarts_total[10m]) > 3
        for: 0m
        labels:
          severity: critical
        annotations:
          summary: "Pipeline {{ $labels.id }} restarted {{ $value }} times in 10m"

      # PTS calibration failed
      - alert: PTSCalibrationFailing
        expr: pts_calibration_status == 0 and pts_calibration_attempts_total > 10
        for: 5m
        labels:
          severity: warning
        annotations:
          summary: "PTS calibration has not succeeded after {{ $value }} attempts"
```

## InfluxDB / Telegraf Setup

Telegraf's `prometheus` input plugin scrapes the same endpoint natively.

### telegraf.conf

```toml
[[inputs.prometheus]]
  urls = ["http://localhost:8080/api/v1/metrics"]
  # If API_KEY is set:
  # [inputs.prometheus.headers]
  #   X-API-Key = "your-api-key"

[[outputs.influxdb_v2]]
  urls = ["http://localhost:8086"]
  token = "your-influx-token"
  organization = "your-org"
  bucket = "hls-scte35"
```

All metrics flow into InfluxDB with the same names and labels. Use Grafana's InfluxDB data source to build the same dashboards described above, using Flux queries:

```flux
from(bucket: "hls-scte35")
  |> range(start: -1h)
  |> filter(fn: (r) => r._measurement == "scte35_events_detected_total")
  |> derivative(unit: 1m, nonNegative: true)
```

## Healthcheck for Load Balancers

The `/api/v1/health` endpoint returns `200 OK` with `{"status": "ok"}` and does not require authentication. Use it for load balancer health checks:

```
GET /api/v1/health
```

For a deeper health check that verifies at least one pipeline is running:
```bash
curl -sf http://localhost:8080/api/v1/pipelines | jq '.pipelines | length > 0'
```
