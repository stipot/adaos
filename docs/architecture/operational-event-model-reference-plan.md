# Operational Event Model Reference Plan

Snapshot date: 2026-05-15.

This document is the reference execution plan for completing the AdaOS
operational event model correctly.

It is not a third roadmap.  It is the checklist of coverage gates that must be
true when work from the master roadmap is implemented.  Use it to answer:

- are we still following one event model rather than a local workaround?
- have we covered events, projections, browser demand, platform emitters, and
  heavy-skill migration together?
- is the current implementation compatible with existing producers and
  consumers?
- can a reviewer verify that a slice is complete without rereading every
  subordinate roadmap?

Authoritative ordering remains in
[Operational Event Model Roadmap](operational-event-model-roadmap.md).
Detailed projection work remains in
[Projection Subscription Roadmap](projection-subscription-roadmap.md).

## Coverage Target

The event model is covered when AdaOS has one shared path for:

1. runtime facts entering the system as typed events
2. core, skill, scenario, and platform services reacting without rewriting
   broad Yjs snapshots
3. browsers declaring which projections they demand
4. dispatchers refreshing only demanded projections per webspace and node scope
5. platform diagnostics, notifications, status, and failures publishing through
   the same projection/runtime contract as skills
6. named entities and human-facing labels changing without retraining or
   duplicating fallback rules
7. heavy skills such as Infrascope migrating onto the shared contract instead
   of defining their own projection ABI

## Non-Negotiable Invariants

These rules should block implementation review if broken.

- Yjs is a collaborative projection cache, not the canonical event log.
- Domain events, projection demand, projection lifecycle, UI intent, named
  entity lifecycle, and platform operational events stay conceptually separate.
- Projection scope is per webspace; one webspace must not force unrelated
  webspaces to receive Yjs churn.
- Node scope must remain expressible in shared state and event metadata.
- Browser demand is explicit and browser-written; it is not inferred from
  domain events alone.
- A projection has lifecycle state independent from its payload.
- Platform emitters are first-class producers, not hidden inside whichever skill
  happens to own the current UI.
- Human labels, aliases, localized names, and fallback names are not routing
  keys.
- Heavy-skill migrations may use the shared contract, but may not create a
  parallel contract.

## Reference Implementation Order

### Slice 1. Shared ABI Foundation

Goal:

Make runtime events, named entities, platform status, and projection records
share one compatible contract surface before client subscription runtime or
Infrascope migration.

Required artifacts:

- shared event envelope fields and compatibility rules
- helper functions for legacy `Event(type, payload, source, ts)` producers
- documented metadata mapping from current payload `_meta` conventions
- status-card ABI aligned with platform emitters and projection lifecycle
- projection record shape
- browser subscription record shape
- regression tests for event envelope helpers and eventbus pressure guardrails

Exit criteria:

- old event producers still work
- new producers can attach event id, trace/cause, scope, actor/source authority,
  priority, schema/version, and timestamps consistently
- status cards and named-entity registry can be used as reference projection
  examples
- no client, skill, or Infrascope-specific projection shape is introduced ahead
  of the shared ABI

### Slice 2. Browser Demand Runtime

Goal:

Make browser projection demand explicit and stable across page, widget, modal,
and pinned-panel consumers.

Progress:

- server-side demand registry, full-overwrite API, browser-state mapper, and
  stale-session marking are implemented
- `/api/node/projection-records/browser-cache` exposes a demanded-only
  browser-facing ProjectionRecord snapshot with missing-record evidence and
  explicit cache read/write policy; optional `client_id` and `session_id`
  filters scope the response to one browser session, and repeated
  `projection_keys` query parameters scope the response to requested demanded
  projections
- browser-cache responses expose `cache.key`, `fingerprint`, and `etag`, while
  matching `If-None-Match` requests return `304 Not Modified`
- each browser-cache `entries[]` item now carries its own `cache.key`,
  `fingerprint`, `etag`, record metadata, and missing-record reason; aggregate
  `entry_cache_keys`, `entry_fingerprints`, and `entry_etags` let adapters
  compare individual demanded ProjectionRecords
- `/api/node/projection-demand/client/{client_id}/{session_id}/touch` can
  refresh an existing browser session timestamp without replacing its current
  subscriptions, so pinned demand survives heartbeat traffic
- direct browser client adapter hookup remains because the Angular client
  submodule is not present in this checkout

Required artifacts:

- browser-side subscription registry
- full-overwrite client subscription writes
- modal open/close, widget mount/unmount, page view, and pinned panel mapping
  into subscription records
- soft session sanitation rules that do not act as projection activity TTL
- client tests for multiple simultaneous consumers

Exit criteria:

- a browser writes its full active subscription set
- two consumers in one webspace can demand different projections at the same
  time
- stale client/session cleanup does not silently delete active pinned demand
- existing compatibility projections continue to render during migration

### Slice 3. Shared Dispatcher

Goal:

Create the reusable runtime path for event-driven semantic updates and demanded
projection refresh.

Progress:

- base dispatcher contract, per-webspace demand selection, no-cross-webspace
  tests, lifecycle state tracking, pressure counters, and inspection API are
  implemented
- projection-family wildcard handlers are implemented; `status-card:*` is the
  first platform handler family
- canonical `ProjectionRecord` results can now be materialized in a shared
  in-memory registry and inspected through `/api/node/projection-records`
- `/api/node/projection-diagnostics` reports whether each demanded projection
  has a materialized shared `ProjectionRecord`, including status, version,
  fingerprint, lifecycle reason, and registry totals
- `/api/node/projection-diagnostics?materialize_projection_records=true`
  can run a demanded-only status-card materialization pass before reporting
  shared `ProjectionRecord` correlation
- `/api/node/projection-records/yjs/materialize` writes the shared
  `ProjectionRecord` registry into `data/projectionRecords`, with optional
  `demanded_only=true` filtering for browser-facing cache updates
- `/api/node/projection-records/yjs/cache` reads the same Yjs cache back and
  reports schema/fingerprint health for Swagger and operator acceptance checks
- `data/projectionRecords` now carries a top-level `envelope` block with
  schema, core owner, write policy, cache role, Yjs path, webspace, node-scope
  summary, and browser/skill read/write boundaries
- `/api/node/projection-diagnostics?include_yjs_cache=true` correlates demanded
  projections with the `data/projectionRecords` cache; `materialize_yjs_cache=true`
  can refresh the demanded cache before reporting
- direct browser client adapter hookup remains, but the server-side Yjs
  materialization boundary is now explicit and test-covered

Required artifacts:

- dispatcher contract for `event -> in-memory update -> demanded refresh`
- per-webspace refresh selection
- no-cross-webspace-churn tests
- lifecycle publication for pending, refreshing, ready, stale, and error
- pressure observability for coalesced, superseded, skipped, and dropped work

Exit criteria:

- a domain or platform event refreshes only the projections demanded in affected
  webspaces
- dispatcher coalescing preserves evidence of incoming pressure
- services can keep richer memory state than they publish to Yjs
- existing eventbus guardrails remain visible through incident artifacts

### Slice 4. Platform Emitters Pilot

Goal:

Prove the shared model with platform-owned state before moving a heavy skill.

Preferred first families:

- status cards
- notifications
- UI/runtime diagnostics
- workspace-manager or browser-shell operational surfaces

Required artifacts:

- platform-owned projection family definitions
- thin summary/read endpoint behavior where relevant
- push or delta consumption path where relevant
- operator-visible stale/error semantics
- tests for versioning, fingerprinting, dedupe, TTL/staleness, and access
  metadata

Current status:

- materialized status-card registry is implemented in the node runtime
- `/api/node/status-cards` publishes and reads status-card projection records
- `/api/node/projection-records/status-cards/materialize` can explicitly copy
  status-card projection records into the shared materialized
  `ProjectionRecord` registry, with optional `card_ids` filtering and
  `demanded_only=true` selection from browser demand
- runtime lifecycle is now emitted as the built-in `status-card:runtime`
  platform card
- `/api/node/ui/diagnostics` now emits browser-side UI runtime diagnostics as
  the built-in `status-card:ui-runtime` platform card while preserving
  skill-scoped logs for details
- `WebToastService` now emits transient toast notifications as the built-in
  `status-card:notifications` platform card while preserving the existing Yjs
  toast list for browser rendering
- browser-shell desktop snapshots now emit the built-in
  `status-card:desktop-shell` platform card through
  `/api/node/status-cards/desktop/refresh`, optional
  `/api/node/status-cards?include_desktop=true`, and desktop state reads
- demanded dispatcher refreshes `status-card:*` subscriptions through the shared
  projection ABI
- missing cards surface as `unavailable`; expired cards surface as `stale`
- push/delta consumption and Yjs record writes remain outside this first pilot

Exit criteria:

- platform status or diagnostics can update without a full skill snapshot
- unchanged status does not produce noisy Yjs writes or large repeated polling
- platform errors are not hidden inside skill-owned payloads
- the pilot exercises the same projection record and lifecycle fields planned
  for skills

### Slice 5. Heavy Skill Pilot

Goal:

Migrate Infrascope only after the shared model has already been exercised by
platform emitters.

Progress:

- first tracked status-card adapter covers overview, incidents, inventory,
  operations, browser runtime, runtime objects, registry, object-inspector,
  and topology sections
- `/api/node/status-cards/infrascope/refresh` publishes these cards through the
  node API from either an explicit snapshot payload or the existing
  `data/infrascope` projection
- `/api/node/status-cards?include_infrascope=true` can refresh the same cards
  from `data/infrascope` while returning the current materialized registry
- `/api/node/reliability/summary?mode=thin&include_infrascope=true` can pull
  the same Infrascope cards into the lightweight polling summary
- `/api/node/status-cards/infrascope/refresh` supports explicit `card_ids`
  and `demanded_only=true` refreshes so early Infrascope checks do not have to
  republish the full card family
- the node projection dispatcher registers a more specific
  `status-card:infrascope-*` handler that refreshes a demanded Infrascope card
  from `data/infrascope` before returning its `ProjectionRecord`
- regression coverage proves that an Infrascope dispatcher refresh scoped to
  one webspace does not read or materialize cards for another demanded
  webspace
- `/api/node/projection-diagnostics?include_infrascope=true` can refresh
  demanded Infrascope cards before reporting handler/card correlation for
  operator acceptance checks
- `/api/node/status-cards/{card_id}/details/refresh` now handles
  tool-backed details for `infrascope-overview` by publishing
  `status-card.details.tool.requested` with the target skill, tool, and
  arguments
- live skill refresh hookup and browser demand wiring remain before this slice
  can replace the monolithic active view

Required artifacts:

- projection-family inventory for overview, inventory, inspector, topology, and
  modal/widget payloads
- demanded-only refresh per webspace
- lazy inspector/detail loading
- access metadata for shared owner/guest/dev payload behavior
- tests that prove unrelated webspaces do not receive churn

Exit criteria:

- Infrascope no longer relies on one monolithic Yjs snapshot for active views
- platform-originated warnings and materialization errors stay outside
  skill-owned payloads
- the migration uses the shared dispatcher and projection ABI
- no Infrascope-only subscription or lifecycle model exists

### Slice 6. Cross-Skill Rollout

Goal:

Make the shared model reusable instead of a one-off pilot.

Required artifacts:

- inventory of remaining monolithic publishers
- SDK/helper layer for projection records and dispatcher integration
- migration notes for skill authors
- compatibility cleanup plan
- test matrix for multi-webspace, multi-consumer, node-aware, platform emitter,
  and access metadata behavior

Progress:

- `/api/node/projection-migration/monolith-inventory` scans workspace skill
  manifests and WebUI declarations for remaining direct `data/<skill>` Yjs
  roots, single-slot Yjs paths, stream receivers, and existing shared bridges
- the first inventory separates high-risk monolithic publishers such as
  `voice_chat_skill` from transitional skills that already have status-card or
  stream-backed coverage, so follow-up migrations can be prioritized
- the shared SDK projection runtime now preserves dirty-event drops,
  coalesced refreshes, and overlapping refresh pressure in diagnostics, which
  keeps cross-skill migrations observable under bursty event traffic
- projection and stream runtimes can restore active demand from existing
  browser subscription consumers, with filtering for hidden/stale consumers,
  unrelated webspaces, and unregistered slots or receivers
- `/api/node/projection-migration/metrics` aggregates migration inventory into
  control metrics: monolith exposure, migration readiness, and legacy pressure
  score; repeatable examples are captured in
  [Projection Migration Control Examples](projection-migration-control-examples.md)
- the same migration inventory now reports skill-local projection shims such
  as direct `ctx_subnet.set*` writes, local fingerprint maps, local executor
  bridges, and per-skill `data_projections` loaders
- `/api/node/projection-migration/recommendations` converts inventory, risk,
  bridge coverage, and shim findings into a prioritized migration backlog for
  the next skill rollout steps
- canonical `ProjectionRecord` construction now normalizes MVP access metadata
  for `shared`, `owner`, `guest`, and `dev`, and tests prove owner/guest
  display or action metadata can differ without creating separate payload
  branches
- the cleanup test matrix now checks platform emitters by proving runtime,
  desktop shell, notifications, and UI runtime diagnostics all materialize
  through the shared `status-card:*` ProjectionRecord ABI
- shared `data/projectionRecords` cache summaries now expose `node_ids` and
  `node_scoped_record_total` while preserving each record's `meta.node_id`
  through Yjs materialization and readback
- shared `data/projectionRecords` now includes a node-aware top-level
  `envelope`, and `/api/node/projection-diagnostics?include_yjs_cache=true`
  exposes `yjs_cache_envelope_ok`, `yjs_cache_envelope`, and
  `yjs_cache_node_ids` for operator checks
- `/api/node/projection-records/browser-cache` now returns active browser
  demands joined with canonical ProjectionRecords, including
  `missing_projection_keys` and a `cache_contract` that keeps browser writes
  disabled
- the same endpoint supports session-scoped reads with `client_id` and
  `session_id`, preventing unrelated browser sessions in one webspace from
  being merged into the future UI adapter read path
- repeated `projection_keys` query filters let a widget-level adapter request
  only its own demanded ProjectionRecords while preserving the full browser
  demand set
- browser-cache now supports HTTP cache validation through `ETag` and
  `If-None-Match`, so unchanged demanded snapshots can skip widget work
- legacy `data/<skill>` Yjs branches are now reported with explicit
  compatibility metadata: monolithic roots, single-slot branches, and
  sectioned roots remain transitional read surfaces, but their write policy is
  `projection-record-only`; the shared `data/projectionRecords` branch is
  classified as the canonical core-owned cache
- `registry.named_entities` is now a self-describing read-only compatibility
  reference with schema, Yjs path, owner, write policy, privacy metadata, and
  stable summary fingerprinting
- `data_projections` manifest targets now support an optional
  `projection_key`, and the shared projection registry exposes an
  `adaos.data-projections.v1` contract report for skill/scenario manifests,
  reserved `data/projectionRecords` violations, legacy monolithic roots, and
  projection-keyed Yjs targets
- projection migration inventory and metrics now expose manifest contract
  evidence through the API, including keyed-target coverage and reserved
  `data/projectionRecords` target violations
- `/api/node/projection-migration/acceptance-summary` provides a compact
  server-side MVP readiness report with `pass`/`warn`/`fail` checks over
  inventory, metrics, manifest guardrails, shared bridge evidence, and ranked
  migration backlog
- the acceptance summary now includes `interpretation` and `manual_review`
  blocks so a manual Swagger check explains what `ready_with_followups` means
  and which fields should be inspected first
- the same summary now exposes ordered `manual_steps` for checking
  `acceptance-summary`, `metrics`, `monolith-inventory`, and
  `recommendations` during a diploma/demo run
- `acceptance-summary` exposes `swagger_verification`, a compact single-endpoint
  checklist with expected success markers, fields to inspect, and a failure
  action for manual Swagger checks
- `acceptance-summary` exposes `request_examples` with curl-ready calls for
  `acceptance-summary`, `metrics`, and `recommendations` so control runs can be
  repeated from a terminal after `adaos api serve`
- `acceptance-summary` exposes `traceability_matrix`, mapping plan items to
  API fields, diploma usage, and verification steps for review/defense
- `acceptance-summary` also exposes `evidence_rows`: a compact table of
  report-ready metrics with value, expected direction, meaning, and diploma
  usage notes
- `acceptance-summary` exposes `measurement_model` with repeatable
  before/after comparison rows, baseline policy, current metric values, and
  comparison rules for chapter 3 control examples
- `acceptance-summary` exposes `control_snapshot`: a compact evidence block
  with readiness result, progress percentages, and key metrics for one saved
  control run
- `acceptance-summary` exposes `plan_review`, which maps the current result
  back to slices 1-6 and separates server-ready work from client hookup,
  Infrascope split, node-envelope, and cleanup follow-ups
- `acceptance-summary` exposes `completion_gates`, a pass/warn/fail checklist
  derived from this Completion Definition so server-ready gates and remaining
  follow-ups can be read directly from Swagger
- `acceptance-summary` exposes `risk_register`, generated from warning gates
  and acceptance checks, with risk, impact, mitigation, and verification text
  for diploma limitations and demo notes
- `acceptance-summary` exposes `demo_script` with a short expected result,
  current result, conclusion, and limitation text for a manual demo or
  presentation
- `acceptance-summary` exposes `defense_summary`, a compact thesis/proof
  section with metrics to quote, limitations, and a closing statement for
  diploma defense
- `acceptance-summary` exposes a `progress` block that separates weighted
  server-side MVP progress from the wider full-plan estimate and names the
  remaining work groups; the block also carries `completed_groups`,
  `remaining_groups`, `remaining_group_details`, `followup_roadmap`, and
  `headline_metrics` so the current status and next milestones can be checked
  without interpreting every low-level metric by hand
- `acceptance-summary` exposes `final_acceptance`, a top-level accept/blocked
  decision for the server-side MVP with required evidence, explicit accepted
  scope, and non-accepted full-plan follow-ups such as browser client
  migration, full Infrascope split, node-aware envelope, and legacy cleanup

Exit criteria:

- low-churn skills can remain simple without violating the contract
- high-churn or multi-surface skills have a clear migration path
- duplicate local fallback rules are removed after shared helpers are adopted

## Required Contract Shapes

### Event Envelope

The minimal shared event envelope should support:

- `event_id`
- `type`
- `source`
- `source_authority`
- `actor`
- `scope`
- `trace_id`
- `cause_event_id`
- `schema`
- `version`
- `priority`
- `ts`
- `payload`

Compatibility rule:

Existing `Event(type, payload, source, ts)` publishers must remain valid.  New
helpers may enrich events by reading or writing compatible metadata, but they
must not require every legacy producer to construct the full envelope manually.

### Projection Record

The canonical projection record should support:

- `status`
- `data`
- `meta`
- `error`

Required `meta` concepts:

- `projection_key`
- `kind`
- `webspace_id`
- `node_id` or equivalent node scope when relevant
- `version`
- `fingerprint`
- `updated_at`
- `changed_at`
- `source`
- `source_authority`
- `access`
- `lifecycle_reason`

### Client Subscription Record

The browser-written subscription record should support:

- `client_id`
- `device_id`
- `session_id`
- `webspace_id`
- `role`
- `subscriptions`
- `updated_at`

Each subscription should support:

- `projection_key`
- `consumer_id`
- `consumer_kind`
- `node_scope`
- `pinned`
- `visibility`
- optional `params`

The client writes the full current set for that client.  Add/remove deltas are
not the primary source of truth.

### Status Card

Status cards are the first small platform-emitter projection family.  They
should support:

- identity: `id`, `owner`, `kind`, `scope`, optional `webspace_id`
- state: `status`, `summary`, `severity`, `updated_at`, `ttl_ms`
- change tracking: `version`, `fingerprint`, `changed_at`
- details reference: receiver, path, tool, or other lazy details target
- incident relation where relevant

Status cards should prove fingerprinting, dedupe, staleness, thin reads, and
push/delta behavior before heavy skill migration.

## Review Checklist

Use this checklist for every implementation slice touching the event model.

- Does the change follow the master roadmap phase order?
- Does it reuse the shared event envelope or remain compatible with it?
- Does it preserve event scope, projection scope, and node scope separately?
- Does it avoid broad Yjs rewrites when only one demanded projection changed?
- Does it publish projection lifecycle state rather than only payload data?
- Does it expose platform-originated failures outside skill-owned payloads?
- Does it avoid treating human labels as routing keys?
- Does it preserve pressure observability when work is coalesced or dropped?
- Does it include tests for multi-webspace or multi-consumer behavior when the
  change affects dispatch?
- Does it avoid introducing a skill-specific ABI that would later need to be
  migrated again?

## Coverage Matrix

| Area | Required before heavy skill pilot | Current status |
| --- | --- | --- |
| Communication prerequisites | Closed for current transport scope | Complete |
| Event taxonomy | Stable vocabulary | Complete |
| Shared event envelope | Helpers and compatibility rules | Helper code added; producer migration remains |
| Named-entity ABI | Records, resolver result, lifecycle topics, invalidation | Mostly complete; `registry.named_entities` now exposes read-only compatibility metadata; consumer migration remains |
| Status-card ABI | Platform-emitter family with dedupe/version/staleness | Helper code, materialized registry, runtime card, TTL sweep, and demanded shared projection-record materialization added |
| Projection record ABI | Canonical record shape | Helper code, deterministic projection-key helpers, shared materialized registry, status-card bridge, diagnostics correlation, `data/projectionRecords` Yjs materialization/readback, and diagnostics cache correlation added |
| Browser subscription ABI | Full-overwrite demand records | Helper code, server runtime, browser-state mapper, session touch, demanded ProjectionRecord browser-cache endpoint, client/session scoped browser-cache reads, projection-key filtered browser-cache reads, ETag/If-None-Match validation, and per-entry cache metadata added; browser client adapter hookup remains |
| Node-aware Yjs envelope | Reserved top-level ownership shape | `data/projectionRecords` now has a top-level envelope with core ownership, write policy, node-scope summary, and read/write boundaries; wider rollout to non-projection Yjs branches remains |
| Client demand runtime | Page/widget/modal/pinned consumers | Server registry/API/mapper, browser-state mapper, stale marking, session touch, and multi-webspace API isolation tests added; browser client hookup remains |
| Shared dispatcher | Per-webspace demanded refresh | Base dispatcher/API, status-card wildcard handler, canonical record materialization, Yjs projection-record cache write/readback, Infrascope-specific demanded refresh handler, and multi-consumer grouping tests added |
| Operator diagnostics | Demand/dispatcher/status-card correlation | `/api/node/projection-diagnostics` correlates demand, dispatcher handlers, status cards, shared materialized ProjectionRecords, optional demanded materialization, optional Yjs projection-record cache, node-aware cache envelope health, and optional Infrascope demanded-card refresh; `/api/node/projection-migration/acceptance-summary` gives a compact MVP readiness report |
| Platform emitter pilot | Status/notifications/diagnostics through shared ABI | Runtime lifecycle, UI runtime diagnostics, toast notifications, and desktop shell snapshots publish platform status cards through the shared ABI |
| Thin reliability summary | Poll-safe status summary over registry | `mode=thin`, registry version, `since_version`, cache hints, ETag headers, `If-None-Match`, telemetry, payload comparison, telemetry reset, and optional Infrascope card refresh added |
| SDK/helper layer | Reusable skill-facing publishing helpers | `adaos.sdk.status` added for status-card publishing |
| Infrastate alignment | Operational overlay uses shared status-card path | Snapshot-to-card adapter, API publication, and lazy details refresh added |
| Infrascope migration | Uses shared ABI and dispatcher | First status-card adapter covers overview/incidents/inventory/operations/browser/runtime/registry plus object-inspector/topology cards; API refresh path can publish from request payload or `data/infrascope`; explicit `card_ids` and `demanded_only` refreshes are supported; dispatcher and diagnostics can refresh demanded `status-card:infrascope-*` records from `data/infrascope`; no-cross-webspace churn is covered; status-card snapshot and thin summary can refresh from `data/infrascope` on read; `infrascope-overview` supports tool-backed lazy details refresh; client/live-skill hookup remains |
| Cross-skill rollout | Inventory and migration path for remaining skills | Monolithic Yjs publisher inventory added through `/api/node/projection-migration/monolith-inventory`; legacy branch compatibility rules classify transitional read surfaces versus the canonical `data/projectionRecords` cache; skill/scenario `data_projections` targets now accept `projection_key`, have shared manifest contract inspection, and expose API metrics for keyed-target coverage and reserved cache violations; skill-local projection shim findings added to the inventory; migration metrics added through `/api/node/projection-migration/metrics`; prioritized migration recommendations added through `/api/node/projection-migration/recommendations`; SDK pressure counters added for dirty drops/coalesced/overlapping refreshes; SDK active-demand restore added for projection and stream runtimes; demanded ProjectionRecord browser-cache read path added; migration and cleanup remain |

## Completion Definition

The operational event model can be considered covered when:

- all required contract shapes above exist in docs and helper code
- at least one platform-emitter family uses the shared projection contract
- browser clients can declare multiple active projection demands in one
  webspace
- the dispatcher refreshes demanded projections without cross-webspace churn
- named-entity lifecycle changes invalidate consumers without reload-only
  behavior
- Infrascope or another heavy pilot uses the shared ABI without adding a
  parallel one
- acceptance tests cover event envelope compatibility, multi-consumer demand,
  multi-webspace dispatch, platform emitter lifecycle, and pressure
  observability
