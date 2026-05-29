# Operational Event Model

This document defines the target-state event model for AdaOS runtime interaction between core services, skills, scenarios, webspaces, browser clients, and future Root MCP operational surfaces.

It is not only a browser-facing model.
It should shape how skills and the core cooperate internally, and how only the relevant parts of that cooperation later become materialized browser-visible projections.

The goal is to keep Yjs as a live collaborative projection layer while making demand, refresh, access, node scope, and platform-originated runtime semantics explicit and reusable across the system.

The implementation order for this target state is tracked in
[Operational Event Model Roadmap](operational-event-model-roadmap.md).
That roadmap is the single authoritative delivery track for event,
projection, browser/runtime, and platform-emitter work.  The companion
[Projection Subscription Roadmap](projection-subscription-roadmap.md) is a
detail checklist for the projection/client/dispatcher parts of that same
track; it must not define an independent implementation order.
For the top-level coverage gates and completion definition used to review
implementation slices, see
[Operational Event Model Reference Plan](operational-event-model-reference-plan.md).

For the target typed ref vocabulary used by browser-facing manifests and
projection-aware runtime contracts, see [UI Addressing](ui-addressing.md).
For the target runtime naming and alias model that feeds NLU canonicalization,
see [Named Entities and Canonical Naming](named-entities.md).

## Current Implementation Boundary

Snapshot date: 2026-05-15.

The target model is now ahead of a pure design note: several foundation pieces
already exist in code and should be treated as implementation constraints for
the next roadmap slice.

Implemented foundation:

- the local event bus has bounded hot-topic async fanout, supersede/drop
  counters, slow-handler logging, and backlog snapshots for incident capture
- named-entity records, localized label metadata, entity resolution results,
  compact registry projection, and `entity.*` topic constants exist
- authoritative device/browser access-link changes emit first lifecycle events
  such as `entity.observed`, `entity.draft_name.suggested`,
  `entity.display_name.changed`, `entity.alias.*`, and
  `entity.registry.changed`
- node ownership is already visible in several compatibility-era browser
  surfaces, stream receiver hints, catalog items, and workspace-manager state
- stream-backed operational overlays already exercise some of the future
  projection pressure paths through `webio.stream.*` events

Remaining architectural gap:

- there is still no shared event envelope for all runtime events
- projection demand is not yet represented by one browser-written subscription
  registry
- projection records are not yet one canonical ABI across platform, skills, and
  scenarios
- the dispatcher is still a pattern spread across services and skills, not a
  reusable runtime service
- platform operational emitters are emerging through diagnostics/status work,
  but notifications, status cards, and projection lifecycle events are not yet
  one shared plane

The next implementation step should therefore be treated as an ABI and runtime
contract slice, not as another skill-specific migration.

## Why This Exists

AdaOS now has enough browser-facing runtime behavior that a simple "subscribe to many events and rewrite one big Yjs snapshot" model no longer scales well.

The main problems are:

- one large projection branch causes noisy Yjs writes and broad client invalidation
- skills often know more than the active UI currently needs
- multiple webspaces may be open at the same time in different browsers
- page, widget, modal, and panel surfaces may need different projections concurrently
- human-facing names, observed hostnames, aliases, and canonical refs change over time and must not be baked into model training
- localized labels and aliases are runtime facts, while canonical refs remain
  language-neutral
- the platform itself emits system messages, failures, and diagnostics that do not belong to one skill
- the same domain event may affect many potential projections, but only a few are actually demanded

The target model therefore separates:

- domain truth
- internal runtime interaction between core and skills
- named-entity lifecycle and canonical resolution
- UI demand for projections
- projection lifecycle state
- platform-originated operational messages and diagnostics
- Yjs materialized projection payloads

## Core Concepts

### 1. Domain Events

Domain events are facts about the runtime and control plane, for example:

- `browser.session.changed`
- `subnet.member.link.down`
- `skill.installed`
- `core.update.status`

These events update the skill or service's in-memory understanding of the world.
They do not require an immediate Yjs write by themselves.

### 1a. Core-Skill Interaction Events

AdaOS also needs an explicit event layer for interaction between the core and skills, not only for browser delivery.

Examples:

- runtime availability changed for one service surface
- projection invalidated by a core-owned state transition
- scenario support skill should warm or cool one active projection family
- node-local runtime state changed and dependent skills need to rebuild their in-memory semantic model

This interaction layer is where the main architectural value sits.
Browser-facing materialization is a downstream consumer of that model, not its whole purpose.

### 1b. Named Entity Lifecycle Events

AdaOS also needs explicit events for human-facing names, localized labels,
observed names, aliases, canonical refs, and resolution diagnostics.

Examples:

- a node hostname was observed and can be used as a display fallback
- a browser draft name was suggested during registration
- a user confirmed or changed a device display name
- an alias was added, removed, deprecated, or found to conflict
- a localized alias was added for one locale without changing the entity id
- an NLU request could not resolve an entity name unambiguously

These events bridge runtime truth and language understanding.
They are not projection payloads by themselves.
They update the named-entity read model, invalidate resolver snapshots, and may
produce operator-facing diagnostics when ambiguity or conflict matters.
They must carry locale hints for linguistic labels, while keeping canonical refs
and dispatch identities locale-neutral.

### 2. UI Intent Events

UI intent events represent what a browser shell is trying to show or do, for example:

- open inspector for one object
- switch from `overview` to `inventory`
- open a modal
- pin a long-lived panel

These intents are allowed to change the set of demanded projections.

### 3. Projection Demand

Projection demand is the declared need for one or more materialized views inside one specific webspace.

This is the central addition in this architecture.

Projection demand is not inferred from domain events alone.
It is explicitly registered by browser clients through subscriptions.

### 4. Projection Lifecycle

Every projection has a lifecycle independent from the underlying domain events.

The minimum MVP lifecycle states are:

- `pending`
- `ready`
- `refreshing`
- `stale`
- `error`

The projection lifecycle tells the client how to treat the current materialized payload.

### 5. Platform Operational Emitters

The platform itself must be treated as a first-class emitter of browser-relevant operational state.

Examples:

- system notifications and banners
- webspace materialization failures
- projection refresh failures
- browser transport degradation
- workspace/session warnings
- runtime-wide diagnostics that do not belong to one skill

This matters because otherwise platform failures get hidden behind whichever skill happened to be active when the browser needed the information.

### 6. Materialized Projections

Materialized projections are the Yjs-visible payloads produced for the active UI demand of a specific webspace.

They are:

- cached views
- not the canonical source of truth
- not an event log
- not a substitute for the in-memory semantic model held by the skill or service

## Scope Model

Two scopes must remain distinct:

- `event scope`: where the domain fact is true
- `projection scope`: where the materialized UI view is needed

These scopes often differ.

Example:

- `skill.installed` is effectively global domain truth
- but the resulting Yjs projection update should only happen for webspaces that currently demand a projection affected by installed skills

For MVP:

- projection scope is always `per-webspace`
- one webspace may have many browser clients
- each webspace may demand a different set of projections

There is also a second axis that the browser does not yet fully model:

- `node scope`

Yjs state is already shared across subnet nodes.
The target event model should therefore assume that some projections and operational emitters are node-scoped inside a shared collaborative space.

That means the architecture should make room for top-level node-owned branches such as:

- `nodes/<node_id>/...`
- `platform/nodes/<node_id>/...`

The browser may initially continue to consume simplified aggregate projections, but the storage and event model should not assume a single anonymous node view forever.

MVP implementation reserves `platform/nodes/<node_id>` as a core-owned branch
for node `status`, `diagnostics`, and `projections` summaries. The existing
`data/projectionRecords` branch remains the demanded browser-facing projection
cache and is not replaced by this node branch.

## Projection Demand Model

### Browser-Written Subscription Records

Each browser client should publish its current projection demand into the Yjs document of the webspace it is attached to.

The important rule is:

- the client writes its full current subscription set
- it does not append individual add/remove deltas as the primary source of truth

This reduces drift because every client update overwrites the previous active set for that client.

Suggested shape:

```json
{
  "runtime": {
    "clients": {
      "client:web-17": {
        "device_id": "device:browser-laptop",
        "session_id": "browser-session:abc",
        "role": "owner",
        "subscriptions": [
          {
            "projection_key": "overview",
            "consumer_id": "page:main",
            "consumer_kind": "page",
            "pinned": false
          },
          {
            "projection_key": "inspector:hub-1",
            "consumer_id": "modal:object-inspector",
            "consumer_kind": "modal",
            "pinned": true
          }
        ],
        "updated_at": "2026-04-20T12:00:00Z"
      }
    }
  }
}
```

Current MVP implementation:

- browser/API demand is normalized as `ClientSubscriptionRecord`
- the node can materialize that demand to `runtime/clients` and an inspectable
  `runtime/projectionDemand` envelope
- startup restore reads `runtime/clients` back into the in-memory demand
  registry without writing projection payloads
- the browser adapter writes full session records for active YDoc-backed
  projection consumers; add/remove deltas are not the source of truth

### Active Projection Set

For each webspace, the active projection set is derived from the union of all client subscriptions currently visible in that webspace.

This means:

- projections are active because at least one consumer currently demands them
- a skill may keep richer semantic state in memory, but only materializes what is demanded
- old projections may remain in Yjs as cache entries, but should stop receiving writes when no longer demanded

### No Projection TTL

For MVP, AdaOS should not use TTL as the primary activity signal for projections.

Instead:

- the browser explicitly registers and unregisters subscriptions
- each client update rewrites that client's whole subscription set
- optional sanitation may exist only at the `client/session` record level

This keeps the activity model explicit and easier to reason about.

### Pinned Consumers

Some consumers need longer-lived projection updates even when they are not the foreground UI.

For MVP, this should be represented by a `pinned` flag on the consumer subscription.

`pinned` means:

- continue treating this consumer as intentionally long-lived
- avoid aggressive cleanup of the client/session record
- allow projections such as anchored modals or long-running panels to remain fresh

## Projection Identity

Projection keys must be deterministic and addressable.

Examples:

- `overview`
- `inventory:members`
- `inventory:skills`
- `inspector:hub-1`
- `topology:hub-1`
- `widget:quota-summary`
- `modal:workspace-manager`
- `platform:notifications`
- `platform:diagnostics`

Projection identity should not depend on random browser-local ids.
Browser-local `consumer_id` values identify who needs the projection, not what projection it is.

## Yjs Projection Contract

This document does not force one top-level namespace for every skill.
It does require a shared contract shape wherever a skill, scenario, or platform service publishes UI projections.

It should also leave room for node-scoped ownership inside one shared Yjs space when the same collaborative state is synchronized across subnet nodes.

Suggested generic shape:

```json
{
  "projections": {
    "<projection_key>": {
      "status": "pending",
      "data": null,
      "meta": {
        "projection_key": "<projection_key>",
        "kind": "overview",
        "webspace_id": "default",
        "version": 1,
        "updated_at": "2026-04-20T12:00:00Z",
        "stale": false,
        "access": {
          "audience": "shared",
          "read_only": false,
          "actions_allowed": []
        }
      },
      "error": null
    }
  }
}
```

The recommended minimum fields are:

- `status`
- `data`
- `meta`
- `error`

The recommended minimum `meta` fields are:

- `projection_key`
- `kind`
- `webspace_id`
- `updated_at`
- `version`
- `stale`
- `access`
- `source`

### Node-Scoped Projection Envelope

Because Yjs is shared across subnet nodes, AdaOS should reserve room for node-owned top-level branches.

The exact shape can evolve, but the architecture should assume a pattern like:

```json
{
  "platform": {
    "nodes": {
      "<node_id>": {
        "status": {},
        "diagnostics": {},
        "projections": {}
      }
    }
  }
}
```

This is important even before the browser fully uses node multiplicity, because:

- it avoids silently baking a single-node assumption into projection storage
- it gives core and skills a clean place to publish node-owned state
- it prepares future browser consumers to distinguish shared webspace state from node-scoped operational state

## Multi-Webspace Dispatch

Projection demand is resolved per webspace.

This means a skill or service may:

- subscribe broadly to domain events
- maintain a single richer semantic model in memory
- selectively materialize projections only for webspaces that currently demand them

Required runtime behavior:

1. receive a domain or platform event
2. update the in-memory model
3. identify which webspaces currently demand affected projections
4. rebuild only the demanded projections for those webspaces
5. write only those projections into the corresponding Yjs documents

This is the main scaling rule that prevents one browser session from forcing all webspaces to receive the same Yjs churn.

## Personalization and Access Metadata

MVP personalization is intentionally limited.

Assumptions:

- one webspace has one owner identity
- guests may access the same webspace concurrently
- projection identity remains per-webspace, not per-user
- owner and guest do not get separate payload branches in MVP

However, each projection should publish access and audience metadata so the client can render safely and clearly.

Recommended MVP `audience` values:

- `shared`
- `owner`
- `guest`
- `dev`

For MVP:

- owner and guest share the same payload
- access differences should be expressed through metadata and client behavior, not duplicated payloads
- if a payload is too sensitive to be shared with guests, it must not be published as a shared webspace projection without an explicit safe-view design
- browser-cache reads enforce `ProjectionRecord.meta.access` before returning
  records; blocked records are reported as `projection_access_denied` rather
  than returned to the client

Suggested MVP access fields:

- `audience`
- `read_only`
- `sensitive`
- `actions_allowed`
- `display_hints`

## Event Classes

The target event model should treat the following classes as first-class:

### Domain Facts

Examples:

- runtime state changes
- connectivity changes
- inventory changes
- policy and governance changes

### Named Entity Lifecycle Events

Examples:

- `entity.observed`
- `entity.draft_name.suggested`
- `entity.display_name.changed`
- `entity.alias.added`
- `entity.alias.removed`
- `entity.alias.conflict.detected`
- `entity.registry.changed`
- `entity.resolution.ambiguous`
- `entity.resolution.failed`

Entity lifecycle events should carry canonical refs whenever known, source
authority, scope, actor, previous/current values, and conflict or ambiguity
metadata.
When the event concerns a linguistic label or alias, it should also carry
`locale` and optionally `preferred_locales` or `profile_id`.
Language-neutral observed values such as hostnames should use `locale: "und"`.

Successful high-volume resolution traces do not need to be persisted as global
events by default.
Conflicts, failed resolutions, ambiguous resolutions, and dev-mode diagnostics
should be eligible for Notifications, node skill logs, and future MCP analysis.

### Projection Demand Events

Examples:

- client subscriptions changed
- modal opened
- widget mounted
- panel pinned

### Projection Lifecycle Events

Examples:

- projection refresh requested
- projection refresh completed
- projection refresh failed
- projection invalidated

### UI Intent Events

Examples:

- change current mode
- focus one object
- change inventory tab
- show hidden panel

### Platform Operational Events

Examples:

- system banner changed
- projection pipeline failed
- webspace readiness degraded
- browser transport not ready
- runtime emitted warning or error for operator-facing surfaces

Not every class must be persisted as a global bus event in MVP.
The important part is to keep the concepts separate in architecture and runtime responsibilities.

## Skill, Scenario, and Platform Responsibilities

For core services, browser-facing skills, scenario-support services, and shared platform emitters, the target contract is:

### They should do

- subscribe to domain, core-interaction, or platform events needed to maintain semantic context
- use named-entity events and canonical refs instead of duplicating name fallback logic
- keep richer semantic and derived state in memory when useful
- restore active projection demand from Yjs on startup
- materialize only demanded projections per webspace
- publish explicit projection lifecycle state
- include access metadata for clients

### They should not do

- treat Yjs as the canonical event history
- rewrite one giant snapshot on every runtime event
- assume one active projection per webspace
- assume one browser per webspace
- assume one effective node view in shared Yjs state
- infer all UI demand from domain events alone
- silently pick one entity when a human-facing name is ambiguous

## Candidate Early Consumers and Architectural Prerequisites

The model is most useful first where all of the following are true:

- the UI has multiple independently visible surfaces
- updates are frequent or bursty
- more than one webspace may demand different projections
- the current implementation uses large plain-JSON Yjs branches

The best architectural preparation steps are:

1. core and skill interaction contract
   The event model should first become a real runtime contract between core-owned services and skills.
2. named-entity lifecycle contract
   Names, aliases, observed labels, conflicts, and canonical refs should become
   a shared runtime contract before NLU and UI surfaces duplicate fallback
   behavior.
3. client/runtime projection adapter
   The browser and shared runtime need a stable way to register demand and consume projection lifecycle state.
4. node-scoped Yjs envelope
   Shared Yjs space should gain explicit room for per-node branches before higher-level scenario pilots rely on it.

After those prerequisites, the best early scenario and skill candidates are:

1. `web_desktop` platform surfaces
   Workspace manager, diagnostics, notification history, apps/widgets catalogs, and browser-shell diagnostics are a strong fit for platform-emitted projections.
2. `Infrascope`
   High event volume, many possible views, object drill-down, multiple concurrent consumers, and clear current pain from monolithic snapshots.
3. `infrastate`-style shared operational overlays
   These already behave like derived operational projections and need better per-webspace demand routing.
4. prompt and dev-oriented workspaces such as `prompt_engineer_scenario`
   Multiple panels, artifacts, and inspector-like views make them good demand-driven candidates after the operator path is stable.
5. voice/media/browser-session heavy surfaces
   These are event-heavy and benefit from keeping richer runtime state in memory while publishing only active views.

The platform should also be treated as an early first-class emitter for:

- notifications
- system warnings
- operator-visible errors
- materialization diagnostics
- browser/runtime transport state
- named-entity conflicts and ambiguous user references

This emitter track is important because it exercises the event model before every skill is migrated.

This model is not a mandatory first step for every simple skill.
Small single-card or low-churn skills can continue to use simpler projection shapes until there is a clear need.

## Relationship to Communication Hardening

This model should not jump ahead of the lower communication layers.

The intended order is:

1. harden node-browser and node/runtime communication semantics
2. fix the core-to-skill event and projection contract
3. fix the browser/client projection demand contract
4. introduce node-scoped projection ownership in shared Yjs
5. only then migrate heavy scenario skills such as `Infrascope`

In other words, this is an architectural solution, not merely an `Infrascope` adaptation.

## Implementation Order

The intended implementation order for this architecture is:

1. `communication prerequisites`
   Harden node-browser and runtime communication semantics first.
2. `core/runtime event contract`
   Introduce the shared taxonomy and ownership rules for domain, core-skill, and platform events.
3. `named-entity event contract`
   Define canonical refs, name/alias lifecycle events, resolver invalidation,
   and ambiguity diagnostics before NLU and UI consumers depend on them.
4. `node-aware Yjs envelope`
   Reserve explicit node-scoped and projection-scoped branches in shared Yjs state.
5. `client projection adapter`
   Teach the browser runtime to register demand, consume projection lifecycle state, and stop assuming a single anonymous node view.
6. `shared projection dispatcher`
   Add one reusable runtime path for `event -> in-memory update -> demanded projection refresh`.
7. `platform-emitted projections`
   Migrate notifications, diagnostics, and system errors as the first real emitted projections.
8. `heavy skill pilots`
   Only after the above layers are stable should complex skills such as `Infrascope` migrate to the new model.

This ordering should be treated as part of the target architecture, not just an implementation suggestion.

## Relationship to Root MCP Foundation and Infrascope

This event model is shared infrastructure.

It should align:

- `Infrascope` and other browser-facing operator scenarios
- scenario and skill projection runtimes
- platform-emitted diagnostics, notifications, and system errors
- Root MCP operational audit and result envelopes
- SDK-facing projection and task-packet services

The intent is one shared vocabulary with multiple consumer surfaces:

- Yjs materialized projections for web clients
- typed result and audit envelopes for MCP
- canonical projections and task packets for SDK and LLM workflows

## MVP Boundary

This document deliberately does not require:

- per-user projection payload forks inside one webspace
- a global event-sourcing system
- immediate deletion of inactive projections
- perfect liveness detection for every browser edge case
- one universal storage layout for every skill

The MVP requirement is narrower:

- explicit projection demand
- per-webspace projection dispatch
- stable projection lifecycle contract
- browser-written subscription truth
- platform-originated operator messages treated as first-class emitters
- Yjs used as collaborative projection cache, not as the whole semantic world
