# Dataset Schema

This document defines the first local dataset contract for
`ai_event_analysis_skill`. It is intentionally skill-local and does not change
AdaOS core contracts.

## Scope

Each event window carries explicit scope:

```json
{
  "node_id": "hub-1",
  "subnet_id": "local",
  "webspace_id": "desktop"
}
```

Scope fields let the same model distinguish local runtime churn from member
node instability or cross-subnet behavior.

## EventEvidenceRecord

Evidence records are normalized facts derived from logs, event exports, or
diagnostic snapshots.

```json
{
  "id": "runtime.log:42",
  "ts": 1779991200.0,
  "ts_iso": "2026-05-29T09:00:00Z",
  "source": "local_log",
  "source_path": ".adaos/state/runtime.log",
  "topic": "projection.lifecycle",
  "severity": "warning",
  "message": "projection refresh repeated for status-card:runtime"
}
```

Required fields:

- `id`
- `ts`
- `source`
- `topic`
- `severity`
- `message`

The `message` field must be redacted before export.

## EventWindowRecord

An event window is the primary dataset unit.

```json
{
  "window_id": "hub-1:1779991200-1779991260",
  "scope": {
    "node_id": "hub-1",
    "subnet_id": "local",
    "webspace_id": "desktop"
  },
  "time": {
    "start": "2026-05-29T09:00:00Z",
    "end": "2026-05-29T09:01:00Z",
    "window_seconds": 60
  },
  "features": {
    "event_total": 128,
    "error_total": 8,
    "drop_total": 3,
    "supersede_total": 12,
    "projection_refresh_total": 42,
    "same_projection_refresh_max": 31,
    "yjs_write_total": 12,
    "browser_reconnect_total": 1,
    "member_disconnect_total": 0,
    "runtime_rebuild_total": 0
  },
  "evidence": [],
  "label": {
    "incident": false,
    "incident_type": "normal",
    "severity": "unlabeled",
    "reasons": [],
    "source": "unlabeled_import"
  },
  "baseline_prediction": {
    "incident": true,
    "incident_type": "projection_refresh_storm",
    "severity": "warning",
    "confidence": 0.95,
    "reasons": ["same_projection_refresh_max", "projection_refresh_total"]
  }
}
```

## Labels

Initial classes:

- `normal`
- `eventbus_backpressure`
- `projection_refresh_storm`
- `yjs_write_pressure`
- `browser_session_instability`
- `member_node_disconnect`
- `runtime_rebuild_churn`

Severity values:

- `info`
- `warning`
- `critical`
- `unlabeled`

## Privacy Rules

The dataset must not contain:

- bearer tokens;
- API keys;
- passwords;
- full local file paths;
- full authorization headers;
- large raw log bodies;
- user-private text unless explicitly approved for research.

The first importer redacts obvious tokens and paths. Manual review is still
required before a dataset is shared outside the local workspace.

## Export Format

Datasets are exported as JSONL:

```text
one EventWindowRecord per line
UTF-8
schema implied by this document
```

Default export path:

```text
.adaos/workspace/skills/ai_event_analysis_skill/data/event_windows.jsonl
```
