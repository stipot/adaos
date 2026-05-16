# Status-Card SDK Migration Notes

This note defines the first skill-facing migration path for publishing small
operational status cards through the shared event/projection model.

## When to use status cards

Use `adaos.sdk.status` when a skill or platform surface needs to publish a
small operator-facing state summary:

- runtime or skill health
- route/realtime connectivity
- active operation state
- warning/degraded/failed summaries
- a compact pointer to heavier diagnostics

Do not embed large diagnostic snapshots in status cards. Put heavy or warm/cold
details behind a stream receiver, tool, API endpoint, or inspector projection
and reference that target through `details_ref`.

## Basic publishing pattern

```python
from adaos.sdk import status

status.publish_status(
    id="runtime",
    kind="runtime",
    webspace_id="desktop",
    status="running",
    summary="Runtime ready",
    ttl_ms=15000,
)
```

When this runs inside a skill context, the helper sets `owner` to
`skill:<current-skill-name>`. Platform code can pass an explicit owner such as
`core:runtime`.

## Batch publishing

Use `publish_status_many(...)` for a small group of related cards that share
the same webspace, owner, and kind.

```python
status.publish_status_many(
    [
        {"id": "operations", "status": "running", "summary": "Operations active"},
        {"id": "realtime", "status": "partial", "summary": "Realtime degraded"},
    ],
    webspace_id="desktop",
    kind="infrastate",
)
```

The helper is intended for compact batches, not for large snapshot publishing.

## Stream-backed details

Use `publish_status_stream(...)` when the card should point to existing stream
or tool details.

```python
status.publish_status_stream(
    id="operations",
    kind="operations",
    webspace_id="desktop",
    status="running",
    summary="Operations active",
    receiver="infrastate.operations.active",
    path="streams/operations",
    params={"limit": 10},
)
```

The card remains small, while the UI can load details lazily from the referenced
receiver.

## Migration rules

- Keep card ids stable and use lower-case tokens such as `runtime`, `realtime`,
  `operations`, or `core-update`.
- Prefer one card per operator-facing state family.
- Use `ttl_ms` for state that can become stale if the producer stops updating.
- Use `summary` for the compact operator sentence; keep raw debug text in
  details.
- Do not write directly to Yjs from a status publisher.
- Do not create skill-specific status-card shapes; use the shared
  `StatusCard -> ProjectionRecord` ABI.

## Infrastate first mapping

The first `infrastate` alignment maps the existing compact snapshot into shared
status cards without removing the legacy snapshot response:

| Card id | Source section | Details target |
| --- | --- | --- |
| `infrastate-summary` | `summary` | `/api/node/infrastate/snapshot` |
| `infrastate-operations` | `operations.active_items` | `infrastate.operations.active` |
| `infrastate-realtime` | `realtime` | `infrastate.realtime` |
| `infrastate-yjs` | `reliability.runtime.state_sync` and `yjs_pressure` | `infrastate.yjs.load_mark` |
| `infrastate-core-update` | `core_update` / update summary | `infrastate.core_update_diagnostics` |

This is intentionally a compatibility bridge. The existing UI can keep reading
the old snapshot while new status-card consumers read the materialized registry.

## Verification

After publishing a card, a developer can inspect it through the node API:

```text
GET /api/node/status-cards?webspace_id=desktop
GET /api/node/status-cards/{card_id}/projection?webspace_id=desktop
```

Demanded refresh can be verified by adding browser demand for
`status-card:<card-id>` and dispatching a related platform or domain event
through `/api/node/projection-dispatcher/dispatch`.
