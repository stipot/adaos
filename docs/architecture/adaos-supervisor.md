# AdaOS Supervisor

## Goal

Introduce a small always-on local supervisor process that remains available while the main AdaOS runtime restarts, updates, validates a new slot, or rolls back.

`adaos-supervisor` is not a transport sidecar.
It is the local process-lifecycle and update-orchestration authority for one node.

Read this together with:

- [Authority And Degraded Mode](authority-and-degraded-mode.md)
- [Transport Ownership](transport-ownership.md)
- [AdaOS Realtime Sidecar](adaos-realtime-sidecar.md)
- [Hub-Root Protocol](hub-root-protocol.md)

## Why this exists

The current autostart/update flow couples:

- local admin API availability
- runtime process lifetime
- update status visibility
- slot validation and rollback logic

This creates an operator gap during restart-heavy paths:

- `update-status` becomes unavailable exactly when update progress matters most
- `node reliability` collapses into connection-refused even when the system is in a known transitional state
- stale `restarting` or `applying` state can survive longer than intended

The supervisor solves this by keeping the update and process-control surface alive while the runtime is stopped or being replaced.

## Scope

`adaos-supervisor` owns:

- local runtime process lifecycle
- runtime start/stop/restart sequencing
- persisted update attempt state
- candidate-to-slot prepare / validate / rollback orchestration
- post-validation bootstrap/root promotion orchestration when bootstrap-managed files changed
- skill runtime migration orchestration for installed skills during core slot transition, including deferred commit after old runtime shutdown when early slot preparation is used
- restart and validation deadlines
- local admin/update API availability during runtime downtime
- recovery from interrupted update attempts

`adaos-supervisor` does not own:

- hub-root protocol semantics
- root-issued trust or cross-subnet truth
- business semantics of skills, scenarios, or local event handling
- realtime transport authority
- browser/member semantic channel policy

## Target runtime split

Target local node layout:

- `adaos-supervisor`
  - always-on local control and update authority
  - owns persisted attempt state and process supervision
- `adaos-runtime`
  - main FastAPI/runtime process
  - production runtime is launched from the active slot manifest
  - owns local execution semantics, storage, skills, scenarios, and APIs
- `adaos-realtime`
  - optional transport-only sidecar
  - owns selected long-lived transport loops such as hub-root realtime transport

This means:

- the runtime is restartable without losing the local control surface
- the realtime sidecar can also be restarted independently
- update progress remains inspectable during shutdown, apply, and validate phases
- root checkout stays out of the production runtime path unless a developer explicitly launches it

For the target media-continuity path, this split also enables a more selective policy:

- if a member currently owns an active live media session, that member update should be deferred
- a hub runtime restart may still be allowed if the hub-side realtime sidecar can stay alive and continue serving the delegated realtime continuity path
- supervisor should therefore treat "restart runtime" and "tear down sidecar" as different actions once live media continuity depends on sidecar ownership

## Runtime source rule

Production runtime must always come from slot `A|B`.

Root checkout is reserved for:

- bootstrap install
- supervisor/autostart/update-control code
- watchdog and other always-on control-plane helpers
- candidate preparation
- explicit developer-run workflows

Control-plane code must run from a stable root checkout and root virtual
environment, for example `~/adaos` plus `~/adaos/.venv`. It must not run from
`state/core_slots/slots/<A|B>/repo` or that slot's `venv`, because an update can
rewrite or replace a slot while the supervisor/watchdog still needs to survive
the transition and make rollback decisions.

This keeps slot switching fast, keeps production runtime independent from root
checkout drift, and keeps the control-plane independent from slot mutation.

When the watchdog is re-enabled, it follows the same rule as supervisor:

- its service wrapper or launch spec uses the stable root checkout and root
  `.venv`
- it may observe, stop, or restart slot runtimes, but it is not itself
  slot-bound
- watchdog diagnostics must expose the effective Python/source path the same
  way autostart status exposes `wrapper_python_is_core_slot`

The watchdog is allowed to act on slot runtime health; it must not depend on a
slot runtime interpreter to remain alive.

For runtime processes, slot resolution is process-local first:

- if `ADAOS_ACTIVE_CORE_SLOT` is set in the runtime environment, that process treats the slot as its effective runtime source
- otherwise the global slot marker from `state/core_slots/active` remains authoritative

This is important for warm-switch work because it allows a `candidate` runtime to boot from the inactive slot for prewarm/diagnostics without mutating the global active-slot marker before cutover is committed.

## Slot-bound runtime ports

Supervisor should treat runtime ports as slot-owned rather than as one global mutable bind.

Target rule:

- slot `A` keeps a stable local runtime port
- slot `B` keeps a different stable local runtime port
- supervisor stays on its own always-on control port

Current MVP direction is:

- default `A` runtime port: `8777`
- default `B` runtime port: `8778`
- supervisor port: `8776`

This creates a clean foundation for warm-switch behavior because the inactive slot can be prepared and, later, prewarmed without fighting for the active slot's listener.

The mapping must remain supervisor-visible in diagnostics and browser-safe transition payloads so local clients can discover:

- active runtime URL
- candidate runtime URL
- current transition mode
- whether warm-switch was admitted or downgraded to stop-and-switch

## Warm-switch admission

Warm-switch is desirable, but not always safe on constrained hardware.

Supervisor should therefore make an explicit admission decision before using a dual-runtime transition:

- if candidate slot uses a different reserved port and memory headroom is sufficient, transition mode is `warm_switch`
- if memory headroom is not sufficient, transition mode is `stop_and_switch`
- this decision must be visible to operator and browser-facing status surfaces before shutdown starts

Admission should be driven by a simple local resource gate such as:

- available memory
- current runtime RSS
- estimated candidate runtime footprint
- configured reserve that must remain free after candidate start

The important rule is that low-memory devices must fail safe into stop-and-switch instead of trying to start two full runtimes and getting stuck mid-transition. Warm-switch admission therefore reserves both an absolute memory floor and a percentage of total RAM after the estimated candidate runtime. The percentage reserve matters on small 4 GiB stands where slot preparation, the supervisor process, filesystem cache pressure, and the passive candidate can freeze the node even when the simple candidate-RSS estimate appears to fit.

## Runtime instance identity

Warm-switch means `active` and `candidate` may overlap for a short period.

That requires identity stronger than just `hub_id` or `subnet_id`.

Each runtime process should therefore carry:

- `runtime_instance_id`
- `transition_role` (`active` or `candidate`)
- slot-bound runtime URL/port

This identity must flow through:

- supervisor runtime status
- browser-safe transition status
- root control lifecycle reports
- root core-update reports
- hub root/NATS session issuance and logs

Without that, a candidate process can look indistinguishable from the active process and accidentally steal or overwrite control-plane state.

## Candidate passive mode

Before cutover is explicitly committed, a prewarmed `candidate` runtime must stay passive on root-facing traffic subjects.

That means a candidate may establish root control connectivity for diagnostics, but it must not yet subscribe to the same root-routed traffic subjects as the active runtime:

- `tg.input.<hub_id>`
- `io.tg.in.<hub_id>.text`
- `route.v2.to_hub.<hub_id>.*`

The intent is:

- root can see that a candidate runtime exists
- active runtime remains the only consumer of live hub traffic before cutover
- candidate reconnects or retries do not supersede the active runtime's root/NATS session merely because they share the same `hub_id`

The same rule must apply to local control discovery:

- `candidate` runtime surfaces must self-identify through lightweight probes such as `/api/ping` and `/api/admin/update/status`
- local fallback control resolvers must ignore a runtime that reports `transition_role=candidate` or `admin_mutation_allowed=false`
- a candidate runtime must reject mutating local update operations (`update.start`, `update.cancel`, `update.rollback`) with an explicit conflict instead of behaving like a second control plane

Fast cutover does not remove that rule.
It only defines the moment when supervisor may end passive mode:

- supervisor explicitly authorizes the already-running candidate to promote itself through `POST /api/admin/runtime/promote-active`
- the candidate flips to `transition_role=active`, reconnects root-facing transport under that new authority, and only then becomes eligible to own live hub traffic
- supervisor adopts that promoted process as the managed active runtime instead of launching a second fresh process when warm-switch succeeds
- if promotion or adoption fails, supervisor tears the candidate down and falls back to the existing stop-and-switch launch path

## Authority boundary

### Supervisor

The supervisor is the authority for:

- whether the runtime is expected to be running
- whether an update attempt is pending, active, failed, validated, or rolled back
- whether a rollback must be triggered after deadline expiry or failed validation
- operator-visible local state of runtime lifecycle

The supervisor is not the authority for:

- whether root-side protocol acks were accepted as global truth
- whether transport-level path selection is healthy beyond its delegated runtimes
- business-policy decisions inside degraded hub execution

But the supervisor must remain able to enforce locally visible continuity guards exposed by the runtime model, for example:

- defer member update while member-owned live media remains active
- refuse a hub restart path that would drop a sidecar continuity contract the system currently depends on
- distinguish "runtime restart allowed with sidecar continuity" from "full local media teardown"

The first conservative version of this policy is now implemented:

- supervisor reads runtime continuity guard data from `GET /api/node/reliability`
- update transitions are deferred into explicit `planned/live_media_guard` state when the continuity contract says restart would be unsafe
- manual runtime restart is refused until sidecar continuity becomes a real ready capability rather than only a planned target

### Runtime

The runtime remains the authority for:

- local API semantics
- local scenario and skill execution
- local persistence and event bus behavior
- local degraded-mode execution once it is running

### Realtime sidecar

The realtime sidecar remains the authority only for:

- transport lifecycle
- reconnect loops
- socket diagnostics
- local relay IO

It must not absorb supervisor responsibilities.

## Local control surfaces

The target local APIs are:

### Supervisor API

Always available while the node is booted:

- `GET /api/supervisor/status`
- `GET /api/supervisor/update/status`
- `POST /api/supervisor/update/start`
- `POST /api/supervisor/update/cancel`
- `POST /api/supervisor/update/defer`
- `POST /api/supervisor/update/rollback`
- `POST /api/supervisor/update/complete`
- `POST /api/supervisor/runtime/restart`
- `POST /api/supervisor/runtime/candidate/start`
- `POST /api/supervisor/runtime/candidate/stop`

This API is the source of truth for:

- update attempt state
- restart reason
- validation deadlines
- rollback decisions
- current managed child processes
- active and candidate runtime process identity/state (`runtime_instance_id`, `transition_role`, slot, port, readiness)
- current skill runtime migration diagnostics for the active core update attempt
- runtime liveness separate from listener bind and runtime API readiness
- active managed runtime command/executable source for the current slot
- active slot structure diagnostics (`manifest` / `repo` / `venv` / nested-slot anomalies)

For browser-facing observability, supervisor should also expose a limited read-only transition surface that can be polled without admin-mutating privileges.
That surface is intended only for restart/update visibility such as:

- `hub restarting`
- `update planned`
- `update applying`
- `rollback in progress`
- `root promotion pending`
- `root restart in progress`
- `subsequent transition queued`
- `update failed`

It must not expose mutating control operations or become a substitute for the authenticated operator API.

Current MVP browser behavior may preserve and display the last known transition state during reconnect windows, and routed hub sessions can now consume that browser-safe transition state primarily as pushed `core.update.status` events over the control `/ws` channel, with `/hubs/<id>/api/supervisor/public/update-status` retained as a fallback when the control channel is unavailable.
The target end state is stronger: every supported browser entry topology should be able to poll that read-only supervisor transition surface directly, so the shell can keep moving from `hub restarting` to `rollback in progress` or `root promotion pending` from supervisor truth rather than only from the last runtime-visible snapshot.
Operator-facing surfaces are also expected to consume that same supervisor truth through the canonical control-plane model, so Infrascope and related overview projections can show core-runtime transition state in `active_runtimes`, health strips, and recent changes instead of presenting a restart only as generic hub instability.
That browser-safe surface now also includes candidate runtime diagnostics needed for warm-switch work:

- `action`
- `candidate_runtime_instance_id`
- `candidate_runtime_state`
- `candidate_runtime_api_ready`
- `candidate_transition_role`
- `candidate_prewarm_state`
- `candidate_prewarm_message`
- `candidate_prewarm_ready_at`

### Runtime API

Available only while `adaos-runtime` is running:

- current node APIs
- current admin APIs that belong to runtime semantics
- cutover-only runtime identity operations such as `POST /api/admin/runtime/promote-active`
- reliability, scenario, skill, Yjs, media, and operator surfaces

## Persisted state

The supervisor should persist explicit local attempt state, separate from transient runtime liveness:

- `state/supervisor/runtime.json`
- `state/supervisor/update_attempt.json`
- `state/supervisor/last_result.json`

`runtime.json` is a compact control-plane snapshot, not an append-only
diagnostic dump. It must stay bounded and must not embed recursive watchdog
history such as `required_upstream_link.watchdog.recent_events` containing prior
watchdog events. Detailed watchdog history belongs in JSONL files that are read
through bounded tail readers; persisted runtime state should keep only current
decision/status summaries and stable refs to larger diagnostics.

Recommended fields for `update_attempt.json`:

- `attempt_id`
- `action`
- `state`
- `phase`
- `target_slot`
- `target_rev`
- `target_version`
- `reason`
- `started_at`
- `deadline_at`
- `validated_at`
- `restored_slot`
- `failure_summary`
- `skill_runtime_migration`
- `scheduled_for`
- `planned_reason`
- `subsequent_transition`
- `subsequent_transition_requested_at`
- `subsequent_transition_request`

The important rule is that this state is committed by the supervisor, not inferred only from whether the runtime currently listens on `127.0.0.1:8777`.

## Update flow

Target flow:

1. operator or root-triggered action reaches supervisor
2. supervisor writes `update_attempt.json`
3. supervisor materializes the candidate source/artifact
4. supervisor prepares the inactive slot from that candidate while the active runtime is still serving traffic
5. supervisor starts countdown only after the target slot is materially ready
6. supervisor requests graceful runtime shutdown
7. supervisor commits deferred installed-skill runtime migration against the target core interpreter after the old runtime is down
8. supervisor activates the target slot and either promotes the prewarmed candidate runtime to active authority or launches production runtime from that slot
9. supervisor validates required runtime checks against that target-slot runtime
10. on slot-validation success, supervisor commits the transition result
11. if bootstrap-managed files changed, supervisor records `root_promotion_required` and promotes root from the same validated candidate
12. on autostart-managed deployments, supervisor requests autostart-service restart so the root-based supervisor/bootstrap code actually switches over
13. on failure or deadline expiry, supervisor rolls back the slot and records failure

Important invariants:

- the attempt record is not cleared before validation succeeds
- `restarting` and `applying` are bounded by deadlines
- interrupted supervisor boot resumes or resolves the last incomplete attempt
- if a new update signal arrives during an active transition, supervisor records exactly one deferred `subsequent_transition` and executes it once after the current transition reaches a terminal state
- minimum update interval gating schedules a future update window instead of rejecting the request outright
- installed skills do not silently inherit old runtime dependencies after core migration
- root/bootstrap promotion never happens before the candidate already passed slot validation
- root promotion must preserve any already-queued subsequent transition metadata so a self-update handoff does not lose the next requested transition
- prepared slot contents must not inherit another slot's git remotes or become the authority for future updates
- code-only core updates should not rebuild the inactive slot venv. When
  dependency metadata is unchanged and the inactive slot already has a valid
  venv, prepare replaces only `repo`, preserves `venv`, validates imports with
  the same `PYTHONPATH=repo/src` overlay used by the runtime manifest, and
  records `venv_prepare.mode=reused_existing_slot_venv`. Fresh `venv + pip
  install` is reserved for dependency metadata changes or missing/invalid slot
  venvs.

## Bootstrap/root promotion

Bootstrap-managed code such as supervisor, autostart, and core-update orchestration is a separate promotion step.

Rules:

- slot validation always happens first
- root promotion is allowed only after the candidate is proven in a slot
- production runtime still restarts from the active slot after root promotion
- root promotion should use the same validated candidate source, not a fresh mutable branch tip
- current implementation promotes bootstrap-managed and operator-control files
  into the explicit validated root target recorded for that slot, writes a
  backup snapshot plus restore metadata, refreshes the autostart wrapper so the
  next supervisor process uses the stable root checkout/root `.venv`, records
  an explicit supervisor attempt state while waiting for restart, and on
  autostart-managed Linux deployments requests the service restart
  automatically so the new supervisor/bootstrap code becomes active
- root promotion checks effective root parity, not only the candidate manifest:
  if the current bootstrap/operator-control path list changes between
  rollouts, stale root files such as `adaos node` diagnostics are detected and
  promoted before acceptance tooling is trusted
- if another transition request arrives before that restart completes, it is queued as `subsequent_transition` on the supervisor attempt instead of being dropped or run concurrently
- manual `adaos autostart update-complete` remains the compatibility and retry path for older supervisors or environments where self-requested restart is unavailable

This keeps root updates out of the fast rollback path while preserving the slot-runtime model.

Autostart status should expose the wrapper Python and whether that Python lives
under `state/core_slots/slots`. `wrapper_python_is_core_slot=true` is a
control-plane isolation defect: it does not mean the active runtime slot is
wrong, but it means the next supervisor/watchdog restart is still coupled to a
mutable runtime slot.

Operator update status must remain available while a slot is being prepared or
launched. CLI status probes should use short HTTP timeouts and fall back to the
local `state/core_update/status.json` and `state/supervisor/update_attempt.json`
files, because those files can still show `preparing`, `countdown`, or
`launch` even when the runtime/supervisor API is temporarily busy.

## Skill runtime migration lifecycle

Installed skills are not automatically valid just because the core slot booted.
Their runtime dependencies must be prepared against the new core interpreter and surfaced as explicit diagnostics.
If a skill uses optional `data/internal`, that data evolves with the runtime compatibility bucket (`v<major>.<minor>`), not with the A/B code slot. Cross-bucket schema changes should be implemented in the reserved `migrations/data_migration.py` file.

### Target migration model

The target AdaOS rule is:

- slot activation is a pointer switch, not a promise of full process-memory migration
- live in-memory objects are disposable unless a runtime explicitly rebuilds them
- migration authority belongs to durable and slot-bound state, not to arbitrary Python object graphs

This means AdaOS should not treat "the skill restarted in memory" and "the skill was safely migrated" as the same thing.
They are separate concerns:

- runtime cutover switches executable code and slot-bound paths
- state migration upgrades persisted state into a form that the new runtime can accept
- runtime rehydration rebuilds ephemeral caches, projections, and subscribers after cutover

The target storage classes per skill are:

- canonical durable state:
  long-lived business state that must survive restart, rollback, and projection rebuild
- bucket-bound schema state:
  data that evolves together with runtime schema and therefore belongs under `v<major>.<minor>/data/internal`
- derived runtime state:
  projections, indexes, caches, thread summaries, embeddings, and similar rebuildable material
- live process memory:
  in-flight handlers, imported modules, object instances, background tasks, subscriptions, and local caches that are not durable by default

The target kernel contract is that only the first two classes are migrated.
Derived runtime state must be rebuilt deterministically, and live memory must be drained and recreated rather than copied forward implicitly.

### Why this matters

The current implementation already follows the first half of this model:

- runtime slot activation switches `active` and `previous` markers atomically
- optional `data/internal` is migrated during `prepare` only when a new compatibility bucket is prepared with a migration hook
- rollback switches the active version/slot marker; patch rollback reuses the same bucket data, while minor rollback points back to the previous bucket data
- service skills are explicitly restarted on activate/rollback
- in-process skills reload code on next invocation by clearing skill modules and re-importing from the active slot

What is still only partial is the second half:

- there is no universal contract for draining long-lived in-process state before cutover
- there is no explicit rehydrate phase for non-service skills after activation
- migration diagnostics focus on slot/data preparation but not yet on runtime state rebuild

### Target lifecycle

Per skill, the target lifecycle should become:

1. `prepare_runtime`
2. `persist_before_switch`
3. `migrate_durable_state`
4. `activate_pointer`
5. `rehydrate_runtime`
6. `healthcheck`
7. `rollback` or `deactivate` on failure

The intended semantics are:

1. `prepare_runtime`
   - stage code, interpreter, dependencies, and resolved manifest in the inactive slot
   - prepare shared bucket data; patch installs reuse it, minor installs run the declared migration hook into the new bucket
2. `persist_before_switch`
   - flush debounced writes and checkpoint any skill-owned durable stores before mutating slot pointers
   - stop accepting new local work if the runtime cannot safely overlap old and new state
3. `migrate_durable_state`
   - run schema migration only against canonical durable state and slot-bound schema state
   - never attempt generic object-memory serialization as the platform default
4. `activate_pointer`
   - atomically switch active runtime version and slot
   - record previous slot and migration metadata for rollback
5. `rehydrate_runtime`
   - rebuild projections, indexes, caches, subscriptions, and other derived state from durable truth
   - re-open service endpoints or re-subscribe platform listeners only after the new slot is authoritative
6. `healthcheck`
   - validate that the new runtime is operational after rehydration, not merely importable
7. failure handling
   - `rollback` if the old slot must be restored
   - `deactivate` if the core/runtime switch remains committed but this skill must be quarantined

Current implementation now also hardens the activation-failure path:

- if pointer cutover succeeds but `rehydrate_runtime` fails, the runtime attempts shutdown hooks on the newly active slot
- the runtime then restores the previous active version/slot selection, internal-data marker, and deactivation state
- lifecycle diagnostics for the failed target slot remain persisted for operator inspection
- runtime-wide drain and stop events now reuse the same skill lifecycle contract:
  `subnet.draining` fans out `drain` across active installed skills, while `subnet.stopping` fans out `dispose` and `before_deactivate`

### Runtime hook direction

The platform should remain functional without custom skill hooks, but the target contract should support optional hooks for stateful skills:

- `before_deactivate()`:
  flush or checkpoint durable state before cutover
- `migrate_state(payload)`:
  explicit migration entry point for schema-sensitive state if `data/internal` copy is insufficient
- `after_activate(payload)`:
  lightweight post-switch initialization once the new slot is active
- `rehydrate()`:
  rebuild derived runtime state from durable truth
- `dispose()` or `drain()`:
  stop background work and release subscriptions/resources before rollback or deactivation

These hooks should be treated as bounded lifecycle hooks, not as an invitation to invent ad-hoc migration protocols per skill.

### Yjs and projection rule

For Yjs-backed experiences, the target rule should be explicit:

- Yjs is a live projection and collaboration layer
- Yjs is not the canonical migration authority for skill business state
- if a skill keeps canonical durable state elsewhere, Yjs must be rebuilt or reconciled from that durable truth after cutover
- if Yjs currently carries working state that must survive reloads, the platform should still persist a durable snapshot outside Yjs and rehydrate from that snapshot after rebuild

This is already the direction used by `nlu_teacher`:

- working state is visible under `data.nlu_teacher.*`
- durable state is also persisted under `.adaos/state/skills/nlu_teacher/<webspace>.json`
- rehydrate merges durable snapshot and current Yjs content after `scenarios.synced`

That pattern is target-aligned because it treats live projection state as rebuildable rather than as the sole owner of migration truth.

Target lifecycle per installed skill:

1. `prepare`
2. `test`
3. `activate`
4. `rollback` on activation or post-activation failure
5. `deactivate` if core transition is committed but a subset of skills must be quarantined afterward

The supervisor remains the authority for the overall core-update decision, but individual skill runtime outcomes must be persisted as part of the update result.

Current MVP implementation now splits this into two moments:

- early slot preparation may build the inactive core slot and mark skill migration as deferred while the old runtime is still live
- the mutating skill runtime commit step still happens only after the old runtime has stopped, so countdown traffic does not start reading partially-switched skill runtime state

Current MVP implementation also starts a best-effort passive `candidate` runtime prewarm when:

- the inactive slot is already prepared
- slot ports are reserved distinctly
- supervisor admitted `warm_switch`

That prewarm now feeds a real fast-cutover path:

- candidate readiness is surfaced through supervisor runtime/public status
- candidate remains passive on root-routed traffic subjects until supervisor explicitly commits cutover
- once the old runtime is down and the prepared slot is activated, supervisor may promote/adopt the already-running candidate instead of starting a fresh runtime process
- if candidate promotion, root reconnect, or supervisor adoption fails, supervisor falls back to the existing stop-and-switch launch path from the same prepared slot

This keeps warm-switch opportunistic and reversible: the node gets a genuine low-downtime cutover path when the candidate is ready, but constrained or unhealthy cases still converge through the proven fallback path.

Recommended per-skill diagnostic fields:

- `skill`
- `ok`
- `failed_stage`
- `prepared_version`
- `prepared_slot`
- `active_slot_before`
- `active_slot_after`
- `rollback_performed`
- `deactivated`
- `tests`
- `error`

Operator surfaces such as Infra State and Infrascope should be able to answer:

- which skill failed migration
- whether the failure happened during prepare, tests, activate, rollback, or deactivate
- whether rollback was performed
- whether the node committed the core update with some skills intentionally deactivated

After a successful core switch, `deactivate` is the preferred local containment mechanism for individual broken skills when the node should keep the new core slot rather than trigger a full rollback.
The default target behavior is:

1. runtime passes core post-switch validation
2. supervisor runs post-commit checks for active skill runtimes
3. failing skills are selectively deactivated
4. core update remains committed, but operator surfaces show degraded skill set

Post-commit checks should not rely only on test suites.
They should also treat persisted lifecycle diagnostics as first-class health signals, especially:

- `rehydrate`
- `healthcheck`
- shutdown-hook failures that indicate the skill cannot be safely recycled on the next transition

Current implementation now feeds lifecycle diagnostics into the skill runtime migration report and allows post-commit checks to fail and selectively deactivate a skill before test execution when runtime lifecycle health is already known to be bad.
Operator-facing projections now also distinguish lifecycle failures from plain test failures, so reports can show `lifecycle/rehydrate` instead of collapsing everything into `tests`.
Selective deactivation now also persists that same failure contract in the skill deactivation marker, so the runtime can distinguish "manually deactivated" from "quarantined after committed core switch because `lifecycle/rehydrate` failed".
That quarantine metadata is now propagated into operator-facing notes and post-validation status messages, so the supervisor-visible transition summary can name the quarantined skill and failing stage directly.

### Roadmap checklist

Use the checklist below as the migration hardening path for the kernel/runtime layer:

- [x] document storage classes per skill explicitly in manifests/runtime docs: canonical durable state, slot-bound schema state, derived runtime state, live memory
- [x] add an explicit `persist_before_switch` stage to skill/core migration orchestration
- [ ] require migration logic to operate on durable and slot-bound state only, not generic process memory
- [x] define optional lifecycle hooks for `before_deactivate`, `after_activate`, `rehydrate`, and `dispose/drain`
- [x] make post-activation rehydration a declared runtime phase instead of an implicit side effect
- [x] persist per-skill migration diagnostics for `persist`, `migrate`, `rehydrate`, and `healthcheck`, not only `prepare/test/activate`
- [x] standardize rollback semantics when pointer switch succeeded but rehydration failed
- [x] connect global runtime drain/shutdown events to skill-level `drain` / `dispose` / `before_deactivate`
- [x] surface lifecycle-vs-test failure classes in operator-facing migration reports
- [x] standardize deactivation metadata when core switch stays committed but one skill cannot complete rehydration
- [ ] make projection-backed skills document which branches are canonical and which are rebuildable caches
- [ ] move Yjs-backed stateful skills toward "durable truth + projection rebuild" instead of "projection is the only truth"
- [ ] add tests that simulate restart/update/rollback with persisted state present before cutover
- [ ] add tests that prove derived state can be rebuilt deterministically after activation and rollback

## Relationship to systemd

Target deployment:

- systemd manages `adaos-supervisor`
- `adaos-supervisor` manages `adaos-runtime`
- `adaos-supervisor` also manages `adaos-realtime` when sidecar mode is enabled in managed topology

This is preferred over systemd managing the main runtime process directly because systemd alone does not hold AdaOS-specific update semantics, slot state, or validation rules.

## Relationship to realtime sidecar

The supervisor and realtime sidecar solve different problems:

- `adaos-supervisor`: process and update authority
- `adaos-realtime`: transport isolation

In the managed topology, the supervisor launches, monitors, and restarts the sidecar.
Standalone runtime-owned sidecar startup remains only as compatibility fallback when supervisor is absent.
The sidecar must remain transport-only.
The sidecar must not become the hidden owner of update status, rollback state, or degraded-mode business policy.

## Memory leak detection and profiling

The supervisor should also become the local authority for memory leak detection, profiling escalation, and remote retrieval of profiling evidence.

This work is intentionally prioritized ahead of the broader supervisor rollout because:

- core-slot evolution can change memory behavior even when update flow is otherwise healthy
- skill runtime evolution can introduce long-lived leaks outside the narrow core-update path
- low-memory devices need an explicit local guard before a leak turns into process death or unstable restart loops

### Goal

Provide an always-on supervisor-owned memory watchdog that can:

- observe runtime process-family memory after start, restart, and slot switch
- distinguish normal warm-up from suspicious sustained growth
- restart the runtime in an explicit profiling mode when policy thresholds are crossed
- correlate memory growth with top-level runtime operations
- persist local profiling sessions and summaries
- publish profiling summaries and artifacts to root so they can be retrieved remotely by zone-scoped operator workflows

### Operating model

The target model is policy-driven rather than "always profile everything".

Supervisor should run the managed runtime in one of these modes:

- `normal`
- `sampled_profile`
- `trace_profile`

Target behavior:

1. runtime starts in `normal`
2. supervisor samples process-family memory and records a rolling baseline
3. if the memory policy detects suspicious growth, supervisor records a profiling session intent
4. supervisor restarts the same slot in `sampled_profile`
5. if the profile confirms continued abnormal growth, supervisor records a leak incident and keeps the node recoverable through restart / rollback / quarantine policy
6. operator or root workflows can later retrieve the profiling summary and, when configured, the heavier profiling artifacts

The important rule is that profiling is an escalated diagnostic mode under supervisor policy, not a permanent runtime tax.

Restart-into-profile is also an availability-affecting action. The automatic policy path must therefore defer profiling restarts while a recent browser session or live member link is observed; critical low-memory restart policy remains the separate last-resort recovery path. A profiling window starts only after the restarted runtime API is ready, so slow bootstrap time is not counted as useful sampled-profile time and cannot prematurely stop the profiler before final artifacts are materialized.

The supervisor also owns a hard control-plane tripwire for failures that do not
look like ordinary runtime RSS growth. It samples supervisor RSS, runtime
process-family RSS, swap used, and top-level `state/supervisor/*.json|*.jsonl`
sizes. Oversized supervisor state files are preserved under
`state/incidents/control-plane-state-tripwire-*` and replaced with compact
placeholders or empty JSONL files; sustained runtime-family pressure restarts
the managed runtime, while sustained supervisor RSS pressure requests an
autostart supervisor self-restart when that is available. This protects the
control plane itself from becoming the source of the memory incident.

### Signals and admission rules

Supervisor should avoid triggering profiling from one instantaneous RSS sample.

Memory suspicion should be based on a combination of:

- absolute process-family RSS over a configured threshold
- positive RSS growth slope over a time window
- post-switch RSS significantly above the pre-switch baseline
- threshold breach sustained beyond a stabilization grace period

This is especially important because AdaOS runtimes may legitimately allocate memory during:

- slot boot and dependency import
- skill runtime preparation or activation
- workspace materialization
- model/session warm-up
- cache rebuild after update or rollback

### Profiler strategy

The preferred default profiler strategy is:

- built-in `tracemalloc` for automatic supervisor-triggered profiling sessions
- optional heavier profiler adapters for deep-dive workflows on supported environments

The current target adapter split is:

- `TracemallocProfilerAdapter`
  - lowest operational complexity
  - safe for automated restart-into-profile mode
  - useful for Python allocation growth snapshots and diffs
- `MemrayProfilerAdapter`
  - optional deep-dive adapter for environments where native-allocation analysis is worth the overhead and platform support is available
  - not required for the first implementation

The supervisor must treat profilers as pluggable adapters.
The policy engine decides when to escalate; the adapter decides how profiling is started, stopped, and materialized into artifacts.

### Runtime launch contract

Escalation into profiling mode must not depend on ad-hoc runtime-specific flags.

The supervisor-owned launch contract should therefore reserve explicit runtime environment keys for memory profiling:

- `ADAOS_SUPERVISOR_PROFILE_MODE`
- `ADAOS_SUPERVISOR_PROFILE_SESSION_ID`
- `ADAOS_SUPERVISOR_PROFILE_TRIGGER`

Rules:

- `normal` remains the default when these keys are absent
- Phase 1 may expose these keys as part of the contract before restart-into-profile is implemented
- Phase 2 uses the same keys for the actual restart-into-profile flow instead of inventing a second launch mechanism
- the runtime may treat these keys as read-only diagnostic context and must not promote itself into profiling mode by local guesswork alone

### Top-level operation log

Profiling artifacts are much more useful when they can be aligned with top-level runtime activity.

The runtime should therefore emit a compact supervisor-consumable operation log for events such as:

- `slot_started`
- `slot_promoted`
- `skill_loaded`
- `skill_activated`
- `skill_unloaded`
- `scenario_started`
- `workspace_opened`
- `model_session_started`
- `tool_invoked`
- `core_update_prepare`
- `core_update_apply`
- `core_update_activate`

This log should stay high-level and bounded.
It is not intended to mirror every internal event-bus message.

Recommended `operations.ndjson` record shape:

- `contract_version`
- `event_id`
- `event`
- `emitted_at`
- `session_id`
- `profile_mode`
- `slot`
- `runtime_instance_id`
- `transition_role`
- `sample_source`
- `sequence`
- `details`

Phase 1 should freeze this envelope even if the runtime is not yet emitting real operation traffic beyond supervisor-owned control intents.

### Persisted profiling state

In addition to the current supervisor state files, the target model should add local profiling storage under supervisor state.

Recommended target layout:

- `state/supervisor/memory/runtime.json`
- `state/supervisor/memory/telemetry.ndjson`
- `state/supervisor/memory/sessions/<session_id>/summary.json`
- `state/supervisor/memory/sessions/<session_id>/operations.ndjson`
- `state/supervisor/memory/sessions/<session_id>/artifacts/...`
- `state/supervisor/memory/sessions/index.json`

Recommended summary fields:

- `session_id`
- `slot`
- `runtime_instance_id`
- `transition_role`
- `profile_mode`
- `trigger_reason`
- `trigger_threshold`
- `baseline_rss_bytes`
- `peak_rss_bytes`
- `rss_growth_bytes`
- `started_at`
- `finished_at`
- `suspected_leak`
- `top_growth_sites`
- `operation_window`
- `published_to_root`
- `artifact_refs`

The important rule is that supervisor owns the diagnostic session record even if runtime produces the raw snapshots.

### Phase 1 implementation baseline

The current Phase 1 implementation establishes the contract and storage baseline without yet enabling automatic restart-into-profile behavior.

Current implementation artifacts:

- `src/adaos/services/supervisor_memory.py`
  - supervisor-owned schema normalization for memory telemetry samples, runtime state, session summaries, operation events, and artifact refs
  - dedicated state-path helpers for supervisor memory storage
- `state/supervisor/memory/runtime.json`
  - persisted memory profiling runtime contract and current live-mode summary
- `state/supervisor/memory/sessions/index.json`
  - persisted index for profiling sessions
- `state/supervisor/memory/sessions/<session_id>/operations.ndjson`
  - contract-stable per-session operation log used for later growth correlation

Current implementation surfaces:

- `GET /api/supervisor/memory/status`
- `GET /api/supervisor/memory/telemetry`
- `GET /api/supervisor/memory/incidents`
- `GET /api/supervisor/memory/sessions`
- `GET /api/supervisor/memory/sessions/{session_id}`
- `GET /api/supervisor/memory/sessions/{session_id}/artifacts/{artifact_id}`
- `POST /api/supervisor/memory/profile/start`
- `POST /api/supervisor/memory/profile/{session_id}/stop`
- `POST /api/supervisor/memory/publish`

Current implementation scope now spans the completed Phase 1 baseline plus the first active Phase 2 slice:

- keeps the authority boundary, launch contract, operation log, and local session store under supervisor ownership
- records rolling process-family telemetry under `state/supervisor/memory/telemetry.ndjson`
- persists baseline RSS, growth, slope, telemetry cadence, and compact suspicion state in `runtime.json`
- exposes implemented launch modes `normal`, `sampled_profile`, and `trace_profile` as supervisor-managed runtime truth
- lets `profile/start` and policy-created sessions converge through the same requested-profile workflow instead of separate ad-hoc paths
- applies requested profile mode through a controlled supervisor restart using the Phase 1 launch contract keys
- creates a supervisor-owned profiling session automatically when telemetry crosses both growth and slope thresholds
- materializes local `tracemalloc` start/final/top-growth artifacts under `state/supervisor/memory/sessions/<session_id>/artifacts`
- adds trace-oriented `tracemalloc` traceback artifacts when `trace_profile` is selected so that trace mode yields richer diagnostics than the sampled baseline
- records top growth sites and artifact refs back into the supervisor-owned session summary when a profiled runtime exits
- suppresses repeated policy-triggered restart loops with cooldown/circuit-breaker guards before opening another automatic profiling session
- exposes telemetry tail and richer per-session inspection so operators can inspect growth samples, operation log, and collected artifacts together
- exposes explicit retry flow for failed/cancelled/stopped profiling sessions instead of overloading manual start semantics

Current implementation control mode is `phase2_supervisor_restart`:

- `profile/start` creates a supervisor-owned profiling request and the monitor applies it through restart-into-profile
- `profile/stop` clears the requested mode and lets the monitor converge the runtime back to `normal`
- suspicion policy can create a `sampled_profile` request automatically when growth remains both large and steep
- `publish` records operator intent locally and now attempts the first dedicated root summary publication path, persisting `published_ref` / `publish_result` back into the supervisor-owned session record
- retry-created sessions now carry explicit retry-chain metadata (`retry_of_session_id`, `retry_root_session_id`, `retry_depth`) so later incidents can be grouped without reconstructing lineage from operations alone

Current implementation deliberately does not yet:

- publish heavy profiling artifacts to root

The first active Phase 3 slice now exists:

- supervisor publishes memory-profile summaries to root through a dedicated `memory_profile` report family
- root-side retrieval can list those summaries by hub, optional session id, compact state filters, and suspected-only filters before heavy artifact transport is added
- operator surfaces can open one remotely published memory-profile session directly to inspect RSS deltas, retry lineage, telemetry tail size, and artifact summary metadata
- operator surfaces can inspect that remote summary path through `adaos hub root reports --kind memory-profile`
- root can expose an artifact catalog with explicit publish-policy status (`inline_available`, `size_limit_exceeded`, `kind_not_allowed`, and similar states) for one published session
- root can serve the currently allowed inline JSON artifact payloads for one published session, while heavy or disallowed artifacts remain local-only until a later transport policy is added
- operator tooling now has a normalized root-side delivery contract for artifacts: root can answer `root_inline_content` directly for small published JSON payloads, and can return an explicit `local_control_pull` contract for heavier local artifacts, including chunked `utf-8` / `base64` transfer metadata for the direct pull path without pretending that those artifacts are replicated at root

### Target-state integration: `ProfileOps`

The next target-state step for this profiling work should be explicitly named:

- `ProfileOps`

`ProfileOps` means:

- supervisor keeps ownership of profiling policy, profiling-mode restarts, local telemetry, sessions, and artifacts
- root keeps the `memory_profile` report family as the publication and retrieval substrate
- `Root MCP Foundation` publishes typed profiler tools as the governed operational surface over that supervisor-owned state

This target state is intentionally not:

- direct remote control of supervisor-only endpoints by external MCP clients
- treating root report endpoints as if they were already the MCP product surface
- bypassing root policy, scope, and audit for profiling writes

The desired layering is:

```text
supervisor profiling authority
  -> local API, session store, telemetry, artifacts

root memory_profile reports
  -> replicated summaries and selected artifacts

Root MCP Foundation / ProfileOps
  -> typed profiler reads
  -> typed bounded profiler controls
  -> scope checks, capability checks, audit, and client-facing contracts
```

Under `ProfileOps`, profiling should become a first-class `MCP Operational Surface`, not a side channel attached after the fact.

### Local control surfaces

The target local supervisor memory API should include read-only status and explicit operator controls:

- `GET /api/supervisor/memory/status`
- `GET /api/supervisor/memory/sessions`
- `GET /api/supervisor/memory/sessions/{session_id}`
- `POST /api/supervisor/memory/profile/start`
- `POST /api/supervisor/memory/profile/{session_id}/stop`
- `POST /api/supervisor/memory/publish`

The browser-safe read-only surface should eventually expose a compact memory incident summary without exposing mutating controls.

Phase 1 and the early Phase 2 slice expose a compact browser-safe memory summary:

- `GET /api/supervisor/public/memory-status`

That surface is intentionally small and read-only:

- current profile/control mode
- requested profiling intent, if any
- suspicion state
- compact baseline/growth summary
- session counters
- compact last-session summary

For manual controls, the safety policy should be explicit:

- manual profile start must be rejected while a core transition is already active
- only one active profiling intent/session may exist at a time unless a future multi-session policy is documented explicitly
- `publish` may record an operator request during Phase 1, but must not claim that root publication has completed until an explicit ack exists
- low-memory or degraded nodes may still downgrade artifact collection even when restart-into-profile is supported

### Root retrieval model

Profiling evidence should follow the same general remote-access philosophy as other root control reports:

- the node keeps local authoritative copies first
- supervisor publishes summaries asynchronously
- root indexes those summaries by hub, subnet, and zone
- heavier artifacts can be fetched only when requested and authorized

Target root-facing capabilities:

- ingest a memory-profile summary report from a hub
- list memory-profile incidents by `hub_id`, `subnet_id`, and `zone`
- fetch a single profiling session summary
- retrieve profiling artifacts when policy and size constraints allow it

This should remain a separate report family rather than overloading generic lifecycle reports.

### Safety and recovery rules

Memory profiling must not reduce the node to an unrecoverable state.

Supervisor policy should therefore preserve these rules:

- profiling restarts must stay bounded by timeouts
- repeated profile-trigger loops must trip a circuit breaker
- low-memory devices may skip heavy artifact collection and keep only summaries
- candidate prewarm and warm-switch memory admission must remain separate from leak suspicion policy
- a confirmed leak may trigger rollback or quarantine policy, but profiling itself must not silently mutate slot authority

## Migration plan

### Phase 1 - Memory watchdog architecture and state model

- freeze the supervisor-owned memory profiling authority boundary
- define memory telemetry, profiling session, and artifact metadata schemas
- document profiler adapter strategy with `tracemalloc` as the default automated path
- define top-level operation-log contracts needed to correlate memory growth with runtime behavior
- freeze the runtime launch contract for future restart-into-profile mode
- expose explicit operator profiling intents in supervisor-owned APIs and state, even before policy-driven automatic restart is implemented

### Phase 2 - Local memory telemetry and profiler-mode restart

- add supervisor-owned rolling process-family memory telemetry
- add suspicion policy based on threshold + slope + stabilization window
- add explicit runtime launch modes `normal`, `sampled_profile`, and `trace_profile`
- implement restart-into-profile flow for the active slot when memory policy is breached using the Phase 1 launch contract
- persist local profiling sessions and summaries under `state/supervisor/memory`

### Phase 3 - Root publication and remote retrieval

- publish memory-profile summaries to root as a dedicated report family
- scope retrieval by `hub_id`, `subnet_id`, and `zone`
- expose lightweight operator retrieval flows before large artifact transport
- keep local-first retention so profiling evidence survives root/network outages

### Phase 3.5 - `ProfileOps` architecture fixation

- declare `ProfileOps` as the goal-state convergence of supervisor profiling and Root MCP
- freeze the first profiler tool ids and capability vocabulary for MCP-facing reads and writes
- define the split between root-published profiling evidence and target-routed profiling control actions
- document that supervisor remains profiling authority while Root MCP becomes the typed external surface

Exit criteria:

- architecture docs consistently describe profiling as a supervisor-owned surface projected through Root MCP
- the system no longer relies on implicit knowledge of raw supervisor/root endpoints to explain the target state

### Phase 4 - `ProfileOps` read-only MCP surface

- add read-oriented profiler contracts to Root MCP for status, incidents, sessions, artifact catalogs, and artifact retrieval
- expose the same read surface through `RootMcpClient`
- expose the same read surface through the local Codex `stdio` bridge
- keep the existing root report endpoints as substrate and compatibility paths

Exit criteria:

- an MCP client can inspect profiling state and published evidence without bespoke knowledge of `/v1/hubs/memory_profile/*`
- profiler reads participate in standard Root MCP policy and audit flows

### Phase 5 - `ProfileOps` bounded control surface

- add typed MCP write tools for `start_profile`, `stop_profile`, `retry_profile`, and `publish_profile`
- gate those tools on explicit target-published profiler capabilities
- keep control execution bounded and environment-scoped in the same style as other `hub.*` write operations
- preserve supervisor as the only authority that decides requested profile mode convergence and session lifecycle

Exit criteria:

- profiling writes can be triggered through Root MCP without bypassing root policy and audit
- profiler control paths are no longer special-cased outside the operational tool model

### Phase 6 - Unified audit and consumer convergence

- align profiler actions with the shared Root MCP operational event model
- let Infrascope and Codex consume the same typed profiler contracts
- make capability-usage and activity views include profiler operations without a second audit vocabulary
- reserve raw endpoints for transport substrate, debugging, and compatibility rather than primary integration

Exit criteria:

- profiling has one governed operational surface with both human and agent consumers
- web and MCP clients do not need separate profiler-specific integration logic

### Phase 7 - Documentation and baseline supervisor state model

- freeze supervisor authority boundary
- define persisted attempt schema
- teach CLI to prefer supervisor-style state when available

Current implementation baseline now covers this phase:

- supervisor-owned update attempt state persists under an explicit contract version instead of an ad-hoc free-form payload
- browser-safe and operator-facing update surfaces both expose normalized supervisor attempt state
- `adaos autostart update-status` prefers supervisor-backed state first, then falls back to the public supervisor transition surface before legacy runtime admin status
- operator-facing field meanings for the normalized attempt payload are documented in `docs/guides/supervisor-update-attempts.md`

### Phase 8 - Resilience before full split

- add stale-attempt timeout handling
- stop clearing update plan before validation commit
- emit explicit failure state for interrupted restart/apply paths

Current implementation baseline now covers this phase:

- stale supervisor attempts now expire deterministically for both in-flight restart/apply transitions and `awaiting_root_restart`
- autostart keeps the pending update plan through launch and clears it only after validation reaches a terminal commit or failure
- boot-time recovery no longer degrades interrupted `restarting` / `applying` paths to generic `idle`; it writes an explicit failed transition state instead

### Phase 9 - Introduce standalone supervisor process

- add `adaos supervisor serve`
- move update state and admin/update endpoints into supervisor
- make systemd unit target supervisor instead of runtime

### Phase 10 - Child runtime management

- launch runtime as a child process of supervisor
- move runtime restart and validation logic out of `autostart_runner`
- persist child process metadata and restart reason in supervisor state

### Phase 11 - Sidecar alignment

- keep `adaos-realtime` lifecycle under supervisor in managed topology
- keep runtime-owned startup/shutdown only as standalone fallback when supervisor is absent
- keep sidecar contract transport-only
- keep warm candidates memory-bounded: warm-switch admission should account for runtime process-family RSS, and candidate prewarm should defer external service-skill startup until cutover

### Phase 12 - Operator UX

- `adaos autostart/update-status` resolves to supervisor API first
- `adaos autostart update-defer` can reschedule a planned/countdown update window without losing the current supervisor attempt context
- `adaos node reliability` now falls back to browser-safe supervisor transition state and reports `runtime_restarting_under_supervisor` instead of only connection failure when runtime `:8777` is temporarily unavailable during a managed transition
- Infra State surfaces supervisor attempt state alongside runtime readiness, including `planned update`, `root promotion pending`, `root restart in progress`, and `subsequent transition queued`
- Infra State and Infrascope surface skill runtime migration diagnostics for the current or last core update attempt
- browser header/status surfaces poll a read-only supervisor transition view so controlled restarts are not shown only as generic `offline`
- canonical control-plane projections keep supervisor-owned restart/promotion phases visible even when runtime API readiness has not converged yet

## Exit criteria

The supervisor target state is complete when:

- update status remains available while runtime is down
- interrupted updates resolve to validated, failed, or rolled_back without manual file edits
- stale `restarting` / `applying` states expire deterministically
- rollback is a supervisor decision, not only a runtime-side best effort
- sidecar remains transport-only and does not absorb process/update authority
