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

- [x] `runtime.event_envelope_abi`: align with the master roadmap's shared event envelope before adding projection-specific metadata
- [x] `runtime.core_skill_contract`: define the core-to-skill invalidation and refresh contract before browser-specific consumption logic
- [x] `runtime.ownership_split`: define which runtime transitions are core-owned and which projection rebuilds are skill-owned
- [ ] `runtime.platform_emitters_defined`: define platform-emitted projections for notifications, warnings, diagnostics, and system errors
- [ ] `runtime.restore_demand_from_yjs`: define startup restoration rules for core and skills reading active demand from Yjs

Current status:

- named-entity ABI is already implemented enough to serve as a model for
  contract-first runtime work
- eventbus backpressure exists for selected hot paths, but does not replace
  the event envelope or dispatcher ABI
- status-card ABI should be treated as the first platform-emitter family and
  kept aligned with this projection contract
- Harvest branch checkpoint: event envelope helpers, runtime ownership,
  dispatcher memory boundary, and core-to-skill refresh contract snapshots now
  exist. Platform emitters are implemented only for the status-card projection
  family; notifications and diagnostics still need migration.

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
- Harvest branch checkpoint: the ProjectionRecord and client subscription code
  ABI is locked together, including access metadata, pinned consumers, session
  touch/delete, and deterministic projection keys. The node-aware Yjs envelope
  exists for `data/projectionRecords`; the broader shared Yjs envelope remains
  open.

### 4. Client Subscription Runtime

- [x] `client.subscription_registry`: add browser-side projection subscription registry support
- [x] `client.full_subscription_overwrite`: make each client write its full active subscription set on change
- [x] `client.surface_lifecycle_to_subscriptions`: ensure modal open/close, widget mount/unmount, and visibility changes update the client subscription record
- [x] `client.multi_projection_support`: add support for multiple active projections in one webspace
- [x] `client.node_multiplicity_ready`: prepare the browser to consume node multiplicity from shared Yjs instead of assuming one anonymous node view
- [ ] `client.soft_session_sanitation`: keep stale-client cleanup as a soft client/session sanitation mechanism, not as projection activity logic

Current status:

- node-aware stream receiver hints and compatibility-era node ownership metadata
  already exist in the browser/runtime path
- the browser now keeps a ref-counted Yjs projection demand registry for
  `kind: y` data sources and sends `webio.yjs.<webspace>.<projection>` control
  subscriptions on observer mount/unmount
- websocket and WebRTC event transports emit `webio.yjs.subscription.changed`
  and `webio.yjs.snapshot.requested`, mirroring the stream control plane
- `ProjectionRuntime.set_if_changed` now requires active demand by default;
  slots can explicitly opt into pinned/bootstrap behavior with
  `ProjectionSlot(..., demand="pinned")`
- node-qualified Yjs demand topics are supported as
  `webio.yjs.<webspace>.nodes.<node_id>.<projection>` for compatibility with
  shared desktop node views
- the full-subscription-overwrite/Yjs-persisted registry remains a future
  hardening step; the current MVP is a live control-plane registry owned by
  connection lifecycle
- Harvest branch checkpoint: server APIs accept full active subscription sets
  and browser-state lifecycle snapshots. This does not yet mean the Angular
  adapter has switched to the new read/write path.

### 5. Skill, Scenario, and Platform Dispatcher

- [x] `dispatcher.shared_pattern`: add a shared dispatcher pattern for `domain/core/platform event -> in-memory update -> demanded projection refresh`
- [x] `dispatcher.skill_projection_sdk`: implement the shared skill-facing
  projection runtime SDK so skills do not open-code projection executors,
  stream receiver routing, fingerprint maps, or dirty-section dispatch
- [x] `dispatcher.per_webspace_refresh`: make demanded projection refresh run per webspace
- [x] `dispatcher.no_cross_webspace_churn`: prevent one webspace from forcing writes into unrelated webspaces
- [x] `dispatcher.memory_richer_than_yjs`: allow skills and platform services to keep richer semantic caches in memory than they publish into Yjs
- [x] `dispatcher.lifecycle_exposed`: expose projection lifecycle transitions through the shared projection record
- [x] `dispatcher.pressure_observable`: preserve eventbus/rebuild/stream pressure counters when dispatcher coalesces or suppresses work

Current status:

- selected eventbus hot topics are already bounded/coalesced as incident
  guardrails
- the shared SDK now owns projection write admission: unchanged writes,
  rate-limits, pressure blocks, and no-demand skips are recorded in one
  diagnostic path
- demand is enforced at write time, so existing skill event handlers can keep
  rebuilding in memory while Yjs only receives slots that have an active
  browser consumer
- follow-up dispatcher work should move expensive rebuilds themselves behind
  demand; the current slice prevents background rebuilds from becoming Yjs
  replication traffic
- Harvest branch checkpoint: the shared dispatcher and diagnostics APIs now
  expose selected, refreshed, skipped, error, coalesced, and lifecycle state for
  demanded projections. Moving expensive skill rebuilds fully behind demand is
  still follow-up migration work.

### 6. Yjs Granularity and Client Adapter

- [ ] `yjs.adapter_projection_records`: update the client-side Yjs adapter to read projection records instead of one giant scenario snapshot
- [x] `yjs.cache_by_projection_key`: cache projection payloads by `projection_key`
- [x] `yjs.reuse_cached_views`: reuse cached payloads when switching back to recently materialized views
- [ ] `yjs.reduce_broad_observers`: avoid broad `observeDeep(data)` patterns where a stable nested projection path is available
- [ ] `yjs.legacy_compat_rules`: document the compatibility rules for legacy plain-JSON projection branches during migration
- [ ] `yjs.named_entity_registry_reference`: use `registry.named_entities` as an implemented read-only compatibility reference for projection fingerprinting and privacy limits

Current status:

- `data/projectionRecords` can now be materialized from the in-memory
  ProjectionRecord registry with a node-aware envelope and projection-keyed
  records.
- `/api/node/projection-records/browser-cache` exposes demanded-only records
  with aggregate and per-entry ETags. The client-side adapter migration and
  observer reduction are still open.

### 7. Early Pilot Sequence

- [ ] `pilot.status_cards_first`: implement status cards as the first small platform-emitter projection family
- [ ] `pilot.platform_surfaces_first`: prepare `web_desktop` and the shared platform surfaces first: notifications, diagnostics, workspace manager, and related modals
- [ ] `pilot.platform_emitter_validated`: validate platform-as-emitter semantics before migrating one heavy skill
- [ ] `pilot.infrascope_after_prereqs`: migrate `Infrascope` only after the core/runtime and client projection contracts are in place
- [ ] `pilot.infrastate_aligned`: align `infrastate`-style shared operational overlays with the same contract
- [ ] `pilot.dev_scenario_followup`: choose one dev-oriented scenario such as `prompt_engineer_scenario` as the first non-operator follow-up
- [ ] `pilot.simple_skills_deferred`: postpone low-churn simple skills until the core contract and adapter behavior are stable

Harvest branch checkpoint:

- Status cards now materialize into canonical ProjectionRecords through the
  existing `services.status` registry and the `status-card:*` dispatcher
  handler.
- The Infrascope-specific adapter from donor PR #87 is intentionally not
  accepted into this branch yet; it remains gated behind platform-emitter and
  browser-cache validation.

### 8. Infrascope Migration Slice

- [ ] `infrascope.split_projection_families`: split `overview`, `inventory`, `inspector`, `topology`, and modal/widget payloads into separate projections
- [ ] `infrascope.stop_full_inspector_snapshot`: stop pre-materializing all inspectors into one Yjs snapshot
- [ ] `infrascope.demanded_only_per_webspace`: publish only the projections actively demanded by each webspace
- [ ] `infrascope.shared_payload_access_metadata`: verify that owner and guest use the same payload but can still receive different display/action treatment through access metadata
- [ ] `infrascope.platform_errors_separate`: publish platform-originated warnings and materialization errors as separate operator-facing projections instead of hiding them inside one skill snapshot

### 9. Cross-Skill Rollout

- [ ] `rollout.monolith_inventory`: identify other browser-facing skills that currently publish monolithic Yjs JSON subtrees
- [ ] `rollout.migrate_to_shared_contract`: migrate them onto the shared projection/subscription contract
- [ ] `rollout.shared_helpers`: provide a common helper layer so each skill does not reimplement subscription parsing and dispatch logic
- [ ] `rollout.manifest_rules`: document how scenario manifests and skill manifests declare projection roots without inventing incompatible shapes

### 10. Cleanup and Hardening

- [ ] `cleanup.remove_monolith_paths`: remove monolithic snapshot paths where the new projection contract fully replaces them
- [ ] `cleanup.remove_inline_debounce`: remove event-specific inline debounce logic that the dispatcher now supersedes
- [ ] `cleanup.operator_projection_diagnostics`: add operator diagnostics for active projections per webspace
- [ ] `cleanup.test_multi_webspace_and_consumers`: add tests for multi-webspace demand routing and multiple simultaneous consumers
- [ ] `cleanup.test_access_metadata_and_dev`: add tests for guest-visible access metadata and `dev` audience handling
- [ ] `cleanup.test_platform_emitters`: add tests for platform-emitted diagnostics and error projections

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
