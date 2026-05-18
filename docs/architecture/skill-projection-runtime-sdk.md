# Skill Projection Runtime SDK

Status: target architecture and implementation checklist.

Snapshot date: 2026-05-15.

This document defines the shared SDK/core layer that should replace
skill-local projection executors, ad hoc stream snapshot handlers, and
monolithic Yjs snapshot writes.

It sits below the broader [Operational Event Model](operational-event-model.md)
and expands the skill-facing parts of the
[Projection Subscription Roadmap](projection-subscription-roadmap.md).

## Problem Statement

Several browser-facing skills currently own their own projection runtime:

- they declare or embed fallback projection paths
- they debounce runtime events locally
- they keep local fingerprint maps
- they open local thread pools to bridge sync handlers into async Yjs writes
- they route `webio.stream.*` requests by hand
- they choose when a full snapshot should be rebuilt

This works as a stabilizer, but it creates a bad default for new skills.
The safe path should be the easiest path.

Recent memory-pressure investigations also showed that repeated large Yjs
materialization is a durable runtime risk even when every individual write
looks bounded. The core should therefore provide the rails for selective,
fingerprinted, demand-aware projection refresh.

## Target Shape

Skills describe projection and stream surfaces declaratively. The SDK/core
runtime owns the mechanics:

- per-webspace refresh selection
- event-to-dirty-section routing
- debounce and single-flight refresh execution
- payload fingerprinting before Yjs writes
- unchanged-payload suppression
- stream receiver tracking
- stream payload fingerprinting and rate limits
- Yjs pressure policy and diagnostic attribution
- lifecycle state for pending, ready, stale, throttled, and error

Skill code should focus on building semantic section payloads and performing
domain actions. It should not open-code projection scheduling.

## Core Concepts

### Projection Slot

A projection slot is a named, compact, browser-facing materialization unit.

Example shape:

```python
ProjectionSlot(
    name="browsers.summary",
    yjs_path="data/browsers/summary",
    build=build_browser_summary,
    events=["browser.session.changed", "device.registered"],
    scope="webspace",
    audience="shared",
)
```

The SDK owns:

- resolving the effective webspace set
- calculating a stable payload fingerprint
- skipping unchanged Yjs writes
- applying pressure limits
- publishing projection lifecycle diagnostics

Direct `ctx_subnet.set_async(...)` remains available for low-level escape
hatches, but skill-owned browser projections should prefer this API.

### Stream Receiver

A stream receiver is an active, volatile consumer. It is appropriate for detail
panels, logs, recent events, large diagnostics, and live operational state that
should not be stored in Yjs by default.

Example shape:

```python
streams.register("infrastate.logs.recent", build_recent_logs)
streams.register("infrastate.operation.detail", build_operation_detail)
```

The SDK owns:

- active receiver registration per webspace
- snapshot request handling
- subscription changed handling
- duplicate payload suppression
- rate limiting and pressure diagnostics

Stream request handling must not require a full skill snapshot unless the
registered receiver explicitly asks for one.

### Dirty Section Router

The dirty router maps runtime/domain/platform events to the smallest semantic
sections that need refresh.

Example shape:

```python
router.on("operations.*").dirty("operations.active")
router.on("browser.session.changed").dirty("runtime.status", "summary")
router.on("skills.registry.changed").dirty("marketplace.summary")
```

The router is not a replacement for the event bus. It is the skill-facing
projection invalidation contract above it.

### Section Cache

The SDK should provide bounded section caches keyed by:

- skill
- section
- webspace
- optional node scope

Caches must support TTL, explicit invalidation, size limits, and pressure
diagnostics. Full monolithic snapshot caches should be treated as migration
artifacts, not the target.

## Required SDK Surface

The first usable SDK slice should expose:

- `ProjectionSlot`
- `ProjectionRuntime`
- `StreamRuntime`
- `DirtyRouter`
- `SectionCache`
- `ProjectionContext`
- `set_projection_if_changed(...)`
- `refresh_dirty(...)`
- `publish_stream_snapshot(...)`

Suggested usage:

```python
projection_runtime = ProjectionRuntime(
    skill_id="browsers_skill",
    projections=[
        ProjectionSlot("browsers.summary", "data/browsers/summary", build_summary),
        ProjectionSlot("browsers.devices", "data/browsers/devices", build_devices),
    ],
    streams=[
        StreamReceiver("browsers.summary", build_summary_stream),
    ],
    router=DirtyRouter()
        .on("sys.ready").dirty("browsers.summary", "browsers.devices")
        .on("browser.session.changed").dirty("browsers.summary", "browsers.devices"),
)
```

The exact API names can change during implementation, but the ownership split
must not.

## Invariants

- Yjs writes must be fingerprinted before writing unless explicitly marked as a
  low-level repair operation.
- A forced refresh may force recomputation, but it must not force an identical
  Yjs write.
- Stream snapshots are not durable Yjs projections.
- A stream request for one receiver must not rebuild every skill section by
  default.
- One webspace must not cause projection churn in unrelated webspaces.
- Heavy diagnostics, logs, large histories, and detail panels are stream-only
  unless a compact summary projection is explicitly declared.
- Projection lifecycle and pressure decisions must be observable.
- Skill-local thread pools and event-loop bridges are transition shims.

## Reference Skills

### `browsers_skill`

`browsers_skill` is the first compatibility-era reference for the desired
shape:

- small projection slots instead of one `data/browsers` object
- per-slot fingerprint suppression
- separated stream snapshot path
- single-flight background refresh
- limited fanout for bootstrap events

The SDK should extract and generalize this pattern, then migrate the skill back
onto the shared helper layer.

### `infrastate_skill`

`infrastate_skill` should be the first heavy operational-skill migration after
the SDK slice exists.

The first migration pass replaces the single `infrastate.snapshot` durable
projection with section slots while keeping the existing WebUI paths stable.
Durable Yjs sections are now declared and written as:

- `infrastate.summary`
- `infrastate.actions`
- `infrastate.nodes`
- `infrastate.node_editor`
- `infrastate.operations.active`
- `infrastate.core_actions`
- `infrastate.yjs_actions`
- `infrastate.update_actions`
- `infrastate.core_update_diag_actions`
- `infrastate.ui_state`
- `infrastate.fallback`
- `infrastate.errors`
- `infrastate.projection_diag`

Heavy logs, event history, core-update diagnostics, marketplace rows, build
details, runtime channel details, skill/scenario tables, and per-item details
are stream/request-only surfaces. `infrastate.operations.active` remains both a
compact durable slot for reconnect recovery and an eager stream for live UI
updates. The current stream migration uses direct builders for operations,
events, installed skills/scenarios, and marketplace tables; remaining complex
diagnostic receivers still fall back to the bounded cached snapshot until their
section builders are split out.

## Implementation Checklist

### 0. Architecture Lock

- [x] `sdk.architecture_doc`: publish this target architecture
- [x] `sdk.boundary_docs_linked`: link this document from adjacent projection
  and event-model roadmaps
- [ ] `sdk.current_shims_inventory`: list skill-local projection shims that the
  SDK must replace

### 1. Minimal Core Primitive

- [x] `sdk.projection_slot_type`: add a typed projection slot declaration
- [x] `sdk.stable_fingerprint`: add a shared stable JSON fingerprint helper
- [x] `sdk.set_if_changed`: add the shared Yjs set-if-changed path
- [x] `sdk.per_webspace_fingerprint_state`: keep fingerprints per skill,
  webspace, and slot
- [x] `sdk.pending_refresh_state`: keep pending refresh state per skill,
  webspace, and slot
- [x] `sdk.force_recompute_skip_identical`: define force refresh as recompute,
  not unconditional write
- [x] `sdk.projection_diagnostics`: expose applied, skipped, throttled,
  errored, and pressure-blocked counters

### 2. Stream Runtime

- [x] `sdk.stream_receiver_type`: add a typed stream receiver declaration
- [x] `sdk.snapshot_request_handler`: centralize
  `webio.stream.snapshot.requested` handling
- [x] `sdk.subscription_changed_handler`: centralize
  `webio.stream.subscription.changed` handling
- [x] `sdk.stream_fingerprint`: suppress duplicate stream payloads per receiver
- [x] `sdk.stream_rate_limit`: add shared per-receiver rate limits
- [x] `sdk.active_receiver_registry`: expose active receiver state for
  diagnostics
- [x] `sdk.stream_runtime_reset`: reset stream fingerprints/demand by
  webspace/receiver during webspace reloads

### 3. Dirty Router and Section Cache

- [x] `sdk.dirty_router`: map event topics to sections/projection slots
- [x] `sdk.single_flight_refresh`: coalesce refresh work per skill/webspace
- [x] `sdk.section_cache`: add bounded section cache with TTL and invalidation
- [x] `sdk.projection_slot_rate_limit`: support per-slot projection write
  throttling and diagnostics
- [ ] `sdk.event_pressure_counters`: preserve coalesced/superseded/dropped
  evidence
- [ ] `sdk.restore_active_demand`: restore active projection/stream demand on
  startup where available

### 4. `browsers_skill` Migration

- [x] `browsers.use_sdk_slots`: migrate current projection entries onto SDK
  `ProjectionSlot`
- [ ] `browsers.remove_local_executor`: remove local projection executor once
  sync/async bridge is shared
- [x] `browsers.remove_local_fingerprints`: remove local fingerprint maps
- [x] `browsers.tests_unchanged_skip`: test duplicate refresh does not write
  unchanged Yjs payloads
- [ ] `browsers.reference_doc`: document the migrated skill as the minimal
  reference implementation

### 5. `infrastate_skill` Migration

- [x] `infrastate.section_inventory`: inventory current snapshot sections and
  classify each as projection, stream-only, action, or internal cache
- [x] `infrastate.slot_map`: replace `infrastate.snapshot` with section slots
  while keeping compatibility consumers working
- [ ] `infrastate.event_routing`: route operations, browser, registry, update,
  Yjs, and webspace events to minimal dirty sections
- [x] `infrastate.stream_only_heavy`: move logs, histories, diagnostics, and
  details to stream/request-only surfaces
- [ ] `infrastate.no_full_snapshot_for_stream`: ensure one stream receiver does
  not build the full operational snapshot
- [x] `infrastate.tests_section_slot_writes`: test durable refresh writes
  section slots instead of the legacy monolithic snapshot
- [ ] `infrastate.tests_minimal_writes`: test event-specific refresh writes only
  the affected slots
- [ ] `infrastate.memory_soak`: run stand soak and verify Yjs load marks and RSS
  do not grow steadily under idle/normal UI use

### 6. Cleanup and Policy

- [ ] `cleanup.deprecate_projection_shims`: mark per-skill projection
  executors/fingerprint maps as deprecated
- [ ] `cleanup.lint_direct_projection_writes`: add lint or diagnostic warnings
  for direct skill-owned browser projection writes
- [ ] `cleanup.remove_monolith_paths`: remove legacy monolithic projection paths
  after client compatibility is migrated
- [ ] `cleanup.operator_dashboard`: expose top noisy slots, skipped writes,
  pressure blocks, and active stream receivers

## Acceptance Criteria

The SDK is ready for broad rollout when:

- `browsers_skill` uses the SDK without losing current behavior
- `infrastate_skill` no longer publishes one large default Yjs snapshot
- unchanged refreshes do not write Yjs payloads
- stream requests build only the requested receiver payload by default
- diagnostics attribute projection pressure to skill, webspace, slot, and
  reason
- stand soak shows no steady RSS growth from idle projection refreshes
