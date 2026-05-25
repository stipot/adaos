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
- `items[].roots[].compatibility`: migration rule for the Yjs branch,
  including `classification`, `read_policy`, `write_policy`, and
  `migration_action`
- `items[].shim_findings`: skill-local projection shims that should move into
  the shared SDK, including direct `ctx_subnet.set*` writes, local fingerprint
  caches, local executor bridges, and local `data_projections` loaders
- `items[].sdk_runtime_present`: whether the skill already imports or creates
  the shared projection/stream runtime

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
| `local_shim_pressure_score` | lower is better | `sum(local_shim * severity_weight)` | Weighted backlog of skill-local projection shims that should move into the shared SDK |

Additional counters:

- `legacy_compatible_root_total`: Yjs roots that are still accepted as
  transitional legacy branches.
- `projection_record_cache_root_total`: roots that point at the canonical
  `data/projectionRecords` cache rather than a skill-owned JSON branch.

### Migration Recommendations

Use:

```text
GET /api/node/projection-migration/recommendations
```

The response turns inventory and metrics into an ordered migration backlog.
Important fields:

- `items[].priority_score`: combined priority from risk, monolithic roots,
  missing bridge coverage, and local shim pressure
- `items[].recommended_next_step`: the next concrete migration step for the
  skill
- `items[].actions`: monolith and shim actions, including the affected roots
  and SDK replacement hints

### Manifest Target Contract

For unit-level checks use `inspect_projection_manifest_entries(entries)` from
`adaos.services.scenario.projection_registry`. The same counters are also
surfaced through `GET /api/node/projection-migration/monolith-inventory` and
`GET /api/node/projection-migration/metrics`.

Important fields:

- `schema`: expected to be `adaos.data-projections.v1`
- `yjs_target_with_projection_key_total`: Yjs targets that already declare a
  canonical `projection_key`
- `legacy_monolithic_target_total`: direct `data/<skill>` targets that still
  need a narrower projection path
- `reserved_cache_target_total`: direct manifest targets to
  `data/projectionRecords`; this must stay `0` because that cache is
  core-owned
- `findings[]`: warnings and errors for unsupported backends, missing
  scope/slot, invalid Yjs paths, legacy monolithic roots, and reserved cache
  violations

Swagger fields:

- `items[].manifest_contract`: per-skill manifest contract report
- `items[].projection_keyed_yjs_target_total`: per-skill keyed target count
- `metrics.manifest_projection_key_coverage_ratio`: aggregate keyed-target
  coverage
- `metrics.reserved_cache_manifest_target_total`: aggregate reserved cache
  violations

### Acceptance Summary

Use:

```text
GET /api/node/projection-migration/acceptance-summary
```

The response aggregates the migration evidence into diploma-oriented checks.
Important fields:

- `server_mvp_ready`: `true` when no blocking `fail` checks are present
- `status`: `ready`, `ready_with_followups`, or `blocked`
- `interpretation.meaning`: plain-language explanation of the status
- `interpretation.next_action`: what to do with the report result
- `manual_review.inspect_first`: fields to inspect first during a manual
  Swagger check
- `manual_steps[]`: ordered Swagger/API checks for validating the server-side
  migration MVP
- `swagger_verification`: single-endpoint Swagger checklist for the compact
  acceptance response
- `request_examples`: terminal request examples for repeating the same checks
  with curl after `adaos api serve` is running
- `traceability_matrix`: mapping from plan items to API fields, diploma usage,
  and verification steps
- `evidence_rows[]`: compact metric table with value, direction, meaning, and
  diploma usage notes
- `measurement_model`: before/after measurement rows with current values,
  baseline policy, comparison rules, and formulas
- `control_snapshot`: compact evidence block for saving one control run in a
  diploma before/after table
- `plan_review`: current alignment with slices 1-6 of
  `operational-event-model-reference-plan.md`
- `completion_gates`: checklist derived from the plan completion definition,
  with pass/warn/fail status for each gate
- `risk_register`: risk/limitation register generated from warning gates and
  remaining acceptance checks
- `final_acceptance`: final accept/blocked decision for the server-side MVP,
  including evidence fields, explicit scope, and out-of-scope follow-up work
- `demo_script`: short explanation block for presenting the acceptance result
- `defense_summary`: short defense-oriented summary with thesis, proof points,
  metrics to quote, limitations, and closing statement
- `progress`: compact progress summary for the server MVP and the larger
  end-to-end plan
- `checks[].status`: per-check `pass`, `warn`, or `fail`
- `checks[].evidence`: metric-backed proof for the check
- `checks[].followup`: explicit remaining work when a warning or failure is
  not part of the server-side MVP

This endpoint is intentionally not a claim that the full AdaOS client and all
skills are migrated. It is a compact acceptance report for the server-side
operational event model MVP.

For a normal diploma MVP demo, the expected result is:

- `server_mvp_ready=true`
- `fail_total=0`
- `status=ready` or `status=ready_with_followups`

`ready_with_followups` is acceptable when the warning checks explicitly name
remaining client hookup, skill migration, or legacy cleanup work.

The `manual_steps` array should be followed in order:

1. `acceptance-summary`: confirm readiness and read the interpretation.
2. `metrics`: inspect aggregate migration ratios.
3. `monolith-inventory`: inspect per-skill roots and compatibility metadata.
4. `recommendations`: inspect prioritized follow-up work.

The `swagger_verification` block is the fastest manual check when only one
endpoint is opened. It names the required header, the expected success markers,
the fields to inspect, and the action to take if `fail_total` is greater than
zero.

The `request_examples` block contains repeatable terminal checks for
`acceptance-summary`, `metrics`, and `recommendations`. Use it when Swagger UI
is not convenient or when a control run needs to be repeated from a script.

The `traceability_matrix` block links the plan, API evidence, and diploma text.
It is useful during review because each row answers: which plan item is covered,
which response fields prove it, and how the result should be verified.

The `evidence_rows` array is the shortest table to cite in a report:

- `monolith_exposure_ratio`: remaining dependence on monolithic Yjs roots.
- `migration_readiness_ratio`: share of surfaces already covered by the new
  model.
- `manifest_projection_key_coverage_ratio`: manifest alignment with
  canonical projection keys.
- `legacy_pressure_score`: remaining weighted migration backlog.
- `reserved_cache_manifest_target_total`: must remain zero to prove the
  core-owned cache is protected.

The `measurement_model` block is the repeatable measurement method for chapter
3. Use `rows[].current_value` as the current control run, store a baseline from
the original branch or first saved snapshot, and compare the values with
`rows[].comparison_rule`. Higher-is-better metrics use
`current_value > baseline_value`; lower-is-better metrics use
`current_value < baseline_value`.

The `control_snapshot` block is the shortest payload to save after a control
run. It repeats the readiness result, server/full-plan progress, and key
metrics in one place so the evidence can be copied into a before/after table
without manually merging `metrics`, `progress`, and `checks`.

The `plan_review` block maps the same acceptance result back to the six slices
of the reference plan. It is the quickest way to explain which slices are
server-ready, which are pilot-ready, and which still have client or cleanup
follow-up work.

The `completion_gates` block converts the plan's Completion Definition into a
checklist. `pass` gates are already covered by the server-side MVP, while
`warn` gates name follow-up work such as direct browser hookup, named-entity
consumer invalidation, and full event producer/client test migration.

The `risk_register` block turns those warnings into demo-ready risk statements.
Each row names a risk, impact, mitigation, and verification step. It is useful
for the diploma limitations section because it keeps the MVP claim honest while
still showing that remaining work is controlled.

The `final_acceptance` block is the top-level decision to cite when closing the
server-side MVP. It says whether the implementation is accepted for the diploma
and Swagger/API demo, which evidence fields support that decision, and which
full-plan items remain outside the accepted scope: browser client migration,
full Infrascope projection-family split, node-aware top-level envelope, and
complete legacy cleanup.

The `demo_script` block is the shortest narrative to say during a manual demo:
it includes the expected result, current result, conclusion, and explicit
limitations.

The `defense_summary` block is the shortest defense-oriented explanation. It
connects the implemented MVP, the metrics worth quoting, and the remaining
limitations in one compact response section.

The `progress` block separates two numbers:

- `server_mvp_percent`: weighted progress for the implemented server-side
  acceptance checks.
- `full_plan_estimate_percent`: conservative estimate for the wider AdaOS
  roadmap, including client adapter, full Infrascope split, node top-level Yjs
  envelope, and legacy cleanup.
- `completed_groups[]`: already implemented server-side migration groups.
- `remaining_groups[]`: explicit groups that keep the full-plan estimate below
  the server MVP number.
- `remaining_group_details[]`: the same remaining groups with a reason and a
  verification hint for the next control run.
- `followup_roadmap[]`: ordered milestones for closing the remaining groups,
  each with a goal and an exit check.
- `headline_metrics`: report-friendly ratios such as
  `migration_readiness_ratio`, `legacy_pressure_score`, and
  `manifest_projection_key_coverage_ratio`.

For diploma reporting, use `server_mvp_percent` to describe the current
implemented backend scope and `full_plan_estimate_percent` to explain why the
whole AdaOS migration is not presented as finished yet.

The `followup_roadmap` array is intentionally ordered. It starts with the
browser read path because that is the first user-visible confirmation that the
new projection cache is consumed by the UI. It then moves to the larger
Infrascope split, node-aware cache envelope, and final cross-skill cleanup.

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
- `local_shim_pressure_score`
- `legacy_compatible_root_total`
- `projection_record_cache_root_total`
- `yjs_target_with_projection_key_total` in manifest inspection
- `reserved_cache_target_total` in manifest inspection
- `server_mvp_ready` and `checks[].status` from the acceptance summary
- `top_monolithic_candidates`
- `items[].recommended_next_step` from the recommendations endpoint

Expected direction:

- `monolith_exposure_ratio` decreases as monolithic roots are split or hidden
  behind shared bridges.
- `migration_readiness_ratio` increases as skills move to sectioned slots,
  streams, status cards, and shared dispatcher paths.
- `legacy_pressure_score` decreases when high-risk monolithic publishers are
  migrated first.
- `local_shim_pressure_score` decreases when direct skill-owned writes,
  fingerprint maps, and executor bridges are replaced by shared SDK runtime
  helpers.

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
5. Execute `GET /api/node/projection-migration/recommendations` to choose the
   next skill migration target.
6. Execute `GET /api/node/projection-migration/monolith-inventory` when a
   detailed per-skill explanation is needed.
7. Execute `GET /api/node/projection-migration/acceptance-summary` when a
   compact diploma/demo readiness report is needed.

These checks do not require the production web UI. They are API-level control
examples for the migration and can be repeated after each skill migration.
