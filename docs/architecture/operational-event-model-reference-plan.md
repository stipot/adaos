# Operational Event Model Reference Plan

Snapshot date: 2026-05-29.

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
- removal of arbitrary runtime ProjectionRecord write surfaces from browser/API
  exposure

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

Required artifacts:

- browser-side subscription registry
- full-overwrite client subscription writes
- modal open/close, widget mount/unmount, page view, and pinned panel mapping
  into subscription records
- soft session sanitation rules that do not act as projection activity TTL
- client tests for multiple simultaneous consumers
- startup and skill-activation restore from Yjs-written subscription state

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

Required artifacts:

- dispatcher contract for `event -> in-memory update -> demanded refresh`
- per-webspace refresh selection
- no-cross-webspace-churn tests
- lifecycle publication for pending, refreshing, ready, stale, and error
- pressure observability for coalesced, superseded, skipped, and dropped work
- skill-facing SDK helpers for registering and unregistering projection refresh
  handlers, binding them to event topics, and restoring active demand on skill
  activation
- a live eventbus bridge that routes selected platform/domain topics through
  the dispatcher without direct Yjs writes

Exit criteria:

- a domain or platform event refreshes only the projections demanded in affected
  webspaces
- dispatcher coalescing preserves evidence of incoming pressure
- services can keep richer memory state than they publish to Yjs
- existing eventbus guardrails remain visible through incident artifacts
- Yjs materialization is demanded-only, fingerprint-gated, and set-if-changed

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
- access filtering on browser reads before sensitive diagnostics are exposed

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
| Shared event envelope | Helpers and compatibility rules | Contract helper implemented on harvest branch; selected producer adoption remains an MVP blocker |
| Named-entity ABI | Records, resolver result, lifecycle topics, invalidation | Mostly complete; consumer migration remains |
| Status-card ABI | Platform-emitter family with dedupe/version/staleness | Implemented through existing `StatusRegistry`; live demanded refresh bridge remains an MVP blocker |
| Projection record ABI | Canonical record shape | Implemented on harvest branch; arbitrary runtime write endpoint removed from the node API |
| Browser subscription ABI | Full-overwrite demand records | Server/domain shape implemented; browser/Yjs source-of-truth adapter remains an MVP blocker |
| Node-aware Yjs envelope | Reserved top-level ownership shape | ProjectionRecord cache carries node metadata; reserved `platform/nodes/<node_id>/...` branch remains an MVP blocker |
| Client demand runtime | Page/widget/modal/pinned consumers | Server mapper exists; browser runtime migration remains an MVP blocker |
| Shared dispatcher | Per-webspace demanded refresh | Dispatcher contract implemented; SDK registration API and live eventbus bridge remain MVP blockers |
| Platform emitter pilot | Status/notifications/diagnostics through shared ABI | Status-card bridge implemented; minimal notifications and diagnostics remain MVP blockers |
| Infrascope migration | Uses shared ABI and dispatcher | Blocked by previous rows |

## Completion Definition

The operational event model can be considered covered when:

- all required contract shapes above exist in docs and helper code
- at least one platform-emitter family uses the shared projection contract
- browser clients can declare multiple active projection demands in one
  webspace
- the dispatcher refreshes demanded projections without cross-webspace churn
- dispatcher refreshes are coalesced and fingerprint-gated so event traffic does
  not create critical Yjs load
- skills register/unregister projection refresh handlers through the SDK rather
  than ad hoc local subscription systems
- runtime browser/API surfaces do not allow arbitrary ProjectionRecord writes
- named-entity lifecycle changes invalidate consumers without reload-only
  behavior
- Infrascope or another heavy pilot uses the shared ABI without adding a
  parallel one
- acceptance tests cover event envelope compatibility, multi-consumer demand,
  multi-webspace dispatch, platform emitter lifecycle, and pressure
  observability
