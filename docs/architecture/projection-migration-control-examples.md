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
The `core_skill_contract_readiness` gate should be `pass` when
`/api/node/projection-dispatcher/core-skill-contract` exposes handler coverage,
readiness metrics, and the core/skill/browser ownership split.
The `platform_emitter_contract` gate should be `pass` when
`/api/node/projection-platform-emitters` exposes the runtime, desktop-shell,
notifications, and UI-runtime platform emitters through the shared status-card
contract.

The `risk_register` block turns those warnings into demo-ready risk statements.
Each row names a risk, impact, mitigation, and verification step. It is useful
for the diploma limitations section because it keeps the MVP claim honest while
still showing that remaining work is controlled.

The `final_acceptance` block is the top-level decision to cite when closing the
server-side MVP. It says whether the implementation is accepted for the diploma
and Swagger/API demo, which evidence fields support that decision, and which
full-plan items remain outside the accepted scope: browser client migration,
full Infrascope projection-family split, cross-branch node-aware Yjs envelope
rollout, and complete legacy cleanup.

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
  roadmap, including client adapter, full Infrascope split, cross-branch
  node-aware Yjs envelope rollout, and legacy cleanup.
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
Infrascope split, cross-branch node-aware Yjs envelope rollout, and final
cross-skill cleanup.

After the node-aware projection cache envelope increment, the Swagger check for
`/api/node/projection-records/yjs/cache` should include:

- `envelope_ok=true`
- `envelope.owner=core:projection_records`
- `envelope.write_policy=core-owned-cache-only`
- `envelope.node_scope.mode=record-meta-node-id`
- `node_ids[]` and `node_scoped_record_total`

The same evidence is available in `/api/node/projection-diagnostics` as
`yjs_cache_envelope_ok`, `yjs_cache_envelope`, and `yjs_cache_node_ids` when
`include_yjs_cache=true`.

For the browser read-path increment, use
`/api/node/projection-records/browser-cache` after at least one browser demand
record exists. The expected evidence is:

- `kind=browser-demanded-projection-records`
- `demanded_only=true`
- `read_path=data/projectionRecords.records[projection_key]`
- `record_total` for demanded records already available in the canonical
  ProjectionRecord registry
- `missing_projection_keys[]` for demanded records not materialized yet
- `cache_contract.browser_read=true`
- `cache_contract.browser_write=false`
- `cache_contract.write_policy=core-owned-cache-only`

This does not prove that the Angular UI has already switched read paths. It
proves that the server now exposes the demanded-only ProjectionRecord read
model the browser adapter should consume.

For a browser-session-specific check, call the same endpoint with
`client_id=<browser-client>` and `session_id=<browser-session>`. The expected
additional evidence is:

- `session_scoped=true`
- `client_id` equals the requested browser client
- `session_id` equals the requested browser session
- `projection_keys[]` contains only projections demanded by that session
- `cache_contract.client_session_filter=true`

This is the server-side guardrail that lets the future browser adapter read its
own demanded records without mixing widget demand from another browser session
in the same webspace.

For a projection-scoped browser-cache check, pass one or more
`projection_keys=<projection-key>` query parameters. The expected additional
evidence is:

- `projection_scoped=true`
- `requested_projection_keys[]` contains the requested keys
- `projection_keys[]` contains only demanded keys that also match the request
- `records` does not include other demanded projections from the same session

This is useful for a widget-level adapter that wants to refresh only its own
ProjectionRecord while keeping the wider browser demand set intact.

For browser-cache HTTP cache validation, repeat the same request with the
previous response's `ETag` as `If-None-Match`. The expected evidence is:

- the first response contains `cache.key`, `cache.fingerprint`, `cache.etag`,
  and `cache.if_none_match_supported=true`
- the HTTP response has `Cache-Control: no-cache`
- the HTTP response has the same `ETag` value as `cache.etag`
- the second matching request returns `304 Not Modified`
- the `304` response preserves the same `ETag`

This lets a browser adapter poll or refresh demanded ProjectionRecords without
forcing downstream widget work when the demanded snapshot has not changed.

For browser-cache per-entry validation, inspect the same response's `entries[]`
and aggregate entry maps. The expected evidence is:

- every `entries[]` item has `cache.key`, `cache.fingerprint`, and `cache.etag`
- `entry_cache_keys[]` contains one cache key per demanded projection
- `entry_fingerprints` maps each demanded `projection_key` to the entry-level
  fingerprint
- `entry_etags` maps each demanded `projection_key` to the entry-level weak
  ETag
- missing demanded records carry
  `cache.missing_reason=demanded_projection_record_not_materialized`

This gives the future browser adapter a stable per-widget signal: the whole
snapshot can be validated through HTTP `ETag`, while each demanded
ProjectionRecord can still be compared independently.

For browser-cache lifecycle consumption, inspect `entries[].lifecycle` and the
top-level `lifecycle_summary`. The expected evidence is:

- missing demanded records are reported as `state=pending`
- cached `loading`, `refreshing`, or `pending` records are reported as
  `state=refreshing`
- cached `ready` records are reported as `state=ready`
- cached `stale` records are reported as `state=stale`
- error-like records are reported as `state=error`
- `lifecycle_summary.states` aggregates the demanded set
- `lifecycle_summary.blocked=true` when pending, refreshing, or error records
  remain

This proves the browser read model can consume projection lifecycle state as a
first-class contract instead of treating every ProjectionRecord as only payload
data.

For the core-to-skill refresh contract, call
`/api/node/projection-dispatcher/core-skill-contract`. The expected evidence is:

- `contract=adaos.core-skill-projection-refresh.v1`
- `demands[]` contains active demanded `projection_key` values for the
  requested webspace
- each demand reports `handler.covered`, `handler.key`, and `handler.kind`
- each demand reports `ownership.core_owned`, `ownership.skill_owned`,
  `ownership.browser_owned`, and `ownership.forbidden`
- each demand reports `refresh_contract.core_selects_demand=true`
- covered demands report `refresh_contract.skill_refreshes_payload=true`
- every demand reports
  `refresh_contract.core_materializes_projection_record=true`
- `covered_total` and `uncovered_total` summarize handler coverage before the
  actual dispatch runs
- `uncovered_projection_keys[]` names demanded projections without handlers
- `readiness.coverage_ratio` reports handler coverage for the demanded set
- `readiness.ready_for_dispatch=true` only when every demanded projection is
  covered

This is the Swagger-facing proof that core owns demand selection and canonical
materialization, while skill/platform handlers own payload refresh.
The ownership block must keep direct browser or skill writes to
`data/projectionRecords` in `ownership.forbidden`.

For platform emitter contract validation, call
`/api/node/projection-platform-emitters`. The expected evidence is:

- `contract=adaos.platform-emitters.status-card.v1`
- `ready_for_mvp=true`
- `projection_keys[]` includes `status-card:runtime`,
  `status-card:desktop-shell`, `status-card:notifications`, and
  `status-card:ui-runtime`
- `coverage.runtime_lifecycle=true`
- `coverage.notifications=true`
- `coverage.diagnostics=true`
- every emitter contract keeps `browser_write=false` and
  `skill_direct_write=false`

This proves that platform-owned operational surfaces are defined as shared
ProjectionRecord/status-card emitters instead of another skill-local snapshot
branch.

For event envelope ABI validation, call `/api/node/event-envelope-contract`.
The expected evidence is:

- `contract=adaos.operational-event-envelope.v1`
- `ready_for_mvp=true`
- `meta_path=_meta.event`
- `required_fields[]` equals `type`, `source`, `ts`, and `payload`
- `compatibility.legacy_event_supported=true`
- `compatibility.nested_meta_preferred=true`
- `normalized_example.event_id=evt-demo-1`
- `dispatcher_ready=true`

This proves that the dispatcher-facing event model can accept legacy AdaOS
events while reading trace, scope, authority, and causal metadata from the
shared `_meta.event` envelope.

The same contract is embedded in
`/api/node/projection-migration/acceptance-summary` under `event_envelope` and
also appears as the `event_envelope_contract` completion gate. For the final
MVP report, verify that `event_envelope.dispatcher_ready=true` and
`final_acceptance.evidence_fields[]` includes `event_envelope`.

For browser demand contract validation, call
`/api/node/projection-demand/contract`. The expected evidence is:

- `contract=adaos.client-projection-subscription.v1`
- `ready_for_mvp=true`
- `record_required_fields[]` includes `client_id`, `device_id`,
  `session_id`, `webspace_id`, `role`, `subscriptions`, and `updated_at`
- `subscription_required_fields[]` includes `projection_key`, `consumer_id`,
  and `consumer_kind`
- `write_policy.mode=replace_full_client_session_set`
- `registry.write_endpoint=/api/node/projection-demand/client`
- `sample_projection_keys[]` includes `status-card:runtime`

The same evidence is embedded in
`/api/node/projection-migration/acceptance-summary` under
`browser_demand_contract`, and the `browser_demand_contract` completion gate
must pass for the server-side MVP report.

For surface lifecycle mapping validation, call
`/api/node/projection-demand/surface-lifecycle-contract`. The expected evidence
is:

- `contract=adaos.browser-surface-lifecycle-subscriptions.v1`
- `ready_for_mvp=true`
- `input_groups[]` equals `page`, `widgets`, `modals`, and `pinnedPanels`
- `server_endpoint=/api/node/projection-demand/browser-state`
- `output_contract=adaos.client-projection-subscription.v1`
- `sample_subscription_total=5`
- `sample_consumer_kinds[]` includes `page`, `widget`, `modal`, and
  `pinned-panel`

The same evidence is embedded in
`/api/node/projection-migration/acceptance-summary` under
`surface_lifecycle_contract`, and the `surface_lifecycle_contract` completion
gate must pass for the server-side MVP report.

For runtime ownership split validation, call
`/api/node/projection-runtime-ownership`. The expected evidence is:

- `contract=adaos.projection-runtime-ownership.v1`
- `ready_for_mvp=true`
- `boundary_total=5`
- `boundaries[]` includes `event_envelope`, `browser_demand`,
  `refresh_dispatch`, `platform_emitters`, and `yjs_projection_cache`
- `browser_demand.forbidden[]` includes browser writes to
  `data/projectionRecords`
- `refresh_dispatch.skill_owned[]` includes payload refresh for owned
  projection keys

The same evidence is embedded in
`/api/node/projection-migration/acceptance-summary` under
`runtime_ownership_contract`, and the `runtime_ownership_contract` completion
gate must pass for the server-side MVP report.

For node multiplicity validation, call
`/api/node/projection-records/node-multiplicity-contract`. The expected
evidence is:

- `contract=adaos.projection-records.node-multiplicity.v1`
- `ready_for_mvp=true`
- `node_scope_mode=record-meta-node-id`
- `sample_node_ids[]` includes `node-a` and `node-b`
- `browser_rules.do_not_assume_single_anonymous_node=true`
- `browser_rules.browser_writes_projection_cache=false`

The same evidence is embedded in
`/api/node/projection-migration/acceptance-summary` under
`node_multiplicity_contract`, and the `node_multiplicity_contract` completion
gate must pass for the server-side MVP report.

For dispatcher memory-vs-Yjs validation, call
`/api/node/projection-dispatcher/memory-contract`. The expected evidence is:

- `contract=adaos.projection-dispatcher.memory-vs-yjs.v1`
- `ready_for_mvp=true`
- `memory_allowed[]` includes rich semantic source snapshots
- `yjs_publication.path=data/projectionRecords`
- `dispatcher_boundaries.core_materializes_record=true`
- `dispatcher_boundaries.handler_writes_yjs_directly=false`
- `dispatcher_boundaries.browser_writes_yjs_cache=false`

The same evidence is embedded in
`/api/node/projection-migration/acceptance-summary` under
`dispatcher_memory_contract`, and the `dispatcher_memory_contract` completion
gate must pass for the server-side MVP report.

For active demand restore validation, call
`/api/node/projection-demand/restore-contract`. The expected evidence is:

- `contract=adaos.projection-demand.restore-from-yjs.v1`
- `ready_for_mvp=true`
- `runtime_helpers.projection_runtime=ProjectionRuntime.restore_active_demand`
- `runtime_helpers.stream_runtime=StreamRuntime.restore_active_demand`
- `restore_modes[]` includes `active_projection_demand` and
  `active_receivers`
- `skip_reasons[]` includes hidden/stale/filter and missing slot/receiver
  cases
- `boundaries.restore_writes_yjs_directly=false`

The same evidence is embedded in
`/api/node/projection-migration/acceptance-summary` under
`demand_restore_contract`, and the `demand_restore_contract` completion gate
must pass for the server-side MVP report.

For browser adapter validation, call
`/api/node/projection-records/browser-adapter-contract`. The expected evidence
is:

- `contract=adaos.projection-records.browser-adapter.v1`
- `ready_for_mvp=true`
- `source_of_truth.canonical_yjs_path=data/projectionRecords`
- `source_of_truth.api_read_path=/api/node/projection-records/browser-cache`
- `adapter_rules.cache_by_projection_key=true`
- `adapter_rules.reuse_cached_views=true`
- `adapter_rules.avoid_observe_deep_data=true`
- `cache_model.if_none_match=supported`

The same evidence is embedded in
`/api/node/projection-migration/acceptance-summary` under
`browser_adapter_contract`, and the `browser_adapter_contract` completion gate
must pass for the server-side MVP report.

For Infrascope projection family validation, call
`/api/node/status-cards/infrascope/projection-family-contract`. The expected
evidence is:

- `contract=adaos.infrascope.projection-families.v1`
- `ready_for_mvp=true`
- `family_total=9`
- `projection_keys[]` includes `status-card:infrascope-overview` and
  `status-card:infrascope-topology`
- `boundaries.uses_shared_status_card_abi=true`
- `boundaries.introduces_infrascope_specific_abi=false`
- `boundaries.pre_materialize_all_inspector_details=false`

The same evidence is embedded in
`/api/node/projection-migration/acceptance-summary` under
`infrascope_projection_family_contract`, and the
`infrascope_projection_family_contract` completion gate must pass for the
server-side MVP report.

For Infrascope demanded-only validation, call
`/api/node/status-cards/infrascope/demanded-only-contract`. The expected
evidence is:

- `contract=adaos.infrascope.demanded-only-refresh.v1`
- `ready_for_mvp=true`
- `selection_rules.projection_key_family=status-card:infrascope-*`
- `selection_rules.demanded_only_flag=demanded_only=true`
- `selection_rules.webspace_scoped=true`
- `boundaries.publishes_only_requested_cards=true`
- `boundaries.cross_webspace_churn=false`
- `boundaries.full_infrascope_refresh_required=false`

The same evidence is embedded in
`/api/node/projection-migration/acceptance-summary` under
`infrascope_demanded_only_contract`, and the
`infrascope_demanded_only_contract` completion gate must pass for the
server-side MVP report.

For Infrascope platform error validation, call
`/api/node/status-cards/infrascope/platform-errors-contract`. The expected
evidence is:

- `contract=adaos.infrascope.platform-errors.v1`
- `ready_for_mvp=true`
- `projection_keys[]` includes
  `status-card:infrascope-materialization-error`
- `separation_rules.not_embedded_in_skill_snapshot=true`
- `separation_rules.not_hidden_inside_data_infrascope=true`
- `boundaries.skill_payload_remains_domain_snapshot=true`
- `boundaries.direct_yjs_write=false`

The same evidence is embedded in
`/api/node/projection-migration/acceptance-summary` under
`infrascope_platform_errors_contract`, and the
`infrascope_platform_errors_contract` completion gate must pass for the
server-side MVP report.

The same evidence is embedded in
`/api/node/projection-migration/acceptance-summary` under `platform_emitters`.
For the final MVP report, verify that:

- `platform_emitters.contract=adaos.platform-emitters.status-card.v1`
- `platform_emitters.ready_for_mvp=true`
- `platform_emitters.projection_keys[]` includes
  `status-card:notifications`
- `platform_emitters.surface_readiness.web_desktop.status=ready`
- `platform_emitters.surface_readiness.related_modals.status=contract_ready`
- `platform_emitters.pilot_order[]` ends with `heavy_skill_pilot`
- `final_acceptance.evidence_fields[]` includes `platform_emitters`

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
