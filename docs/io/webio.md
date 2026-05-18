# Web IO (Webspace/Desktop)

This note explains how the hub drives the browser-based desktop shell through
Yjs, scenarios, and skills. It complements the existing IO docs by focusing on
the current "webspace" implementation that powers the Inimatic desktop used in
A2.

For the target architecture and the agreed evolutionary path, see
[Webspace Scenario Pointer/Projection Roadmap](../architecture/webspace-scenario-pointer-projection-roadmap.md).
For the browser-facing typed ref vocabulary used by semantic UI manifests, see
[UI Addressing](../architecture/ui-addressing.md).
For the target browser client structure above the current runtime contracts, see
[Web UI Architecture](../architecture/web-ui-architecture.md).

## Relationship to UI Addressing

This note documents concrete current runtime contracts.
The target browser-facing addressing model treats those contracts as part of a
larger typed vocabulary rather than as isolated implementation details.

The key mapping is:

- Yjs-backed runtime state in this note corresponds to `y:` refs in
  [UI Addressing](../architecture/ui-addressing.md)
- `webio.receivers` correspond to `stream:` refs
- browser-local interaction state such as filters, selection, and open tabs
  corresponds to `view:` refs
- demanded materialized views described elsewhere through `projection_key`
  correspond to `projection:` refs

This distinction matters because `webio` is not the whole UI contract.
It is the stream and browser-runtime transport slice inside a larger semantic
UI architecture.

## Architecture Overview

1. **Webspace** - each browser session attaches to a named Yjs document
   (default: `desktop`). The doc is stored in an SQLite YStore under
   `.adaos/state/ystores/<webspace>.sqlite3`.
2. **Scenario** - declarative JSON (`scenario.json`) defines desktop UI
   building blocks (apps, widgets, registry). During migration the hub may
   still seed compatibility caches in `ui.scenarios.*`, `data.scenarios.*`,
   and `registry.scenarios.*`. As of 2026-05-01 the compatibility cache
   shape used by the current desktop/subnet migration is node-scoped only:
   `ui.scenarios.<node_id>.<scenario_id>`,
   `registry.scenarios.<node_id>.<scenario_id>`,
   `data.scenarios.<node_id>.<scenario_id>`.
   Active scenario switching still primarily updates `ui.current_scenario`
   and lets semantic rebuild repopulate effective runtime branches.
3. **Skills** - runtime packages can inject additional UI declaratives via
   `webui.json`. A core runtime (`WebspaceScenarioRuntime`) resolves
   scenario + skills into ready-to-render runtime branches such as
   `ui.application`, `data.catalog`, and `data.installed`. In this model
   `web_desktop_skill` is just another skill that contributes core desktop
   widgets and modals. Skill-owned runtime declarations now also carry local
   `node_id` metadata so webio receivers and catalog entries can preserve
   ownership through the Yjs/runtime bridge.
4. **Event mesh** - UI commands travel over `/ws` (`events_ws`). Each command
   becomes a domain event (`desktop.*`) with metadata containing the current
   `webspace_id`. Skills subscribe to these events and publish results through
   governed projection or stream helpers; the runtime owns the normal YDoc
   mutation boundary. Legacy direct skill YDoc mutation is deprecated and
   should be explicit-capability-gated. The same semantic event channel now
   also carries lightweight runtime pushes such as `node.status`,
   `core.update.status`, `supervisor.update.status.raw`, and `webio.stream.*`.
5. **Frontend** - the Angular/Ionic runtime consumes shared state through
   `YDocService` and transport-independent stream receivers through
   `WebIoStreamService`. It renders apps/widgets from the merged runtime state
   and exposes CRUD for switching or managing webspaces. For the current
   desktop/subnet scope, node-owned skill apps stay visible in shared desktop
   catalogs the same way node-owned widgets do; only scenario shortcuts and
   dev-only surfaces remain filtered by policy.

```text
Browser <--y-websocket--> Hub y_gateway <--YDoc--> SQLiteYStore
            events/ws             ^               ^
              |                   |               |
    (desktop.*, webio.stream.*)   |      WebspaceScenarioRuntime + skills
              |                   |
         WebIoStreamService <-----+
```

## State vs Stream

AdaOS now treats Yjs and browser streams as different contracts:

* **Yjs** is for collaborative or reconnect-stable shared state.
  Keep here the compact state that should survive reconnect, page reload, or
  later semantic rebuild.
* **Node-owned Yjs skill state** is projected under
  `data.nodes.<node_id>.*` in the shared webspace. A member can keep using
  local skill paths such as `data/weather/current`; the runtime scopes those
  paths when the data enters the shared desktop contract.
* **`dataSource.kind = "stream"`** is for transport-independent live data.
  Use it for telemetry, active operations, rolling event tails, log fragments,
  and other high-churn arrays that do not need CRDT merge semantics.
* **Control events** on the semantic event channel stay separate again:
  they carry commands and small runtime notifications, not large client state.

Recommended rule of thumb:

* put `summary`, `latest`, selected object ids, compact diagnostics, and other
  "what is currently true" facts into Yjs
* put samples, append-heavy tails, active operation rows, live log lines, and
  rolling event feeds into stream receivers
* if a widget must show a recent bounded history instead of only live events,
  build that bounded history explicitly in the skill/runtime and publish it as
  the receiver snapshot rather than expecting Yjs to preserve the tail for free

For the target browser contract, the same rule can be expressed as:

- use `y:` refs for reconnect-stable shared facts
- use `stream:` refs for high-churn live feeds
- use `view:` refs for browser-local interaction state

Skills and scenarios should not invent a second hidden state vocabulary on top
of those three families when designing semantic browser surfaces.

## Stream Receivers

`webui.json` can now declare transport-independent browser receivers:

```json
{
  "webio": {
    "receivers": {
      "infrastate.operations.active": {
        "mode": "replace",
        "initialState": []
      },
      "chat.tail": {
        "mode": "append",
        "collectionKey": "items",
        "dedupeBy": "id",
        "maxItems": 100,
        "initialState": { "items": [] }
      }
    }
  }
}
```

Widgets bind them through `dataSource.kind = "stream"`:

```json
{
  "id": "infrastate-operations",
  "type": "ui.list",
  "dataSource": {
    "kind": "stream",
    "receiver": "infrastate.operations.active",
    "nodeId": "member-01",
    "transport": "member"
  }
}
```

In target semantic UI terms, the same binding should later be representable as
a typed source object such as:

```json
{
  "source": {
    "kind": "stream",
    "ref": "stream:infrastate.operations.active",
    "scope": "node",
    "nodeRef": "$context.nodeId"
  }
}
```

The current `dataSource.kind = "stream"` contract remains valid during
migration.
The semantic binding form above exists to make browser authoring and LLM
authoring less dependent on renderer-era widget conventions.

Current client behavior:

* `replace` means each incoming payload replaces the local receiver state
* `append` means the client appends into a local collection, with optional
  `dedupeBy` and `maxItems`
* the receiver state is local to the current browser session and is not stored
  back into Yjs
* `nodeId` scopes the receiver to one node-owned producer when the same
  receiver family may be emitted by more than one node
* receiver declarations in `webio.receivers` describe reduction and transport
  semantics only; node ownership belongs to the widget/modal `dataSource`
  after runtime projection
* `transport` controls routing preference:
  `auto` subscribes to both node-aware and hub-routed topics,
  `member` prefers member-originated delivery only,
  `hub` listens only to the hub-routed aggregate topic

## Publishing Stream Data

Skills publish browser stream data via `io.out.stream.publish`:

```python
from adaos.sdk.io import stream_publish

stream_publish(
    "infrastate.operations.active",
    operations_rows,
    _meta={"webspace_id": "desktop"},
)
```

`stream_publish` stamps the local `node_id` and `source_node_id` into `_meta`
when the caller did not provide them explicitly. The hub router resolves
targets from `_meta` and emits semantic browser events of the form
`webio.stream.<webspace_id>.<receiver>`. When `_meta.node_id` is present, the
router also emits node-qualified topics:

* `webio.stream.<webspace_id>.nodes.<node_id>.<receiver>`
* `webio.stream.nodes.<node_id>.<receiver>`

The browser runtime then reduces those events into the receiver's local state.

For node-aware member delivery, the browser and router may also use
node-qualified topics:

* `webio.stream.<webspace_id>.nodes.<node_id>.<receiver>`
* `webio.stream.nodes.<node_id>.<receiver>`

The browser runtime may subscribe to both the node-qualified and the hub-routed
topic at the same time and accept whichever path is currently available.
This is the current compatibility bridge for "P2P/member if possible, hub if
not" behavior.

When a user requests a semantic Yjs soft reload for the currently open
webspace, the browser now also performs an explicit provider resync even if
the transport still reports `connected`. This keeps the browser aligned with
the backend-owned effective branches after rebuild instead of trusting an
already-open provider session to notice the semantic reset on its own.
The browser link indicator treats an open Yjs transport as connected while the
first sync event or semantic recovery catches up, so a slow initial snapshot
does not leave the whole desktop stuck in "recovering" when the websocket path
is already alive.

Targeting rules:

* `_meta.webspace_id` targets one webspace
* `_meta.webspace_ids` fans out to several webspaces
* `_meta.route_id` resolves through `data.routing.routes[route_id]`
* if no target metadata is present, the router falls back to `desktop`

At the addressing level, those routing details should be read as transport and
delivery ownership metadata, not as a replacement for semantic ref identity.
For example, `stream:chat.live` remains the browser-facing receiver identity,
while `_meta.webspace_id`, `_meta.node_id`, and transport preferences decide
how that receiver is delivered.

`desktop` is the canonical default webspace. Legacy clients and stored browser
preferences may still ask for `default`; the browser runtime, Yjs gateway, and
node API normalize that value to `desktop` before selecting Yjs rooms, catalog
state, desktop ordering, or stream snapshot topics. This keeps stale browser
sessions from opening a second empty `default` room while the shared desktop
state lives in `desktop`.

## Snapshot-on-Subscribe

Streams are live transports, not implicit persistent state. To avoid blank
widgets on first subscribe, the browser requests an initial snapshot when it
subscribes to `webio.stream.*`. The gateway also emits the same request when it
observes the websocket subscription, so initial materialization has two
independent triggers instead of depending on one subscribe-side effect.

Receivers whose first value is loaded asynchronously should use an explicit
`initialState` envelope such as `{ "status": "init", "message": "Loading data...", "items": [] }`.
Widgets can then show a loading/data-not-delivered state until the first
snapshot arrives, rather than rendering an empty collection as if the source had
successfully reported "no rows".

The server emits `webio.stream.snapshot.requested` with:

* `receiver`
* `webspace_id`
* `node_id` when the subscription was node-qualified
* `target_node_id` and `_meta.target_node_id` when a concrete member should
  answer the request
* `transport`

For semantic browser surfaces, this means one `stream:` ref may still require:

- `shared` webspace scope
- node-aware ownership
- transport preference
- bounded snapshot semantics

Those are runtime binding properties, not reasons to make the browser manifest
invent a separate stream identity for every transport variant.

Node-targeted skill tool calls should follow the same contract. If the browser
asks the hub to execute `tools/call` with `target_node_id`, the hub must proxy
that tool invocation to the selected member instead of executing its own local
copy of the skill.
Skill handlers that participate in node-scoped desktop flows should therefore
accept optional `node_id`, `target_node_id`, and `**_` keyword arguments even
when the tool does not use them directly. This keeps proxied member calls and
local calls compatible under the same `tools/call` contract.

Skills that own a stream receiver should answer that request by publishing the
current bounded state for the receiver. This is the recommended way to provide
"recent history" for stream widgets.

Practical guidance:

* for pure live telemetry, a receiver snapshot may be just the latest sample
  or a small rolling ring buffer
* for logs or event history, keep an explicit bounded buffer in memory or on
  disk and publish that buffer on subscribe
* do not rely on previous Yjs projection contents as an accidental history
  mechanism
* expensive snapshot producers should coalesce concurrent subscribe bursts per
  webspace/receiver family; one browser opening several widgets must not
  trigger the same heavy snapshot rebuild many times in parallel
* when the browser releases a stream receiver, the server emits
  `webio.stream.subscription.changed` with `action = "unsubscribed"`; skills
  should drop active receiver bookkeeping and cached stream fingerprints for
  that receiver

## Member Skills and Delivery

Member skills do not need to manage delivery transport themselves, but they do
need to scope stream output correctly.

Current path for member-originated stream events:

```text
member skill
  -> io.out.stream.publish
  -> member local bus
  -> subnet member link
  -> hub local bus
  -> RouterService
  -> webio.stream.<webspace>.nodes.<node>.<receiver>
     or webio.stream.<webspace>.<receiver>
  -> browser event channel (ws / webrtc events / member-direct when admitted)
```

Important implications:

* transport is handled by the runtime, member link, and router
* the skill is still responsible for `_meta.webspace_id` or `_meta.route_id`
* if a tool/subscription already runs under `io_meta`, `io.out.*` helpers
  inherit `_meta` automatically
* background tasks and observers should pass `_meta` explicitly unless they are
  sure the current execution context already carries it
* member nodes now also keep a lightweight self-projected desktop catalog in
  their local snapshot; the hub aggregates that snapshot into the shared
  desktop/runtime contract instead of inventing member-owned apps/widgets on
  its own
* the hub desktop runtime depends on the prepared slot package carrying the
  same projection helpers as the slot repo (`runtime_refresh`,
  `node_display`, `node_runtime_state`, and `webspace_runtime`); these files
  are treated as bootstrap-critical so a core update validates them before the
  active slot starts projecting subnet state
* prepared slots also run an import preflight against the installed venv
  package without a `PYTHONPATH` overlay; this catches the class of failures
  where `repo/src` contains a new helper but direct CLI/runtime imports still
  resolve an older package from `site-packages`
* member catalog entries are merged into shared desktop catalogs under a
  node-scoped id (`node:<node_id>:<local_id>`). Skill and scenario ids only
  need to be unique inside one node; the shared desktop must not dedupe
  different nodes just because their local ids match.
* node-owned modal ids are scoped the same way, so two nodes can both expose a
  local `weather_modal` without sharing modal state or action context
* node-owned actions carry `node_id`, `target_node_id`, and
  `_meta.target_node_id`; the hub forwards such events only to the matching
  member, while skill subscription wrappers ignore events targeted to another
  node
* semantic reload/reset events are now mirrored to members so they can refresh
  their own subnet snapshot contribution with local throttling, instead of
  waiting for the hub to pull every detail synchronously

Recommended skill-authoring rules:

* use Yjs projections when the value should be shared durable state
* use `stream_publish` when the value is high-churn or append-heavy
* always define the receiver's initial subscribe snapshot if the widget should
  not appear empty on first open
* keep stream payloads bounded; publish fragments, not unbounded logs
* if the UI needs both "latest summary" and "recent tail", put the summary in
  Yjs and the tail in a stream receiver

## Current Desktop Ownership Rules

The current `web_desktop` client now treats ownership explicitly even before
the full subnet transport roadmap is complete.

Current browser-facing rules:

* hub and member nodes may both contribute browser `apps` and `widgets`
* member `widgets` are allowed in the shared desktop/widget space
* member `apps` are shown in desktop catalogs, but dev-only and
  scenario/skill-authoring surfaces remain hub-only for now
* widget and catalog cards may display the producing node label/name
* install actions may carry `node_id` so marketplace/runtime install flows can
  target one concrete node
* desktop drag ordering is now shared per webspace through
  `data.desktop.iconOrder` and `data.desktop.widgetOrder`
* desktop app and widget cards now expose explicit drag handles instead of
  making the whole card a drag target, so text inside the card remains
  selectable
* modal titles, widget headers, and catalog badges now prefer node labels
  (first node name) over generic `hub` / `member` labels
* if a node has no explicit human-friendly name, the hub assigns and persists a
  stable display index so the UI can fall back to `Node N` / `Nn`
* the shared browser contract may now also carry `node_compact_label`,
  `node_index`, and `node_color` for lightweight node differentiation without
  exposing raw `node_id` in normal UI chrome
* webspaces themselves remain shared Yjs documents; node binding currently
  belongs to node-owned apps/widgets/streams and to the selected
  `home_scenario_ref`, not to the webspace container itself
* node-owned skill state in the shared Yjs document lives under
  `data.nodes.<node_id>.*`; shared runtime branches such as `data.catalog`,
  `data.desktop`, `data.webio`, and `registry` stay node-agnostic

This means the UI is already node-aware at the catalog/widget surface even
though browser-to-member transport can still fall back through the hub.

## Webspaces in Detail

* **Creation and registry** - each webspace is tracked in `y_workspaces`
  (SQLite). We store `workspace_id`, on-disk path, timestamps, and a display
  name. Bootstrap scripts install the default `web_desktop` scenario and
  `weather_skill` into the default `desktop` webspace.
* **Seeding** - `ensure_webspace_seeded_from_scenario` runs for every Yjs room
  creation and for webspace CRUD actions. It keeps migration-era
  node-scoped compatibility caches available
  (`ui.scenarios.<node_id>.<id>`, `data.scenarios.<node_id>.<id>.catalog`,
  `registry.scenarios.<node_id>.<id>`) and seeds `ui.current_scenario` if
  needed, but normal scenario switch no longer depends on copying the full
  scenario payload into those branches.
* **Syncing across docs** - the core runtime updates `data.webspaces` in each
  YDoc when a webspace is created, renamed, or deleted. Each entry carries
  `{ id, title, created_at, node_id, node_label, node_compact_label,
  node_index, node_color }` plus current `home_scenario`, optional
  node-bound `home_scenario_ref`, and validation metadata so any client can
  render the available desktops without inventing ownership labels locally.
* **Switching webspaces** - the frontend remembers a preferred webspace in
  `localStorage`. Switching issues `desktop.webspace.use`, which re-seeds the
  target doc if needed, updates `/ws` routing metadata, and reconnects the
  browser to `/yws/<id>`.

## Desktop Scenario Flow

1. Scenario install calls `ScenarioManager.install_with_deps()` which:
   - installs dependent skills and activates them in the default webspace via
     `SkillManager.activate_for_space`
   - projects scenario declaratives into migration-era compatibility caches in
     the YDoc (`sync_to_yjs`)
   - emits `scenarios.synced {scenario_id, webspace_id}`
2. `WebspaceScenarioRuntime` merges:
   - scenario catalog/registry entries, marked as `source: "scenario:<id>"`
   - active skill contributions from `webui.json`, marked as
     `source: "skill:<name>"`
3. The resulting catalog is written to `data.catalog.{apps,widgets}` plus the
   filtered `data.installed`. The Angular client reads those arrays directly
   and renders icons/widgets.
4. End users toggle items via catalog modals; the UI updates
   `data.desktop.installed` optimistically and sends `desktop.toggleInstall`
   so the hub reconciles `data.installed` for everyone.

Scenario switching itself is now pointer-first by default: the hub validates
the target scenario, updates `ui.current_scenario`, schedules semantic
rebuild, and returns without eagerly copying the full payload into
`ui.application` or other effective runtime branches. Legacy branches such as
`ui.scenarios.*`, `registry.scenarios.*`, and `data.scenarios.*` remain
compatibility caches and fallback inputs during migration, not the primary
switch-time output.

## Webspace CRUD (core hooks)

The webspace lifecycle is handled by the core runtime
(`WebspaceScenarioRuntime`), not by any one skill:

| Event | Action |
|-------|--------|
| `desktop.webspace.create` | Normalize/allocate ID, insert into registry, seed from scenario, rebuild UI, sync listing. |
| `desktop.webspace.rename` | Update display name, sync listing. |
| `desktop.webspace.delete` | Remove registry entry and YStore file (except the default), sync listing. |
| `desktop.webspace.refresh` | Force re-sync of the `data.webspaces` listing across all docs. |

These events always carry `_meta.webspace_id`, so downstream helpers know the
origin but can still operate on other targets.

## Weather Skill and Webspace Routing

The weather observer publishes `weather.city_changed {webspace_id, city}`
whenever `data.weather.current.city` changes. The runtime `weather_skill`
listens for the same event type and resolves the destination webspace from
either `payload.webspace_id` or `payload._meta.webspace_id`. This ensures:

* multiple browsers in different webspaces can change the city independently
* browser-visible writes go through governed projection helpers such as
  `ctx_subnet` / `ProjectionService`, so only declared slots in the intended
  webspace are updated
* when `target_node_id` is present, only the matching node applies the change

`RouterService` follows the same pattern: when emitting runtime events
destined for UI-integrated skills, include a `_meta.webspace_id` hint so
runtimes can reply into the proper doc.

For chat/TTS in webspaces prefer the dedicated "web IO" topics:

* `io.out.chat.append` -> append into `data.voice_chat.messages`
* `io.out.say` -> enqueue into `data.tts.queue`
* `io.out.media.route` -> write the normalized media route contract into
  `data.media.route`

These events are routed purely by `_meta.webspace_id`, so different
devices/webspaces can receive replies independently. `data.media.route` stays
a plain JSON subtree so browser widgets can observe one router-owned view of
need/capability/ability/attempt/degradation/observed-failure state without
depending on a specific transport adapter.

For node-owned skills, the effective shared-desktop branches are now:

* `data/nodes/<node_id>/voice_chat`
* `data/nodes/<node_id>/tts`
* `data/nodes/<node_id>/media`

The browser/runtime may still declare local skill defaults under
`data/voice_chat`, `data/tts`, or `data/media`, but once a widget/modal is
opened for a concrete node the effective contract must stay node-scoped.
For `voice.chat.*` the runtime should also preserve `target_node_id`
end-to-end so a member-targeted browser session cannot leak requests into the
hub or another member's chat flow.

For browser observability, `voice.chat.user` command acknowledgement must be
treated only as "the runtime accepted the command", not as "the chat history is
already materialized in the browser". Member-owned chat flows add one more hop
(`member local YDoc -> hub/shared desktop -> browser sync`), so the browser now
falls back to `voice_chat_skill.get_snapshot` whenever node-scoped chat history
is remote or the current Yjs runtime reports a recent semantic recovery
failure (`first_sync_timeout`, failed resync, and similar states).

Operational node-scoped snapshots such as `infrastate` and `subnet_env`
should stay responsive on members. Member-side snapshot generation must not
block on hub-only marketplace catalog enrichment such as remote registry URL
fetches or git-ref registry probes; those enrichments belong on the hub path,
while member snapshots should prefer local state and local workspace scans.

## Frontend Experience

* Toolbar displays the current webspace and offers CRUD buttons
  (create/rename/delete/refresh). Selecting a different entry sends
  `desktop.webspace.use` and reloads the UI after the hub acknowledges.
* The merged catalog already exposes DEV badges, so no additional filtering
  happens on the client.
* The weather modal sends a small city-change command; the skill projects the
  resulting compact weather state asynchronously.

## Materialization and Readiness

The Angular client treats merged runtime branches as the primary render
contract:

* `ui/application/desktop/pageSchema`
* `ui/application/modals/*`
* `data.catalog.*`
* `data.installed`
* `data.desktop`

For the current desktop/subnet scope, `data.desktop` now also carries shared
desktop ordering through:

* `iconOrder` for desktop app/icon order
* `widgetOrder` for desktop widget order
* `hiddenSections` for persistent per-webspace section visibility

For the current desktop/subnet migration scope, the browser should treat those
effective branches as the render contract and should not depend on
`ui.scenarios.*` fallback reads for desktop schema or modal definitions.
Scenario compatibility caches remain backend/runtime data only.

For workspace/home selection in the same migration scope, the browser may
consume a node-aware `home_scenario_ref` from `data.webspaces` and
`/api/node/yjs/webspaces`, while the webspace document itself remains shared
and node-agnostic.

The control API (`/api/node/yjs/webspaces/<id>`) and `YDocService` expose a
shared materialization diagnostic contract:

* `ready`
* `readiness_state`
* `missing_branches`
* `compatibility_caches`

For lighter operator polling, the same state is also split across:

* `/api/node/yjs/webspaces/<id>/rebuild`
* `/api/node/yjs/webspaces/<id>/materialization`

Both lightweight endpoints keep the expensive `runtime` snapshot optional via
`?include_runtime=1`. Normal benchmark polling should stay on the default
lightweight shape without `runtime`.

`rebuild` now also carries the last-known `materialization` snapshot when one
is available. Semantic rebuild refreshes that snapshot across its
`structure`/`interactive` phases, so hot switch loops can often observe
readiness progression from the rebuild surface alone. The dedicated
`materialization` endpoint may also return that cached snapshot while a rebuild
is still pending, or briefly after a fresh ready transition, instead of
reopening the YDoc immediately.

When those endpoints do need to inspect the document directly, they now prefer
read-only YDoc sessions so diagnostics/catalog reads avoid unnecessary flush
and room rebroadcast work on exit.

Rebuild/control surfaces also expose phase-oriented timing and apply detail so
pointer-only scenario switch can be evaluated against real runtime slices:

* `phase_timings_ms.time_to_first_structure`
* `phase_timings_ms.time_to_interactive_focus`
* `phase_timings_ms.time_to_full_hydration`
* `apply_summary.phases.structure`
* `apply_summary.phases.interactive`
* `apply_summary.fingerprint_unchanged_branches`

For repeated operator measurements, use:

* `adaos node yjs benchmark-scenario --webspace <id> --scenario-id <target> --baseline-scenario <baseline>`
* `adaos node yjs benchmark-scenario --webspace <id> --scenario-id <target> --detail`
* `adaos node yjs materialization --webspace <id>`

The command runs measured target switches, restores the baseline scenario
between iterations, can wait for terminal background rebuild completion, and
prints aggregated phase timing stats so heavy scenarios such as `infrascope`
can be compared across optimization slices. The benchmark path now polls the
lightweight `rebuild` and `materialization` control surfaces instead of
re-fetching the full webspace snapshot on every readiness loop, which keeps
operator measurements usable even when the runtime is under pressure. `--detail`
additionally prints switch/rebuild/semantic timing breakdowns plus observed
materialization milestones (`time_to_first_paint`, `time_to_interactive`,
`time_to_ready`) for each run and in the aggregate. It now also derives a
server-side ready estimate from `phase_timings_ms.time_to_full_hydration`
(falling back to rebuild/materialization timestamps when that phase timing is
missing) and reports the remaining observation gap as
`summary.ready_observation_lag`. It now also prints `ydoc_timings_ms`, which
separates YDoc/YStore session overhead from the inner semantic rebuild
timings.

Normal read-write YDoc sessions now persist the incremental diff they just
produced and reuse that same diff for room rebroadcast, instead of re-encoding
the full document state on every flush. Recent benchmark runs should therefore
show `ydoc_timings_ms.ystore_write_update` on the writeback side, while the
remaining dominant costs move toward `ystore_apply_updates` and other
replay/open-path timings.

Attached live-room flows now also avoid reopening a throwaway YDoc when they
do not need to: read-only control/status reads and semantic rebuild hot paths
prefer the already-attached `room.ydoc` when they are running on the room
owner loop. In benchmark output this should collapse
`ydoc_timings_ms.ystore_apply_updates` toward zero for hot attached runs, so
the remaining YStore work becomes easier to isolate as a cold-room
reconnect/reload problem rather than a steady-state switch problem. When that
happens, a large `ready` value in benchmark output is now more likely to be a
control-path observation lag than a true semantic rebuild cost.

YStore runtime diagnostics now also expose extra pressure indicators such as:

* `update_log_bytes`
* `replay_window_bytes`
* `replay_window_byte_limit`
* `base_snapshot_present`
* `last_compact_reason`
* `auto_backup_total`
* `last_auto_backup_reason`
* `diff_write_total`
* `snapshot_write_total`
* `apply_total`
* `applied_update_total`
* `applied_update_bytes`

Those counters help distinguish "slow because we keep replaying a large log"
from "slow because we are still writing whole snapshots too often".

The replay window is now bounded both by entry count and by bytes. The
operator knob `ADAOS_YSTORE_MAX_REPLAY_BYTES` sets the byte budget for the
bounded replay tail, and compaction reason reporting makes it explicit whether
the store compacted because of the update-count cap or because replay bytes
grew too large.

When replay pressure does trigger compaction, YStore can now also schedule a
debounced auto-backup to disk and attempt to collapse the in-memory log to a
single base snapshot. This is intended to shorten later cold-room replay
paths, not to change the semantic source of truth. The related knobs are:

* `ADAOS_YSTORE_AUTOBACKUP_AFTER_COMPACT`
* `ADAOS_YSTORE_AUTOBACKUP_COOLDOWN_SEC`
* `ADAOS_YSTORE_AUTOBACKUP_DEBOUNCE_SEC`

Operator-triggered room reset/reload now also uses that same storage path more
deliberately: after a live YRoom is dropped, the runtime releases the old room
references aggressively and may request an idle YStore compaction pass so the
replay tail is collapsed in the background instead of staying attached to the
next cold open.

When `rebuild.materialization` is present, benchmark polling now reuses that
embedded snapshot before falling back to the separate `materialization`
endpoint, which further reduces polling pressure on the hub during repeated
scenario switch measurements.

Pointer-first scenario switch now also prefers a live-room fast path when the
target webspace is already attached in memory: `ui.current_scenario` is
updated on the active room first, then persisted stale-safely in the
background. This reduces hot-path store reopen cost and improves time to first
visible switch reaction for attached clients.

If the requested local control endpoint becomes stale during supervisor
prewarm/candidate activity, the benchmark CLI can now consult supervisor
public status and retry against the currently active local runtime URL before
failing the measurement.

`readiness_state` follows a coarse ladder:

* `pending_structure`
* `first_paint`
* `interactive`
* `hydrating`
* `ready`
* `degraded`

The important distinction is that `hydrating` is an expected staged state,
while `degraded` means the runtime could not account for required desktop
branches and the client should surface a stronger recovery hint.

`compatibility_caches` exposes the migration-era legacy cache surface for
operators:

* `client_fallback_readable` reports whether migration-era scenario caches are
  still present for diagnostics, even though the current desktop client no
  longer reads them for schema/modal fallback
* `present_count` / `required_count` and `missing_branches` show how much of
  the active scenario's legacy cache surface still exists
* `switch_writes_enabled` is retained for diagnostics compatibility and now
  always reports `false`; switch-time compatibility cache materialization has
  been removed
* `legacy_fallback_active` reports whether semantic rebuild had to read legacy
  scenario branches instead of canonical loader-backed content
* `runtime_removal_ready` becomes `true` only when effective runtime branches
  are ready and resolver fallback is no longer using legacy caches

## `webui.json` schema

AdaOS includes a JSON Schema for `webui.json` so that:

* `adaos ... skill validate` can catch structural mistakes early
* IDEs and LLM programmers can follow a stable contract when generating UI
  declaratives

Schema file: `src/adaos/abi/webui.v1.schema.json`

Recommended to add this to the top of every `webui.json`:

```json
{
  "$schema": "../../../src/adaos/abi/webui.v1.schema.json"
}
```

`webui.v1` now enumerates the client-supported widget renderer types. The
first reusable stream/file widgets are:

* `visual.frameViewer` - renders a stream-backed image/frame payload with
  optional status badges and metric chips
* `visual.image` - image-only alias for the frame viewer renderer
* `visual.timeseriesChart` - MVP line chart over a stream/Yjs point array,
  configured with `inputs.xKey` and `inputs.yKey`
* `input.fileUpload` - uploads a browser-selected file to the core
  skill-owned file store and dispatches `uploaded` actions with `artifact_ref`

The schema also supports coarse staged-readiness hints on page/widget/modal
and catalog surfaces via a `load` object:

```json
{
  "widgets": [
    {
      "id": "chat_widget",
      "type": "ui.chat",
      "load": {
        "structure": "visible",
        "data": "deferred",
        "focus": "off_focus",
        "offFocusReadyState": "hydrating"
      }
    }
  ]
}
```

These hints are intent-level only. They tell the backend/renderer which
surfaces are first-paint critical versus off-focus or deferred, but they do
not encode a low-level scheduler. Keep them at page/widget/modal/catalog
granularity rather than annotating every leaf control.

### Overlay presentation and focus lifecycle

Schema-driven modals are runtime-owned overlays. A `webui.json` manifest
declares the modal/action intent; the browser runtime owns DOM mechanics such
as Ionic overlay creation, background hiding, focus release, and focus restore.
This keeps widget actions data-driven and prevents skill manifests from
encoding browser-specific accessibility workarounds.

By default, opening a modal captures the currently focused element, releases
background focus before the renderer hides the desktop surface, and restores
the origin focus after dismissal when that element is still connected and
visible. This prevents the background `ion-router-outlet` from being marked
`aria-hidden` while one of its descendants still owns focus.

Modal definitions can override the default presentation policy:

```json
{
  "registry": {
    "modals": {
      "nlu_teacher_modal": {
        "title": "NLU Teacher",
        "presentation": {
          "kind": "modal",
          "focus": {
            "captureOrigin": true,
            "releaseBackground": true,
            "restoreOrigin": true
          }
        }
      }
    }
  }
}
```

Per-action overrides use the same policy through `params.presentation`,
`params.focus`, or the `restoreFocus` shortcut:

```json
{
  "on": "select",
  "type": "openModal",
  "params": {
    "modalId": "nlu_teacher_modal",
    "restoreFocus": true
  }
}
```

Most manifests should omit this block and rely on the runtime defaults. Use
explicit focus policy only for unusual overlay flows, such as nested overlays
or flows that intentionally do not return focus to the opener.

## Simplifications and Limitations

* Switching webspaces still reloads the page to keep the YDoc tree simple.
  Future iterations may let `YDocService` swap docs without a full refresh.
* Deleting a webspace does not forcibly disconnect existing clients yet; they
  reconnect to a fresh doc if they issue commands afterwards.
* Event/WebSocket APIs are still dev-oriented (no auth scopes, single hub).
  Production deployments should harden these surfaces.

Despite the shortcuts, this architecture already models how declarative
scenarios, runtime skills, and the browser cooperate via Yjs and streams.
Extending it to additional scenarios or skills requires declaring the right
projection or stream contracts, publishing events with `webspace_id` metadata,
and letting governed runtime helpers update the browser-visible state.
