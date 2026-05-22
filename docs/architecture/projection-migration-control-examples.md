# Projection Migration Control Examples

This note defines repeatable checks for the operational event model migration.
The goal is to compare the legacy monolithic Yjs state against the newer
projection/subscription model with stable metrics.

## Control Endpoints

### Monolith Inventory

Use:

```text
GET /api/node/projection-migration/monolith-inventory
```

The response is a per-skill inventory of browser-facing Yjs roots, stream
receivers, and shared bridges. Important fields:

- `monolithic_candidate_total`: skills that still expose a direct monolithic
  Yjs root such as `data/<skill>`
- `risk_counts`: high, medium, and low migration risk buckets
- `items[].roots[].shape`: `monolithic-yjs-root`, `sectioned-yjs-root`, or
  `single-yjs-slot`

### Migration Metrics

Use:

```text
GET /api/node/projection-migration/metrics
```

The response aggregates the inventory into diploma-friendly control metrics:

| Metric | Direction | Formula | Meaning |
| --- | --- | --- | --- |
| `monolith_exposure_ratio` | lower is better | `monolithic_root_total / observed_surface_total` | Share of observed projection surfaces that still depend on direct monolithic Yjs roots |
| `migration_readiness_ratio` | higher is better | `modern_surface_total / observed_surface_total` | Share already represented by sectioned/single-slot Yjs, stream receivers, or shared bridges |
| `legacy_pressure_score` | lower is better | `sum(monolithic_roots * risk_weight)` | Weighted backlog of monolithic publishers |

Risk weights:

| Risk | Weight |
| --- | --- |
| `high` | 3 |
| `medium` | 2 |
| `low` | 1 |

## Before/After Method

For a strict comparison, run the metrics endpoint twice:

1. On the original branch before the migration increment.
2. On the current `rev2026` branch after the migration increment.

Record:

- `monolith_exposure_ratio`
- `migration_readiness_ratio`
- `legacy_pressure_score`
- `top_monolithic_candidates`

Expected direction:

- `monolith_exposure_ratio` decreases as monolithic roots are split or hidden
  behind shared bridges.
- `migration_readiness_ratio` increases as skills move to sectioned slots,
  streams, status cards, and shared dispatcher paths.
- `legacy_pressure_score` decreases when high-risk monolithic publishers are
  migrated first.

## Runtime Write Suppression

Use `ProjectionRuntime.diagnostics_snapshot()` for SDK-level checks:

- `applied_total`: writes that actually changed a projection
- `skipped_unchanged_total`: recomputations suppressed because the payload was
  unchanged
- `dirty_dropped_total`: events that did not map to any dirty section
- `refresh_coalesced_total`: duplicate concurrent refreshes joined into one
  operation
- `refresh_superseded_total`: overlapping refresh pressure that should be
  inspected during migration

The useful ratio for a control run is:

```text
write_suppression_ratio = skipped_unchanged_total / (applied_total + skipped_unchanged_total)
```

Higher values mean the SDK avoids more redundant Yjs writes under repeated or
unchanged refreshes.

## Manual Swagger Checks

1. Open `http://127.0.0.1:8777/docs`.
2. Authorize requests with `x-adaos-token: dev-local-token`.
3. Execute `GET /api/node/projection-migration/metrics`.
4. Save the `metrics` block as the current control snapshot.
5. Execute `GET /api/node/projection-migration/monolith-inventory` when a
   detailed per-skill explanation is needed.

These checks do not require the production web UI. They are API-level control
examples for the migration and can be repeated after each skill migration.
