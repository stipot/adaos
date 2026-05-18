# Skill Projection and Stream Boundary

This document tracks the current stabilization work around skill-owned data,
Yjs projections, stream data, node-aware UI addressing, and the temporary
bridges added while the core projection boundary is being hardened.

It is intentionally operational: use it to understand which fixes are target
architecture, which fixes are transition shims, and which follow-up tasks must
be removed from individual skills once the core owns the contract.

## Current Diagnosis

The browser issues that triggered this work were not caused by one broken
transport only.

Observed stand behavior showed:

- hub/browser Yjs could be `ready/fresh` while selected skill panels stayed
  empty.
- `voice_chat`, `browsers`, and `infra_access` data existed in Yjs under
  `data/nodes/<hub_node_id>/...`, but UI lookup could still miss it.
- `subnet_env` returned a successful tool response while no projection appeared
  in the active `desktop` webspace.
- `ui.application.modals` contained recursively prefixed ids such as
  `node:A:node:B:node:C:voice_chat_modal`, which is both a lookup hazard and a
  Yjs state growth vector.
- Some runtime skill artifacts may not carry the original `skill.yaml`, so
  handler-side attempts to load projection metadata from a neighboring manifest
  can fail silently.
- Synchronous `ctx_subnet.set(...)` could schedule an async projection task and
  return before the projection was durably applied.

## Target Architecture

The target state is:

- Skills declare `data_projections` in `skill.yaml`.
- Runtime packaging and activation preserve those projection declarations.
- Core loads projection declarations during skill activation and refresh.
- `ctx_subnet.set(...)` and `ctx_subnet.set_async(...)` are the only skill API
  needed for projected state writes.
- `ProjectionService` owns the sync/async bridge, pressure governance, live-room
  mutation preference, fallback YStore writes, and diagnostics.
- The shared [Skill Projection Runtime SDK](skill-projection-runtime-sdk.md)
  owns per-slot fingerprinting, dirty-section routing, stream receiver handling,
  and the migration path away from skill-local projection runtimes.
- Skills do not open-code thread pools, event loop bridges, node-scoped Yjs
  paths, or stream subscription routing.
- Browser UI consumes node-aware data through shared addressing helpers, not
  through ad hoc string rewriting in each widget or modal.
- Stream data is for active details and volatile panels; Yjs projections are
  for compact materialized state needed by shell widgets, modal skeletons, and
  reconnect recovery.

## Architectural Fixes Already Started

- [x] Introduced a shared client-side data addressing service.
- [x] Scoped node-owned modal data to `data/nodes/<node_id>/...`.
- [x] Kept shared desktop roots such as `data/catalog`, `data/desktop`,
  `data/webspaces`, and device/browser inventory out of accidental node
  scoping.
- [x] Normalized node-scoped modal ids in scenario projection so
  `node:A:node:B:<modal>` does not keep compounding.
- [x] Added client tolerance for node-prefixed modal ids.
- [x] Changed the Browsers desktop action to open the modal in the current node
  context.
- [x] Identified sync `ctx_subnet.set(...)` fire-and-forget behavior as a core
  durability hazard for short tool calls.

## Transition Shims

These are useful for stand stability, but should not remain as the final
architecture:

- [ ] Per-skill projection fallback declarations embedded in handlers.
- [ ] Per-skill `ThreadPoolExecutor` bridges for projection completion.
- [ ] Skill-specific defaulting to `desktop` when activation metadata is absent.
- [ ] Handler-side manifest fallback logic for runtime artifacts that do not
  include `skill.yaml`.

## Core Roadmap

- [ ] Add a first-class `ProjectionService.apply_sync(...)` or equivalent SDK
  bridge so sync skill handlers can durably publish without per-skill executors.
- [ ] Implement the shared projection runtime SDK described in
  [Skill Projection Runtime SDK](skill-projection-runtime-sdk.md), starting with
  `ProjectionSlot`, `StreamReceiver`, dirty routing, set-if-changed, and
  diagnostics.
- [ ] Make runtime packaging preserve projection metadata for every installed
  skill artifact.
- [ ] Load skill projection declarations during activation before any tool,
  subscription, or startup refresh can publish.
- [ ] Add diagnostics for projection registry misses:
  `scope`, `slot`, `skill`, `webspace_id`, and whether the skill manifest was
  available.
- [ ] Add a Yjs/write log event when a skill returns `ok` but no projection rule
  exists for the slot it attempted to publish.
- [ ] Add projection-pressure attribution that distinguishes:
  core rebuild, skill projection, stream snapshot, browser gateway replay, and
  live-room repair.
- [ ] Add cleanup/migration for already-materialized recursive modal ids in
  existing Yjs documents.
- [ ] Remove per-skill fallback projection declarations after core projection
  loading is guaranteed.
- [ ] Remove per-skill projection executors after the SDK/core sync bridge is
  available.

## Skill Roadmap Index

Private skill checklists are tracked next to each skill:

- `.adaos/workspace/skills/browsers_skill/TODO.md`
- `.adaos/workspace/skills/subnet_env/TODO.md`
- `.adaos/workspace/skills/voice_chat_skill/TODO.md`
- `.adaos/workspace/skills/infra_access_skill/TODO.md`
- `.adaos/workspace/skills/infrastate_skill/TODO.md`
- `.adaos/workspace/skills/infrascope_skill/TODO.md`
- `.adaos/workspace/skills/mediaserver/TODO.md`

## Current Progress

Overall status: stabilization in progress.

Approximate split:

- Target-aligned architecture: 60%.
- Transition shims still present: 40%.

Do not treat the current per-skill fallback code as the final design. It is a
deliberate stabilizer so the stand remains observable while the core projection
boundary is made durable.
