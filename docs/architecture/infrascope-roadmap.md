# Infrascope Roadmap

This roadmap breaks `Infrascope` into implementation Phases that fit the current AdaOS codebase and documentation model.

Architecture is defined in [Infrascope](infrascope.md). This page focuses on sequencing, deliverables, and acceptance criteria.

## Sequencing Principles

1. Contracts before screens.
   The canonical object model and projections come before specialized UI widgets.
2. Reuse current runtime producers.
   Existing node, runtime, Yjs, workspaces, and observe services should feed the new control plane.
3. Operator value before visual complexity.
   `overview`, `inventory`, and inspector should land before ambitious full-graph experiences.
4. LLM compatibility from day one.
   Every Phase should improve machine-readable context, not bolt it on later.
5. Governance is part of the object model.
   Ownership, visibility, and policy should not be postponed until after the UI is built.

## Priority Pre-Phase: Projection Demand and Event Harmonization

Before expanding browser-facing scenarios with more monolithic Yjs snapshots,
AdaOS should establish a shared projection/subscription runtime contract.

This pre-phase is now tracked separately so that it stays implementation-focused:

- [Operational Event Model](operational-event-model.md): target-state event, demand, lifecycle, platform-emitter, and access contract
- [Operational Event Model Roadmap](operational-event-model-roadmap.md): master implementation order and dependency map across communication, runtime, client, and pilot work
- [Projection Subscription Roadmap](projection-subscription-roadmap.md): priority checklist for demand-driven per-webspace projections

This priority slice should land before major new browser-facing materialization
work because it defines:

- how browser clients declare projection demand
- how multiple active projections coexist in one webspace
- how skills and platform emitters dispatch updates per webspace instead of globally
- how platform-owned diagnostics, system messages, and errors appear without being hidden inside one skill snapshot
- how access metadata such as `shared`, `owner`, `guest`, and `dev` is published without introducing separate owner/guest payload branches in MVP

This slice should still follow the communication-hardening phases.
It is intended to become the architectural layer that sits above node-browser
and runtime communication guarantees, not a shortcut around them.

## Reference Scenarios

Each Phase should improve at least one of these end-to-end scenarios:

1. Diagnose why a scenario stopped working.
2. Determine whether a broken automation is caused by root, hub, member, browser, or device failure.
3. Preview impact before restarting a hub or updating a skill.
4. Explain which object exceeded an LLM or external-service quota.
5. Prepare a safe, policy-aware context packet for an LLM assistant.

## Companion Track: Root MCP Foundation

`Infrascope` is the human-facing control-plane track. In parallel, AdaOS should establish [Root MCP Foundation](root-mcp-foundation.md) on `root` as the agent-facing companion layer.

This companion track matters because:

- Phase 0 canonical vocabulary should be shared by web, SDK, and future MCP surfaces
- Phase 1 projections and task packets should be reusable by the future `MCP Development Surface`
- Phases 2-4 should treat `infra_access_skill` as a first-class operational skill whose web UI and observability are inspectable inside `Infrascope`
- Phases 5-6 should converge human and agent control surfaces around the same policy, audit, and action vocabulary

The expected Root MCP Foundation sequencing is:

1. `Architectural fixation`: terminology, root-first boundaries, development/operations split, managed target model, `infra_access_skill`, and operational event model
2. `Foundation skeleton`: minimal root MCP entrypoint, typed envelopes, tool-contract registry, and audit primitives
3. `MCP-to-SDK base`: machine-readable SDK and contract descriptors for LLM-assisted development
4. `Test-hub operational pilot`: first managed target, `infra_access_skill`, read-only diagnostics first, then controlled writes with observability
5. `Convergence`: shared objects, actions, event history, and review flows across web and MCP surfaces

One explicit convergence slice inside that companion track should now be named:

- `ProfileOps`

`ProfileOps` is the goal-state where supervisor-owned memory profiling is exposed through typed Root MCP contracts and then consumed by both:

- agent clients such as Codex
- human-facing control-plane surfaces such as `Infrascope`

For `Infrascope`, this means profiling should not be integrated as a separate bespoke report browser.
It should eventually bind to the same profiler tool contracts that Root MCP publishes for agent clients.

`Infrascope` should not wait for that whole track to finish, but it should avoid diverging from it.

## Phase 0: Canonical Vocabulary

### Goal

Create a stable object contract and shared vocabulary for states, relations, and actions.

### Deliverables

- define `CanonicalObject` and supported `kind` registry
- normalize status and health facets across root, hub, member, browser, device, skill, scenario, runtime, and quota objects
- define relation kinds such as `parent`, `depends_on`, `hosted_on`, `connected_to`, `uses`, and `affects`
- define formal action descriptors with risk and permission metadata

### Current Anchors

- `src/adaos/services/reliability.py`
- `src/adaos/services/node_config.py`
- `src/adaos/services/workspaces/index.py`
- `src/adaos/services/user/profile.py`
- `src/adaos/apps/api/node_api.py`
- `src/adaos/apps/api/skills.py`
- `src/adaos/apps/api/scenarios.py`

### Done When

- every core object class can be represented with `id`, `kind`, `title`, `status`, `relations`, and `governance`
- the same status words mean the same thing across node and runtime APIs

### Current Implementation Slice

The first code slice for Phase 0 is intentionally narrow:

- add shared canonical vocabulary and object envelope under `src/adaos/services/system_model/*`
- add adapter functions for current node, skill, scenario, profile, and workspace shapes
- expose SDK entry points for canonical self, skill, scenario, and reliability projection access while keeping API as a thin facade
- verify the vocabulary and adapters with focused unit tests before adapting more APIs

The current checkpoint extends that same envelope into selected reliability snapshots, adds explicit `kind` and `relation` registries, and introduces canonical coverage for workspace, profile, browser-session, device, capacity, and quota objects. It also adds shared governance defaults for SDK-facing objects, protocol traffic-budget quota projections, and a subnet-neighborhood projection over directory snapshots plus nearby root/control connections. The local inventory projection now combines local catalog objects with selected reliability-derived root/quota objects for SDK and LLM use. External API growth stays intentionally narrow: raw responses remain intact, while SDK keeps the broader surface area and the added HTTP facades stay limited to aggregate projections.

## Phase 1: Projection Layer

### Goal

Build machine-readable projections that can feed both UI and LLM workflows.

### Deliverables

- object projection service
- topology projection service
- narrative projection for summaries and operator focus
- action projection for formal safe actions
- task packet builder for LLM and automation

### Current Anchors

- `src/adaos/services/scenario/projection_service.py`
- `src/adaos/services/scenario/projection_registry.py`
- `src/adaos/apps/api/observe_api.py`
- `src/adaos/apps/api/subnet_api.py`
- `src/adaos/services/observe.py`
- `src/adaos/services/eventbus.py`

### Done When

- a selected object can be fetched as `object`, `neighborhood`, and `task_packet`
- UI and LLM no longer need bespoke serializers for each object kind

This phase should also preserve compatibility with future `ProfileOps` task packets and object inspectors so profiling sessions and incidents can later be projected through the same object model instead of a profiling-only UI path.

## Phase 2: Operator Workspace MVP

### Goal

Ship the first useful operator-facing control plane.

### Deliverables

- `overview` with health strip, active incidents, quota summary, active runtimes, and recent changes
- `inventory` tabs for hubs, members, browsers, devices, skills, and scenarios
- unified right-side `object inspector`
- drill-down from incident to object without leaving context

### Current Anchors

- `src/adaos/services/workspaces/*`
- `src/adaos/services/yjs/*`
- `src/adaos/services/scenario/webspace_runtime.py`
- browser-facing workspace shells that already consume Yjs-backed state

### Done When

- an operator can locate the failing area of the subnet in under five seconds
- object details open in-context instead of as disconnected standalone pages

### Current Implementation Slice

The current Phase 2 checkpoint turns that MVP into an installable operator workspace:

- canonical `overview` and `object inspector` projections are exposed through `src/adaos/services/system_model/projections.py`, `src/adaos/services/system_model/service.py`, `src/adaos/apps/api/node_api.py`, and the control-plane SDK surface
- canonical runtime projections now also include the supervisor-owned core runtime object so operator surfaces can keep transition state such as `root promotion pending` and `root restart in progress` visible instead of collapsing them into generic hub instability
- supervisor-backed operator projections now also carry scheduled/deferred core-update state, queued follow-up transitions, countdown defer actions, warm-switch admission context (`transition_mode`, candidate runtime URL/port, fallback reason), and runtime instance identity (`runtime_instance_id`, `transition_role`) so operators can see not only when an update is planned but also whether supervisor intends a warm candidate handoff or a stop-and-switch fallback and which process currently owns active vs candidate authority
- the reusable client-side page runtime now supports richer `visibleIf` expressions and state-driven data reloads for declarative widgets, so one scenario can switch between `overview`, `inventory`, and inspector tabs without bespoke UI code
- `.adaos/workspace/skills/infrascope_skill/*` provides the UI-facing adapter skill that shapes overview sections, inventory rows, and selected-object inspector payloads for Web Desktop
- `.adaos/workspace/scenarios/infrascope/*` provides the first desktop `Infrascope` scenario with overview panels, inventory tabs for hubs/members/browsers/devices/skills/scenarios, a unified right-side inspector, and incident-to-object drill-down without leaving context
- focused tests cover the Phase 2 projections, API facades, SDK helpers, and workspace skill/scenario assets before later topology and impact work expands the UI

## Phase 3: Topology and Impact Mode

### Goal

Make relationships, failure paths, and change impact visible.

### Deliverables

- layered topology map for `physical`, `connectivity`, `runtime`, `resource`, and `capability` views
- neighborhood isolation and graph filtering
- dependency graph for selected object
- impact preview before `restart`, `reconnect`, `update`, or `disable`
- version and sync drift map

### Current Anchors

- `src/adaos/services/subnet/link_manager.py`
- `src/adaos/services/subnet/link_client.py`
- `src/adaos/services/subnet/runtime.py`
- `src/adaos/services/root/service.py`
- `src/adaos/services/reliability.py`

### Done When

- the operator can identify the path of failure, not only the failing node
- destructive or disruptive actions show affected objects before execution

## Phase 4: Runtime, Incidents, and Resources

### Goal

Turn `Infrascope` into a real operational cockpit instead of a static inventory.

### Deliverables

- runtime timeline for scenario and skill executions
- failed runs, retries, and event backlog surfaces
- incident model with impact radius and root-cause candidates
- resource and quota views tied back to owning objects
- trace from browser or user action to scenario, skill, and device effect

### Current Anchors

- `src/adaos/services/scenario/workflow_runtime.py`
- `src/adaos/services/skill/runtime.py`
- `src/adaos/services/skill/service_supervisor_runtime.py`
- `src/adaos/services/observe.py`
- `src/adaos/services/resources/*`
- `src/adaos/services/chat_io/telemetry.py`

### Done When

- a runtime failure can be traced through execution and event flow
- quota spikes can be attributed to a concrete scenario, skill, or subnet object

`ProfileOps` alignment point for this phase:

- profiling incidents, sessions, and artifact summaries should appear as first-class inspectable operational objects once the typed profiler MCP surface exists

## Phase 5: Profile-Aware Overlays

### Goal

Support multiple human and machine consumers over the same canonical objects.

### Deliverables

- identities, profiles, workspaces, roles, and ownership overlays
- profile-aware landing pages and default filters
- object representations for `operator`, `developer`, `household`, and `llm`
- ownership and review metadata on changes and incidents

### Current Anchors

- `src/adaos/services/user/profile.py`
- `src/adaos/services/workspaces/index.py`
- `src/adaos/services/policy/*`
- Yjs workspace overlays already present in workspace manifests

### Done When

- the same hub object can render differently for admin, household user, and LLM without diverging IDs or relations
- visibility and actions are enforced by the same governance layer that shapes the UI

## Phase 6: LLM-Assisted Operations and Change Loop

### Goal

Move the LLM from passive observer to governed participant in diagnosis and controlled change.

### Deliverables

- task packets with `desired_state`, `actual_state`, `gap`, and `constraints`
- change proposals referencing formal actions instead of ad hoc text instructions
- dry-run and impact simulation before proposal submission
- review and approval flow for LLM-generated changes
- recommendation and anomaly layers on top of structured projections
- LLM-assisted conflict resolution for git/workspace merge conflicts: a node
  detects a conflict, asks an LLM through root, sends bounded artifacts, and
  accepts the result only as a validated patch/action proposal

### Future Task: LLM Conflict Resolution

Workflows such as `skill push`, workspace registry updates, and other governed
git operations need a dedicated conflict-resolution loop. When a node detects a
merge or rebase conflict, it should not repair the worktree with ungoverned local
heuristics. The target flow is:

1. The node records the conflict as a runtime incident and builds a
   `conflict_pack`: `git status`, conflicted paths, base/ours/theirs versions,
   conflict-marker snippets, commit metadata, intended operation, and policy
   context.
2. The node sends the `conflict_pack` through a root-managed LLM endpoint without
   exposing secrets, tokens, or personal workspace data.
3. The LLM returns a structured `resolution_proposal`, not shell commands:
   explanation, patch or file replacements, confidence, affected paths, risk
   notes, and required checks.
4. The node applies the proposal in a temporary or isolated worktree, runs
   JSON/schema/tests/dry-run validation, and builds an impact summary.
5. Only after validation and, where required, operator approval does the node
   apply the resolution to the real merge/rebase through the existing git
   adapters.

The first safe candidates are `registry.json` conflicts during `skill push` and
`scenario push`, where the LLM can reconcile remote additions with the local
version bump, preserve both sets of entries, and update metadata without losing
catalog records.

### Current Anchors

- projection services introduced in Phase 1
- governance and ownership overlays introduced in Phase 5
- existing CLI and API control paths under `src/adaos/apps/cli`, `src/adaos/apps/api`, and `src/adaos/sdk/manage/*`

### Done When

- an LLM can explain a problem, propose a change, and hand over a reviewable action plan without bypassing policy
- operators can accept, reject, or refine proposals using the same control-plane objects

## Release Grouping

The roadmap can be grouped into user-visible releases:

- `v1 / operational MVP`: Phases 0-2
- `v2 / topology and runtime control`: Phases 3-4
- `v3 / profile-aware and LLM-native control plane`: Phases 5-6

## Suggested Implementation Order

1. Normalize object envelopes in existing API and service responses.
2. Introduce projection services and neighborhood/task-packet builders.
3. Build `overview`, `inventory`, and unified inspector over those projections.
4. Add layered topology and impact views.
5. Add runtime traces, incidents, and resource attribution.
6. Add profile overlays, ownership, and review/change workflows.

## Practical First Backlog

If implementation starts immediately, the first backlog should stay narrow:

- define `CanonicalObject` and `ActionDescriptor`
- create projection adapters for `root`, `hub`, `member`, `skill`, and `scenario`
- expose one `overview` endpoint composed from existing health and runtime signals
- expose one `object inspector` endpoint that returns object, incidents, recent events, and actions
- add one `task_packet` endpoint for a selected object and task goal

That is enough to begin shipping `Infrascope` without waiting for the full graph or multi-user polish.
