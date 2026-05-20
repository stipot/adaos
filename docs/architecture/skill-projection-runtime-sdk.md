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

Skills describe projection and stream surfaces declaratively. The skill author
chooses which data belongs in Yjs, which data belongs in streams, and which
data should stay behind tools/details. The SDK/core runtime owns the mechanics:

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

The SDK must not silently reroute a skill's data between Yjs and streams.
Runtime guardrails may warn, throttle, block, quarantine, and log pressure, but
route ownership remains a design-time skill responsibility.

## Pressure-Fixture Policy

Some existing skills are intentionally left noisy during the core guard rollout.
They are not the target behavior, but they are valuable pressure fixtures for
proving that the shared runtime survives inefficient skill code.

Until the guard-observability acceptance checks pass, `browsers_skill`,
`infrastate_skill`, `infrascope_skill`, and similarly chatty operational skills
should be treated as load generators before they are treated as optimization
targets. The core must:

- keep the runtime alive and the primary desktop recoverable
- attribute pressure to skill, route, receiver/path, webspace, and observed
  owner bucket
- warn, throttle, block, or quarantine with explicit TTL and retry context
- keep compact operator status available when allowed by policy
- preserve raw evidence in logs, guard cards, streams, or 360log-style
  diagnostics

Do not hide a core weakness by prematurely quieting the skill. Skill migration
starts after the core can prove it protected itself and logged enough evidence
to send the skill back for design-time route correction.

YWS connectivity is part of that proof. The guard should unload excess input
without making a healthy channel flap: duplicate reconnects from the same
client provider attempt may replace the stale socket, but distinct provider
attempts for one browser identity get a small bounded overlap before the oldest
socket is trimmed. Diagnostics must expose both the server `yws_attempt_id` and
the browser `client_yws_attempt_id` so a red/green indicator can be attributed
to either real channel loss, duplicate providers, or skill pressure instead of
being inferred from fallback HTTP snapshots.

Root-routed YWS also has a separate control-plane handshake. `open_ack` is not
skill data and must not wait behind heavy Yjs frames, fallback snapshots, or
stream payloads. The hub route treats `open_ack`, `close`, and `http_resp` as
control replies, fast-drains them through the NATS/WS bridge, and the root
proxy uses a bounded retry window before closing the browser. This preserves
the YWS channel contract: pressure may delay or drop data frames according to
policy, but it should not make the status/control handshake flap red when the
local room is otherwise healthy.

## Core Concepts

### Projection Slot

A projection slot is a named, compact, browser-facing materialization unit for
reconnect-stable bootstrap/control state. It should not be used for hot
diagnostics, event tails, large tables, or routine telemetry variables.

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

Stream receivers are not durable projections. They need bounded payloads,
initial state, snapshot-on-subscribe, duplicate/stale suppression, and explicit
rate limits.

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
- `publish_stream_variable(...)` for bounded replace-mode current-state streams
- `publish_stream_snapshot(...)`
- `StatusCard`, `StatusRegistry`, `publish_status(...)`,
  `publish_status_many(...)`, and `publish_status_stream(...)` for small
  operator status summaries that point to streams/tools for details

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
- Status cards are not data transport. They may carry compact state,
  freshness, guard context, and references, but live rows, inventories,
  operation tables, logs, and diagnostic tails must stay in declared Yjs,
  stream, or details routes.
- `statusPlane` and thin reliability summaries are bootstrap/migration indexes,
  not legal `data_routes[*].route` values.
- Stream snapshots are not durable Yjs projections.
- A stream request for one receiver must not rebuild every skill section by
  default.
- One webspace must not cause projection churn in unrelated webspaces.
- Heavy diagnostics, logs, large histories, and detail panels are stream-only
  unless a compact summary projection is explicitly declared.
- Hot transport/session events must be routed through explicit debounce or
  budget rules before they affect operator-facing status.
- Raw hot-event evidence and smoothed operator state should be separate
  sections or receivers.
- Subscription flaps must not cause unbounded Yjs writes.
- Projection lifecycle and pressure decisions must be observable.
- Skill-local thread pools and event-loop bridges are transition shims.
- A completed core slot transition must leave one managed active runtime
  listener plus the root supervisor. Inactive slot runtimes that survive a
  warm switch or root-promotion restart are pressure leaks, not standby
  capacity, and the supervisor must stop them with explicit logs.

## Control-Plane HTTP Fallbacks

Root-routed HTTP reads and tool calls are recovery and diagnostic paths, not a
replacement for declared Yjs or stream data routes. A browser may still receive
`/api/node/infrastate/snapshot` while its local Yjs provider is red; that is a
fallback read path and must stay compact/read-only unless a caller explicitly
requests projection. It should not be used as a hidden live-state channel.

The same boundary applies to `/api/tools/call`: it can serve details, actions,
and fallback reads, but route ownership still belongs to the skill's declared
Yjs, stream, or detail route. The hub-route local hop must choose the active
runtime process first, give `tools/call` a timeout budget compatible with Root's
outer budget, and avoid retrying a mutating/read-timed-out request against a
different slot port.

The YWS session guard follows the same rule. Pressure can reject invalid,
unauthorized, or globally over-limit connections, but reconnect storms are
treated as diagnostic pressure and replacement/backoff input rather than a
reason to quarantine a browser or the whole webspace for the full cooldown. A
red Yjs indicator plus a successful HTTP snapshot therefore means "fallback
path is alive, realtime path still needs YWS diagnostics", not "Yjs was
replaced by HTTP".

Control-plane status events have one extra rule: they must remain bounded, but
they must not disappear behind a noisy skill's Yjs-owner quarantine. Core update
status, hub update status, member link up/down, member snapshot change, and
member update result subscriptions may pass through a tiny hot-event budget even
when the owner guard is blocking normal skill work. Hot browser/session refresh
events are not in this exception; they stay governed by the normal guard and
their own debounce/budget design.

YWS gateway `browser.session.changed` publication is the first source-side
application of that design. The gateway updates the browser-session registry on
every accepted close/open decision, but it does not publish every reconnect
micro-transition into the EventBus. Events are admitted through a
per-webspace/per-device debounce/window budget; suppressed updates keep the
latest payload and flush a coalesced event after the budget opens. The YWS
channel itself remains open, and guard/auth/active-limit denials can still be
forced through as policy evidence.

This is a compatibility valve, not the final ownership model. The target split
is logical-plane separation:

- `status/control communication`: system-owned, compact, durable enough for
  operator truth such as update epoch, active slot, member state, guard state,
  and retry/TTL context
- `skill communication`: skill-owned subscriptions, tools, Yjs projections, and
  stream receivers that may be throttled or quarantined without hiding the
  status/control truth

After that split, operational skills can render status/control facts, but they
do not own their delivery.

## Browser And YWS Diagnostics

The browser runtime keeps the full `adaos.runtime_debug.logs.v1` ring in
localStorage, but node-side investigation must not depend on opening DevTools.
The client therefore exports bounded diagnostic evidence through
`/api/node/ui/diagnostics`:

- notable runtime-debug events: YJS provider state, close/error, materialization
  changes, control-WS state, slow/error HTTP calls, and skill call failures
- `runtime_debug.cursor` heartbeat: latest retained seq, exported seq, estimated
  dropped events, last YJS provider/sync/close state, last materialization
  state, last control-WS state, last computed YJS indicator state/reason, and
  compact supervisor transition/probe/suppression breadcrumbs
- browser correlation fields: `device_id`, browser family, OS, form factor,
  runtime-debug `session_id`/`tab_id`, and the current client YWS provider
  attempt id

This diagnostic export is not a data route. It does not replace Yjs, stream
variables, tool details, or status cards. Its only job is to make browser-side
transport truth visible from the node when investigating red/green flicker or a
stale widget.

The visible YJS indicator should be computed from the local browser Yjs
provider/document first: if the provider is connected, the provider has synced,
and the desktop document materialized, the indicator may be green even while a
status/reliability projection still reports stale `state-sync` metadata. The
stale status fact must remain visible in the indicator title and semantic/link
diagnostics, but it must not turn a healthy Yjs document into a false transport
failure. This keeps `status/control communication` separate from the
skill/status projections that can be delayed by pressure guards.

YWS server diagnostics must carry the same correlation boundary. Every accepted
or rejected YWS attempt receives a `yws_attempt_id`; the id appears in
`browser.session.changed`, server open/close/guard logs, and
`transport.attempts` in the YWS runtime snapshot. The browser also sends a
`client_yws_attempt_id` for the current provider instance; server logs and
browser runtime-debug events carry that id so a red Mobile/Opera tab can be
matched to the corresponding server-side attempts without relying on loose
timestamps. Browser close code/reason and server-side guard decisions can then
be stitched by these ids instead of by time alone.

Room bootstrap has its own nested correlation id. When a YWS attempt waits for
room creation, the gateway records a `yroom-*` bootstrap attempt with the
originating `yws_attempt_id`, current step, duration, terminal state, error, and
wait-timeout counters. Reliability exposes this under the selected webspace and
`state-sync.bootstrap`; a red realtime indicator can therefore be traced to a
specific websocket attempt and room-bootstrap path without treating HTTP
snapshots as an alternate data route.

Root-route diagnostics must keep the handshake visible as well: `open_ack`
publish/flush failures, slow control flushes, and browser close reasons such as
`hub_open_ack_timeout` are status-plane evidence, not Yjs document evidence.
They are checked together with `yroom-*` bootstrap state before declaring the
core ready for skill optimization.

Status-plane freshness also has to be semantic. Thin reliability summaries may
include timestamps, boundary observations, and cache metrics for operators, but
their ETag/`If-None-Match` contract should change only when the compact state a
browser would render changes. Repeated unchanged status-card publishes must
therefore return `304` even if local diagnostic timestamps or observed byte
high-water marks moved.

Core-update status follows the same reconciliation rule. A supervisor may reject
a terminal success while the active slot still points at the old target, but if
the slot later catches up and the active manifest matches the requested target,
the failed mismatch attempt should be self-healed to a completed recovered
attempt. Otherwise operator status becomes stale evidence and can be mistaken
for a YJS or stream regression.

## Reference Skills

### `browsers_skill`

`browsers_skill` is the first compatibility-era reference for the desired
shape, but during the current guard rollout it also remains a deliberate
pressure fixture. Its `browser.session.changed` and device/session refresh paths
are useful for proving quarantine, route attribution, and hot-event budgeting
before the skill is fully optimized.

- small projection slots instead of one `data/browsers` object
- per-slot fingerprint suppression
- separated stream snapshot path
- single-flight background refresh
- limited fanout for bootstrap events

The SDK should extract and generalize this pattern, then migrate the skill back
onto the shared helper layer.

### `infrastate_skill`

`infrastate_skill` should be the first heavy operational-skill migration after
the SDK slice exists and the core guard evidence is good enough to trust. Until
then, its broad runtime/update/browser event subscriptions remain a useful
pressure source for validating Yjs owner guards, stream guards, status cards,
and quarantine diagnostics.

The earlier migration step split one large durable `infrastate.snapshot` into
multiple Yjs section slots. That was useful as a compatibility stabilizer, but
it is no longer the target shape. The target is stream-first:

- Yjs keeps only minimal reconnect-stable bootstrap/control state, such as
  current readiness, selected node, last refresh marker, degraded/error badge,
  and a compact subscription summary.
- Operator-facing variables are stream receivers: summary rows, action lists,
  nodes, active operations, build state, runtime channels, marketplace rows,
  skills/scenarios, Yjs load marks, and recent events.
- Details and large evidence stay behind detail tools, requested stream
  snapshots, disk snapshots, or 360log.
- Hot inputs such as `browser.session.changed`, `device.registered`,
  `webrtc.peer.state.changed`, and YWS guard/open/close events must have
  explicit debounce/budget behavior. Raw evidence remains available in
  diagnostics streams while operator-facing status is smoothed.

This keeps the operational skill useful after reconnect while preventing its
diagnostic surface from becoming a primary Yjs pressure source.

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
- [x] `sdk.event_pressure_counters`: preserve coalesced/superseded/dropped
  evidence
- [ ] `sdk.restore_active_demand`: restore active projection/stream demand on
  startup where available

### 3a. Status Plane

- [x] `status.card_contract`: define `StatusCard` with canonical status,
  severity, TTL, fingerprint, version, `details_ref`, `route`, and `guard_ref`
- [x] `status.registry_first_slice`: add in-memory registry with fingerprint
  dedupe, version increments, stale diagnostics, and changed events
- [x] `status.sdk_publish`: add `adaos.sdk.status.publish_status`,
  `publish_status_many`, and `publish_status_stream`
- [x] `status.bootstrap_registry`: wire the registry into normal API/server
  bootstrap so thin summaries can read it without test setup
- [x] `status.guard_cards`: project compact Yjs/stream guard degraded cards
  into `statusPlane` through the shared card shape
- [x] `status.summary_endpoint`: expose registry-backed thin status snapshots
  through `/api/node/reliability/summary` or its successor
- [x] `status.summary_etag`: support `mode=thin` plus ETag/`If-None-Match`
  for unchanged polling responses
- [x] `status.summary_semantic_etag`: keep volatile diagnostics such as
  `lastChangedAt` and observed high-water byte marks out of the thin-summary
  ETag so unchanged cards can reuse `304`
- [x] `status.summary_client_cache`: use thin-summary ETags in the Angular
  communication runtime before requesting full compatibility details
- [x] `status.summary_metrics`: expose summary mode, response bytes, cache
  hits, and `304` reuse for soak verification
- [x] `status.acceptance_metrics`: extend summary metrics with compact
  status-registry diagnostics, Yjs owner-guard counters/quarantine context,
  stream guard counters, stream-control snapshot/coalescing counters, and
  merged per-receiver rows for pressure soaks
- [x] `status.acceptance_metrics_cli`: expose the same counters through
  `adaos node reliability-metrics` for operator and soak-note capture
- [x] `status.yjs_guard_acceptance`: include attempted/allowed/blocked/
  throttled counts, active quarantine, TTL/retry, denied count, and last
  guarded path/tool in the low-cost acceptance surface
- [x] `status.critical_control_plane_budget`: allow bounded critical
  update/member status subscriptions to pass owner-guard starvation while
  normal hot refresh events remain guarded
- [x] `status.browser_yjs_signal_export`: export the actual client-computed
  YJS indicator signal in runtime-debug cursor diagnostics
- [x] `status.browser_yjs_local_doc_truth`: keep the visible YJS indicator green
  when the local browser provider has synced and materialized the document even
  if a compact status/reliability projection is stale; expose both facts in the
  diagnostic title/cursor
- [x] `update.active_slot_target_validation`: reject terminal update success
  when the active slot manifest does not match the requested target version,
  so acceptance soaks cannot accidentally measure an old runtime
- [x] `update.active_slot_target_recovery`: self-heal a failed mismatch attempt
  after the active slot manifest catches up to the terminal target status, so
  public update status does not preserve stale failure evidence
- [x] `update.orphan_slot_runtime_cleanup`: after a terminal or idle update
  state, scan slot listeners and stop untracked non-active AdaOS runtimes so
  warm-switch/root-promotion leftovers cannot double memory, Yjs, or stream
  load.
- [x] `status.hot_event_budget`: add a shared debounce/window budget helper
  for hot event-to-status paths before skill-specific migrations
- [x] `status.compact_boundary_diagnostics`: expose max card bytes, observed
  max card bytes, oversized card count, and last oversized card identity so
  accidental use of status cards as payload transport is visible

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
- core updates leave a single active slot runtime after promotion, with orphan
  cleanup and root-promotion status visible in operator diagnostics
- stand soak shows no steady RSS growth from idle projection refreshes
