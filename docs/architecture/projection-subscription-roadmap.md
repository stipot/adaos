# Projection Subscription Roadmap

This roadmap is the detailed delivery checklist for moving AdaOS runtime
interaction, browser-facing skills, and scenarios from monolithic Yjs snapshots
and ad hoc refresh logic to demand-driven projections.

It is intentionally narrower than the broader target-state architecture
documents so implementation work has a focused checklist.

The target architecture is defined in [Operational Event Model](operational-event-model.md).
The master implementation order across all adjacent workstreams is defined in
[Operational Event Model Roadmap](operational-event-model-roadmap.md).
The skill-facing SDK/core rails are defined in
[Skill Projection Runtime SDK](skill-projection-runtime-sdk.md).

## Ownership Rule

Snapshot date: 2026-05-15.

This document no longer owns an independent priority order.
It expands the projection-specific parts of the master
[Operational Event Model Roadmap](operational-event-model-roadmap.md).

Use it as the detailed checklist for:

- Phase 3: projection record shape, client subscription shape, node-aware Yjs
  envelope, and compatibility rules
- Phase 4: browser projection subscription runtime and client adapter
- Phase 5: shared dispatcher behavior
- Phase 7: Infrascope migration slice
- Phase 8: follow-up pilots
- Phase 9: cross-skill rollout and cleanup

Before adding more large scenario snapshots, new widget-specific caches, or
more ad hoc event debouncing, AdaOS should establish the shared
projection/subscription runtime contract described here.  However, this work
should be started only when it is the next active slice in the master roadmap
or when it removes a blocker for that slice.

This contract is intended as an architectural layer above the communication model, not as a one-off adaptation for one skill.

If this checklist and the master roadmap disagree, the master roadmap wins.
Update this document instead of creating a second delivery track.

## Goals

- make projection demand explicit
- materialize projections per webspace, not globally
- allow page, widget, modal, and panel consumers to coexist
- reduce Yjs write noise and broad client invalidation
- keep richer semantic state in skill memory while publishing only demanded views
- treat platform diagnostics, system messages, and browser/runtime errors as first-class emitted projections where appropriate
- reuse the same contract across skills and scenarios

## Non-Goals for MVP

- per-user payload forks inside the same webspace
- mandatory deletion of inactive projections
- universal generic renderer semantics for every possible UI surface
- replacing the existing domain event bus

## Checklist

### 0. Communication and Runtime Ordering

- [x] `ordering.fixed`: place this projection/event work explicitly after node-browser and runtime communication hardening
- [x] `ordering.runtime_first`: treat the new model as a core/skill/platform interaction contract first, and a browser materialization contract second
- [x] `ordering.aligned_with_comm_phases`: align the implementation order with the communication phases described in the runtime reliability roadmap

Current status:

- the communication prerequisite ordering is closed in the master roadmap
- deeper sidecar continuity and media work remain follow-on reliability work,
  not hidden blockers for the current projection ABI slice

### 1. Architectural Fixation

- [x] `arch.event_model_published`: publish `Operational Event Model` as the shared target-state contract for runtime and browser projection work
- [x] `arch.event_taxonomy_fixed`: define the canonical distinction between `domain events`, `core-skill interaction events`, `projection demand`, `projection lifecycle`, `ui intent`, and `platform operational events`
- [x] `arch.webspace_scope_fixed`: define `projection scope` as `per-webspace`
- [x] `arch.node_scope_reserved`: define room for `node scope` inside shared Yjs state
- [x] `arch.audience_contract_fixed`: define the MVP access/audience metadata contract with `shared`, `owner`, `guest`, and `dev`
- [x] `arch.shared_payload_rule_fixed`: explicitly freeze the MVP rule that owner and guest do not get separate payload branches

Current status:

- architectural fixation is complete for this checklist
- unresolved work now belongs to ABI and implementation phases, not to
  vocabulary debate

### 2. Core and Shared Runtime ABI

- [ ] `runtime.event_envelope_abi`: align with the master roadmap's shared event envelope before adding projection-specific metadata
- [ ] `runtime.core_skill_contract`: define the core-to-skill invalidation and refresh contract before browser-specific consumption logic
- [ ] `runtime.ownership_split`: define which runtime transitions are core-owned and which projection rebuilds are skill-owned
- [ ] `runtime.platform_emitters_defined`: define platform-emitted projections for notifications, warnings, diagnostics, and system errors
- [ ] `runtime.restore_demand_from_yjs`: define startup restoration rules for core and skills reading active demand from Yjs

Current status:

- named-entity ABI is already implemented enough to serve as a model for
  contract-first runtime work
- eventbus backpressure exists for selected hot paths, but does not replace
  the event envelope or dispatcher ABI
- status-card ABI should be treated as the first platform-emitter family and
  kept aligned with this projection contract
- the SDK now has a `restore_active_demand(...)` helper for projection and
  stream runtimes; the remaining roadmap item is wiring that helper to the
  durable Yjs/source-of-truth restoration path

### 3. Projection ABI

- [x] `abi.projection_record_shape`: define the canonical projection record shape: `status`, `data`, `meta`, `error`
- [x] `abi.projection_keys_fixed`: define deterministic `projection_key` rules for page, widget, modal, panel, platform-emitted, and node-scoped projections
- [x] `abi.client_subscription_shape`: define the browser-written client subscription record shape
- [ ] `abi.node_aware_yjs_envelope`: define the node-scoped top-level Yjs envelope so shared subnet state can preserve multiple node emitters
- [x] `abi.pinned_consumer_semantics`: define `pinned` consumer semantics

Next active projection task:

- lock `abi.projection_record_shape` and `abi.client_subscription_shape`
  together; either shape without the other will recreate the current
  compatibility drift
- include platform status cards and named-entity registry as reference
  examples, not only skill-owned projections

### 4. Client Subscription Runtime

- [ ] `client.subscription_registry`: add browser-side projection subscription registry support
- [x] `client.full_subscription_overwrite`: make each client write its full active subscription set on change
- [ ] `client.surface_lifecycle_to_subscriptions`: ensure modal open/close, widget mount/unmount, and visibility changes update the client subscription record
- [x] `client.multi_projection_support`: add support for multiple active projections in one webspace
- [ ] `client.node_multiplicity_ready`: prepare the browser to consume node multiplicity from shared Yjs instead of assuming one anonymous node view
- [x] `client.soft_session_sanitation`: keep stale-client cleanup as a soft client/session sanitation mechanism, not as projection activity logic

Current status:

- node-aware stream receiver hints and compatibility-era node ownership metadata
  already exist in the browser/runtime path
- shared `data/projectionRecords` Yjs cache now preserves `meta.node_id` and
  reports `node_ids` plus `node_scoped_record_total` so operator diagnostics
  can see node-scoped projection records without parsing every payload entry
- the same shared cache now writes a top-level `envelope` with core ownership,
  write policy, node-scope summary, and browser/skill boundary flags; cache
  readback and projection diagnostics expose `envelope_ok` for Swagger checks
- `adaos.domain.projection_keys` now fixes deterministic helpers for
  `status-card:<id>`, `projection:<surface>/<id>`, and
  `projection:node/<node_id>/<projection_key>` forms
- a server-side browser demand registry, full-overwrite API, and browser-state
  mapper are implemented
- direct Angular client hookup remains pending because the client submodule is
  not initialized in this checkout
- avoid adding another browser-local cache or modal-specific registry before
  the shared subscription shape is locked

### 5. Skill, Scenario, and Platform Dispatcher

- [x] `dispatcher.shared_pattern`: add a shared dispatcher pattern for `domain/core/platform event -> in-memory update -> demanded projection refresh`
- [x] `dispatcher.per_webspace_refresh`: make demanded projection refresh run per webspace
- [x] `dispatcher.no_cross_webspace_churn`: prevent one webspace from forcing writes into unrelated webspaces
- [x] `dispatcher.skill_projection_sdk`: implement the shared skill-facing
  projection runtime SDK so skills do not open-code projection executors,
  stream receiver routing, fingerprint maps, or dirty-section dispatch
- [ ] `dispatcher.memory_richer_than_yjs`: allow skills and platform services to keep richer semantic caches in memory than they publish into Yjs
- [x] `dispatcher.lifecycle_exposed`: expose projection lifecycle transitions through the shared projection record
- [x] `dispatcher.pressure_observable`: preserve eventbus/rebuild/stream pressure counters when dispatcher coalesces or suppresses work

Current status:

- selected eventbus hot topics are already bounded/coalesced as incident
  guardrails
- the base dispatcher now owns demanded refresh selection and exposes lifecycle
  and pressure state through API
- wildcard projection-family handlers are supported; `status-card:*` is now
  backed by the materialized status-card registry
- Yjs projection record writes remain the next integration step

### 6. Yjs Granularity and Client Adapter

- [ ] `yjs.adapter_projection_records`: update the client-side Yjs adapter to read projection records instead of one giant scenario snapshot
- [ ] `yjs.cache_by_projection_key`: cache projection payloads by `projection_key`
- [ ] `yjs.reuse_cached_views`: reuse cached payloads when switching back to recently materialized views
- [ ] `yjs.reduce_broad_observers`: avoid broad `observeDeep(data)` patterns where a stable nested projection path is available
- [x] `yjs.legacy_compat_rules`: document the compatibility rules for legacy plain-JSON projection branches during migration
- [x] `yjs.named_entity_registry_reference`: use `registry.named_entities` as an implemented read-only compatibility reference for projection fingerprinting and privacy limits

Current status:

- migration inventory now attaches `compatibility` metadata to each discovered
  `data/<skill>` Yjs root, separating legacy monolithic roots, single-slot
  branches, sectioned roots, unsupported references, and the canonical
  `data/projectionRecords` cache
- legacy plain-JSON branches are allowed as transitional read surfaces only;
  new writes are expected to flow through `ProjectionRecord` materialization
  and stable `projection_key` values
- `data/projectionRecords` is explicitly classified as the core-owned
  canonical cache rather than another skill-owned monolithic branch
- control metrics now expose `legacy_compatible_root_total` and
  `projection_record_cache_root_total` so branch compatibility can be tracked
  during rollout
- `registry.named_entities` now carries `schema`, `yjs_path`, read-only
  access metadata, owner, write policy, and privacy limits while preserving
  the compact fingerprinted entity descriptor payload

### 7. Early Pilot Sequence

- [x] `pilot.status_cards_first`: implement status cards as the first small platform-emitter projection family
- [ ] `pilot.platform_surfaces_first`: prepare `web_desktop` and the shared platform surfaces first: notifications, diagnostics, workspace manager, and related modals
- [x] `pilot.platform_emitter_validated`: validate platform-as-emitter semantics before migrating one heavy skill
- [ ] `pilot.infrascope_after_prereqs`: migrate `Infrascope` only after the core/runtime and client projection contracts are in place
- [x] `pilot.infrastate_aligned`: align `infrastate`-style shared operational overlays with the same contract
- [ ] `pilot.dev_scenario_followup`: choose one dev-oriented scenario such as `prompt_engineer_scenario` as the first non-operator follow-up
- [ ] `pilot.simple_skills_deferred`: postpone low-churn simple skills until the core contract and adapter behavior are stable

Current status:

- `status-card:*` demand is now refreshed through the shared dispatcher rather
  than reported as `no_handler`
- `/api/node/status-cards` provides a thin publish/read surface for materialized
  status-card projection records
- `/api/node/status-cards` materializes the built-in runtime lifecycle card by
  default, and `/api/node/status-cards/runtime/refresh` refreshes it explicitly
- `/api/node/infrastate/snapshot` now publishes first `infrastate` status-card
  projections for summary, operations, realtime, Yjs, and core-update sections
- `/api/node/projection-diagnostics` now correlates active demand, dispatcher
  handler coverage, and status-card materialization per webspace
- `/api/node/status-cards/{card_id}/details/refresh` requests lazy stream
  details for stream-backed status cards without expanding the card payload
- `/api/node/status-cards/sweep` previews or removes TTL-expired cards and
  registry stats now include last publish latency plus sweep counters
- `/api/node/reliability/summary?mode=thin` now reads the materialized
  status-card registry and supports `since_version` unchanged responses
- `since_version` is backed by registry-level `registry_version`, so adding a
  new card with local version `1` still advances the thin polling token
- thin summary responses include cache hints for the registry key, registry
  version, `since_version` parameter, and individual status-card cache keys
- thin summary responses expose registry ETags and cache headers, and matching
  `If-None-Match` requests return `304 Not Modified` without the full
  compatibility payload
- `/api/node/reliability/summary/telemetry` records summary mode, status codes,
  estimated response bytes, unchanged hits, and `304 Not Modified` hits for
  acceptance analysis
- telemetry compares average full and thin payload sizes to make reduction
  evidence visible without a separate log parser
- telemetry can be reset through `/api/node/reliability/summary/telemetry/reset`
  before a clean manual or soak acceptance run
- missing cards return `unavailable`; TTL-expired cards return `stale`, so the
  operator-visible lifecycle semantics are exercised before the heavy skill
  pilot

### 8. Infrascope Migration Slice

- [x] `infrascope.status_card_adapter_first`: map overview, active incidents, inventory, operations, browser runtime, runtime, and registry sections into shared status cards
- [ ] `infrascope.split_projection_families`: split `overview`, `inventory`, `inspector`, `topology`, and modal/widget payloads into separate projections
- [ ] `infrascope.stop_full_inspector_snapshot`: stop pre-materializing all inspectors into one Yjs snapshot
- [ ] `infrascope.demanded_only_per_webspace`: publish only the projections actively demanded by each webspace
- [x] `infrascope.shared_payload_access_metadata`: verify that owner and guest use the same payload but can still receive different display/action treatment through access metadata
- [ ] `infrascope.platform_errors_separate`: publish platform-originated warnings and materialization errors as separate operator-facing projections instead of hiding them inside one skill snapshot

### 9. Cross-Skill Rollout

- [x] `rollout.monolith_inventory`: identify other browser-facing skills that currently publish monolithic Yjs JSON subtrees
- [ ] `rollout.migrate_to_shared_contract`: migrate them onto the shared projection/subscription contract
- [x] `rollout.shared_helpers`: provide a common helper layer so each skill does not reimplement subscription parsing and dispatch logic
- [x] `rollout.manifest_rules`: document how scenario manifests and skill manifests declare projection roots without inventing incompatible shapes

Current status:

- `/api/node/projection-migration/monolith-inventory` now reports direct
  `data/<skill>` Yjs roots, smaller single-slot paths, stream receivers, and
  shared bridge hints for workspace skills
- the same inventory now reports `shim_findings` for direct skill-local Yjs
  writes, local fingerprint caches, executor bridges, and per-skill projection
  manifest loaders that should be replaced by shared SDK helpers
- `/api/node/projection-migration/metrics` now exposes control metrics for
  monolith exposure, migration readiness, and weighted legacy pressure; the
  repeatable check procedure is captured in
  [Projection Migration Control Examples](projection-migration-control-examples.md)
- inventory roots include compatibility rules for legacy Yjs branches, so
  rollout reports can distinguish read-compatible migration branches from the
  canonical `data/projectionRecords` cache
- `/api/node/projection-migration/recommendations` now turns the same evidence
  into a prioritized migration backlog with concrete next actions per skill
- `adaos.sdk.status` provides first shared helpers for publishing status-card
  projections from skills and platform code
- `adaos.sdk.data.projections` now keeps diagnostics for dirty-event drops,
  coalesced refreshes, and overlapping refresh pressure
- `ProjectionRecord` normalizes MVP access metadata with `shared`, `owner`,
  `guest`, and `dev` audiences while keeping owner and guest payloads shared
- platform emitters now have a shared status-card projection test covering
  runtime lifecycle, desktop shell, notifications, and UI runtime diagnostics
- helpers preserve current skill ownership as `skill:<name>` and support stream
  details through `details_ref`
- `data_projections` targets in `skill.yaml` and `scenario.yaml` can now carry
  `projection_key`, and the shared projection registry exposes an
  `adaos.data-projections.v1` manifest contract inspector for reserved cache
  targets, legacy monolithic roots, and projection-keyed Yjs targets
- `/api/node/projection-migration/monolith-inventory` and
  `/api/node/projection-migration/metrics` now surface manifest contract
  counters, including `projection_keyed_yjs_target_total`,
  `reserved_cache_manifest_target_total`, and
  `manifest_projection_key_coverage_ratio`
- `/api/node/projection-migration/acceptance-summary` aggregates inventory,
  metrics, manifest guardrails, shared-bridge presence, and ranked backlog
  into `pass`/`warn`/`fail` checks for the server-side MVP
- migration notes are captured in `status-card-sdk-migration.md`; first real
  skill conversion remains pending

### 10. Cleanup and Hardening

- [ ] `cleanup.remove_monolith_paths`: remove monolithic snapshot paths where the new projection contract fully replaces them
- [ ] `cleanup.remove_inline_debounce`: remove event-specific inline debounce logic that the dispatcher now supersedes
- [x] `cleanup.operator_projection_diagnostics`: add operator diagnostics for active projections per webspace
- [x] `cleanup.test_multi_webspace_and_consumers`: add tests for multi-webspace demand routing and multiple simultaneous consumers
- [x] `cleanup.test_access_metadata_and_dev`: add tests for guest-visible access metadata and `dev` audience handling
- [x] `cleanup.test_platform_emitters`: add tests for platform-emitted diagnostics and error projections

## Priority Candidates and Critical Assessment

The new model is best used first where all of the following are true:

- the UI has multiple independently visible surfaces
- updates are frequent or bursty
- more than one webspace may demand different projections
- the current implementation uses large plain-JSON Yjs branches

Recommended order:

1. core/runtime plus client preparation
   The architectural contract should exist before one heavy skill becomes the pilot.
2. `web_desktop` platform surfaces
   Best place to validate platform-as-emitter semantics for system messages, diagnostics, and shared browser/runtime failures.
3. `Infrascope`
   Strongest heavy-skill pressure test after the shared architecture exists.
4. `infrastate` and similar operational overlays
   Good follow-up once operator-facing demand dispatch is proven.
5. one dev-oriented scenario
   Good for testing `dev` audience behavior and more panel-heavy view switching.
6. voice/media or other bursty interactive surfaces
   Valuable after the core dispatcher and client adapter are stable.

For the browser-client semantic ABI work that should precede broader renderer
expansion, see [Web UI Architecture](web-ui-architecture.md).
The first concrete validation target should be a demo scenario and demo skill
with a table-oriented view, a chart-oriented view, and one shared
selection/filter model rather than a broad speculative visualization catalog.

Current status of that validation target:

- the repository now contains `demo_metrics_skill` and
  `taiga_ui_demo_scenario`
- the browser runtime can already materialize the demo table/chart/event slice
  through the current compatibility bridge
- first-environment stand verification is the next milestone before broader
  projection-oriented renderer work

Counter-example:

- simple low-churn skills with one small projection do not need to be forced onto this model immediately

Execution note:

- preparatory inventory for Infrascope is allowed before the platform pilot
- Infrascope must not introduce its own projection ABI, subscription record, or
  lifecycle contract ahead of phases 3-6 in the master roadmap

## Acceptance Criteria

This roadmap is successful when:

- at least one complex operator scenario uses demand-driven projections instead of one monolithic snapshot
- multiple browser consumers in one webspace can demand different projections concurrently
- multiple webspaces can receive different projection refreshes from the same domain and platform event streams
- skills and platform services no longer need to publish their whole UI model into Yjs to keep the browser working
- the browser can switch back to a recently opened view without forcing a full rebuild every time
