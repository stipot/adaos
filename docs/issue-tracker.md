# Issue Tracker

This document is the single living issue tracker for active AdaOS stabilization
and delivery work.

Use sections as goals. Each goal owns task groups that can be extended,
executed, and closed without creating a separate tracker document.

## Device Identity and Access Usability

### Goal

Make node/browser settings explain identity, editable human names, lifetime, and
detach behavior without leaking transport implementation details into the
operator UI.

### Current Status

Snapshot date: 2026-05-14.

Local debugging found that the local hub can still be addressed by legacy
`member:<local_node_id>` refs in desktop settings. That is an addressing alias,
not a true member identity. Device access must normalize this alias to
`hub:<subnet_id>` so policies, name storage, and disabled hub-only actions are
derived from `.adaos/node.yaml`.

### Tasks

#### DIAU-001: Normalize local hub identity in settings flows

Status: in progress.

Actions:

- [x] Treat `member:<local_node_id>` as `hub:<subnet_id>` when local node config
  says `role: hub`.
- [x] Keep hub display names editable through `.adaos/node.yaml:
  node.node_names`.
- [x] Keep hub lifetime and detach actions disabled with explicit reasons.
- [ ] Verify live desktop settings now shows `kind=hub` and `ID hub:<subnet_id>`
  after hub restart/client reload.

Human verification:

- Open local node Settings and confirm ID is `hub:<subnet_id>`, kind is `hub`,
  Save name enables after editing, and lifetime/detach remain disabled with
  policy hints.

#### DIAU-002: Harden settings modal controls

Status: in progress.

Actions:

- [x] Use native modal controls with pointer/click duplicate suppression for
  Settings actions.
- [x] Keep Close, Apps, Marketplace, Hide, Save name, Lifetime, and Detach
  responsive after editing text fields.
- [ ] Add broader browser regression coverage once the modal E2E harness is
  available.

Human verification:

- Open Settings, edit Name, click Close.
- Open Settings, click Hide, then Close.
- Open Settings, edit Name, then Apps/Marketplace.

#### DIAU-003: Clarify browser settings identity

Status: in progress.

Actions:

- [x] Show immutable browser Device ID separately from editable Browser name.
- [x] Add explicit Save name flow for browser-name edits.
- [x] Document that the name is hub-side access policy state and is not written
  back to the remote browser.
- [ ] Implement immediate remote browser logout on Detach, or add a control-plane
  event if the current runtime has no safe rail for it.

Human verification:

- Open `[node] Browser settings`, confirm Device ID is visible, edit Browser
  name, Save name, then refresh Browsers and confirm the new label remains.

#### DIAU-004: Align terminology around subnet endpoints

Status: open.

Actions:

- [x] Prefer `subnet endpoint` for software participants attached to a subnet:
  browser, member node, LLM agent, IoT bridge, or future headless client.
- [x] Keep `device` for the operator-facing managed/trusted endpoint class.
- [x] Keep `client` as a policy class for temporary browser access, not as the
  general architectural term.
- [ ] Audit UI copy and docs for places where `device`, `browser`, `member`,
  and `client` are still conflated.

## UI Runtime Diagnostics and Skill-Scoped Logs

### Goal

Keep browser-side UI failures, in-process skill runtime logs, and service-skill
logs attached to the skill being developed so LLM-assisted debugging can stay in
the correct entity context.

### Current Status

Snapshot date: 2026-05-13.

Implemented baseline is documented in
`docs/architecture/ui-runtime-diagnostics.md`.

### Tasks

#### UILOG-001: Complete skill-scoped diagnostics pipeline

Status: in progress.

Actions:

- [x] Add explicit skill log paths to `CurrentSkill` and `PathProvider`.
- [x] Route in-process skill-context `adaos.*` logs to
  `service.<skill>.runtime.log` instead of platform-wide `adaos.log`.
- [x] Send dev-mode browser UI diagnostics to the node.
- [x] Persist browser UI diagnostics to `service.<skill>.ui_runtime.log`.
- [x] Extend MCP `get_skill_logs(skill=...)` to include
  `service.<skill>.*.log`.
- [ ] Add widget-level ownership metadata for renderer failures that are not
  modal-owned.
- [ ] Add a typed ABI schema for UI diagnostic payloads.
- [ ] Add rate limiting and duplicate suppression for repeated renderer errors.
- [ ] Feed skill logs into the future LLM skill-debugging MCP workflow.

## Browser Startup and Progressive Hydration

### Goal

Make the browser desktop usable immediately after login by rendering from
available local state first, while live Yjs sync and materialization catch up in
the background.

### Current Status

Snapshot date: 2026-05-13.

The browser client now keeps a read-only last-good desktop render snapshot in
localStorage. `YDocService` uses it only as a fallback for missing `ui`, `data`,
and `registry` reads; live Yjs branches always take precedence, and Yjs
IndexedDB persistence remains opt-in. `DesktopRendererComponent` now binds the
desktop view before `initFromHub()` resolves, so login no longer blocks first
paint on first Yjs sync/materialization. Runtime data-source 401/403 responses
that arrive while Yjs bootstrap is still pending are now treated as transient
load failures instead of forcing a page reload, preventing startup reload loops
when cached first paint races ahead of live runtime authorization.

### Tasks

#### BSPH-001: Render desktop before Yjs first-sync completion

Status: in progress.

Actions:

- [x] Add a read-only last-good render snapshot separate from Yjs persistence.
- [x] Save snapshots only from live `interactive` or `ready` materialization.
- [x] Let desktop schema/UI reads fall back to the snapshot while live branches
  are absent.
- [x] Start desktop rendering before `YDocService.initFromHub()` resolves.
- [x] Defer page reload on transient runtime data-source unauthorized responses
  while Yjs bootstrap is still pending.
- [x] Add focused Angular tests for snapshot fallback and non-blocking desktop
  startup.
- [ ] Add browser-visible "syncing latest state" affordance for cached first
  paint, without hiding normal link/Yjs diagnostics.
- [ ] Add an end-to-end timing assertion for login-to-first-desktop-paint once
  the browser E2E harness is available.

## Operational Event Model Roadmap Consolidation

### Goal

Keep event, projection, browser/runtime, platform-emitter, and heavy-skill
migration work on one master delivery track so the project can execute the
target event model without drifting between parallel roadmaps.

### Current Status

Snapshot date: 2026-05-15.

The target architecture remains valid, but the documentation had two related
roadmaps that could be read as competing priority sources:

- `docs/architecture/operational-event-model-roadmap.md`
- `docs/architecture/projection-subscription-roadmap.md`

The master roadmap is now explicitly authoritative.  The projection roadmap is
now a subordinate detailed checklist for projection record, browser
subscription, Yjs adapter, shared dispatcher, Infrascope, and rollout tasks.

The next execution slice is intentionally contract-first:

1. freeze a minimal shared event envelope
2. align named-entity and status-card ABI work with the event model
3. lock projection record and client subscription shapes
4. implement the client subscription runtime
5. add a shared demanded-projection dispatcher
6. validate platform emitters before heavy skill migration

### Tasks

#### OEM-001: Consolidate roadmap authority

Status: in progress.

Actions:

- [x] Mark `Operational Event Model Roadmap` as the single authoritative
  delivery sequence.
- [x] Recast `Projection Subscription Roadmap` as a subordinate detail
  checklist instead of a separate priority track.
- [x] Record the 2026-05-15 implementation boundary in the target event model:
  eventbus guardrails, named-entity ABI, node-aware compatibility surfaces,
  and remaining shared ABI gaps.
- [x] Add a top-level reference execution plan with coverage gates, contract
  shapes, review checklist, and completion definition.
- [x] Update roadmap progress for named-entity contract/runtime ABI and
  eventbus hot-topic guardrails.
- [x] Define the minimal shared event envelope and compatibility rules for
  existing `Event(type, payload, source, ts)` producers.
- [x] Bind `STATUS-*` work to the platform-emitter phase so status cards do not
  become a separate monitoring-only architecture.

#### OEM-002: Lock projection ABI before client/runtime migration

Status: helper ABI, server-side browser demand runtime, and base shared dispatcher implemented; projection handlers remain planned.

Actions:

- [x] Define canonical projection record fields: `status`, `data`, `meta`,
  `error`, lifecycle timestamps, version/fingerprint, access metadata, and
  source ownership.
- [x] Define browser-written subscription records for page, widget, modal, and
  pinned panel consumers.
- [ ] Define compatibility rules for legacy Yjs branches during migration.
- [ ] Use `registry.named_entities` and planned status cards as reference
  examples before Infrascope migration.
- [x] Add base demanded-projection dispatcher with per-webspace selection,
  lifecycle state, pressure counters, and inspection API.

#### OEM-003: Keep heavy-skill pilots behind platform-emitter validation

Status: planned.

Actions:

- [ ] Allow Infrascope inventory/tests that do not create a parallel projection
  ABI.
- [ ] Migrate status cards, notifications, diagnostics, or workspace-manager
  surfaces first through the shared projection contract.
- [ ] Start Infrascope split only after event envelope, projection ABI, client
  subscriptions, dispatcher, and at least one platform-emitter pilot are
  materially in place.

## Modal Projection and Runtime Recovery Integrity

### Goal

Keep desktop and modal data contracts explicit while recovering from missing
runtime projections. A widget that declares `kind: y` must render from Yjs; a
widget that declares `kind: stream` must render from stream data. Recovery may
request refresh/project work, but it must not silently substitute direct
skill/API payloads and hide broken projection paths.

### Current Status

Snapshot date: 2026-05-11.

Recent local debugging found several related issues:

- Modal data for `Subnet Environment`, `Infra Access`, `Infrastructure State`,
  and `Browsers` could appear fixed by direct client fallbacks while the real
  projection/materialization contract was still broken.
- The full Python suite currently has a collection-order hazard around test
  modules that stub `sys.modules["nats"]`; fixing that locally exposes a
  separate set of pre-existing runtime API expectation failures that need their
  own cleanup pass.
- Workspace skill changes are delivered through `adaos skill push`, while root
  git commits track client/core/tests; CI needs a clearer way to prove the two
  layers remain compatible.

### Tasks

#### MRI-001: Keep Yjs and stream data-source recovery contract-first

Status: in progress.

Actions:

- [x] Stop direct client fallback payloads from rendering Yjs modal data for
  the operational projections currently under debug.
- [x] Treat empty browser arrays as valid live Yjs data, not as missing data.
- [ ] Move the temporary client-side recovery registry toward declarative
  schema metadata such as `dataSource.recovery` / `projection.refresh`.
- [ ] Audit modal schemas and ensure each data source uses `kind: y` or
  `kind: stream` intentionally, with no implicit source swapping.

#### MRI-002: Make workspace skill publishing verifiable

Status: open.

Actions:

- [ ] Add a lightweight verification command or test fixture that confirms the
  pushed skill version used by tests contains the expected projection handlers.
- [ ] Document the expected workflow: edit workspace skill, run targeted tests,
  `adaos skill push <name> -m ...`, then commit root/client changes.
- [ ] Avoid root tests whose only passing implementation lives in ignored
  `.adaos/workspace` state unless the skill push/version is part of the test
  setup.

#### MRI-003: Restore full pytest suite health after nats test shadowing

Status: open.

Actions:

- [ ] Replace broad `sys.modules["nats"]` stubs with helpers that prefer the
  installed `nats-py` package and only stub when unavailable.
- [ ] After collection is stable, triage the currently exposed runtime API
  expectation failures separately from modal/projection work.
- [ ] Add a regression that `tests/test_nats_ws_transport.py` can import
  `nats.errors` regardless of test collection order.

#### MRI-004: Make weather provider behavior explicit

Status: in progress.

Actions:

- [x] Stop showing raw runtime i18n keys when a weather provider returns an
  error.
- [x] Migrate the legacy OpenWeatherMap endpoint to the no-key Open-Meteo path
  for local development fallback.
- [ ] Document provider selection and API-key behavior so `401` is actionable
  instead of looking like a modal rendering bug.

#### MRI-005: Keep scenario switching fast without hiding rebuild problems

Status: in progress.

Actions:

- [x] Move scenario-switch worker `data.webspaces` sync out of the ready path;
  fresh-doc rebuild remains explicit, while listing fanout is post-ready and
  coalesced.
- [x] Normalize nested/stringified `webspace_id` values before workspace index
  reads/writes, and dedupe malformed legacy rows from listing output.
- [ ] Investigate why `collect_inputs` dominates semantic rebuild time
  (~300-400ms locally) and make resolver input collection cheaper without
  weakening projection contracts.
- [ ] Investigate why fresh-doc switches naturally replace all effective
  branches; decide whether branch fingerprint reuse can be preserved safely
  without reintroducing stale Yjs state.

## Hub Memory Growth Under Snapshot and Webspace Fanout

### Goal

Prevent runaway hub memory growth during snapshot, webspace rebuild, and Yjs
fanout storms without hiding the underlying overload signal from operators,
skills, or core diagnostics.

Success means:

- A hub does not grow from a normal working set into multi-gigabyte RSS during
  a 10-minute snapshot/rebuild storm.
- `webio.stream.snapshot.requested` and
  `subnet.member.snapshot.changed` bursts are coalesced into bounded work.
- Route, Yjs, and eventbus backpressure enter degraded mode before memory
  runaway, while preserving causal diagnostics.
- Guardrails reduce amplification but do not suppress evidence needed to fix
  the originating skill or core hot path.
- Policy-triggered memory profiling always leaves an operator-visible reason,
  state transition, and artifact trail even when the live profile mode cannot
  be applied immediately.

### Current Status

Snapshot date: 2026-05-06.

Incident reference:

- Live hub: `ssh -i c:/Users/Zver/.ssh/adaos_linux_exp root@192.168.0.30`
- Subnet: `sn_92ffc943`
- Runtime: `rt-b-a-ff6605f0`
- Growth window: `2026-05-06 18:12:50 UTC` -> `18:22:21 UTC`
- RSS growth in best 10-minute window: about `100 MiB` -> `2.07 GiB`

Observed behavior:

- NATS bridge was connected normally at runtime start, so this incident was not
  driven by a root reconnect loop.
- The hot path was a local storm of `webio.stream.snapshot.requested`,
  `subnet.member.snapshot.changed`, multi-webspace semantic rebuilds, and
  `webio` / Yjs fanout.
- In the critical window the hub emitted repeated slow async handlers for
  `infrastate_skill`, `infrascope_skill`, and
  `webspace_runtime._on_subnet_member_snapshot_changed`.
- The route layer showed repeated starvation via `publish_slow`,
  `pending_data`, and `flush_slow`.
- Yjs pressure warnings showed repeated large update bursts during the same
  window.
- The current sampled-profile session `mem-78c3dab0` stayed stuck in
  `requested`, and supervisor repeatedly logged `failed to apply requested
  memory profile mode`, so memory guardrails detected the incident but did not
  capture a useful growth artifact.
- A concurrent skill bug also appeared in the hot path:
  `browsers_skill ... NameError: current_device_id is not defined`.

Working hypothesis:

- The primary cause is internal snapshot/fanout amplification, not external
  root traffic.
- The dominant amplification chain is:
  `snapshot.requested` -> `snapshot.changed` -> multi-webspace rebuild ->
  repeated `webio` / Yjs publish -> route starvation -> websocket reconnect /
  reattach -> another snapshot cycle.
- The memory plateau near `2 GiB` is consistent with a backlog-stuck runtime:
  allocations stop accelerating because useful processing has mostly stalled,
  not because retained memory was released.
- Guardrails must therefore be designed as observability-first reducers of
  amplification, not as opaque drops that erase the evidence needed to improve
  core and skills.

### Tasks

#### HMG-001: Coalesce snapshot storms before they become fanout storms

Status: in progress. Wave 1 landed in core and skills: duplicate stream
snapshot requests are debounced/coalesced, and repeated
`subnet.member.snapshot.changed` bursts now collapse into bounded rebuild
cycles.

Evidence:

- Dense bursts of `webio.stream.snapshot.requested source=events_ws`.
- Repeated `subnet.member.snapshot.requested` /
  `subnet.member.snapshot.changed` cycles during websocket reconnects.
- Slow async handlers clustered around snapshot handlers in
  `infrastate_skill` and `infrascope_skill`.

Actions:

- [ ] Add a single in-flight snapshot guard per `(stream, webspace, node,
  subscriber)` key.
- [x] Coalesce repeated `webio.stream.snapshot.requested` events into a dirty
  flag plus last-request metadata instead of spawning duplicate work.
- [x] Add debounce / batch windows for `subnet.member.snapshot.changed` so one
  flap burst produces one bounded rebuild cycle.
- [ ] Separate full snapshot paths from incremental refresh paths; reconnect
  and resubscribe must prefer bounded incremental bootstrap where possible.
- [x] Emit first-wave per-key counters for `requested`, `forced`, and
  `coalesced`; extend the same boundary with `executed`,
  `skipped_unchanged`, and `dropped_due_to_guardrail`.
- [x] Make coalescing observable in logs and telemetry so operators can still
  see the original incoming pressure and the amount of suppressed duplicate
  work.

#### HMG-002: Bound webspace rebuild amplification

Status: in progress. Wave 1 and Wave 7 landed in core: overlapping rebuild
triggers for the same `(node, webspace)` key now coalesce into one active
rebuild plus at most one dirty rerun, with preserved trigger reasons,
counters, and operator-visible rebuild request IDs carried through dirty /
delayed / rerun states.

Evidence:

- In the incident window the same snapshot wave rebuilt `desktop`, `default`,
  `test1`, and `test1-1` repeatedly.
- Semantic rebuild durations rose into hundreds of milliseconds and over a
  second for some spaces while new rebuild triggers were still arriving.

Actions:

- [x] Add first-wave per-webspace rebuild counters for `requested`,
  `scheduled`, `rerun`, `coalesced_running`, `coalesced_interval`, and
  `delayed`; extend with queue depth, newest generation, and oldest waiting
  age.
- [x] Skip or supersede stale rebuild requests when a newer request for the
  same key is already queued or executing.
- [x] Prevent one snapshot event from scheduling overlapping semantic rebuilds
  for the same webspace.
- [ ] Add a degraded rebuild mode that defers noncritical projections or
  secondary webspaces while the hub is in memory or route pressure.
- [x] Record which upstream event caused each rebuild so we can trace pressure
  back to a skill, browser, reconnect, or subnet state change.

#### HMG-003: Add route and Yjs guardrails that preserve root-cause visibility

Status: in progress. Wave 2 and Wave 5 landed in core: route starvation now
exposes a guardrail state with activation reasons, Yjs rooms publish reusable
pressure state, and noncritical `load_mark` / `events.recent` fanout now
downshifts under both Yjs pressure and route guardrail activation without
hiding the incoming pressure.

Evidence:

- `hub-route` starvation repeatedly reported `publish_slow`, `pending_data`,
  and `flush_slow`.
- Yjs owner-flow and `yroom pressure` warnings showed large update bursts in
  the same interval.

Actions:

- [x] Add first-wave degraded / pressure thresholds for route pending age/data
  and Yjs buffer, pending task, and update-size pressure; extend with explicit
  publish-latency and persist-backlog thresholds where still missing.
- [x] When a threshold is crossed, downshift the first noncritical stream
  paths: repeated `load_mark` and `events.recent` fanout are now suppressed
  under active Yjs pressure; extend the same policy to equivalent cosmetic
  fanout.
- [x] Preserve observability by logging both the original attempted work and
  the reduced emitted work through pressure-state transitions and suppression
  counters.
- [x] Export first-wave route metrics/state for pending age/data and guardrail
  activation; extend with pending messages, max flush latency, and suppressed
  publication totals.
- [x] Export Yjs metrics for update bytes, pending send/store tasks, replay
  bytes, and per-webspace pressure state; extend with persist queue depth where
  still missing.
- [x] Ensure every guardrail activation produces a structured reason record
  that points back to the triggering stream, webspace, skill, or event type.

#### HMG-004: Make eventbus and async backlog visible and bounded

Status: in progress. Wave 4 and Wave 5 landed in core: eventbus now bounds
selected hot-topic async fanout through per-topic worker queues, preserves
incoming / queued / dropped visibility, supersedes stale queued snapshot work,
and exposes richer backlog state for incident artifacts.

Evidence:

- The incident produced about 210 slow async handler warnings in one window.
- Current logs show slow handlers, but not the complete backlog shape or the
  amount of queued overlapping async work.

Actions:

- [x] Add first-wave live backlog snapshot data for eventbus pending async
  tasks plus per-topic and per-handler in-flight counts; extend with oldest
  pending task age and per-handler slow-count totals where still missing.
- [x] Bound selected hot-path async fanout with first-wave per-topic work
  queues instead of unlimited `create_task` growth; extend the same approach
  to more hot topics as incident evidence evolves.
- [x] Add per-topic and per-handler cancellation / supersede semantics for
  stale snapshot work.
- [x] Keep raw incoming-event counters visible even when bounded execution
  drops or coalesces work.
- [x] Add an operator-facing incident summary that names the top topics and
  handlers contributing to backlog growth.

#### HMG-005: Make memory incident capture reliable before the hub stalls

Status: in progress. Wave 3 and Wave 6 landed in supervisor: requested
memory-profile sessions now expire instead of hanging indefinitely, apply
failures persist structured context, supervisor writes local incident artifacts
with telemetry, operations, Yjs pressure, route diagnostics, rebuild pressure,
and eventbus backlog snapshots, and the artifact now includes a compact
operator-facing incident summary/headline.

Evidence:

- Supervisor detected the growth threshold but left session `mem-78c3dab0` in
  `requested`.
- Repeated `failed to apply requested memory profile mode` warnings prevented a
  useful memory artifact from being captured during the live incident.

Actions:

- [x] Fix the supervisor profile-mode transition so a triggered session cannot
  remain indefinitely in `requested`.
- [x] Persist a structured first-wave failure reason when automatic profile
  mode cannot be applied, including slot, runtime, requested mode, and the
  most recent blocking / apply-error context.
- [x] Add a fallback capture path that records growth context without
  requiring a full runtime restart; extend with allocator-level artifacts where
  available.
- [x] Tie memory incidents to the active operation and first-wave pressure
  context through telemetry, operation history, Yjs pressure, route
  diagnostics, and member snapshot rebuild pressure; extend with finer-grained
  snapshot/request counters where still missing.
- [x] Publish enough local-only artifacts to debug the next incident even if
  root publication is unavailable.

#### HMG-006: Fix skill-level amplifiers in snapshot and webio hot paths

Status: in progress. Wave 1 landed in skills: duplicate
`webio.stream.snapshot.requested` bursts are now debounced in
`infrastate_skill` and `infrascope_skill` before they can multiply into
repeated full publishes. Wave 8 hotfix extends the same policy for
`infrastate_skill`: noncritical streams are no longer eager-published on every
snapshot refresh, active pressure skips heavy detail snapshot construction, and
pressure-mode snapshot cache TTL expands to keep the hub responsive during a
burst. This is not the primary safety mechanism: the deliberately heavy
`infrastate` path remains a useful crash-test for kernel-level containment, and
the owner-quarantine work is tracked under HMG-007.

Evidence:

- The heaviest repeated slow handlers in the incident were
  `infrastate_skill.on_webio_stream_snapshot_requested` and
  `infrascope_skill.on_webio_stream_snapshot_requested`.
- A concurrent `browsers_skill` background task failed with
  `NameError: current_device_id is not defined`.

Actions:

- [x] Refactor `infrastate_skill` snapshot publishing to avoid bursty repeated
  republish of unchanged payloads.
- [x] Refactor `infrascope_skill` snapshot publishing to prefer cached or diff
  output when the source generation did not materially change.
- [ ] Ensure skill snapshot handlers are idempotent and generation-aware.
- [x] Add first-wave suppression for noncritical skill-triggered
  `webio.stream.*` fanout under active Yjs pressure; extend the same policy to
  degraded route and additional cosmetic receivers.
- [x] Extend `infrastate_skill` suppression from `events.recent` to all
  noncritical diagnostic/detail receivers while preserving `operations.active`
  as the small eager status stream.
- [x] Avoid constructing a full `infrastate` snapshot for detail stream requests
  when Yjs/route guardrails are already active; record suppression counters
  instead of hiding the dropped work.
- [x] Increase `infrastate` snapshot cache TTL under active primary-doc pressure
  so repeated browser refreshes and member snapshot flaps reuse bounded work.
- [ ] Fix the `browsers_skill` `current_device_id` bug and ensure background
  snapshot tasks fail noisily but safely, without leaving orphan churn behind.
- [ ] Review all skills subscribed to `subnet.member.snapshot.changed` and
  `webio.stream.snapshot.requested` for duplicate work, full-state publish, and
  missing debounce.

#### HMG-007: Keep guardrails observability-first

Status: in progress. Wave 1, Wave 2, Wave 5, Wave 6, and Wave 7 guardrails were
implemented with preserved evidence at the same logical boundary so
suppression does not hide the original incoming pressure. Wave 9 adds the
missing containment layer: write pressure can now promote from a local
write-boundary decision to a short-lived owner quarantine, so the same skill
cannot keep launching expensive tools while the primary Yjs document is already
in `block` or sustained `throttle`. Wave 10 fixes the first live regression in
that containment layer: implicit webspace events now publish quarantine service
state to the configured desktop webspace, and hot browser stream events are
coalesced by handler so stale queued subscription work cannot keep growing
after pressure has already been detected.

Principle:

- Predohranitel must reduce amplification, not erase cause.
- If the hub suppresses or coalesces work, operators still need to see:
  what arrived, what would have run, what was skipped, why it was skipped, and
  which skill/core path created the pressure.

Actions:

- [x] For every new guardrail, define the preserved evidence set before
  implementing the drop/coalesce behavior.
- [x] Add structured first-wave counters for `requested`, `forced`,
  `coalesced`, `scheduled`, and `rerun` at the same logical boundary; extend
  the same pattern with `suppressed`, `timed_out`, and `failed` as degraded
  mode expands.
- [x] Ensure telemetry and logs distinguish "incoming load reduced by
  guardrail" from "incoming load disappeared".
- [x] Add kernel-level Yjs primary-doc governance at the write boundary:
  `get_ydoc`, `async_get_ydoc`, `mutate_live_room`, and direct
  `YStore.write_update` now evaluate the shared `warn` / `throttle` / `block`
  policy before persisting or broadcasting skill-owned writes.
- [x] Move `ProjectionService` onto the shared Yjs governor and tag
  already-governed writes to avoid double-throttling in downstream Yjs paths.
- [x] Attach explicit SDK Yjs ownership metadata for sync and async skill
  wrappers so LLM-generated skills are attributable even when they use the
  supported SDK facade.
- [x] Add owner-level Yjs pressure quarantine with TTL, visible deny counters,
  and structured `skill_owner_quarantined` tool results instead of silent
  fallback.
- [x] Run skill tool admission through the Yjs owner guard before skill runtime
  context is established, so an overloaded owner is stopped before it can build
  another full snapshot payload.
- [x] Notify quarantined skills through optional `onQuarantine` /
  `on_quarantine` tools with `ttl_s`, `reason`, blocked tool, owner, webspace,
  and quarantine metadata; this hook bypasses skill admission but remains
  subject to Yjs write governance.
- [x] Persist skill-local quarantine incidents to
  `ADAOS_SKILL_MEMORY_PATH/logs/quarantine.jsonl` so later LLM-assisted skill
  repair can recover the exact pressure event from the skill context.
- [x] Publish active Yjs owner quarantines into the primary doc service branch
  `data.yjs_qrnt` with `items`, `by_owner`, and `by_skill`, allowing web UI
  consumers to disable affected apps/widgets explicitly.
- [x] Run projection admission through the same owner guard before payload
  compaction and primary-doc mutation, preserving evidence while skipping
  avoidable work.
- [x] Surface active quarantine state in reliability and `adaos node
  reliability` output (`quarantine=active`, reason, trigger, retry-after, tool,
  path).
- [x] Normalize implicit Yjs owner-guard webspaces through runtime webspace
  policy instead of hard-coding `default`, so `data.yjs_qrnt` appears in the
  same webspace the browser is rendering.
- [x] Bound `webio.stream.subscription.changed` in the eventbus hot-topic queue
  and supersede stale queued `webio.stream.snapshot.requested` /
  `webio.stream.subscription.changed` work by handler before it can accumulate
  into memory pressure.
- [ ] Keep operator-visible correlation IDs or generation IDs across snapshot,
  rebuild, route, and Yjs stages.
  First wave landed for member snapshot rebuild pressure and incident summary;
  extend the same IDs into route and Yjs pressure payloads.
- [ ] Reject any guardrail that improves memory only by hiding the overload
  source from incident review.

#### HMG-008: Make ProjectionService the normal skill write ingress

Status: planned. Kernel pressure governance is now the last-resort safety net,
but the target architecture is stricter: LLM-authored skills should not write
browser-visible primary Yjs state directly during normal operation.

Principle:

- `ProjectionService` is the normal skill-facing write boundary for primary
  shared document state.
- Direct skill-owned Yjs writes are legacy or capability-gated.
- Details and large diagnostics belong in section endpoints, streams, or
  `360log`, not broad primary-doc branches.

Roadmap:

- [ ] Observe and count direct skill-owned Yjs writes with owner, source,
  channel, root, path, and update size.
- [x] Ensure `infrastate_skill` projections preserve skill identity when calling
  `ProjectionService`, preventing background refresh tasks from being
  mis-attributed as `_by_owner/core`.
- [ ] Emit `deprecated_direct_skill_yjs_write` warnings for skill paths that
  bypass `ProjectionService`.
- [ ] Apply stricter budgets to direct skill writes than to governed projection
  writes.
- [ ] Block broad direct skill writes to shared roots such as `data`, `ui`,
  `registry`, and desktop-wide branches unless explicitly allowlisted.
- [ ] Add `skill.yaml` capability declarations for direct Yjs exceptions and
  projection targets.
- [ ] Make direct skill-owned primary-doc writes deny-by-default outside
  declared capabilities.
- [x] Teach `web_desktop` and the client shell to consume `data.yjs_qrnt` and
  render quarantined apps/widgets as disabled with a visible reason and
  retry-after, rather than silently hiding or retry-spamming them.
- [ ] Add app/widget manifest metadata mapping UI entries to owning skill IDs so
  `data.yjs_qrnt.by_skill[skill_id]` can be applied consistently across desktop
  icons, widgets, modals, and details panes.
- [ ] Add migration tooling/reporting for skills that still depend on direct
  Yjs access.
- [ ] Update LLM skill templates and prompts so generated skills use
  projections, streams, HTTP details, or skill-local storage by default.

## Realtime First 3 Minutes

### Goal

Provide stable hub-root connectivity and error-free runtime behavior during the
first 3 minutes after startup.

Success means:

- NATS-over-WS stays connected for at least 180 seconds without watchdog reconnects.
- Root-routed HTTP and WS requests do not timeout during normal startup probes.
- Browser `/ws` and `/yws` handshakes complete without fallback-only operation.
- Yjs persistence does not create sustained high-pressure warnings.
- Startup and first browser attach do not block the event loop above diagnostic thresholds.
- Process memory is sampled during loading-to-ready and through the first 3 minutes; it reaches a stable startup plateau and does not show runaway growth.

### Current Status

Snapshot date: 2026-05-01.

Overall completion: 99% for the expanded local + root-routed browser goal. Windows root-routed `/nats` is accepted again after fixing an AdaOS env-name collision: the legacy `HUB_NATS_WS_PROXY=auto` variable was treated by Python proxy discovery as a generic `*_PROXY` variable and selected the bad one-way route. The stable default is now `HUB_NATS_WS_PROXY_MODE=auto`, with the legacy name hidden during `websockets.connect`. Linux/RU root-routed browsers already load data and stay inside the first-window memory guard; the remaining work is a rollout reconfirmation and longer plateau soak, not a first-window blocker.

Done:

- Structured terminal/log diagnostics are now available for NATS WS receive failures, direct control frames, route reply lifecycle, root log extracts, event loop lag/hang, and Yjs owner pressure.
- Hot-path `load_config()` was removed from route key matching.
- Skill runtime status reads no longer force slot prepare/path creation during snapshot calls.
- Selected synchronous skill subscription handlers can run in worker threads.
- Root log extracts now summarize repeated incidents instead of flooding the terminal by default.
- Windows Selector loop is now an explicit diagnostic mode only.
- Startup native capacity and subnet directory registry work now runs off the event loop thread.
- YRoom pressure diagnostics no longer call ystore runtime filesystem/SQLite snapshot code from the realtime hot path by default.
- In the active local `infrascope_skill` workspace/runtime copy, background refresh target discovery now runs in a worker thread.
- `ui.notify` delivery no longer holds the eventbus critical path; RouterService schedules notification delivery in background and drains briefly on shutdown.
- Root MCP local SDK calls use a local-first embedded registry path for local runtime queries, so normal startup no longer probes the public Root MCP bridge or emits `fetch failed` fallback diagnostics.
- Yjs gateway persistence keeps immediate writes for durability, while owner-pressure diagnostics now treat gateway first-attach peak bursts separately from sustained pressure.
- The hub subnet-directory staler heartbeat/stale sweep no longer commits SQLite work on the event loop thread.
- NATS WS diagnostic JSONL writes are emitted from a worker thread instead of the NATS supervisor hot path.
- Active local `infra_access_skill` and `infrastate_skill` workspace/runtime copies no longer perform heavy snapshot refresh from `sys.ready` subscription callbacks.
- Active local `infrastate_skill` runtime event handling now returns from `sys.ready` without a worker-thread hop, eliminating the last startup slow-handler warning.
- Active `.adaos` skill hotfixes are present in the workspace skill registry repo through `d208cd3`; DEV Forge publish dry-run is not applicable because these are workspace-registry skills, not DEV Forge drafts.
- Final soak verification now includes process-tree memory sampling during loading-to-ready and the full 180-second window.
- Local API serve disables WebSocket per-message deflate to avoid CPU-heavy compression during root-routed Yjs first-sync bursts.
- `/api/node/reliability` and `/api/node/reliability/summary` build reliability payloads off the event loop, so browser polling no longer runs `load_config()` / runtime-state filesystem checks on the loop thread.
- Skill service discovery refresh no longer submits recurring watchdog work to the default thread executor, avoiding the observed Windows `Thread.start()` event-loop freeze path.
- Control lifecycle await-resume stack watcher is now opt-in diagnostics only, avoiding a fresh diagnostic thread on every control heartbeat during normal runs.
- Backend route-open retry is deployed and visible in root logs: `open ack retry`/`open republish` replaced the old fallback flush path.
- NATS-over-WS core transport now follows the stable `tools` behavior by default: `websockets` system proxy auto-detect (`proxy=True`) is used unless `HUB_NATS_WS_PROXY_MODE=none` explicitly forces direct-route diagnostics.
- The legacy `HUB_NATS_WS_PROXY` name remains backward-compatible but is no longer documented as the steady-state default because Python treats any `*_PROXY` environment variable as a proxy setting.
- NATS-over-WS control-frame handling now replies to coalesced root `PING` frames without corrupting `MSG` payload boundaries.
- Local and root-routed browser runs on 2026-04-30 initially confirmed stable `/nats` and `/yws` behavior after the proxy-auto core change.
- Normal diagnostic thresholds are relaxed out of deep-debug mode: loop-lag warnings now default to 1000ms and eventbus slow async warnings default to 250ms.
- Backend-origin Yjs updates are marked so the live room can fan them out to browsers without persisting the same detached diff again as `gateway_ws`.
- `infrastate_skill.get_snapshot` is read-only for HTTP callers by default and returns a compact client snapshot instead of projecting multi-megabyte diagnostic payloads into Yjs on every root-routed fallback probe.
- Supervisor memory telemetry still records growth, but automatic policy-triggered sampled-profile restarts are delayed for the first 300 seconds by default and are deferred while recent browser sessions are live, so diagnostics cannot break the first browser attach/interaction window.
- Supervisor sampled-profile timing now starts after the profiled runtime API becomes ready, preventing slow bootstrap from consuming the whole profiling window and producing empty start-only artifacts.
- `infrascope_skill`, `infrastate_skill`, and core Yjs load-mark streams now publish only active subscribed receivers, deduplicate unchanged payloads, and rate-limit high-churn diagnostics under browser load.

In progress:

- Keep an eye on residual sub-second event-loop drift and occasional `infrastate` / `infrascope` browser-runtime handlers, but do not treat them as connectivity blockers unless they exceed the normal thresholds.
- Reconfirm Windows after rollout from a clean operator environment where `HUB_NATS_WS_PROXY` is unset and `HUB_NATS_WS_PROXY_MODE=auto` is used.
- Reduce follow-up Linux/RU YStore replay pressure (`sync_runtime: pressure`, replay around 700 KiB) without reintroducing expensive live-backup work on the runtime hot path.

Known follow-up outside the current goal:

- If public remote Root MCP access to local hubs is required, design and deploy a backend/infra route that resolves upstream by hub route/NATS instead of direct `ADAOS_BASE` HTTP proxying.

Latest verification:

- `first3m_20260428_225403`: 180-second soak, no NATS recv failure, no route timeout, no open ack fallback, no event loop lag; one shutdown idle wait was classified as a false-positive hang.
- `first3m_20260428_230658`: after YRoom hot-path diagnostic changes, no NATS recv failure, no route timeout, no open ack fallback, no event loop lag/hang, no `runtime_snapshot()`/`Path.stat()` stack.
- `first3m_20260428_231152`: after `infrascope_skill` target-discovery offload, ready in about 13 seconds, 180-second soak completed, no NATS recv failure/watchdog, no route timeout, no open ack fallback, no event loop lag/hang, no control resume warning stack. NATS diagnostics showed Proactor loop, connected read task, `pending_data_size=0`, and no task errors.
- `first3m_20260429_065606`: after RouterService background `ui.notify` delivery, ready in about 15 seconds, 180-second soak completed, no NATS recv failure/watchdog, no route timeout, no open ack fallback, no event loop lag/hang, no slow `ui.notify`, and no router background delivery failure. Remaining warnings were two off-thread `sys.ready` durations and two `_by_owner/gateway_ws` Yjs pressure warnings.
- `first3m_20260429_080100`: final 180-second soak completed and stopped cleanly. Counts: NATS recv failure/watchdog/ConnectionClosedError/WinError 10054 = 0, route timeout/proxy failed = 0, open ack fallback = 0, real event loop lag/hang = 0, slow async handler = 0, slow `ui.notify` = 0, Yjs owner pressure/unknown/gateway warning = 0, infra_access snapshot failure = 0, traceback = 0. Expected non-failing signals: one embedded Root MCP fallback debug line, one idle-wait hang suppression during shutdown, one NATS disconnect during requested shutdown.
- `first3m_20260429_final_mem4`: final 180-second soak with process-tree memory sampling. Ready in 13.461s. Counts: NATS recv failure/watchdog/ConnectionClosedError/WinError 10054 = 0, route timeout/proxy failed = 0, open ack fallback = 0, event loop lag = 0, real event loop hang = 0, slow async handler = 0, slow `ui.notify` = 0, Yjs owner pressure = 0, infra_access snapshot failure = 0, traceback = 0, Root MCP `fetch failed` = 0, embedded Root MCP fallback = 0. Expected non-failing signals: one idle-wait hang suppression during shutdown and one NATS disconnect during requested shutdown. Memory: process tree WorkingSet first/ready/peak/last = 121.695/238.305/250.066/248.066 MB; PrivateMemory first/ready/peak/last = 95.172/218.930/230.555/228.117 MB; loading-to-ready sampled 121.695 -> 238.305 MB WorkingSet and 95.172 -> 218.930 MB PrivateMemory; no runaway growth observed.
- `first3m_20260429_final_accept`: repeat final acceptance run. Ready in 12.795s, browser `/ws` accepted, and `YRoom ready webspace=desktop` observed. Counts: NATS recv failure/watchdog/ConnectionClosedError/WinError 10054 = 0, route timeout/proxy failed = 0, open ack fallback = 0, event loop lag = 0, real event loop hang = 0, slow async handler = 0, slow `ui.notify` = 0, Yjs owner pressure = 0, infra_access snapshot failure = 0, traceback = 0, Root MCP `fetch failed` = 0, embedded Root MCP fallback = 0. Expected non-failing signals: one idle-wait hang suppression during shutdown and one NATS disconnect during requested shutdown. Memory: process tree WorkingSet first/ready/peak/last = 30.949/136.863/146.445/145.836 MB; PrivateMemory first/ready/peak/last = 21.930/137.117/145.211/145.211 MB; no runaway growth observed.
- `root_remote_browser_20260429_0753Z`: live local + root-routed browser load reopened the goal. Local browser remained usable, but the remote browser repeatedly reconnected through root. Evidence: root reverse-proxy accepted `/hubs/sn_6acf0c01/yws/desktop` with `101`, then nginx emitted repeated `SSL_read() failed ... bad record mac` on keepalive/upgraded paths; backend `ws-nats-proxy` reported `/nats` close `1006` with `natsKeepalivesSent=0`, `lastClientPongAgo_s=67.0`, and only one client ping; hub-side `nats_ws_diag.jsonl` showed `pending_data_size=0` while `last_rx_ago_s` grew above 300s and `ka_pings_rx=1`. Conclusion: this is not local pending-queue starvation; the root WS-NATS tunnel lacks regular hub<->root application-level liveness traffic under remote browser load.
- `root_remote_after_summary_offload_20260429_111439`: 3+ minute local + root-routed browser diagnostic after disabling API WebSocket compression and offloading reliability summary generation. Counts in the verification window: NATS recv failure/ConnectionClosedError/WinError 10054/watchdog `_reading_task` = 0, event loop lag = 0, control-lifecycle warning stack = 0, `node_reliability_summary` / `current_reliability_payload` warning stack = 0. Expected signals only: one `nats bridge connected`, one `yws connection open`, one `yws connection closed` during requested shutdown, and one NATS disconnect during requested shutdown. Caveat: this run used local raw NATS keepalive diagnostics; public root now intentionally keeps `WS_NATS_PROXY_KEEPALIVE_ENABLE=0`, so the remaining verification target is route-open retry / supersede behavior under that mode.
- `root_remote_backend_deploy_20260429_0835Z`: after latest backend deploy, remote browser still failed to load Yjs data. Root is intentionally configured with `WS_NATS_PROXY_KEEPALIVE_ENABLE=0`, so backend `natsKeepalivesSent=0` is expected and is not the primary bug marker. Local log shows a repeated cycle: NATS bridge connects, root-routed `yws` opens, `/nats` fails with `ConnectionClosedError` / `WinError 121` after about 20-25s, then `yws` closes and reconnects. Root logs show the remote route can publish `open` while the hub route subscription is not yet reinstalled after reconnect; the old fallback then flushes early Yjs frames without a local upstream, producing `no_upstream`. Patch prepared: root route proxy now retries `open` instead of flushing early frames after missing `open_ack`; WS-NATS supersede waits for the new connection's route subscription before closing old peers, with a 10s max grace; WS-NATS config is logged explicitly.
- `codex_first3m_20260429_125941`: local run after changing skill-service discovery refresh away from recurring `asyncio.to_thread`. Ready in about 16.5s. No NATS recv failure, no `ConnectionClosedError`, no `WinError`, no route timeout, no `open ack fallback`, no `no_upstream`, and no `service_supervisor` / `Thread.start` stack. Minor short loop-lag diagnostics remained, and shutdown emitted only the expected idle-wait suppression.
- `codex_first3m_20260429_130538`: local + root-routed browser run after making control lifecycle await watcher opt-in. Ready in about 16.0s. The previous 60s `service_supervisor -> Thread.start()` freeze did not recur. Counts still showed `/nats` churn under root-routed load: `nats_recv_failed=8`, `nats_watchdog=40`, `ConnectionClosedError=64`, `WinError=14`, with `yws_open=8` and `yws_closed=8`. Counts stayed clean for route-level symptoms: `route_timeout=0`, `http_proxy_failed=0`, `open_ack_fallback=0`, `no_upstream=0`. Memory stayed bounded: process tree WorkingSet about 108 MB first sample, 150 MB at ready, 174 MB at the end; PrivateMemory about 85 MB first sample, 143 MB at ready, 165 MB at the end.
- `codex_heartbeat_ab_20260429_131015`: A/B run with hub-side `HUB_NATS_WS_DATA_HEARTBEAT_S=10`. It did not stabilize root `/nats`: `nats_recv_failed=4`, `nats_watchdog=20`, `ConnectionClosedError=32`, `WinError=6`, with repeated root-routed YWS open/close. Route-level symptoms stayed clean: `route_timeout=0`, `http_proxy_failed=0`, `open_ack_fallback=0`, `no_upstream=0`. Conclusion: hub-side heartbeat alone is insufficient when root `WS_NATS_PROXY_KEEPALIVE_ENABLE=0`; the next required experiment is restoring root proxy application-level keepalive.
- `root_remote_keepalive_enabled_20260429_1035Z`: after root params were changed and backend recreated, remote browser still did not load data. Local terminal shows `/nats` `ConnectionClosedError` with `WinError 64` and root-routed `yws connection closed` at the same time. Root close diagnostics now show `natsKeepalivesSent=2` / `upstreamNatsPingsSent=2`, so the application keepalive is active; however `wsPingsSent=0` / `wsPongsReceived=0`, and local `nats_ws_diag.jsonl` shows only one or zero NATS PINGs observed by the hub before the socket dies. Root logs also show `closing superseded hub ws-nats connection reason=route_ready` while browser route `open` retries are still possible. Conclusion: add explicit WS control ping for `/nats` and stop closing superseded `/nats` peers immediately on `route_ready`; keep them through a longer grace window.
- `root_remote_after_ws_ping_20260429_1150Z`: after backend deploy and updated root variables, remote browser still did not load data. The hub-root `/nats` tunnel again failed after about 22s with `ConnectionClosedError` / `WinError 64`, and the root-routed Yjs connection closed in the same window. A local diagnostic with `.env` forcing `HUB_NATS_WS_HEARTBEAT_S=10`, `HUB_NATS_WS_HEARTBEAT_FORCE=1`, and transport trace confirmed hub-side WS heartbeat traffic is active (`heartbeat_s=10.0`, repeated `nats ws heartbeat tx kind=PING`) but still does not prevent `/nats` churn. Route-level symptoms stayed clean in that diagnostic (`route timeout=0`, `http proxy failed=0`, `open ack fallback=0`, `no_upstream=0`). Conclusion: treat a missing root NATS keepalive PONG as a hard liveness failure and close/reopen the tunnel proactively.
- `root_nats_independent_tools_20260429_1746`: independent `tools` probes without `adaos api serve` split the problem. Raw `websockets` NATS framing against `wss://api.inimatic.com/nats` stayed healthy for 25-45s with nats-py-like CONNECT/SUB/PUB formatting, repeated client NATS PINGs, split PUB frames, and full echo delivery (`22/22`, `31/31`, `41/41`). Raw `aiohttp` framing stopped after 4 echo messages and failed with missing PONG / close `1006`; stock `nats-py`/aiohttp stopped after 3 echo messages and failed with `UnexpectedEOF` / close `1006`; AdaOS custom transport stopped after 4 echo messages and failed with `ConnectionClosedError` / close `1006` / `WinError 121` while TX continued. Conclusion: the public `/nats` channel is not generically unreachable; the failing path is WebSocket-client/proxy behavior under active nats-py-like traffic. Backend patch prepared to add per-connection frame counters (`clientFrames`, `upstreamWrites`, `upstreamFrames`, `downstreamSend*`) to close summaries so the next deploy shows exactly where frames stop.
- `root_remote_frame_accounting_20260429_1730Z`: after backend frame-accounting deploy, root confirms route traffic is alive before forced close: `PUB -> MSG -> downstream` counters increment, `downstreamSendErrors=0`, and YWS `open_ack` can be received for a fresh route key. The tunnel is then closed after a single root NATS keepalive miss: `natsKeepalivesSent=1`, `clientFrames.pong=1`, `clientFrames.pub=2`, `upstreamFrames.msg=8-9`, `wsPingsSent=1`, `wsPongsReceived=0`, followed by `nats keepalive pong missing: closing tunnel` and close `1006` at about 15s uptime. Conclusion: the next backend fix should stop treating one missed keepalive as fatal, stagger WS control ping and NATS-data keepalive, and close only after repeated misses with no client data / WS pong.
- `core_proxy_auto_20260429_223126`: decisive A/B after comparing `tools` vs core. Stable `tools/diag_nats_ws.py` runs used the `websockets` default `proxy=True` route, while AdaOS core forced `proxy=None` on Windows and selected a direct route that half-stalled after the first few `PUB` frames. After changing core default to proxy-auto, an isolated `nats-py + AdaOS WebSocketTransport` test stayed healthy for 45s (`sent=42`, `got=42`, clean close `1000`). A full `adaos api serve` soak of about 190s then completed with `nats ws recv failed=0`, watchdog/`ConnectionClosedError`/`WinError=0`, route timeout/proxy failed/open-ack fallback/`no_upstream=0`, event loop lag/hang/traceback=0, root PING/PONG continuing through the run, and clean NATS WS close `1000` during requested shutdown. Caveat: that memory CSV sampled the launcher wrapper rather than the uvicorn child, so memory acceptance remains covered by the earlier process-tree runs.
- `hub_browser_accept_20260430_0350` and `hub_browser_accept_20260430_0431`: user-confirmed Windows hub-browser connectivity restored. Two latest `api serve` windows show `nats bridge connected=1` each, `nats ws recv failed=0`, watchdog/`ConnectionClosedError`/`WinError=0`, route timeout/proxy failed/open-ack fallback/`no_upstream=0`, traceback/error level=0, and expected NATS disconnect only during requested shutdown. Root-routed Yjs connections opened and closed without route errors. Residual non-blocking issues: many sub-second loop-lag diagnostics under the old 250ms threshold and a few slow browser-runtime handlers in `infrastate_skill` / `infrascope_skill`; normal defaults have been polished to warn only above 1000ms loop drift and 250ms async-handler duration.
- `linux_ru_zone_split_20260430_0734`: Linux hub `sn_92ffc943` reports `hub_root: ready/stable` on `wss://ru.api.inimatic.com/nats`, with `control_subs=1` and `route_subs=1` after a clean autostart restart. Independent checks show `https://ru.api.inimatic.com/v1/browser/hub/status?hub_id=sn_92ffc943` returns `online`, while `https://api.inimatic.com/v1/browser/hub/status?hub_id=sn_92ffc943` returns `offline`; RU root logs show no current browser `route: open` / YWS attempts. Conclusion: the remaining Linux browser failure is zone selection in the browser client, not a broken Linux hub-root NATS channel. Patch prepared: browser root-proxy base now learns and probes `hub_id -> zone` before `/ws`/`/yws` attach.
- `linux_ru_two_browser_memory_guard_20260430_0758`: after zone-aware browser deploy, backend-origin Yjs dedupe, compact/read-only `infrastate` snapshots, capped load-mark history, and supervisor memory-profile grace, a two-browser Linux/RU soak stayed ready for more than 4 minutes. Counts in the verification window: `nats ws recv failed=0`, route timeout/proxy failed=0, supervisor route watchdog reset=0, event-loop lag/hang=0, memory apply/complete profile restart=0. Browser path was active: `hub_root_browser: ready/stable`, `route: ready`, `sync_runtime.yws=2`, live media peer `1/1`. Runtime RSS moved from about 256 MiB at 45s to about 304 MiB at 4m33s, later about 336 MiB at 5m15s; this is no longer the previous runaway-to-3GB behavior, but it still needs a longer plateau soak.
- `linux_ru_diag_polish_20260430_0939`: after raising the gateway tiny-write warning threshold and restarting autostart, a 3m45s Linux/RU soak stayed clean: `nats ws recv failed=0`, route timeout/proxy failed=0, supervisor route watchdog reset=0, event-loop lag/hang=0, memory apply/complete profile restart=0, and `YJS owner flow above threshold=0`. Runtime RSS stayed in a narrow first-window band of about 247 MiB at 31s to 276 MiB at 3m44s; supervisor public memory status exposes `auto_profile_min_uptime_sec=300.0` and remained `current_profile_mode=normal`, `suspicion_state=stable`.
- `hub_workspace_sync_20260430`: Linux hub workspace `/root/.adaos/workspace` was checked after the abnormal workstation reboot. The only hub workspace diff is `skills/infrastate_skill/handlers/main.py`; local `.adaos/workspace/skills/infrastate_skill/handlers/main.py` matches it semantically and is already present in the workspace HEAD commit `ea28d74` (`perf: memory menagement`). The remaining local workspace dirt is only `.gitignore`; it is unrelated to the Linux hub hotpatch.
- `hub_core_sync_20260430`: Linux hub core slots `A` and `B` were compared against local core changes after the abnormal workstation reboot. The runtime-hotpatched files `sdk/io/out.py`, `services/logging.py`, `services/router/service.py`, `services/webspace_id.py`, `services/yjs/doc.py`, `services/yjs/gateway_ws.py`, `services/yjs/load_mark.py`, `services/yjs/load_mark_history.py`, `services/yjs/update_origin.py`, and `services/yjs/webspace.py` match local source in both slots. The only remaining local deltas are intentional commit polish: `apps/api/node_api.py` formatting around the compact `infrastate/action` snapshot call and `apps/supervisor.py` keeping `suspicion_state=suspected` while recording `auto_profile_last_block_reason` instead of hiding the suspicion as `suppressed`.
- `windows_data_ping_regression_20260430`: Windows root-routed browser load regressed after the earlier acceptance runs. Evidence: `/nats` fails after about 40s with `ConnectionClosedError` / `WinError 121`, remote browser does not load data, while independent AdaOS transport tooling can keep the raw `/nats` echo stable. An initial hypothesis blamed client data `PING`, but the later `sn_6acf0c01-b5f3b8a6d2` run failed with `data_pings_tx=0`, `ka_pings_rx=0`, and root still reporting `route downstream send done`. Conclusion: client data ping is not the root cause and may be part of the confirmed Windows-stable profile. Patch prepared: restore Windows+`websockets` `HUB_NATS_WS_DATA_PING_S=auto` to a conservative 5s, while Linux stays disabled unless explicitly requested.
- `windows_raw_ws_channel_20260430`: raw WebSocket tools show the public `/nats` channel itself is healthy. `tools/diag_nats_ws_concurrent.py` held 90s with concurrent reader/writer (`tx_pub=88`, `rx_msg=88`, `rx_ping=10`, `tx_pong=10`, `errors=[]`). `tools/diag_nats_ws.py` with nats-py CONNECT style, empty queue/reply spacing, split PUB frames, and binary frames held 90s (`pubs_tx=80`, `msgs_rx=80`, `nats_pings_rx=10`, `nats_pongs_tx=10`, `errors=[]`). At that checkpoint, `tools/diag_nats_client.py` through AdaOS `WebSocketTransportWebsockets` and `tools/diag_nats_py_ws.py` through stock nats-py/aiohttp both stopped receiving after 3-4 echo messages and closed with `1006` / `WinError 121` or `UnexpectedEOF`. Conclusion: the failure was not a raw root channel failure; it was in nats-py-style transport/runtime behavior around the first keepalive window. Later `windows_ws_control_ping_guard_20260430` re-validated AdaOS transport in isolation after backend keepalive hardening.
- `windows_proxy_ping_termination_20260430`: follow-up root-log analysis shows the failing nats-py-style path correlates with Root/proxy NATS `PING` data frames delivered downstream to the hub: after the first proxy/upstream keepalive window, Root reports missing PONG/client data and the hub sees `ConnectionClosedError`. The confirmed Windows commit had `WS_NATS_PROXY_KEEPALIVE_ENABLE=0`; current defaults are restored to that. Backend patch prepared: for normal hub WS-NATS clients, Root now answers upstream NATS `PING` locally and strips those `PING` command frames before forwarding downstream; transparent realtime sidecar connections (`rt-*`) still receive raw NATS control frames. The stripper is protocol-aware and skips `MSG/HMSG` payload bytes, so route/Yjs payloads containing `PING\r\n` are not modified.
- `windows_legacy_keepalive_guard_20260430`: after deploying upstream-`PING` termination, the Windows regression persisted. Fresh client diagnostics show `data_pings_tx=0` but `ka_pings_rx=2`, and a partial root log read shows both `ping (upstream->proxy) answered and stripped downstream` and a separate `ping (keepalive -> client) sent`. Conclusion: the backend patch is active, but the active root env can still enable the older client-facing NATS-data keepalive. Patch prepared: legacy `WS_NATS_PROXY_KEEPALIVE_ENABLE=1` is ignored for normal hub clients unless `WS_NATS_PROXY_KEEPALIVE_FORCE=1`; focused diagnostics must use `WS_NATS_PROXY_CLIENT_KEEPALIVE_ENABLE=1`.
- `windows_ws_control_ping_guard_20260430`: after the legacy NATS-data keepalive guard, standalone `tools/diag_nats_client.py` held 75s cleanly on the AdaOS `WebSocketTransportWebsockets`, but full `adaos api serve` still dropped during browser/Yjs load. Root logs for `sn_6acf0c01-753137252c` show `ws ping enabled pingMs=10000`, `wsPingsSent=6`, `wsPongsReceived=2`, then close `1006`; no NATS-data client keepalive was sent (`natsKeepalivesSent=0`). Conclusion: a stale root `WS_NATS_PROXY_WS_PING=1` can still break Windows hub clients under full runtime load. Patch prepared: ignore legacy `WS_NATS_PROXY_WS_PING=1` for normal hub clients unless `WS_NATS_PROXY_WS_PING_FORCE=1`; explicit diagnostics use `WS_NATS_PROXY_CLIENT_WS_PING_ENABLE=1`. Realtime sidecar `rt-*` remains allowed to use its own WS ping.
- `windows_observe_and_transport_ab_20260501`: the weather observer hypothesis was ruled out again; the observed hard stall was in `services.observe._write_local`, where synchronous `events.log` file I/O blocked the event loop for about 56s during the two-browser load. Local observe logging now goes through a non-blocking queue and daemon writer thread. A follow-up aiohttp run had `observe.py=0`, `_write_local=0`, loop lag=0, and `flush_slow=0`, but still flapped every 20-50s with `RuntimeError: ws closed` / `ClientConnectionResetError: Cannot write to closing transport`. A follow-up websockets run also flapped under the current Root env, with no observe/loop starvation, indicating the remaining delta from the confirmed `0cfbc9e` profile is Root-side keepalive/proxy behavior rather than local file I/O. `0cfbc9e` used `HUB_NATS_WS_IMPL=websockets` and `WS_NATS_PROXY_KEEPALIVE_ENABLE=0`.
- `windows_supersede_grace_experiment_20260501`: after root env/container refresh, a 190-second Windows two-browser run still flapped (`nats ws recv failed=7`, `ConnectionClosedError=56`, `yws open/close=9/9`) while memory stayed bounded around 213 MiB and observe/weather/event-loop diagnostics stayed clean. Root logs showed `supersede_grace_ms=15000` for the fresh runtime tags, so the immediate-supersede hypothesis has not been tested yet. With current backend parsing, `WS_NATS_PROXY_SUPERSEDE_GRACE_MS=0` would be coerced back to `15000`; use `WS_NATS_PROXY_SUPERSEDE_GRACE_MS=1` for the focused no-code experiment, and only patch parser/defaults if that experiment proves useful.
- `windows_proxy_env_collision_20260501`: decisive bisection found a local env collision, not a Root channel failure. Raw `tools/diag_nats_ws.py` with `HUB_NATS_WS_PROXY=auto` changed the Root-observed source to `77.37.240.23` and reproduced one-way/`1006` behavior; the same raw probe with the variable unset used the stable `217.216.106.x` route and closed cleanly. Fix prepared and verified: introduce `HUB_NATS_WS_PROXY_MODE=auto`, keep legacy `HUB_NATS_WS_PROXY` as compatibility input only, and hide the legacy variable during `websockets.connect` so Python proxy discovery cannot consume it.
- `windows_proxy_env_sanitized_20260501`: standalone `tools/diag_nats_client.py` intentionally ran with legacy `HUB_NATS_WS_PROXY=auto` still set. The patched AdaOS transport held 45s cleanly (`tx_count=8`, `rx_count=7`, no task errors, close `1000`). Root confirmed the healthy route: `from=217.216.106.4`, `pub=8`, `msg=8`, `keepaliveMisses=0`, `downstreamSendErrors=0`, close `code=1000`.
- `windows_two_browser_accept_20260501`: full `adaos api serve` under browser load ran about 178s and stopped cleanly. Local logs: `nats ws recv failed=0`, watchdog/`ConnectionClosedError`/`WinError=0`, route timeout/proxy failed/starvation=0, event-loop lag/hang=0. Root logs for `rt-a-5358db7fb0`: `from=217.216.106.4`, `uptime_s=177.789`, `pub=1054`, `keepaliveMisses=0`, `downstreamSendErrors=0`, close `code=1000`.
- `windows_memory_recheck_20260501`: follow-up run monitored the real uvicorn PID instead of the launcher wrapper. RSS moved from 135.7 MiB at 5s to 165.3 MiB at 121s; PrivateMemory moved from 137.8 MiB to 169.5 MiB. Root confirmed a clean 121.6s NATS session (`code=1000`, `keepaliveMisses=0`, `downstreamSendErrors=0`). Residual non-blocking signals: expected shutdown disconnect, high first-attach `infrastate` YJS owner-flow bursts, and one slow weather handler at 0.264s.
- `linux_ru_two_browser_plateau_20260501`: live Linux hub `192.168.0.30` with two browsers attached was hotpatched with active-receiver/fingerprint stream guards for `infrascope_skill`, `infrastate_skill`, and core `yjs.load_mark`. A 6-minute window warmed from about 245 MiB RSS to about 439 MiB and plateaued; a follow-up 10-minute window moved from about 462 MiB to about 547 MiB, then stayed flat for the last 3-4 minutes. Connectivity stayed stable: `hub_root: ready/stable`, `hub_root_browser: ready/stable`, `media_runtime live_peers=2/2`, `nats ws recv failed=0`, route timeout/proxy failed=0, event-loop lag/hang=0. Remaining issue is bounded YStore replay pressure (`sync_runtime: pressure`, replay about 715 KiB), not the previous 3 GiB runaway/restart pattern. A manual live `/api/node/yjs/webspaces/*/backup` request did not return within 60s, so live compaction needs a safer off-hot-path design.
- `replay_pressure_semantics_20260510`: after auth-model rollout on RU stand, login reached connected YWS/WebRTC paths but diagnostics still showed `state-sync=degraded:aging`, `replay=32/32`, and `_by_owner/gateway_ws` pressure. Root cause: entry-limit YStore compaction kept a full replay tail (`snapshot + replay_window`), and state-sync treated bounded replay maintenance pressure as semantic sync degradation even when transport was attached, first sync was complete, and materialization was ready. Patch: entry-limit compaction now targets a smaller replay tail by default, bounded replay maintenance pressure stays visible as a blocker without turning ready materialized sync red, and the browser treats the same blocker as non-stale during mixed-version rollout.
- `replay_pressure_autocompact_20260510`: follow-up hardening now requests background YStore runtime compaction when reliability observes bounded replay pressure on an eligible webspace. The request is quiet-window guarded (`ADAOS_YSTORE_AUTOCOMPACT_REPLAY_PRESSURE_QUIET_SEC`, default 2s) and can be disabled with `ADAOS_YSTORE_AUTOCOMPACT_ON_REPLAY_PRESSURE=0`, so reliability polling does not perform snapshot encoding inline.

### Tasks

#### F3M-001: NATS-over-WS disconnects after 25-60 seconds

Status: accepted again for Windows root-routed `/nats` after the local proxy-env collision fix; Linux/RU first-window connectivity remains accepted. Local observe file I/O starvation is fixed.

Evidence:

- `nats ws recv failed ... ConnectionClosedError: no close frame received or sent code=1006`
- `ConnectionResetError: [WinError 10054]`
- watchdog reports `_reading_task terminated`.
- `nats_ws_diag.jsonl` shows `last_rx_ago_s` and `last_ping_rx_ago_s` growing before disconnect while `pending_data_size` stays near zero.
- Under remote browser load, backend `ws-nats-proxy` close diagnostics show `code=1006`, `natsKeepalivesSent=0`, and long `lastClientPongAgo_s` / `lastClientPingAgo_s`, while root-routed `/yws` requests are repeatedly accepted with `101`.

Working hypothesis:

- The disconnect is not caused by local NATS pending queue starvation.
- The public root `/nats` endpoint is reachable and stable with raw `websockets` tooling.
- The decisive difference was route selection: tooling used `websockets` system proxy auto-detect (`proxy=True`), while AdaOS core forced direct route (`proxy=None`) on Windows.
- The direct route can become one-way under active NATS traffic: local sends appear successful, but Root stops receiving client frames and later closes with `1006`.
- Proxy-auto fixed the first Windows regression and remains the correct core default, but the mode must be expressed as `HUB_NATS_WS_PROXY_MODE=auto` or left unset. The legacy `HUB_NATS_WS_PROXY=auto` name can perturb Python proxy discovery before AdaOS parses it.
- The current reopened regression is narrower: nats-py-style hub clients can stall after Root/proxy-originated or upstream NATS `PING` command frames are delivered downstream during route/Yjs load.
- Backend keepalive and frame-accounting diagnostics remain useful, but the current primary fix is to make Root terminate those NATS `PING` frames for normal hub WS-NATS clients while preserving transparent control frames for realtime sidecar clients.
- Aiohttp is not the stable fallback for Windows multi-browser root-routed load; it still fails with `Cannot write to closing transport`.
- `HUB_NATS_WS_IMPL=auto` should resolve to the patched websockets transport on both Windows and Linux; aiohttp remains an explicit diagnostic override.
- The latest Windows regression was not fixed by supersede or keepalive changes; it was fixed by preventing `HUB_NATS_WS_PROXY=auto` from leaking into Python proxy auto-discovery.
- Weather observer callbacks are not the blocker; slow-callback diagnostics stayed at zero during the focused transport runs.

Actions:

- [x] Log structured close/error diagnostics from the Python WS transport.
- [x] Keep client-side NATS ping interval task disabled for WS transport by default.
- [x] Add backend WS/NATS proxy keepalive and supersede diagnostics.
- [x] Make Windows Selector loop an explicit diagnostic mode only.
- [x] Run multiple 180-second soaks with `ADAOS_WIN_SELECTOR_LOOP=0`.
- [x] Confirm the current loop is `WindowsProactorEventLoopPolicy` / `ProactorEventLoop`.
- [x] Confirm no local pending-data backpressure during the verified 180-second runs.
- [x] Reopen after root-routed browser load captured repeated remote reconnects and quiet NATS WS diagnostics.
- [x] Compare root proxy upstream ping/pong cadence against client-side `last_rx_ago_s`.
- [x] Keep backend WS-NATS `nats keepalive -> client` available only as an explicit diagnostic opt-in (`WS_NATS_PROXY_CLIENT_KEEPALIVE_ENABLE=1` or legacy `WS_NATS_PROXY_KEEPALIVE_ENABLE=1` plus `WS_NATS_PROXY_KEEPALIVE_FORCE=1`).
- [x] Confirm root currently runs with `WS_NATS_PROXY_KEEPALIVE_ENABLE=0`; treat `natsKeepalivesSent=0` as expected in that mode.
- [x] Increase backend WS-NATS supersede max grace to 10s, close superseded peers on new route readiness, and log resolved proxy config on startup.
- [x] Deploy backend route-open retry / supersede-grace patch to root.
- [x] Confirm root logs show `open ack retry` / `open republish` and `route_ready` supersede behavior instead of fallback frame flush.
- [x] Run a local diagnostic with raw NATS keepalive and both browsers connected; confirm no NATS watchdog reconnect or remote YWS close loop before requested shutdown.
- [x] Run A/B with hub-side `HUB_NATS_WS_DATA_HEARTBEAT_S=10`; confirm it does not stabilize `/nats` while root proxy keepalive is disabled.
- [x] Re-enable root proxy application-level keepalive and confirm root close diagnostics increment `natsKeepalivesSent`.
- [x] Prepare backend patch for configurable `/nats` WS control ping: `WS_NATS_PROXY_WS_PING=1`, `WS_NATS_PROXY_WS_PING_MS=10000`.
- [x] Prepare backend patch to keep superseded `/nats` peers until grace timeout by default instead of closing them immediately on `route_ready`.
- [x] Deploy backend WS ping / supersede-grace patch to root.
- [x] Prepare backend patch to close and optionally terminate `/nats` when root NATS keepalive PONG is missing: `WS_NATS_PROXY_CLOSE_ON_KEEPALIVE_MISS=1`, `WS_NATS_PROXY_TERMINATE_ON_KEEPALIVE_MISS=1`.
- [x] Run independent raw WS and AdaOS/nats-py transport probes from `tools` without `adaos api serve`; confirm raw root `/nats` echo is stable while AdaOS/nats-py transport still stalls after the first few messages.
- [x] Align diagnostic tools with runtime default: prefer `wss://api.inimatic.com/nats`; probe `wss://nats.inimatic.com/nats` only when `HUB_NATS_PREFER_DEDICATED=1`.
- [x] Prepare backend WS-NATS frame-accounting patch to report client, upstream-write, upstream-read, and downstream-send counters on close.
- [x] Deploy backend frame-accounting patch to root and inspect the next `conn close` / `upstream close` summaries for `PUB -> MSG -> downstream` mismatches.
- [x] Confirm frame counters show route traffic reaches the hub before the root closes on the first keepalive miss.
- [x] Prepare backend patch to stagger WS ping vs NATS-data keepalive and close only after repeated keepalive misses (`WS_NATS_PROXY_KEEPALIVE_MAX_MISSES`, default 3).
- [x] Compare stable `tools` WebSocket route with failing AdaOS core route and identify the proxy-auto vs direct-route difference.
- [x] Change core default to `proxy=True` / system proxy auto-detect, with `HUB_NATS_WS_PROXY_MODE=none` as an explicit direct-route diagnostic override.
- [x] Add transport regression tests for proxy default and coalesced root `PING` control frames.
- [x] Verify isolated `nats-py + AdaOS WebSocketTransport` stays connected and echoes traffic for 45s through the proxy-auto route.
- [x] Re-run `adaos api serve` for about 190 seconds and confirm no NATS watchdog reconnect, route timeout, remote-route fallback, or event-loop lag/hang before requested shutdown.
- [x] Re-run live local + root-routed browser acceptance with the updated core and confirm the remote browser loads Yjs data.
- [x] Update code/env defaults so the stable route is the default: `HUB_NATS_WS_PROXY_MODE=auto` / unset, and direct route only via `HUB_NATS_WS_PROXY_MODE=none`.
- [x] Reopen the Windows 2026-04-30 regression and test with client-originated NATS data ping disabled; confirm `/nats` still fails without any `data_pings_tx`.
- [x] Restore the confirmed Windows profile: `HUB_NATS_WS_DATA_PING_S=auto` sends a conservative 5s NATS-data ping only on Windows+`websockets`; Linux remains disabled unless explicitly requested.
- [x] Validate raw `/nats` channel with `tools/diag_nats_ws.py` and `tools/diag_nats_ws_concurrent.py`; confirm raw WebSocket framing remains healthy for 90s under concurrent PUB/MSG and Root NATS keepalive traffic.
- [x] Confirm nats-py-style clients still fail while raw WebSocket clients stay healthy.
- [x] Prepare Root WS proxy fix for nats-py-style clients: disable proxy-originated client keepalive by default and terminate upstream NATS `PING` frames at the proxy for non-transparent hub clients.
- [x] Harden Root WS proxy against stale root env: ignore legacy `WS_NATS_PROXY_KEEPALIVE_ENABLE=1` for normal hub clients unless `WS_NATS_PROXY_KEEPALIVE_FORCE=1`; use `WS_NATS_PROXY_CLIENT_KEEPALIVE_ENABLE=1` only for targeted diagnostics.
- [x] Harden Root WS proxy against stale root WS control ping env: ignore legacy `WS_NATS_PROXY_WS_PING=1` for normal hub clients unless `WS_NATS_PROXY_WS_PING_FORCE=1`; use `WS_NATS_PROXY_CLIENT_WS_PING_ENABLE=1` only for targeted diagnostics.
- [x] Add `WebSocketTransportWebsockets` send-path diagnostics (`current_send`, `last_send`, send counters/errors) to prove whether local sends actually correspond to Root-received client frames.
- [x] Add weather observer slow-callback diagnostics and rule out weather as the source of the `/nats` disconnect.
- [x] Move local `events.log` observe writes off the event loop; confirm two-browser run no longer shows `observe.py`, `_write_local`, loop lag, or `flush_slow` starvation.
- [x] Re-test aiohttp under two-browser load; confirm the old aiohttp `Cannot write to closing transport` failure still exists.
- [x] Re-test websockets under the current Root env; confirm it still flaps, so the remaining blocker is not the local observe file I/O path alone.
- [x] Change `HUB_NATS_WS_IMPL=auto` back to the patched websockets transport on both Windows and Linux; keep aiohttp explicit-only.
- [x] Restore Root env to the confirmed Windows profile for normal hub clients: `WS_NATS_PROXY_KEEPALIVE_ENABLE=0`, `WS_NATS_PROXY_CLIENT_KEEPALIVE_ENABLE=0`, `WS_NATS_PROXY_KEEPALIVE_FORCE=0`, `WS_NATS_PROXY_WS_PING=0`, `WS_NATS_PROXY_CLIENT_WS_PING_ENABLE=0`, `WS_NATS_PROXY_WS_PING_FORCE=0`, and disable deep wiretap/ping trace unless diagnosing one run.
- [x] Detect that current backend parsing would coerce `WS_NATS_PROXY_SUPERSEDE_GRACE_MS=0` back to `15000`.
- [x] Run a no-code Root env experiment with `WS_NATS_PROXY_SUPERSEDE_GRACE_MS=1` and verify root logs show `supersede_grace_ms=1` / immediate `closing superseded hub ws-nats connection`; conclude this was not the decisive fix.
- [x] Identify and fix the decisive Windows route regression: do not expose the legacy `HUB_NATS_WS_PROXY=auto` environment variable to Python proxy discovery.
- [x] Re-run live Windows root-routed browser acceptance after the proxy-env fix; confirm remote Yjs data loads without `/nats` watchdog reconnects and without route starvation.
- [x] Decide not to patch backend supersede parser/defaults for the current acceptance path; keep `WS_NATS_PROXY_SUPERSEDE_GRACE_MS=1` as an optional Root experiment, not the primary stable profile.
- [ ] Reconfirm Linux/RU after rollout still uses `auto -> websockets` and keeps the accepted first-window behavior.

#### F3M-002: Root-routed HTTP requests timeout during startup

Status: closed for the current 3-minute goal.

Evidence:

- Repeated `http route: timeout` and `http proxy failed` for `/api/node/status`, `/api/node/reliability`, `/api/node/reliability/summary`, and `/api/node/infrastate/snapshot`.
- Root logs show many `route.v2.to_hub` requests reach the WS/NATS proxy and are sent downstream, but not all responses return before `15000ms`.

Working hypothesis:

- Route request delivery is not the only failure point.
- Missing or delayed hub/browser response handling, reconnect overlap, or slow local handlers may leave route replies unpublished.

Actions:

- [x] Add route request/reply lifecycle diagnostics.
- [x] Add route publish/flush slow warnings and pending-data diagnostics.
- [x] Remove route key hot-path config reload.
- [x] Verify latest 180-second soaks have no `http route: timeout` and no `http proxy failed`.
- [ ] Reopen and correlate one timed-out `keyTag` from root logs with hub route callback logs if a timeout recurs.
- [ ] Add a compact route timeout summary grouped by path and keyTag.
- [ ] Rate-limit or defer non-critical root probes while NATS is reconnecting.

#### F3M-003: Browser WS open ack fallback is still observed

Status: fixed in backend and deployed; supersede close behavior refined locally and awaiting backend deploy.

Evidence:

- `ws route: open ack fallback elapsed`
- early frame counters are present before `open_ack`.
- During root-routed remote Yjs reconnects, a browser `open` can be dropped while hub route subscription is absent; fallback then flushes early frames and the hub records `no_upstream`.
- Latest root logs after the backend deploy show bounded `open ack retry` / `open republish` behavior and no captured `open ack fallback` or `no_upstream` in the local diagnostics window, but a superseded `/nats` peer can still be closed on `route_ready` while route open retries are in flight.

Working hypothesis:

- The hub can receive early frames before the route open acknowledgement is returned to root.
- The previous fallback is harmless only when the hub actually processed `open` but did not send `open_ack`; it is harmful when `open` was dropped during NATS route reconnect because it forwards frames before an upstream tunnel exists.

Actions:

- [x] Add early frame count/bytes to open ack fallback logs.
- [x] Verify latest 180-second soaks have no `open ack fallback`.
- [x] Reopen and correlate fallback cases with NATS reconnect and route timeout windows.
- [x] Replace fallback frame flush with bounded `open` retry (`ROUTE_WS_OPEN_ACK_MAX_ATTEMPTS`, default 4).
- [x] Deploy backend route-open retry patch to root.
- [x] Verify latest root-routed browser diagnostics produce bounded `open ack retry` recovery, not `open ack fallback` frame flush.
- [x] Prepare backend switch `WS_NATS_PROXY_CLOSE_SUPERSEDED_ON_ROUTE_READY=0` so route readiness no longer cuts the grace window short by default.
- [ ] Reconfirm after root `/nats` keepalive is restored that root-routed browser load produces no `no_upstream` incident.

#### F3M-004: Yjs write pressure during first attach

Status: closed for the current 3-minute goal.

Evidence:

- `YJS owner flow above threshold ... source=yjs.gateway_ws channel=core.yjs.gateway.live_room.persist`
- High write count and byte bursts around first browser attach.

Resolution:

- Initial gateway first-attach bursts are expected and preserve durable YStore/subnet replication semantics.
- The current fix keeps immediate persistence and changes diagnostics to alert on sustained gateway pressure rather than peak-only attach bursts.

Actions:

- [x] Attribute Yjs pressure by source/channel.
- [x] Split gateway persistence out of `_by_owner/unknown` as `_by_owner/gateway_ws`.
- [x] Move YRoom diagnostic ystore runtime snapshots out of the realtime hot path by default.
- [x] Confirm latest 180-second soak has no `_by_owner/unknown` pressure and no YRoom `runtime_snapshot()`/`Path.stat()` blocking stack.
- [x] Decide not to batch/debounce `gateway_ws` ystore writes for this goal; durability wins over cosmetic write smoothing.
- [x] Tune gateway-owner pressure alerts to suppress peak-only first-attach warnings while preserving sustained-pressure alerts.
- [x] Confirm final 180-second soak has no Yjs owner pressure warning.

#### F3M-005: Event loop lag/hang during startup and shutdown

Status: locally fixed for the severe freeze path; final root-load verification pending after `/nats` keepalive is restored.

Evidence resolved:

- Earlier lag stacks pointed to route key config reload, skill runtime path preparation, subnet-directory SQLite commit, NATS diagnostic file append, and skill snapshot refreshes triggered by `sys.ready`.
- The final accepted run has no real event loop lag/hang, no control lifecycle delayed warning, and no slow async handlers.
- Shutdown can still emit an expected idle-wait suppression debug line and a requested NATS disconnect warning.
- The 2026-04-29 root-load run exposed a new severe Windows freeze stack: `service_supervisor._watchdog_loop -> refresh_discovered -> asyncio.to_thread -> run_in_executor -> ThreadPoolExecutor._adjust_thread_count -> Thread.start`.
- After making service discovery refresh inline and cached, the `service_supervisor` / `Thread.start` stack did not recur in follow-up diagnostics.
- A 2026-05-14 direct Root MCP smoke correctly classified remote MCP as
  `upstream_unavailable`, but the concurrently running local hub emitted a
  severe hang stack in `browsers_skill._on_refresh ->
  _refresh_snapshot_sync -> _run_coro -> Future.result()`. The smoke was not
  the cause; it exposed a skill refresh handler blocking the event loop while
  waiting for snapshot projection.

Resolution:

- Known synchronous startup/hot-path operations have been moved off the event loop or deferred out of `sys.ready`.
- Diagnostic writes remain enabled, but NATS WS JSONL append now runs in a worker thread.
- `browsers_skill` refresh now schedules snapshot projection on its single
  projection executor without waiting when it is invoked from an active event
  loop. Pending refreshes are coalesced by webspace, and projection failures are
  logged asynchronously.

Actions:

- [x] Add structured loop lag/hang logs.
- [x] Move selected sync subscriptions to worker threads.
- [x] Make Selector loop opt-in diagnostics only.
- [x] Add per-topic/adapted-handler labels to slow handler warnings.
- [x] Move startup native capacity/subnet registry work to a worker thread.
- [x] Suppress idle Proactor wait stacks as hang false positives.
- [x] Move active local `infrascope_skill` background target discovery to a worker thread.
- [x] Move slow `ui.notify` network work away from eventbus critical path.
- [x] Move hub subnet-directory staler heartbeat/stale sweep SQLite work off the event loop.
- [x] Move NATS WS diagnostic file writes off the NATS supervisor hot path.
- [x] Preserve skill/handler labels through SDK bus adaptation so slow warnings identify the exact skill.
- [x] Remove heavy `sys.ready` refresh work from active local `infra_access_skill` and `infrastate_skill` workspace/runtime copies.
- [x] Avoid worker-thread hop for `infrastate_skill.on_runtime_event` on `sys.ready`.
- [x] Confirm final 180-second soak has no real event loop lag/hang and no slow async handler warnings.
- [x] Avoid recurring `asyncio.to_thread` submission in skill-service discovery refresh.
- [x] Make control lifecycle await-resume watcher opt-in so normal heartbeats do not start a diagnostic thread from the event loop.
- [x] Confirm follow-up diagnostics have no `service_supervisor` / `Thread.start` stack and no 60-second event-loop freeze.
- [x] Remove blocking `Future.result()` wait from `browsers_skill` refresh
  handlers and add regression coverage for event-loop invocation.
- [ ] Reconfirm no real loop lag/hang during the final root-routed browser acceptance after `/nats` keepalive is restored.

#### F3M-006: Root MCP local startup uses fallback as the normal path

Status: closed for the current 3-minute goal.

Evidence resolved:

- Earlier accepted runs emitted `Root MCP bridge upstream unavailable; using embedded local Root MCP operation=surface` because local SDK calls went through the public Root MCP bridge.
- The public backend bridge is a direct HTTP proxy to `ADAOS_BASE`/`X-AdaOS-Base`, which is not a reliable route from the public root service back to a local hub.

Resolution:

- Local SDK `get_local_*` Root MCP calls now mark local target contexts and use embedded local registry/session/token/audit operations first.
- The remote bridge fallback remains available for explicit non-local Root MCP usage and for resilience when local-first is disabled.

Actions:

- [x] Add `ADAOS_ROOT_MCP_LOCAL_FIRST` with local-first enabled by default.
- [x] Keep `ADAOS_ROOT_MCP_LOCAL_FIRST=0` as an escape hatch for explicit bridge validation.
- [x] Add a regression test proving local runtime calls do not probe the bridge.
- [x] Confirm final 180-second soak has `root_mcp_fetch_failed=0` and `embedded_fallback=0`.

#### F3M-006A: Keep node selectors out of Root MCP managed target IDs

Status: implemented, awaiting local UI confirmation.

Evidence:

- Infra Access `issue_codex_session` received the UI node UUID
  `8db40740-b3ff-44bf-baf5-9fb013b35b01` as `target_id` and Root MCP rejected
  it with `managed target ... is not registered`.
- The current managed-target registry uses hub-scoped target IDs such as
  `hub:<subnet_id>`; UI node selectors and named-entity device refs are a
  different addressing layer.

Resolution:

- Root MCP SDK local target context now resolves local aliases such as
  `node_id`, `node:<node_id>`, `device:member:<node_id>`, and bare
  `subnet_id` to `hub:<subnet_id>`.
- Infra Access treats non-`hub:` selectors from the UI as node selectors and
  lets the SDK infer the local hub target instead of forwarding the selector to
  the Root MCP target registry.

Actions:

- [x] Add SDK regression coverage for local selector to managed-target
  resolution.
- [x] Add Infra Access runtime coverage that `issue_codex_connection` does not
  pass a UI node UUID as the Root MCP target.
- [ ] Confirm manually from `[Node 0] Infra Access`: click the Codex session
  action and verify no `managed target '<node uuid>' is not registered` error.

#### F3M-006B: Align Codex ProfileOpsRead with advertised Root MCP read tools

Status: implemented and locally smoke-verified.

Evidence:

- The local stdio Codex bridge advertised operational read tools such as
  `get_status`, `get_runtime_summary`, `get_operational_surface`, and
  `get_activity_log`.
- Fresh `ProfileOpsRead` MCP session leases only received generic
  `operations.read.*` plus memory-profile capabilities, so the advertised tools
  returned policy-denied payloads instead of operational data.

Resolution:

- `ProfileOpsRead` now includes the read-only `hub.get_*` capabilities that the
  Codex bridge exposes.
- `ProfileOpsControl` now includes the same read set plus `hub.run_healthchecks`.
- The public backend capability-profile definition is kept in sync with the
  local hub implementation.

Verification:

- `pytest tests/test_root_mcp_foundation.py` passes.
- `pytest tests/test_sdk_root_mcp.py tests/test_infra_access_skill_runtime.py`
  passes in the focused MCP/infra_access slice.
- Local stdio MCP smoke against `adaos-local-hub` reports 37 tools and
  `ok=true` for `foundation`, `get_status`, `get_runtime_summary`,
  `get_operational_surface`, `get_activity_log`, `get_skill_logs`, and
  `get_subnet_diagnostics`.

#### F3M-006C: Classify direct remote MCP health separately from bearer validity

Status: implemented locally; public deployment/fresh-bearer validation pending.

Evidence:

- Fresh `ProfileOpsRead` MCP session for `hub:sn_92ffc943` was active, but
  direct remote MCP smoke on 2026-05-14 returned HTTP `502` for:
  `GET /v1/root/mcp/foundation`, JSON-RPC `initialize`, JSON-RPC
  `tools/list`, and JSON-RPC `tools/call:get_status`.
- The same `502` class reproduced on the regional `ru.api.inimatic.com`
  endpoint and the global `api.inimatic.com` endpoint. This means the check is
  failing before useful bearer/tool-level validation, not as an ordinary
  `401/403` token rejection.
- Backend inspection showed the public `/v1/root/mcp` route was still installed
  as a legacy upstream proxy to `ADAOS_BASE` (`http://127.0.0.1:8777` by
  default). In public zones that makes a healthy bearer look like an upstream
  outage because the backend is trying to reach its own localhost instead of a
  native Root MCP surface.
- The observed 2026-05-13 `deny` -> `allow` transition for the same
  `mcp_session_lease:*` actor should be treated as profile/runtime drift during
  rollout, not expected steady-state behavior. Session leases should carry a
  frozen grant snapshot; after changing profiles or endpoint mode, issue a
  fresh bearer and correlate events by session id and issued-at time.

Resolution:

- Added `adaos dev root mcp smoke` so operator and LLM diagnostics use one
  repeatable transport check instead of manual curl snippets.
- The smoke command redacts auth by design, exits non-zero on failure, and
  classifies `401/403` as `auth_failed`, `404` as `endpoint_not_found`,
  JSON-RPC errors as `jsonrpc_error`, and `5xx` responses such as `502` as
  `upstream_unavailable`.
- The public backend now installs only the native `/v1/root/mcp`
  HTTP/JSON-RPC route. The historical `/v1/root/mcp -> ADAOS_BASE` upstream
  proxy has been removed for the MVP to avoid ambiguous operator diagnostics.
- Follow-up live smoke after deployment still returned
  `adaos_root_mcp_upstream_failed`, proving the legacy proxy was still taking
  precedence in that deployment. After legacy removal, this response body means
  the deployed backend is stale.
- A later deployment attempt did not update backend because reverse-proxy
  health failed before slot cutover. `nginx -t` rejected
  `ssl_verify_client off` inside `location` blocks in
  `vhost.d/api.inimatic.com`. The API vhost now keeps
  `ssl_verify_client optional` only at server level; public routes do not need
  per-location disablement, and protected routes still enforce mTLS via
  `$ssl_client_verify`.
- Backend Root MCP `ProfileOpsRead`/`ProfileOpsControl` capabilities were
  aligned with the Python Root MCP profile shape, including `hub.get_status`,
  `hub.get_runtime_summary`, `hub.get_operational_surface`, activity/capability
  summaries, and memory read tools.
- After the route repair deployed, direct public smoke advanced from `502` to
  `401`, which confirms the public request is reaching an auth-gated Root MCP
  handler instead of the removed legacy upstream proxy. The failing bearer had
  an `rmcp_session_*` prefix produced by the local SDK/`infra_access_skill`
  embedded session issuer, while the public backend native route stores and
  validates its own `mcp_*` session leases in backend Redis. A local hub-issued
  `rmcp_session_*` token is therefore valid for the local/embedded Root MCP
  context, but not for direct public `https://api.inimatic.com/v1/root/mcp`
  smoke.
- The backend auth fallback previously returned `client_certificate_required`
  for any unrecognized Root MCP credential. That made a bearer issuer mismatch
  look like an mTLS problem. The backend now reports `invalid_token` when an
  auth header is present but not accepted; the CLI smoke also surfaces JSON
  error/message bodies so operators can see the real rejection reason.

Verification:

- `pytest tests/test_root_mcp_smoke.py` covers `502`,
  auth-failure, and JSON-RPC-error classification.
- `npm run build:api` passes in `src/adaos/integrations/adaos-backend`.
- The 2026-05-15 live response body
  `{"error":"adaos_root_mcp_upstream_failed","detail":"fetch failed"}`
  identifies the legacy proxy path rather than the native Root MCP handler.
- The later 2026-05-15 live response body
  `{"error":"client_certificate_required","message":"Client certificate is required."}`
  is auth-gated native behavior before the improved backend error text is
  deployed; with an unrecognized bearer it should become `invalid_token`.
- Manual check to repeat after backend/root route work:
  `adaos dev root mcp smoke --mcp-http-url https://ru.api.inimatic.com/v1/root/mcp --auth-env-var ADAOS_ROOT_MCP_AUTH`.

Actions:

- [x] Add CLI smoke check for direct remote MCP.
- [x] Document failure classification and human verification path.
- [x] Fix the public backend route shape so native Root MCP can answer
  `initialize`, `tools/list`, and `get_status`.
- [x] Remove the legacy Root MCP upstream proxy from the backend MVP.
- [x] Remove invalid location-level `ssl_verify_client off` directives from
  API nginx vhost templates so reverse-proxy health can pass.
- [x] Deploy the backend route repair to the target zone.
- [x] Surface JSON error bodies in `adaos dev root mcp smoke` output.
- [x] Return `invalid_token` instead of `client_certificate_required` when the
  public Root MCP route receives an auth header that does not resolve.
- [ ] Align Infra Access `Fresh Bearer Token` issuance with the selected
  endpoint: local bridge flows may keep `rmcp_session_*`, while direct public
  Root MCP smoke must use backend-native `mcp_*` sessions or a backend-accepted
  owner bearer.
- [ ] After deployment, issue a fresh backend-native `ProfileOpsRead` session
  and run the smoke against the fresh session, then record the target/tool
  result here.

#### F3M-006D: Split public API and mTLS API surfaces

Status: planned.

Context:

- The current MVP uses one `api.inimatic.com` nginx server with
  `ssl_verify_client optional`. This keeps public browser/bootstrap endpoints
  reachable while still forwarding `$ssl_client_verify` and certificate headers
  to backend routes that enforce mTLS.
- nginx chooses `ssl_verify_client` during TLS handshake, before a URI-specific
  `location` is selected. That means we cannot safely express "do not request a
  client cert for this public path" with `ssl_verify_client off` inside
  `location`; nginx rejects that config.
- For now, public routes rely on server-level `optional`, and protected routes
  enforce mTLS in backend/nginx routing by checking `$ssl_client_verify`.

Target architecture:

- Keep `api.inimatic.com` as the hub/node API surface that requests client
  certificates during TLS handshake with `ssl_verify_client optional`. Backend
  routes on this host use `$ssl_client_verify` and forwarded certificate
  headers to enforce mTLS where required.
- Add `pub.inimatic.com` as the public API surface with no client-certificate
  request during TLS handshake. Browser, bootstrap, pairing, operator bearer,
  and Codex/Root MCP public entrypoints should move here unless they explicitly
  need the mTLS-aware surface.
- Make backend route policy explicit: public bearer/JWT routes and mTLS routes
  should be distinguishable by host/surface, not only by path conventions.

Checklist:

- [x] Choose canonical host split: `api.inimatic.com` for mTLS-aware API,
  `pub.inimatic.com` for public API.
- [ ] Add nginx/vhost templates for `pub.inimatic.com` without
  `ssl_verify_client`.
- [ ] Keep `api.inimatic.com` configured with `ssl_verify_client optional` at
  server scope for hub/node mTLS-aware routes.
- [ ] Move browser/bootstrap/pairing/operator bearer/Codex Root MCP defaults
  to `pub.inimatic.com`.
- [ ] Add deploy smoke that runs `nginx -t` and validates both public and mTLS
  host routing before slot cutover.
- [ ] Update bootstrap/node docs once the host split is live.

#### F3M-007: First-3-minute memory footprint

Status: closed for the current 3-minute goal.

Evidence:

- User requested memory state as part of the final loading evaluation.
- A naive first sampler captured only a launcher stub; the final accepted sampler measures the whole process tree and the heaviest child process.

Resolution:

- The final accepted run sampled the `adaos api serve` process tree during loading-to-ready and throughout the 180-second soak.
- Memory reached a startup plateau and stayed bounded: process-tree peak PrivateMemory was 230.555 MB and the last sample was 228.117 MB.
- Repeat final acceptance with browser `/ws` attach stayed bounded as well: process-tree peak PrivateMemory was 145.211 MB and the last sample was 145.211 MB.

Actions:

- [x] Add process-tree memory sampling to the final soak verification.
- [x] Capture first, ready, peak, and final memory samples.
- [x] Confirm peak and final memory values are in the same plateau range.
- [x] Confirm no memory-related traceback, supervisor failure, or event-loop lag appears in the final accepted run.

#### F3M-008: Remote root-routed Yjs attach closes under browser load

Status: closed for the current connectivity goal; keep Yjs load performance as a watch item.

Evidence:

- With a local browser and a root-routed remote browser connected at the same time, the remote browser repeatedly hit `connection closed` while local access remained usable.
- Root reverse-proxy accepted remote `/hubs/sn_6acf0c01/yws/desktop` upgrades with `101`, then emitted repeated `SSL_read() failed ... bad record mac` around keepalive/upgraded traffic.
- Hub logs showed `yws connection closed webspace=desktop` around the same window as NATS WS reconnects.
- Control lifecycle delay stacks under load pointed at WebSocket/Yjs send/write paths, including expensive websocket compression and Yjs load-mark history append.

Working hypothesis:

- The primary remote disconnect is downstream of root route/NATS liveness, not local browser failure.
- Large first-sync Yjs bursts should avoid WebSocket compression and avoid avoidable synchronous diagnostics on the event loop.
- Root-routed Yjs must tolerate a dropped `open` during hub route reconnect by retrying `open`, not by flushing browser frames before upstream exists.
- Latest diagnostics show route-open retry is working; the remaining remote browser close loop followed `/nats` `ConnectionClosedError` / watchdog reconnects.
- The core proxy-auto fix removes the reproduced `/nats` churn in isolated, full local API, and live hub-browser runs.

Actions:

- [x] Disable local uvicorn WebSocket per-message deflate for API serve.
- [x] Re-run a 3+ minute local + root-routed browser diagnostic with local raw NATS keepalive and confirm no NATS watchdog reconnect, no unexpected YWS close, and no compression-related control-lifecycle warning stack before requested shutdown.
- [x] Replace root route `open_ack` fallback frame flush with bounded `open` retry.
- [x] Deploy backend route-open retry / supersede-grace patch to root.
- [x] Confirm latest root logs show route retry/supersede behavior and no captured fallback frame flush.
- [x] Prepare backend WS-NATS liveness refinement: configurable WS ping and no immediate supersede close on `route_ready`.
- [x] Deploy backend WS ping / supersede-grace refinement to root.
- [x] Prepare backend keepalive-miss close refinement so a half-open `/nats` tunnel is proactively closed and replaced.
- [x] Align core `/nats` transport with stable raw `tools` route by defaulting to `websockets` proxy-auto.
- [x] Re-run root-routed browser soak with updated core and both browsers connected.
- [x] Confirm no `permessage_deflate.encode` control-lifecycle delay stack recurs in the latest accepted windows.
- [x] Confirm no `hub route frame arrived while upstream is not connected` / `no_upstream` incident recurs in the latest accepted windows.
- [x] Confirm `/nats` stays connected for at least 180 seconds and remote `/yws` does not close due to route errors before requested shutdown.
- [ ] If load-mark history append still appears in loop-delay stacks, move history append off the event loop or batch it under diagnostics-only mode.

#### F3M-009: Reliability summary polling blocks the event loop

Status: closed for the current 3-minute goal.

Evidence:

- While local and root-routed browsers were connected, repeated client polling of `/api/node/reliability/summary` produced a control-lifecycle warning stack through `node_reliability_summary -> current_reliability_payload -> load_config -> runtime_state_mtime_ns -> Path.resolve`.
- The endpoint response is relatively large and mostly stable, so the long-term architecture should move toward a reusable status plane with thin monitoring deltas.

Resolution:

- `/api/node/reliability` and `/api/node/reliability/summary` now build the reliability payload in an AnyIO worker thread.
- The follow-up architecture work is tracked under `Reusable Status Plane And Thin Monitoring`.

Actions:

- [x] Move reliability payload construction for the high-frequency HTTP endpoints off the event loop.
- [x] Update the isolated reliability endpoint test double so it reflects current bootstrap imports.
- [x] Verify targeted reliability endpoint tests pass.
- [x] Re-run a 3+ minute local + root-routed browser diagnostic and confirm no `node_reliability_summary` / `current_reliability_payload` warning stack recurs.

#### F3M-010: Linux/RU root-routed browser selects the wrong root zone

Status: fixed for connectivity; follow-up memory pressure is tracked separately.

Evidence:

- Linux hub `sn_92ffc943` is configured for `zone=ru` and keeps `hub_root: ready/stable` through `wss://ru.api.inimatic.com/nats`.
- `https://ru.api.inimatic.com/v1/browser/hub/status?hub_id=sn_92ffc943` returns `online`, while the central root returns `offline` for the same hub.
- Earlier Linux runtime saw root-routed HTTP status probes but no `/ws` or `/yws` route-open attempts, so the remote browser data path was not reaching the RU hub runtime.
- After the zone-aware browser bundle deploy, two Linux root-routed browsers loaded data, confirming the route-zone selection fix.

Working hypothesis:

- `AppComponent` and pairing flows use the deployment-zone service, but `AdaosClient.rootHubBaseUrl()` independently falls back to `ROOT_BASE`.
- A browser can therefore pass status/pairing through the RU root while YJS/WS attaches through the central root, where the Linux hub is offline.

Actions:

- [x] Add browser-side `hub_id -> zone` persistence after successful root status and pairing approval.
- [x] Make YDoc root-proxy attach probe known zones through `/v1/browser/hub/status` and select the online root before setting `/hubs/<hubId>` base.
- [x] Fall back from `adaos_hub_id` to `adaos_last_subnet_id` when restoring a browser session.
- [x] Confirm client build succeeds after the async root-zone resolver change.
- [x] Deploy the updated client bundle and confirm Linux remote browsers open/load data through the RU root.
- [x] Confirm Linux reliability changes from `sync_runtime.yws=0 rooms=0 opens=0/0` to active browser/YWS behavior after remote browser attach.
- [x] Mark raw hub-credential NATS diagnostic tools as potentially superseding the live runtime connection.
- [ ] Update raw diagnostic tools or root auth semantics so diagnostic NATS probes do not supersede the live runtime connection.

#### F3M-011: Linux remote-browser attach triggers runaway memory growth

Status: fixed for the first-3-minute goal; long-run plateau confirmation pending.

Evidence:

- With two Linux root-routed browsers attached, browser data loaded, then links oscillated between recovery/degraded and the supervisor restarted the slot.
- Supervisor memory telemetry showed RSS growth around 1.8GB in the active runtime and growth slope above 800MB/min.
- `yjs_load_mark.jsonl` grew to hundreds of MB; recent load-mark rows showed both `_by_owner/skill_infrastate_skill` and `_by_owner/gateway_ws` carrying large sustained byte rates.
- This pattern indicates backend-originated detached Yjs diffs are persisted once by `async_get_ydoc` and then persisted again by the live room while being fanned out to browsers.
- 2026-05-08 regression pass: memory still climbed under destructive `infrastate` load even though tool-call quarantine fired. Hub logs showed `skill:infrastate_skill` quarantined via `infrastate_skill:get_snapshot`, while slow `webio.stream.snapshot.requested` subscription handlers kept running outside `SkillManager`.
- 2026-05-08 follow-up regression pass on slot A / `2ec14c5`: after browser
  click activity, the hub reached about 1.16GB RSS. Logs showed more than 1000
  `webio.stream.snapshot.requested` and about 1900
  `webio.stream.subscription.changed` events since restart, while quarantine
  service state was written under `default` for events without explicit
  `webspace_id`; the desktop browser therefore missed the visible
  `data.yjs_qrnt` signal.
- Browser symptom was only `Action failed: skill_owner_quarantined`; the response already carried owner/tool/reason, but the client collapsed it to the error code. Skill-local quarantine logging also failed when `ADAOS_SKILL_MEMORY_PATH` pointed at `data/db/skill_env.json` instead of a directory.
- Scenario shortcut icons were present in the effective catalog as node-attributed scenario apps, but the client-side app filter treated every scenario app with a real `node_id` as remote/non-desktop and hid it.

Working hypothesis:

- Skill/core writes that skip the direct live-room fast path correctly write a detached diff to YStore, then apply that diff to the active room so browsers receive it.
- The active room currently treats that already-persisted backend diff like a browser-origin update and writes it to YStore again as `gateway_ws`.
- Under two remote browsers and active infrastate streams, duplicate YStore writes plus unbounded load-mark history amplify memory, disk, and diagnostic pressure.
- A second amplifier was `/api/node/infrastate/snapshot`: root-routed browser fallback probes called `get_snapshot`, which projected the full diagnostic snapshot into Yjs and returned multi-megabyte payloads.
- A third amplifier was supervisor policy profiling: the memory detector could restart the runtime into `sampled_profile` during the first browser attach, causing recovery/degraded oscillation even after transport was healthy.
- A fourth amplifier is any skill `@subscribe(...)` path that writes to Yjs without passing through `SkillManager.run_tool`; owner quarantine must gate skill event handlers as well as public tools.

Actions:

- [x] Add a short-lived exact-update marker for backend-originated room fanout updates that were already persisted.
- [x] Make `DiagnosticYRoom` skip only matching duplicate backend-origin YStore writes while preserving browser fanout and browser-origin persistence.
- [x] Add gateway tests covering duplicate skip and unmarked browser-update persistence.
- [x] Cap `yjs_load_mark.jsonl` by default and limit load-mark stream rows to the top pressure buckets.
- [x] Disable full event payload logging by default to avoid duplicating large `io.out.stream.publish` payloads into rotating logs.
- [x] Make `infrastate_skill.get_snapshot` read-only for HTTP callers unless `project=True` is explicitly requested.
- [x] Return a compact client snapshot with truncated diagnostic card content and `last_refresh_ts` so browsers stop repeatedly falling back to heavy HTTP snapshot loads.
- [x] Delay automatic policy-triggered memory profiling restarts until after the first 300 seconds of runtime uptime.
- [x] Deploy the active core/skill hotpatches to the Linux hub.
- [x] Sync the Linux hub workspace `infrastate_skill` hotpatch back into local `.adaos/workspace` for the next `skill push`.
- [x] Compare Linux hub core slots `A` and `B` with local source and identify the only remaining intentional local commit deltas.
- [x] Run a two-browser Linux soak and confirm the first 3 minutes do not hit NATS churn, route timeouts, event-loop hangs, or memory-profile restarts.
- [x] Confirm memory no longer grows toward the previous 3GB supervisor restart pattern during the first acceptance window.
- [x] Run a longer 10-15 minute Linux two-browser soak and confirm RSS reaches a bounded plateau.
- [x] Confirm load-mark no longer reports simultaneous sustained high byte rates for the same backend diff under both `skill_infrastate_skill` and `gateway_ws`.
- [x] Add Yjs owner-guard admission to SDK skill subscription wrappers before executing `@subscribe` handlers.
- [x] Make browser skill-action quarantine warnings include owner/tool/reason/retry instead of only `skill_owner_quarantined`.
- [x] Fix skill quarantine JSONL logging when the skill memory path resolves to `data/db/skill_env.json`.
- [x] Keep scenario shortcut apps visible on the desktop even when the effective catalog tags them with the local hub `node_id`.
- [x] Hotpatch the Linux hub active slot B with backend subscription/logging changes and restart the runtime under supervisor.
- [x] Fix owner-guard implicit webspace normalization so desktop browsers can
  see `data.yjs_qrnt` for subscription-triggered quarantines.
- [x] Add bounded/coalesced eventbus handling for
  `webio.stream.subscription.changed` and stale queued stream handlers.
- [x] Hotpatch Linux hub active slot A and Mediapoint/member active slot B with
  Wave 10 owner-guard/eventbus containment changes, then restart runtimes under
  supervisor.
- [ ] Rebuild/redeploy the client bundle so quarantine diagnostics and scenario shortcut filtering changes are visible in the browser.
- [ ] Run a destructive `infrastate` browser-load soak after the client deploy and confirm skill subscription quarantine stops the memory climb.
- [ ] Design safe off-hot-path YStore replay compaction for live browser load; direct live backup can exceed a 60s request window.
- [ ] Tune YStore replay defaults or background compaction so `sync_runtime` leaves `pressure` after browser warm-up.

### Operating Checklist

Before a 3-minute soak:

- [ ] `ADAOS_WIN_SELECTOR_LOOP=0`.
- [ ] `HUB_NATS_WS_DIAG_FILE=.adaos/diagnostics/nats_ws_diag.jsonl`.
- [ ] `HUB_ROOT_LOG_SNAPSHOT=1`.
- [ ] `HUB_NATS_WS_PROXY` is unset in normal Windows and Linux runs. Use `HUB_NATS_WS_PROXY_MODE=auto` for the stable route and `HUB_NATS_WS_PROXY_MODE=none` only for direct-route diagnostics.
- [ ] For zoned root-routed browser acceptance, verify `/v1/browser/hub/status?hub_id=<hubId>` is `online` on the expected root zone and `rootHubBaseUrl()` resolves `/hubs/<hubId>` under that same zone.
- [ ] For root-routed browser acceptance, root backend can use its stable defaults: `WS_NATS_PROXY_KEEPALIVE_ENABLE=0`, `WS_NATS_PROXY_CLIENT_KEEPALIVE_ENABLE=0`, `WS_NATS_PROXY_KEEPALIVE_FORCE=0`, `WS_NATS_PROXY_KEEPALIVE_MS=20000`, `WS_NATS_PROXY_KEEPALIVE_REQUIRE_HANDSHAKE=1`, `WS_NATS_PROXY_UPSTREAM_NATS_PING_MS=20000`, `WS_NATS_PROXY_WS_PING=0`, `WS_NATS_PROXY_CLIENT_WS_PING_ENABLE=0`, `WS_NATS_PROXY_WS_PING_FORCE=0`, `WS_NATS_PROXY_CLOSE_SUPERSEDED_ON_ROUTE_READY=0`, `WS_NATS_PROXY_SUPERSEDE_GRACE_MS=15000`, `WS_NATS_PROXY_CLOSE_ON_KEEPALIVE_MISS=1`, `WS_NATS_PROXY_TERMINATE_ON_KEEPALIVE_MISS=1`, `WS_NATS_PROXY_KEEPALIVE_MAX_MISSES=3`.
- [ ] Capture process-tree memory samples during loading-to-ready and final acceptance runs.
- [ ] Keep normal diagnostic defaults common across Windows and Linux: `ADAOS_LOG_EVENTS_PAYLOAD=0`, `ADAOS_YJS_LOAD_MARK_STREAM_MIN_INTERVAL_SEC=2.0`, `ADAOS_YJS_LOAD_MARK_STREAM_TICK_INTERVAL_SEC=2.0`, `ADAOS_YJS_LOAD_MARK_STREAM_UNCHANGED_KEEPALIVE_SEC=30.0`, `ADAOS_YJS_LOAD_MARK_STREAM_TOP_N=24`, `ADAOS_YJS_LOAD_MARK_GATEWAY_HIGH_WPS=64`, `ADAOS_YJS_LOAD_MARK_GATEWAY_CRITICAL_WPS=128`, `ADAOS_YJS_LOAD_MARK_HISTORY_MAX_BYTES=10485760`, `ADAOS_YJS_BACKEND_ROOM_UPDATE_SKIP_TTL_S=30`, `ADAOS_INFRASTATE_SNAPSHOT_CONTENT_MAX_BYTES=4096`, `ADAOS_SUPERVISOR_MEMORY_AUTO_PROFILE_MIN_UPTIME_SEC=300`.
- [ ] Deep trace is off unless investigating one focused case.

During analysis:

- [ ] Start with `.adaos/logs/adaos.log`.
- [ ] Use `.adaos/diagnostics/nats_ws_diag.jsonl` to distinguish RX silence from local backpressure.
- [ ] Use `.adaos/root_log_snapshots/*__extract.log` to correlate root route timeout keyTags.
- [ ] Only request full terminal output when the local logs are missing the incident window.

### Post-Goal Follow-Ups

The current local runtime goal is complete. Keep these follow-ups in the issue
tracker so they are not lost:

- [ ] If public remote Root MCP access to local hubs is required, design a hub-routed backend/infra bridge rather than a direct upstream HTTP proxy.
- [ ] If any future 180-second run reopens NATS/route/Yjs/loop symptoms, add a new task under this same goal with the run id and exact log evidence.

## Reusable Status Plane And Thin Monitoring

### Goal

Replace high-frequency polling of large monitoring payloads with a reusable
status-plane architecture that core services and skills can both feed.

Success means:

- The client no longer polls large mostly-static payloads such as
  `/api/node/reliability/summary` for badge/status UI.
- Core services and skills can publish small versioned status cards through one
  common SDK/service contract.
- Heavy diagnostic data stays behind lazy streams, explicit details requests, or
  debug-only full snapshots.
- `infrastate_skill` and `infrascope_skill` use the same reusable status
  projection pattern instead of maintaining parallel ad-hoc debounce,
  fingerprint, stream snapshot, and last-good-cache logic.
- Existing UI views remain compatible during migration through thin summary
  endpoints and backward-compatible stream receivers.

### Current Status

Snapshot date: 2026-04-29.

Overall completion: 0% for the new status-plane goal.

Problem statement:

- The client periodically requests
  `http://127.0.0.1:8777/api/node/reliability/summary`.
- The response is large, while most values are unchanged between requests.
- This creates unnecessary local CPU/serialization work, route traffic, and
  diagnostic noise during the realtime startup window.
- `infrastate_skill` and `infrascope_skill` already demonstrate the desired
  split: compact Yjs state plus heavy webio stream receivers, but each skill
  implements its own projection helpers.

Design direction:

- Treat monitoring as a materialized status plane, not as repeated full
  snapshot construction.
- Use small status cards for hot UI state, stream receivers for warm/cold
  details, and explicit debug endpoints/tools for raw full diagnostics.
- Make the pattern reusable for current and future skills.

### Tasks

#### STATUS-001: Define the shared status card contract

Status: implemented for the shared ABI and first runtime pilot.

Target shape:

- A status card has stable identity: `id`, `owner`, `kind`, `scope`, and
  optional `webspace_id`.
- A status card has operator-facing state: `status`, `summary`, `severity`,
  `updated_at`, `ttl_ms`, and optional `incident_id`.
- A status card has change tracking: `version`, `fingerprint`, and
  `changed_at`.
- A status card can point to details without embedding them:
  `details_ref.kind`, `details_ref.receiver`, `details_ref.path`, or
  `details_ref.tool`.

Actions:

- [x] Define status values and normalization rules shared with
  `CanonicalStatus`.
- [x] Define JSON schema or typed dataclass for status cards.
- [x] Define staleness semantics when `ttl_ms` expires.
- [x] Define how cards map to incidents and active warnings.
- [ ] Document examples for core, `infrastate_skill`, `infrascope_skill`, and a
  future third-party skill.

#### STATUS-002: Add a materialized status registry/service

Status: in progress. First in-memory registry, runtime status-card emitter,
status-card dispatcher handler, and Node API surface are implemented.

Expected behavior:

- Producers publish small cards into an in-memory materialized registry.
- The registry deduplicates unchanged cards by fingerprint.
- The registry increments versions only on meaningful changes.
- The registry exposes cheap reads for thin UI summaries.
- The registry emits changed events for stream/push consumers.

Actions:

- [x] Add a core status registry service.
- [x] Add per-card fingerprinting that ignores volatile fields such as
  `updated_at`, `_age_s`, and `_ago_s`.
- [x] Add TTL/staleness sweep.
- [x] Add compact registry diagnostics: card count, changed count, stale count,
  and last publish latency.
- [x] Add unit tests for dedupe, versioning, TTL expiry, and owner scoping.

Implemented artifacts:

- `adaos.services.status_card_registry` stores materialized status cards per
  webspace and card id.
- `adaos.services.runtime_status_cards` publishes the built-in runtime lifecycle
  card as `status-card:runtime`.
- `status-card:*` dispatcher handler maps browser demand to canonical projection
  records.
- `/api/node/status-cards` publishes and reads thin status-card projections and
  materializes the runtime card by default.
- Missing demanded cards are reported as `unavailable`; expired cards are
  reported as `stale`.
- `/api/node/status-cards/sweep` can preview or remove TTL-expired cards per
  webspace; registry stats include `last_publish_latency_ms`, `sweep_total`,
  and `swept_total`.
- Registry snapshots include `registry_version`; it advances on meaningful
  status-card changes and sweep removals, not on unchanged keepalive publishes.

#### STATUS-003: Add skill-facing SDK helpers

Status: implemented for the first shared status-card SDK layer.

Expected API:

- `publish_status(...)` publishes one card.
- `publish_status_many(...)` publishes a small batch.
- `publish_status_stream(...)` binds a card to an existing webio stream receiver.
- Helpers normalize status tokens, compute fingerprints, and preserve
  skill/handler ownership.

Actions:

- [x] Add `adaos.sdk.status` or equivalent SDK module.
- [x] Preserve current skill identity in status ownership metadata.
- [x] Provide helpers for `details_ref` pointing to webio stream receivers.
- [x] Add tests showing a skill can publish status without touching Yjs or
  rebuilding a full snapshot.
- [x] Add migration notes for skill authors.

Implemented artifacts:

- `publish_status(...)` publishes one compact card and uses the current skill
  context as `skill:<name>` when `owner` is not provided.
- `publish_status_many(...)` publishes small batches with shared defaults.
- `publish_status_stream(...)` stores a `details_ref` to an existing stream or
  tool instead of embedding heavy diagnostics in the card.
- `docs/architecture/status-card-sdk-migration.md` records first migration
  rules for skill authors.

#### STATUS-004: Convert `infrastate_skill` to the shared status plane

Status: in progress. First snapshot-to-status-card adapter and Node API
publication path are implemented.

Current useful pattern:

- Compact durable UI data is projected into `infrastate.snapshot`.
- High-churn sections use stream receivers such as
  `infrastate.operations.active`, `infrastate.realtime`,
  `infrastate.yjs.load_mark`, and `infrastate.core_update_diagnostics`.
- Projection helpers already perform fingerprinting and rate limiting, but the
  logic is local to the skill.

Actions:

- [x] Identify `infrastate` status cards: runtime, route/realtime, Yjs,
  operations, core update, marketplace, and skill/scenario registry.
- [x] Publish those cards through the shared SDK helpers.
- [x] Keep existing stream receivers as `details_ref` targets.
- [ ] Remove or reduce duplicated local projection bookkeeping where the shared
  helper covers it.
- [ ] Confirm existing `infrastate` UI still receives current streams.
- [x] Add regression tests around unchanged snapshot/card dedupe.

Implemented artifacts:

- `adaos.services.infrastate_status_cards` maps compact `infrastate` snapshots
  into status cards for summary, operations, realtime, Yjs, and core update.
- `/api/node/infrastate/snapshot` now publishes those cards through
  `adaos.sdk.status` while keeping the existing snapshot response compatible.
- Stream-backed sections keep `details_ref` targets such as
  `infrastate.operations.active`, `infrastate.realtime`,
  `infrastate.yjs.load_mark`, and `infrastate.core_update_diagnostics`.
- `/api/node/projection-diagnostics` correlates active browser demand,
  dispatcher handler availability, and materialized status-card records so the
  migration can be inspected per webspace.
- `/api/node/status-cards/{card_id}/details/refresh` requests lazy stream
  details for stream-backed cards through `webio.stream.snapshot.requested`
  without embedding heavy diagnostics into the status-card payload.

#### STATUS-005: Convert `infrascope_skill` to the shared status plane

Status: in progress. First tracked status-card adapter is implemented for
overview, active incidents, inventory, and operations.

Current useful pattern:

- Compact durable UI data is projected into `infrascope.snapshot`.
- High-churn and large sections use receivers such as
  `infrascope.overview.*`, `infrascope.inventory.*`,
  `infrascope.operations.active`, and `infrascope.inspector.*`.
- It already maintains last-good snapshots and per-webspace projection
  fingerprints locally.

Actions:

- [x] Identify first `infrascope` status cards: overview, active incidents,
  inventory, and operations.
- [x] Publish first cards through the shared SDK helpers.
- [x] Keep overview/inventory/operations streams as details targets.
- [ ] Extend cards to browser/runtime state and registry sections.
- [ ] Wire the tracked adapter into the live `infrascope_skill` refresh path.
- [ ] Ensure inspector data stays lazy and is not embedded in status cards.
- [ ] Add tests proving the overview badge can update without full inventory
  reconstruction.

Implemented artifacts:

- `adaos.services.infrascope_status_cards` maps Infrascope summary, active
  incidents, inventory, and operations snapshots into shared status cards.
- The first adapter preserves stream details references:
  `infrascope.overview.active_incidents`, `infrascope.inventory.all`, and
  `infrascope.operations.active`.
- Unit tests verify shared-registry publication, owner metadata, details refs,
  and unchanged snapshot dedupe.

#### STATUS-006: Make `/api/node/reliability/summary` thin and versioned

Status: in progress. Compatibility mode remains unchanged; `mode=thin` now
uses the materialized status-card registry.

Expected behavior:

- Default response is small and backed by the materialized status registry.
- Full diagnostic snapshot requires `?full=1` or a separate debug endpoint.
- The endpoint supports ETag or explicit version checks.
- Unchanged polling returns `304 Not Modified` or a minimal unchanged response.

Actions:

- [ ] Measure current response size and polling frequency.
- [x] Add `mode=thin` or make thin mode the default with a compatibility flag
  for full mode.
- [x] Add `ETag` / `If-None-Match` support or `since_version`.
- [ ] Keep a migration-safe full snapshot path for existing debug tools.
- [x] Add tests for unchanged response behavior and full-mode compatibility.

Implemented artifacts:

- `/api/node/reliability/summary?mode=thin` returns status-card backed fields:
  card totals, stale/ready totals, registry stats, and thin card descriptors.
- `since_version` returns `unchanged=true` with an empty card list when the
  materialized status-card registry version has not advanced.
- Thin responses include cache hints: registry cache key, current registry
  version, `since_version` parameter name, and per-card cache keys.
- Thin HTTP responses expose `ETag`, `Cache-Control: no-cache`,
  `X-AdaOS-Cache-Key`, and `X-AdaOS-Registry-Version` headers, and matching
  `If-None-Match` requests return `304 Not Modified`.
- The existing compatibility summary remains the default response when `mode`
  is omitted.

#### STATUS-007: Move client monitoring from polling to push/delta

Status: planned.

Expected behavior:

- Client bootstraps from a small status snapshot.
- Client receives status changes through a stream or existing realtime channel.
- Client requests full details only when a panel/inspector is opened.

Actions:

- [ ] Identify the current caller(s) polling
  `/api/node/reliability/summary`.
- [ ] Replace badge/status polling with thin status snapshot plus updates.
- [x] Wire existing webio stream receivers as lazy detail sources.
- [x] Expose cache hints for status-card registry and card descriptors.
- [ ] Add client-side cache keyed by status card version.
- [ ] Verify the client no longer requests large summary payloads repeatedly
  during the first 3 minutes.

Implemented artifacts:

- Stream-backed status cards can request their existing detail receiver through
  `/api/node/status-cards/{card_id}/details/refresh`.
- API-style details references stay declarative and are reported without a
  synthetic stream request.

#### STATUS-008: Acceptance and observability

Status: planned.

Acceptance criteria:

- Repeated first-3-minute run shows no high-frequency large
  `/api/node/reliability/summary` responses.
- Thin status payload size is bounded and recorded.
- Full details remain available on demand.
- `infrastate_skill` and `infrascope_skill` both publish status cards through
  the shared path.
- Existing realtime stability criteria from `Realtime First 3 Minutes` remain
  green.

Actions:

- [x] Add log/metric for reliability summary mode, response bytes, and
  unchanged/304 counts.
- [x] Add status registry diagnostics to the final soak analysis.
- [ ] Run a 180-second acceptance with browser attached.
- [ ] Record payload size reduction and polling reduction in this tracker.
- [ ] Close this goal only after logs confirm no large repeated monitoring
  responses during normal UI operation.

Implemented artifacts:

- `adaos.services.projection_diagnostics` builds an operator read model over
  browser demand, dispatcher handlers, and the materialized status-card
  registry.
- `/api/node/projection-diagnostics` exposes active projection totals, missing
  handlers, missing status cards, stale demand, and per-projection consumers for
  Swagger/manual verification.
- `/api/node/reliability/summary/telemetry` exposes in-memory counters for
  summary mode, status code, estimated response bytes, unchanged hits, and
  `304 Not Modified` hits.
- Telemetry includes average response bytes per mode plus a full-vs-thin payload
  comparison with estimated byte and ratio reduction.
- `/api/node/reliability/summary/telemetry/reset` clears the in-memory counters
  and returns the previous snapshot for clean manual acceptance runs.

### TEST-001: Make `test_infrastate_skill_projection.py` hermetic

Status: planned.

Observed while debugging stream-backed modals:

- The full file can fail when marketplace cache/remote-probe defaults leak into
  tests that expect mocked remote registry data.
- `_project_async` stream assertions can be affected by live/local Yjs pressure
  guard state unless the guardrail is explicitly mocked.

Actions:

- [ ] Clear marketplace caches and set remote-probe flags inside affected
  tests.
- [ ] Mock `_active_noncritical_stream_guardrail` in projection tests that
  assert exact stream publications.
- [ ] Re-run the full file as part of the stream modal regression suite.

### UI-RT-001: Forward UI runtime notifications to node skill logs

Status: planned.

Expected behavior:

- Client-side runtime issues are visible in `[Node 0] Notifications` first.
- Dev mode may include diagnostic `details`; prod mode keeps the user-facing
  notification compact.
- The same notification envelope is eventually mirrored into node skill logs so
  an LLM/debugger can analyze UI contract mismatches without browser console
  access.

Actions:

- [ ] Define a stable notification envelope for UI runtime issues.
- [ ] Add a backend ingestion endpoint or stream receiver for client runtime
  notifications.
- [ ] Mirror accepted notifications into node skill logs with webspace, node,
  scenario, widget, action, and modal context.
- [ ] Add LLM-oriented grouping for repeated contract issues.

## Named Entity Registry and NLU Canonicalization

### Goal

Keep human-facing names, localized labels, runtime-observed names, aliases, and
canonical refs in one governed model so NLU, UI, skills, and LLM tooling can
refer to the same objects without retraining models after every rename or
language-specific alias change.

### Current Status

Snapshot date: 2026-05-13.

Target architecture is documented in
`docs/architecture/named-entities.md`.

Recommended implementation order:

1. Contract and fixtures: `NamedEntityRecord`, `EntityResolutionResult`, topic
   constants, and ambiguity examples.
2. Read-only registry: `NamedEntityService`, shared display helper, localized
   label metadata, and optional diagnostic projection.
3. Event integration: observed/draft/name/alias events plus
   `entity.registry.changed` invalidation.
4. NLU dry-run: resolver trace without dispatch changes.
5. Governed writes: adopt, rename, alias add/remove/deprecate with conflict
   checks and audit metadata.
6. MCP and skill migration: canonical descriptors for LLM tooling and removal
   of ad hoc fallback logic.

Integration progress:

- Overall: 98%.
- Completed: target architecture, addressing boundary, event model contract,
  initial roadmap, code-level record/result contracts, topic constants,
  read-only device entity adapter, modal/app/scenario/webspace lookup adapter,
  skill lookup adapter, browser draft-name helper, exact resolver, SDK read
  helpers, NLU dry-run trace subscriber, compact read-only
  `registry.named_entities` projection, live-room-safe NLU trace writes,
  voice/chat router live-room writes, read-only NLU Yjs reads, browser metadata
  capture from Yjs handshakes, access-links-driven
  `entity.registry.changed` invalidation, Root MCP/Codex read access to the
  compact named-entity registry, core node-display hostname-before-fallback
  behavior, client node-display helper alignment for legacy `Node N` fallback
  labels, client catalog/modal title enrichment from `registry.named_entities`,
  read-only registry label conflict diagnostics, localization-as-label-metadata
  architecture, compact registry label metadata, locale-aware resolver trace
  hints, per-locale conflict diagnostics, Root MCP `NLUAuthoringPlane`
  read-only context with canonical named entities, Teacher probe live entity
  matches, per-locale ambiguity evidence in NLU trace, runtime-only
  model-training evidence for alias resolution, first governed alias-add
  proposal/apply contract, SDK alias helpers, lifecycle event envelopes for
  alias add/conflict, durable device/browser alias persistence in
  `access_links`, `device_access.add_device_alias`,
  `sdk.data.entities.add_device_alias`, authoritative alias lifecycle event
  publishing, Root MCP / NLUAuthoringPlane `add_device_alias` write exposure
  guarded by `development.write.named_entities`, entity-level fingerprints,
  `base_fingerprint` stale-write protection, dedicated Root MCP
  `entity.alias.add` audit records, governed alias remove/deprecate proposal
  and apply flows, durable device/browser remove/deprecate persistence,
  NLUAuthoringPlane remove/deprecate write tools, dedicated
  `entity.alias.remove` / `entity.alias.deprecate` audit records, and focused
  tests, plus first authoritative device/browser observation, browser
  draft-name, and display-name lifecycle events from `access_links`.
- Current implementation slice: named-entity operational lifecycle events over
  authoritative device/browser registry changes.
- Not started yet: profile-owned aliases, conflict-resolution UX, remote
  target routing, and consumer migration.
- Verification note: targeted MCP/named-entity checks pass, and
  `test_root_mcp_foundation` is green again after test fixture alignment. The
  broader Yjs projection runs still expose pre-existing
  `AdaosMemoryYStore.started` drift; track that separately so it does not mask
  NER regressions.

Human verification:

- Check that docs consistently say human labels are not routing keys.
- Check that localization is described as label/alias selection, not as a
  change to canonical refs.
- Check that `Node N` is described as fallback-only.
- Check that the implementation starts read-only and does not change NLU
  dispatch until dry-run trace is visible.
- Check alias lifecycle manually: add a browser alias, deprecate it and confirm
  it remains visible for compatibility, then remove it and confirm NLU no
  longer resolves that phrase.

Next implementation steps:

1. Start migrating node/browser labels to the shared display helper.
2. Extend observed/draft/display-name lifecycle events from device/browser
   sources to workspace and manifest sources.
3. Add conflict-resolution UX around Root MCP alias writes.
4. Add profile-owned alias storage and policy boundaries.
5. Migrate node/browser labels to shared display helpers in remaining skill
   projections.

### Tasks

#### NER-001: Establish canonical named-entity read model

Status: in progress.

Actions:

- [x] Add `NamedEntityRecord` schema or dataclass.
- [x] Add `EntityResolutionResult` schema or dataclass.
- [x] Add shared `entity.*` event topic constants.
- [x] Add golden fixtures for node/browser/device alias and ambiguity examples.
- [x] Add golden fixtures for webspace, scenario, modal, and app examples.
- [x] Add golden fixtures for skill examples.
- [x] Document localized labels and aliases as read-model metadata.
- [ ] Build a read model over device inventory, node display, workspace
  manifests, system model objects, and desktop registry entries.
- [x] Build the first read-only device entity adapter over
  `DeviceInventoryService`.
- [x] Build the first read-only modal/app/scenario/webspace adapter over
  existing NLU lookup tables.
- [x] Preserve source authority: device access remains owned by
  `access_links` / `DeviceInventoryService`, not by the named-entity read
  model.
- [x] Project a compact read-only entity registry for UI/debug consumers.

#### NER-002: Make device and browser display names consistent

Status: planned.

Actions:

- [ ] Prefer user-confirmed display name, then node names, then observed
  hostname/browser+OS, then `Node N`.
- [ ] Preserve exact user-confirmed names while allowing localized aliases and
  localized system fallbacks.
- [x] Generate draft names for newly registered browsers.
- [x] Make core node display helpers use observed hostname before `Node N`
  fallback.
- [x] Make the client node-display helper treat `Node N` as fallback when
  observed hostname or registered names are present.
- [x] Use compact named-entity registry labels for client catalog and modal
  node display when the local label is still fallback-like.
- [x] Add locale metadata to compact registry labels while keeping
  `display_label` compatibility for current UI consumers.
- [ ] Make observed-only device rename flow explicitly adopt or adopt+rename.
- [x] Add read-only conflict diagnostics for duplicate display names or aliases
  in the compact registry payload.
- [ ] Surface conflict diagnostics in Notifications and operator-facing skill
  logs when user attention is useful.
- [ ] Invalidate display-name consumers through `entity.registry.changed`
  instead of reload-only behavior. Backend invalidation emission is in place;
  client/name-rendering consumers still need migration.

#### NER-003: Add NLU entity canonicalization

Status: in progress.

Actions:

- [x] Add a resolver dry-run mode that records NLU trace without changing
  dispatch behavior.
- [x] Resolve registered names and aliases before or alongside
  `nlp.intent.detect.request`.
- [x] Accept `request_locale` and `preferred_locales` as resolver hints.
- [x] Add `normalized_text`, `resolved_entities`, canonical refs, and ambiguity
  records to NLU trace.
- [x] Add per-locale conflict evidence to compact registry diagnostics.
- [x] Add per-locale ambiguity evidence to NLU trace.
- [x] Update Teacher probe output to show live entity resolver matches.
- [x] Add golden tests proving runtime aliases do not require Rasa/neural
  retraining.

#### NER-004: Expose named entities to SDK/MCP/LLM tooling

Status: in progress.

Actions:

- [x] Add `sdk.data.entities` read helpers.
- [x] Add first governed alias-add proposal/apply service and SDK helpers.
- [x] Add durable device/browser alias write helper:
  `sdk.data.entities.add_device_alias`.
- [x] Expose named-entity descriptors through Root MCP read capabilities.
- [x] Include named entities in NLUAuthoringPlane context.
- [x] Expose governed device alias add through Root MCP / NLUAuthoringPlane
  with a write capability separated from `ProfileOpsRead`.
- [x] Expose entity `fingerprint` values and accept `base_fingerprint` on
  governed alias writes.
- [x] Expose governed device alias remove/deprecate through SDK and Root MCP /
  NLUAuthoringPlane with the same write capability and stale-write guard.

#### NER-005: Integrate named entities with the operational event model

Status: in progress.

Actions:

- [x] Emit `entity.observed` when authoritative device/browser access-link
  sources report observed labels.
- [ ] Extend `entity.observed` to workspace and manifest sources.
- [x] Emit `entity.draft_name.suggested` for generated browser draft names.
- [ ] Extend `entity.draft_name.suggested` to generated node draft names once
  node draft-name policy is explicit.
- [x] Emit alias lifecycle events from the first authoritative device/browser
  alias write path.
- [x] Emit display-name lifecycle events from the first authoritative
  device/browser display-name write path.
- [x] Emit alias remove/deprecate lifecycle events from authoritative
  device/browser write paths.
- [x] Return `entity.alias.added`, `entity.alias.conflict.detected`, and
  `entity.registry.changed` event envelopes from the governed alias-add
  apply contract.
- [x] Include `locale` or `locale: "und"` in the first authoritative alias-add
  lifecycle events.
- [x] Add stale-write protection through `base_fingerprint` and explicit
  `status: stale` results.
- [ ] Emit `entity.alias.conflict.detected`,
  `entity.resolution.ambiguous`, and `entity.resolution.failed` into
  Notifications and node skill logs when operator attention is useful.
- [x] Add dedicated audit trail records for Root MCP alias writes beyond the
  generic Root MCP invocation audit envelope.
- [x] Add dedicated audit trail records for Root MCP alias remove/deprecate
  writes.
- [ ] Treat `entity.registry.changed` as the cache invalidation signal for
  `EntityResolver` and demanded name-rendering projections. The compact Yjs
  projection already subscribes to this signal; resolver cache ownership is
  still pending.

#### NER-006: Migrate consumers away from ad hoc name fallback

Status: planned.

Actions:

- [x] Replace the first client-side node display fallback path with the shared
  named-entity display helper for catalog and modal titles.
- [ ] Extend client-side named-entity display enrichment to widget-level node
  badges and workspace manager surfaces.
- [ ] Update operator-facing skills to consume canonical refs and shared display
  names instead of raw labels.
- [ ] Remove duplicate fallback rules after the shared helper is adopted.
- [x] Add regression test proving a newly persisted browser alias resolves
  through NLU without model retraining.
- [ ] Add remaining regression tests for `Node N` fallback, hostname display,
  browser draft names, alias ambiguity, and renamed-device NLU resolution.
