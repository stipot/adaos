# Operational Event Model Roadmap

This roadmap is the master implementation plan for the event, projection, and browser/runtime work discussed across the control-plane documents.

It exists to prevent roadmap drift between:

- [Operational Event Model](operational-event-model.md)
- [Projection Subscription Roadmap](projection-subscription-roadmap.md)
- [Infrascope Roadmap](infrascope-roadmap.md)
- communication and webspace/runtime hardening tracks

This document is intentionally orchestration-first.
It should not duplicate the detailed target-state contracts from the source documents above.
Instead, it defines order, dependencies, milestones, and pilot strategy.

## Primary Sources

Use these documents as the authoritative sources for detailed design:

- [Operational Event Model](operational-event-model.md)
  Event taxonomy, ownership, node scope, projection lifecycle, Yjs envelope, and platform emitters.
- [Operational Event Model Reference Plan](operational-event-model-reference-plan.md)
  Top-level coverage gates, required contract shapes, review checklist, and
  completion definition for implementing the model correctly.
- [Projection Subscription Roadmap](projection-subscription-roadmap.md)
  Detailed checklist for projection ABI, client demand registration, dispatcher behavior, and migration work.
- [Skill Projection Runtime SDK](skill-projection-runtime-sdk.md)
  Skill-facing SDK/core rails for projection slots, stream receivers, dirty
  routing, set-if-changed Yjs writes, and migration checklists.
- [Infrascope Roadmap](infrascope-roadmap.md)
  Operator-workspace sequencing and the later heavy-skill migration target.
- [Webspace Scenario Pointer/Projection Roadmap](webspace-scenario-pointer-projection-roadmap.md)
  Webspace rebuild, semantic ownership, and projection/materialization evolution.
- [Realtime Reliability Roadmap](realtime-reliability-roadmap.md)
  Communication hardening and ordering constraints that must come first.

## Why This Roadmap Exists

AdaOS now has several related but distinct workstreams:

- communication hardening
- core/runtime event ownership
- skill/core interaction semantics
- demand-driven projections
- Yjs shape evolution
- browser/client projection consumption
- platform-emitted diagnostics and system messages
- heavy-skill migration such as `Infrascope`

Without a shared roadmap, these tracks can easily fork into:

- one-off skill adaptations
- duplicated Yjs conventions
- browser-only fixes that ignore core/runtime semantics
- platform diagnostics hidden inside unrelated skill payloads

This roadmap defines one implementation order across all those branches.

## Roadmap Ownership

Snapshot date: 2026-05-15.

This document is the single authoritative delivery track for the operational
event model.

The companion [Projection Subscription Roadmap](projection-subscription-roadmap.md)
is now a subordinate detail checklist.  It owns the detailed browser
subscription, projection ABI, Yjs adapter, and dispatcher work items, but it
does not own a separate priority order.  When the two documents appear to
disagree, this roadmap wins and the projection checklist should be updated to
match this sequence.

Use the documents this way:

- `Operational Event Model` defines the target architecture and vocabulary.
- `Operational Event Model Reference Plan` defines the coverage gates,
  required contract shapes, review checklist, and completion definition for
  implementing the model correctly.
- `Operational Event Model Roadmap` defines the implementation order and phase
  gates.
- `Projection Subscription Roadmap` expands phases 3, 4, 5, 7, 8, and 9 into
  concrete client/projection/dispatcher checklists.
- `Skill Projection Runtime SDK` expands the SDK/core helper layer needed by
  phases 5, 8, 9, and the `browsers_skill`/`infrastate_skill` migrations.
- `issue-tracker.md` records current execution tasks, incidents, and acceptance
  evidence.

Do not start a heavy skill migration just because the projection checklist has
an attractive local task.  A projection task enters active work only when it is
the next item in this master sequence or when it closes a blocker for that
sequence.

## Guiding Rules

- contracts before migrations
- communication guarantees before projection runtime adoption
- core/runtime ownership before browser specialization
- node-aware shared Yjs shape before heavy scenario pilots
- platform emitters before skill-specific pressure tests
- `Infrascope` as a later architectural pilot, not the starting point

## Global Ordering

The intended order across all workstreams is:

1. communication prerequisites
2. event envelope and runtime ownership contract
3. named-entity and status-plane ABI alignment
4. node-aware Yjs and projection record shape
5. client projection adapter and subscription runtime
6. shared dispatcher for demanded projection refresh
7. platform-emitted projections
8. heavy-skill pilots such as `Infrascope`
9. cross-skill rollout
10. cleanup and hardening

## Current Execution Slice

The next coherent slice is `Phase 1 -> Phase 2 -> Phase 3` as one ABI pass:

1. freeze a minimal shared event envelope for runtime events
2. update SDK/core emit helpers to preserve envelope metadata without forcing
   every producer to understand every field
3. mark the named-entity ABI as current foundation and finish its invalidation
   and operator-diagnostic gaps
4. define status cards as the first platform-emitter family, aligned with the
   projection lifecycle model
5. lock the projection record and client subscription shapes before adding more
   browser-specific compatibility paths

This is deliberately smaller than "migrate Infrascope" and larger than "add one
more debounce".  It creates the contract layer needed for both projection work
and platform operational emitters.

## Checklist

### Phase 0. Communication Prerequisites

- [x] `phase0.comm_order_locked`: treat communication hardening as a prerequisite for this roadmap
- [x] `phase0.node_browser_ready`: Realtime Reliability now treats browser/member semantic channels, `Yjs as SyncChannel`, and the current transport-only `/yws` handoff through sidecar local websocket ingress as complete for the current scope
- [x] `phase0.runtime_comm_ready`: hub-root Class A hardening, browser-safe supervisor transition state, routed-browser active-runtime selection, and the current transport-only `/ws` plus `/yws` sidecar handoff are now explicit and complete for the current scope
- [x] `phase0.webspace_runtime_baseline`: webspace rebuild/materialization ownership is aligned with the pointer/projection roadmap, and the browser runtime now consumes that baseline through lightweight diagnostics plus shared page-runtime adapters instead of bespoke component-only reads

Current checkpoint as of 2026-04-21:

- communication-first ordering is now explicitly locked in this roadmap and the companion projection roadmap
- browser runtime consumers now treat `infrastate`, `infrascope`, and `subnet_env` as one operational-overlay class, observing root `data` updates consistently instead of drifting per branch
- page runtime now exposes `runtime.sync`, `runtime.channels`, `runtime.materialization`, and `runtime.phase0.baseline` transforms so declarative surfaces can consume local communication/materialization prerequisites directly
- page runtime now also exposes `runtime.reliability`, `runtime.supervisor`, and `runtime.phase0.communication` transforms, so declarative browser surfaces can consume hub-root hardening, sidecar handoff, and browser-safe supervisor state without re-implementing component-local probes
- focused client tests cover the Phase 0 baseline for operational-overlay reads, observer placement, semantic communication snapshots, and runtime prerequisite snapshots
- browser and hub runtime now expose an explicit SyncChannel contract for Yjs, and the current transport-only `/yws` handoff now closes the remaining browser/member transport prerequisite for this phase
- Realtime Reliability runtime now exposes explicit Yjs ownership boundaries for `ui.current_scenario`, effective `ui/data/registry` branches, compatibility caches, and `yws` transport/session lifecycle, so Phase 0 blockers are no longer hidden behind implicit subtree semantics
- full sidecar-owned Yjs room/session continuity is now explicitly tracked as a separate deferred block in the subordinate Realtime Reliability and sidecar docs; it is not an extra hidden acceptance criterion for current Event Model `Phase 0` beyond the existing `/yws` transport ownership work
- browser header semantic diagnostics now surface `hub_root` Class A coverage, sidecar continuity, and current browser handoff state, so runtime communication evidence stays visible in the same surface that already carries sync-contract and transport-state evidence
- `GET /api/node/reliability`, `adaos node reliability`, canonical control-plane reliability projection, and browser/page runtime now share one explicit `event_model_phase0_communication` checkpoint for the tracked communication prerequisites, so Phase 0 status no longer has to be inferred independently per surface
- those same shared reliability surfaces now also carry `supervisor_runtime`, so browser-safe transition state, candidate runtime visibility, and warm-switch evidence no longer depend on per-surface local heuristics
- routed browser continuity is now also explicit through shared reliability surfaces: `event_model_phase0_communication` carries supervisor-aware active-runtime base selection for root-routed `/ws`, and browser header diagnostics surface that same `supervisor-route` checkpoint instead of hiding it inside bootstrap-only behavior
- `event_model_phase0_communication` now treats sidecar continuity as blocking only when the current runtime/media contract actually requires it, so default Phase 0 runtime communication debt no longer gets overstated beyond the current transport-only scope
- sidecar rollout policy is now also explicit through shared reliability/browser surfaces, so opt-in hub transport adoption can be audited separately from deeper post-Phase-0 continuity and session-runtime work
- `adaos-realtime` now boots dedicated local websocket listeners for `/ws` and `/yws`, root-routed browser ingress prefers those listeners for matching paths, and shared reliability/browser surfaces therefore report both handoffs as `ready` instead of only `proxy_ready`
- for Event Model `Phase 0`, the current transport-only communication prerequisites are now complete across runtime, CLI, control-plane, and browser surfaces; deeper sidecar continuity, media, and sidecar-owned Yjs session runtime remain separate follow-on work rather than hidden acceptance criteria for this phase
- webspace pointer/projection ownership remains materially aligned, and with realtime transport cutover now complete for the current scope, all four Event Model `Phase 0` checklist items are closed
- dependency reading rule for this roadmap: subordinate status still wins over local convenience adapters, but the subordinate Realtime Reliability note now marks the current transport-only prerequisite set as sufficient to close Event Model `Phase 0`

References:

- [Realtime Reliability Roadmap](realtime-reliability-roadmap.md)
- [Webspace Scenario Pointer/Projection Roadmap](webspace-scenario-pointer-projection-roadmap.md)

### Phase 1. Event Model Fixation

- [x] `phase1.master_event_taxonomy`: freeze the shared taxonomy from the Operational Event Model
- [ ] `phase1.core_skill_contract`: define the core-skill interaction contract as a first-class runtime layer
- [x] `phase1.named_entity_contract`: freeze name, localized label, alias, conflict, registry-changed, and resolver-diagnostic events
- [ ] `phase1.platform_emitters`: define the platform as a first-class emitter of notifications, diagnostics, and system errors
- [x] `phase1.scope_model`: freeze `per-webspace` projection scope plus reserved `node scope`
- [x] `phase1.access_contract`: freeze MVP access metadata with `shared`, `owner`, `guest`, and `dev`
- [ ] `phase1.event_envelope`: define the minimal shared event envelope fields and compatibility rules for existing `Event(type, payload, source, ts)` producers

Current checkpoint as of 2026-05-15:

- the taxonomy is stable in the architecture document and should no longer be
  redefined independently by projection, Infrascope, or status-plane work
- named-entity topic constants and lifecycle envelopes exist in code, including
  observed, draft-name, display-name, alias add/remove/deprecate, conflict, and
  registry-changed events
- the remaining named-entity work is consumer migration and operator
  diagnostics, not basic event vocabulary
- eventbus backpressure and incident observability are implemented for selected
  hot topics, but this is a guardrail over the current bus, not yet the shared
  event envelope contract
- platform emitters remain partially defined through diagnostics and the
  planned status-card work; they need one explicit ABI before migration

Primary source:

- [Operational Event Model](operational-event-model.md)

### Phase 2. Shared Runtime ABI

- [ ] `phase2.event_envelope_abi`: implement helpers for event id, trace/cause, actor/source authority, scope, priority, schema/version, and timestamp metadata without breaking legacy publishers
- [ ] `phase2.runtime_ownership_split`: define which invalidations are core-owned and which rebuilds are skill-owned
- [ ] `phase2.refresh_contract`: define the shared invalidation and refresh contract before browser-specific migration
- [ ] `phase2.restore_demand`: define startup restoration from Yjs demand state for core and skills
- [ ] `phase2.platform_projection_families`: define the initial platform-owned projection families, starting with status cards and runtime diagnostics
- [x] `phase2.named_entity_runtime_abi`: define `NamedEntityRecord`, localized label metadata, `EntityResolutionResult`, and `entity.registry.changed` invalidation semantics
- [ ] `phase2.status_card_abi`: align the shared status-card contract with projection lifecycle, platform emitters, and thin reliability summaries

Current checkpoint as of 2026-05-15:

- `NamedEntityRecord`, `EntityResolutionResult`, localized label metadata,
  compact registry payloads, fingerprints, and governed alias write contracts
  exist
- access-link backed browser/member entity changes now publish lifecycle events
  and registry invalidation envelopes
- Root MCP and SDK named-entity helpers exist for read paths and governed alias
  add/remove/deprecate paths
- the core runtime still uses a minimal event object; envelope metadata is
  carried inconsistently in payload `_meta`
- status-card work in `issue-tracker.md` should become the first explicit
  platform-emitter ABI instead of a separate monitoring-only feature

Primary sources:

- [Operational Event Model](operational-event-model.md)
- [Named Entities and Canonical Naming](named-entities.md)
- [Projection Subscription Roadmap](projection-subscription-roadmap.md)

### Phase 3. Node-Aware Yjs Shape

- [ ] `phase3.projection_record_shape`: lock the canonical projection record shape
- [ ] `phase3.client_subscription_shape`: lock the client-written subscription shape
- [ ] `phase3.node_top_level_reserved`: add a reserved node-aware top-level envelope in shared Yjs state
- [x] `phase3.named_entity_projection_path`: lock the read-only named-entity projection path and privacy constraints
- [ ] `phase3.compat_layer_defined`: define compatibility rules for legacy skill/scenario JSON branches

Current checkpoint as of 2026-05-15:

- browser/platform surfaces in `web_desktop` now already propagate lightweight
  node ownership metadata for catalog items, pinned widgets, workspace labels,
  and marketplace install targeting
- compatibility-era Yjs scenario caches used by the current desktop/subnet
  migration are now node-scoped only:
  `...scenarios.<node_id>.<scenario_id>`
- this is intentionally a compatibility-first client step, not yet the final
  shared Yjs node envelope described by this phase
- node multiplicity is therefore now visible in the browser contract, but the
  backend projection record shape and reserved top-level Yjs ownership branches
  are still open work
- the browser/runtime contract now also carries stable presentation metadata
  for nodes: `node_label`, `node_compact_label`, `node_index`, and
  `node_color`
- when explicit node names are missing, the hub assigns and persists stable
  fallback numbering (`Node N` / `Nn`) so multi-node UI no longer falls back
  to raw UUID-like labels
- for current desktop/subnet work, webspaces themselves still remain shared
  Yjs documents; node-aware ownership is now carried by catalog items,
  stream routes, and persisted `home_scenario_ref` metadata rather than by
  making the webspace container node-owned
- the compact named-entity registry path is implemented as
  `registry.named_entities` and should be treated as the current read-only
  compatibility projection
- the general projection record shape, client subscription shape, and
  top-level node-owned envelope remain the blocking ABI work before broad
  dispatcher/client migration

Primary sources:

- [Operational Event Model](operational-event-model.md)
- [Projection Subscription Roadmap](projection-subscription-roadmap.md)

### Phase 4. Client Projection Runtime

- [ ] `phase4.subscription_registry`: implement browser-side projection subscription registry
- [ ] `phase4.full_overwrite_model`: make the browser write full active subscription sets
- [ ] `phase4.multi_projection_consumers`: support concurrent page, widget, modal, and panel consumers
- [ ] `phase4.node_multiplicity_visible`: prepare the client to consume node multiplicity from shared Yjs
- [ ] `phase4.lifecycle_consumption`: consume `pending/refreshing/ready/stale/error` as first-class projection state
- [ ] `phase4.cache_by_projection_key`: cache projection payloads by `projection_key`

Current checkpoint as of 2026-05-02:

- browser page runtime now supports node-aware stream receiver hints
  (`nodeId`, `transport`) in addition to the existing transport-independent
  receiver abstraction
- `WebIoStreamService` can subscribe in `auto`, `member`, or `hub` mode and
  bridge node-qualified and hub-routed stream topics
- backend router/runtime now emits those node-qualified stream topics when
  `_meta.node_id` is present, and browser transport layers propagate `node_id`
  through snapshot/subscription control events
- the desktop client now reads desktop schema and dynamic modal definitions
  only from effective runtime branches (`ui.application.*`) for the current
  subnet-migration scope, leaving scenario-specific structure in Yjs/API
  ownership rather than in client fallback logic
- semantic reload/reset events are now mirrored to members so they can
  self-refresh their subnet snapshot contribution after desktop rebuilds
  instead of depending on a purely hub-pulled recovery loop
- this partially advances `phase4.node_multiplicity_visible`, but the general
  subscription registry and projection lifecycle ABI for all consumers are not
  complete yet

Next gate:

- do not add another client-local projection cache format before
  `phase3.projection_record_shape` and `phase3.client_subscription_shape` are
  locked
- the first implementation should support page, modal, widget, and pinned panel
  consumers through the same subscription record shape

Primary source:

- [Projection Subscription Roadmap](projection-subscription-roadmap.md)

### Phase 5. Shared Dispatcher

- [ ] `phase5.dispatcher_exists`: implement one reusable dispatcher for `event -> in-memory update -> demanded projection refresh`
- [ ] `phase5.per_webspace_dispatch`: ensure dispatch runs per webspace
- [ ] `phase5.no_cross_webspace_churn`: ensure one webspace cannot force unrelated Yjs churn
- [ ] `phase5.memory_vs_yjs_boundary`: preserve the rule that runtime memory may be richer than published Yjs projections
- [ ] `phase5.eventbus_guardrail_tests`: add regression coverage for bounded hot-topic queues, supersede/drop counters, and backlog snapshots so dispatcher work can rely on observable pressure behavior

Current checkpoint as of 2026-05-15:

- selected hot topics are bounded in `LocalEventBus`, including stream snapshot
  requests, stream subscription changes, and subnet member snapshot changes
- webspace rebuild and stream snapshot storm coalescing exists in several
  local hot paths
- this reduces incident amplification, but it is not a replacement for the
  shared dispatcher; the dispatcher still needs to decide which demanded
  projections refresh per webspace

Primary sources:

- [Operational Event Model](operational-event-model.md)
- [Projection Subscription Roadmap](projection-subscription-roadmap.md)

### Phase 6. Platform Emitters First

- [ ] `phase6.status_cards_pilot`: implement shared status cards as the first small platform-emitter family
- [ ] `phase6.notifications_pilot`: migrate notifications through the shared projection contract
- [ ] `phase6.diagnostics_pilot`: migrate diagnostics and operator-visible failures through the shared projection contract
- [ ] `phase6.workspace_manager_pilot`: migrate shared workspace-manager and similar platform surfaces
- [ ] `phase6.emitter_validation`: validate that platform emitters exercise the architecture before one heavy skill is migrated

Current checkpoint as of 2026-05-15:

- `web_desktop` now acts as an early node-aware platform pilot:
  workspace manager surfaces show node ownership,
  home-scenario choices can surface scenarios seen across node-owned webspaces,
  desktop catalogs/widgets show node identity,
  and install requests may target a concrete node
- the current desktop client no longer hides member-owned skill apps from the
  shared apps catalog; only scenario shortcuts and dev-only surfaces remain
  filtered by policy
- desktop apps/widgets ordering is now emitted through shared desktop state
  (`data.desktop.iconOrder`, `data.desktop.widgetOrder`) instead of staying in
  browser-local storage only
- workspace-manager state now also persists a node-aware `home_scenario_ref`
  alongside plain `home_scenario`, so UI selection can distinguish
  same-named scenarios on different nodes even though full runtime resolution
  of remote scenario refs remains follow-on backend work
- semantic Yjs soft reload for the currently open webspace now forces an
  explicit provider resync even when the transport still reports `connected`,
  so browser runtime state follows backend-owned rebuilds instead of trusting
  the old live session
- batch skill migration/update flows can now defer `skills.activated`
  webspace rebuild side effects until one explicit final rebuild, so subnet
  reconnect or slower `rebuild_webspace_from_sources(...)` paths do not
  multiply rebuild cost by the number of migrated skills
- `setup update` and `skill migrate` now converge on shared
  runtime-refresh/rebuild helpers, and post-core-update validation performs
  one required shared webspace refresh after boot instead of hiding rebuild
  work inside multiple per-skill paths
- `Infrastructure State` now also exposes a hub-side `forget_subnet` action so
  stale experimental members can be cleared from the subnet directory and
  active members can republish their snapshot contribution
- this means the pilot has started, but the roadmap item should remain open
  until the same semantics are emitted through the shared dispatcher/projection
  ABI instead of compatibility-era catalog/runtime branches
- the `STATUS-*` issue-tracker track should be executed here, not as a separate
  monitoring-only roadmap: status cards are the smallest useful platform-owned
  projections and should prove fingerprinting, versioning, thin reads, and
  push/delta consumption before Infrascope migration

Why this comes first:

- it validates the contract with platform-owned state
- it avoids coupling the first pilot to the complexity of one heavy skill
- it gives the browser a real projection consumer path before `Infrascope`

Primary sources:

- [Operational Event Model](operational-event-model.md)
- [Projection Subscription Roadmap](projection-subscription-roadmap.md)

### Phase 7. Heavy Skill Pilot

- [ ] `phase7.infrascope_gate`: do not start `Infrascope` migration before Phases 0-6 are materially in place, except for preparatory inventory and tests that do not create a parallel projection contract
- [ ] `phase7.infrascope_split`: migrate `Infrascope` from monolithic snapshots to projection families
- [ ] `phase7.infrascope_platform_errors_outside_skill`: keep platform-originated diagnostics separate from skill-owned payloads
- [ ] `phase7.infrascope_access_metadata`: validate shared payload plus access metadata behavior for owner/guest/dev audiences

Primary source:

- [Infrascope Roadmap](infrascope-roadmap.md)

### Phase 8. Follow-Up Pilots

- [ ] `phase8.infrastate_followup`: align `infrastate`-style operational overlays
- [ ] `phase8.dev_scenario_followup`: choose one dev-oriented scenario such as `prompt_engineer_scenario`
- [ ] `phase8.bursty_surface_followup`: test one bursty interactive surface such as voice/media/browser-session-heavy UX

Primary source:

- [Projection Subscription Roadmap](projection-subscription-roadmap.md)

### Phase 9. Cross-Skill Rollout and Cleanup

- [ ] `phase9.monolith_inventory`: identify remaining monolithic Yjs publishers
- [ ] `phase9.shared_helpers`: provide shared helper layers for subscriptions, dispatcher use, and projection records
- [ ] `phase9.compat_cleanup`: remove legacy monolith paths once replacements are stable
- [ ] `phase9.test_matrix`: add tests for multi-webspace, multi-consumer, node-aware Yjs, platform emitters, and access metadata

## Pilot Priority

The intended pilot order is:

1. platform surfaces in `web_desktop`
2. `Infrascope`
3. `infrastate` overlays
4. one dev-oriented scenario
5. later bursty interactive surfaces

Counter-priority:

- simple low-churn skills should not be forced into the new model first

## Done When

This roadmap is successful when:

- the communication model and projection model no longer compete for ownership
- the core/skill/platform event contract is explicit and documented
- shared Yjs space is ready for node-scoped ownership
- browser clients can demand and cache multiple projections safely
- platform-owned diagnostics and system errors use the same projection runtime as skills
- `Infrascope` can migrate as an architectural pilot rather than a one-off workaround
