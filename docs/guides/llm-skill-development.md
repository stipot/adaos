# LLM-Safe Skill Development Guide

Status: current guidance and target contract.

This guide is written for humans and LLM agents that create or update AdaOS
skills. Its goal is simple: a generated skill should be useful without being
able to overload the shared desktop, hide failures, or bypass runtime
governance.

Read this together with:

- the repository note `docs/io/webio.md`
- [UI Addressing](../architecture/ui-addressing.md)
- [Named Entities and Canonical Naming](../architecture/named-entities.md)
- [Semantic State Plane](../architecture/semantic-state-plane.md)
- [Runtime Guarding](../architecture/runtime-guarding.md)
- [Projection Subscription Roadmap](../architecture/projection-subscription-roadmap.md)

## Golden rule

Do not treat the primary Yjs document as a free-form database.

Normal skill-owned browser-visible state must go through governed SDK helpers
and declared projection routes. Direct Yjs mutation from a skill is legacy or
explicitly capability-gated, not the default authoring model.

Preferred data-plane choices:

- `data_projections` plus `ctx_subnet.set()` / `ctx_subnet.set_async()` for
  compact reconnect-stable bootstrap/control state.
- `stream_variable_publish()`, `stream_publish()`, and `webio.receivers` for
  high-churn live variables, append-heavy data, and operator-facing variables.
- skill-local storage for private durable skill state.
- tool/detail endpoints for explicit user-requested details.
- 360log or disk snapshots for later diagnostics, not browser steady-state
  rendering.

## Responsibility model

The skill author chooses the data route. The runtime does not silently move a
skill's data between Yjs and streams.

That choice is part of the skill design and must be visible in `skill.yaml`,
`webui.json`, handler code, and tests. For LLM-authored work, the route choice
must be treated as a reviewable implementation decision, not an accidental
side effect of the helper API used.

Runtime guardrails still enforce shared safety:

- Yjs owner guards count attempted and applied Yjs writes, attribute pressure to
  the skill owner, and may warn, throttle, block, or quarantine unsafe owners.
- Stream guards bound payload size, publish rate, snapshot request bursts, and
  receiver fanout, and must log suppressions or degraded delivery.
- Guards emit diagnostics and quarantine records so the UI and future LLM repair
  loops can explain the failure.
- Guards are emergency control, not a replacement for a well-designed data
  route.

The desired failure mode is explicit: a badly routed skill should become
visible as a design defect and be returned for repair. It should not be hidden
by runtime magic that makes the browser appear healthy while the skill keeps
producing unsafe data.

## Required data route plan

Before editing a browser-facing skill, write down the route plan. A concise
comment in the implementation notes, PR description, or adjacent docs is
enough, but the design must be explicit.

For every widget, modal section, status row, and detail view, answer:

- `surface`: what browser surface consumes this data?
- `route`: `yjs`, `stream`, `tool/details`, `skill-local`, or `disk/360log`.
- `why`: reconnect-stable bootstrap, live variable, explicit drill-down,
  private durable cache, or diagnostic evidence.
- `first_paint`: what does the user see before live data arrives?
- `recovery`: how does the surface recover after room rebuild, reconnect, or
  stream resubscribe?
- `update_source`: which events or commands can update it?
- `budget`: expected payload size, event rate, coalescing window, and maximum
  fanout.
- `guard_visibility`: what warning, degraded state, or incident is shown when
  the route is throttled, blocked, or quarantined?

If a route cannot answer these questions, do not add it yet.

## Data-plane decision table

| Need | Use | Avoid |
| --- | --- | --- |
| Bootstrap/control state needed for first paint | Yjs projection | Full operational snapshot in Yjs |
| Selected ids, compact health badge, latest stable status | Yjs projection | Rewriting `data`, `ui`, or `registry` broadly |
| Operator-facing variables, active operations, logs, telemetry, chat/event tail | Stream receiver | Unbounded arrays in Yjs |
| Big diagnostics or object inspector payload | Details tool / stream snapshot / disk snapshot | Embedding full diagnostics in primary Yjs |
| Small operator health/guard summary | Status card pointing to stream/tool/details route | Treating `statusPlane` as a live data route |
| Durable private skill cache | Skill-local files or DB | Hidden browser-only state as source of truth |
| Command from UI to runtime | `callHost` / tool with small ack | Large command response used as data transport |
| Raw high-frequency evidence | Stream or disk/360log | Smoothed Yjs status that loses diagnostic truth |
| Smoothed operator status | Debounced stream or compact Yjs badge | Flickering every raw transport event |

## Skill manifest checklist

Every browser-facing skill should make its data contract explicit.

Use `skill.yaml` to declare:

- `data_routes` for the reviewable route plan: surface, route, first paint,
  recovery, budget, and guard visibility.
- `tools` with stable input and output schema.
- `exports.tools` for callable public tools.
- `events.subscribe` for every `@subscribe(...)` handler, including
  `webio.stream.snapshot.requested` and
  `webio.stream.subscription.changed` when the skill serves stream receivers.
- `data_projections` only for browser-visible Yjs branches the skill owns.
- `webui.receivers` in `webui.json` for live stream variables.
- optional lifecycle hooks such as `healthcheck`, `drain`, `dispose`, and
  `onQuarantine` / `on_quarantine` when the skill can clean up or explain a
  guard action.

Every declared Yjs projection should have a reason to be reconnect-stable.
Every stream receiver should have bounded delivery semantics and an initial or
snapshot-on-subscribe story.

Example:

```yaml
data_routes:
- surface: widget:weather_status
  route: yjs
  projection_slot: weather.snapshot
  first_paint: cached compact weather status
  recovery: Yjs replay restores the latest compact status
  update_source: [weather.refresh.completed]
  budget:
    max_payload_bytes: 4096
    max_publish_hz: 0.2
    snapshot_policy: on_subscribe
  guard_visibility:
    degraded_state: weather status shows stale/degraded
    log: service.weather_skill.runtime.log
    quarantine: true
- surface: modal:weather_history
  route: stream
  receiver: weather.history
  first_paint: empty history with loading state
  recovery: bounded stream snapshot requested on subscribe

data_projections:
- scope: subnet
  slot: weather.snapshot
  targets:
  - backend: yjs
    path: data/weather

tools:
- name: get_snapshot
  description: Return the compact current weather state.
  entry: handlers.main:get_snapshot
  input_schema:
    type: object
    properties:
      webspace_id:
        type: string
      target_node_id:
        type: string
  output_schema:
    type: object
    required: [ok]
    properties:
      ok:
        type: boolean
      current:
        type: object
```

## Browser-visible Yjs writes

Use logical slots, not raw paths, in handler code.

Yjs is for the minimum reconnect-stable state needed to bootstrap the surface,
preserve collaborative/control state, and explain health. It is not the normal
transport for changing variables, diagnostic tables, event tails, or raw
runtime evidence.

Preferred:

```python
from adaos.sdk.data import ctx_subnet

ctx_subnet.set(
    "weather.snapshot",
    {"current": current},
    webspace_id=webspace_id,
)
```

For async handlers:

```python
await ctx_subnet.set_async(
    "adaos_connect.current",
    current,
    webspace_id=webspace_id,
)
```

Avoid in normal skills:

- `webspace_ydoc`
- `get_ydoc()`
- `async_get_ydoc()`
- direct `y_py` transactions
- replacing broad roots such as `data`, `ui`, `registry`,
  `data.catalog`, `data.installed`, or `data.desktop`
- writing hot telemetry, logs, session churn, transport events, or stream tails
  into Yjs because a widget needs to see them

If a legacy skill still needs direct Yjs access, document why and keep it on a
short migration path toward `ProjectionService` / `ctx_subnet`.

Make hot projection writes idempotent before calling the SDK helper. Runtime
projection code can skip physical no-op mutations, but guard/governance checks
still see the attempted write. For refresh-heavy skills, keep a small
per-`(webspace_id, slot)` fingerprint and do not call `ctx_subnet.set*()` when
the semantic payload has not changed. Keep an explicit recovery path, such as a
user/API `refresh_snapshot`, that can bypass this fingerprint when the browser
reports a missing projection after room rebuild or reconnect.

Do not fan out routine projection refreshes to every webspace by default.
Target the webspace from event metadata or the UI action. Reserve all-webspace
fanout for boot, activation, migration, or explicit resync events.

Yjs payloads should be small enough to inspect in logs and reason about in code
review. If a projection is hard to summarize in one short schema paragraph, it
is probably too large for Yjs and should be split into stream variables or
details.

## Stream data

Use streams for data that changes often, grows by appending, or represents
operator-facing variables that should not be durable collaborative state.

Streams are not a free replacement for Yjs. They are active volatile delivery:
messages can be missed during reconnect, subscriptions can flap, and duplicate
or out-of-order payloads can happen around recovery. Design every stream as a
bounded replace or append channel with explicit recovery.

Declare receivers in `webui.json`:

```json
{
  "webio": {
    "receivers": {
      "voice_chat.messages": {
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

Publish from the skill:

```python
from adaos.sdk.io import stream_publish, stream_variable_publish

stream_variable_publish(
    "voice_chat.status",
    {"state": "ready", "peer_count": 1},
    var_id="status",
    ttl_ms=30000,
    _meta={"webspace_id": webspace_id, "owner": "skill:voice_chat_skill"},
)

stream_publish(
    "voice_chat.messages",
    {"items": [message]},
    _meta={
        "webspace_id": webspace_id,
        "target_node_id": target_node_id,
        "owner": "skill:voice_chat_skill",
    },
)
```

Stream rules:

- keep payloads bounded
- keep owner attribution explicit when using low-level publish helpers; the
  higher-level `StreamRuntime` adds `owner=skill:<skill_id>` automatically, but
  direct `stream_publish()` calls should still carry `owner` in `_meta`
- dedupe events with stable ids
- provide snapshot-on-subscribe for widgets that should not open empty
- coalesce repeated snapshot requests per receiver/webspace/node
- include `updated_at`, `seq`, stable ids, or a content fingerprint when the
  receiver needs to reject stale or duplicate payloads
- prefer `stream_variable_publish()` for replace-mode current-state variables;
  it wraps `id`, `value`, `seq`, `updated_at`, `fingerprint`, and optional
  `ttl_ms` consistently
- use `mode: "replace"` for current-state variables and include a complete
  bounded current value in each snapshot
- use `mode: "append"` only for true tails, with `maxItems`, `dedupeBy`, and
  a clear truncation policy
- provide an honest `initialState` such as `loading`, `stale`, `degraded`, or
  an empty bounded collection
- do not eager-publish a replace stream for the same state that the widget is
  already reading from Yjs; use streams for separate high-churn state or
  snapshot-on-subscribe recovery
- do not copy stream tails back into Yjs just to make them visible

Stream variables should be demand-aware. A stream receiver that is not
subscribed should not keep rebuilding full snapshots just in case a browser
opens later. Prefer receiver-specific builders over one monolithic skill
snapshot.

## Status cards

Use status cards for small operator summaries that must be cheap to poll,
stream, or project. A card is not a detail payload. It carries identity,
current state, freshness, and a pointer to the details route.

`statusPlane` is not a third data route. It is a compact index over the routes
you already declared in `data_routes`, `data_projections`, and
`webui.receivers`. If a card needs rows, inventories, logs, diagnostics, or a
tail, put those values in a stream receiver or details tool and put only the
reference in the card.

```python
from adaos.sdk.status import publish_status, publish_status_stream

publish_status(
    id="runtime",
    kind="runtime",
    scope="infrastate",
    status="ready",
    summary="runtime ready",
    ttl_ms=30000,
    details_ref={"kind": "stream", "receiver": "infrastate.runtime"},
    route={"kind": "stream", "receiver": "infrastate.runtime"},
    webspace_id=webspace_id,
)

publish_status_stream(
    "infrastate.runtime",
    id="runtime",
    kind="runtime",
    scope="infrastate",
    status="warning",
    summary="route reconnecting",
    ttl_ms=30000,
    webspace_id=webspace_id,
    _meta={"webspace_id": webspace_id},
)
```

Status card rules:

- use `status` values that normalize through `CanonicalStatus`: `ready`,
  `online`, `warning`, `degraded`, `down`, `offline`, or `unknown`
- keep `summary` short and operator-facing
- include `ttl_ms` for live runtime cards so stale UI can degrade honestly
- use `incident_id` only when the card represents a real active warning or
  incident
- put stream/tool references in `details_ref`; do not embed logs, tables,
  inventories, or tails into the card
- never declare `route: status` or `route: statusPlane`; the route belongs to
  Yjs, stream, details/tool, skill-local storage, or diagnostic evidence
- put the design-time data route in `route` so guard diagnostics can map
  pressure back to the skill route plan
- use `publish_status_stream()` when the card itself should also be available
  as a replace-mode stream variable
- when publishing a batch of cards from a refresh, build them from the compact
  snapshot that was already computed and put only counters plus
  `details_ref`/`route` receiver pointers in the card; do not rebuild
  inspectors or inventories just to make cards
- verify cards through `GET /api/node/status/cards`; the compatibility
  `/api/node/reliability/summary` surface also carries a compact `statusPlane`
  block for badge/status UI during migration
- polling clients should prefer
  `GET /api/node/reliability/summary?mode=thin&webspace_id=<id>` and send
  `If-None-Match` on the next request; unchanged snapshots return `304`
  without rebuilding the full reliability payload
- use `GET /api/node/reliability/summary/metrics` during soak/debug runs to
  verify thin/full mode counts, response bytes, `304` reuse, and the compact
  `acceptance` block with status-registry, Yjs owner-guard/quarantine, stream
  guard, stream-control, and per-receiver pressure counters
- for a human-readable check, use
  `adaos node reliability-metrics --webspace <id> --receiver <stream>` and
  include the `acceptance.*` lines in soak notes
- verify `statusPlane.diagnostics.oversizedCardTotal == 0`; a nonzero value
  means a status card is being used as a payload container and needs a route
  redesign
- Yjs pressure, stream guard, and stream-control pressure are also projected as
  compact guard cards in `statusPlane`; use their `guardRef` to map observed
  pressure back to owner, route, receiver/path, budget, and quarantine context

## Hot events and smoothing

Some events are useful evidence but terrible UI clocks. Examples include:

- `browser.session.changed`
- `device.registered`
- `webrtc.peer.state.changed`
- YWS open, close, guard, quarantine, and reconnect events
- network route flaps
- fast operation progress ticks

Handle these as two different products:

- Raw evidence goes to diagnostics streams, bounded logs, or 360log so the
  operator and LLM repair loop can see what really happened.
- Operator-facing state is smoothed through debounce, coalescing, or a small
  state machine so short transport bumps do not shake the UI.

Recommended rules:

- coalesce by the narrowest useful key, usually
  `(webspace_id, device_id, receiver)` or `(webspace_id, node_id, section)`
- set an explicit burst window, for example 10-15 seconds for browser session
  churn
- publish the latest stable state, plus counters such as `flap_count`,
  `last_raw_state`, and `last_raw_at` when useful
- keep budget diagnostics in Yjs stable: counters and last reason are fine, but
  do not put a live `updated_at` diagnostic snapshot into a projected object
  because it defeats fingerprint-based dedupe
- let hard states bypass smoothing: revoked, denied, auth required, guard
  quarantined, explicit user disconnect, or admin shutdown
- never trigger a full skill snapshot rebuild for each raw hot event
- do not write raw hot-event churn into Yjs
- apply the budget before writing `pending`, `running`, or `last_changed`
  markers into Yjs; otherwise the coalescer still turns every raw event into a
  primary-doc write
- use the shared `HotEventBudget` helper when turning hot raw events into
  status cards or stream variables; keep the raw event trail in diagnostics
  and publish only coalesced operator state
- if a suppressed event changes current-state UI, schedule a trailing refresh
  after `retry_after_ms` so the final stable state is delivered without
  rebuilding on every raw event

```python
from adaos.services.status import HotEventBudget

budget = HotEventBudget(debounce_ms=1000, window_ms=10000, max_events=5)
decision = budget.admit(
    "browser.session.changed",
    key=f"{webspace_id}:{device_id}",
)
if not decision.admitted:
    schedule_trailing_refresh(delay_ms=decision.retry_after_ms)
    return
```

This smoothing is part of the skill design. Runtime guards may limit abusive
bursts, but they should not be the main mechanism that keeps the UI calm.

## Minimal UI plus details

The primary desktop Yjs document should contain the minimum state needed to
render the surface and explain whether it is healthy.

For heavy skills, prefer this split:

- minimal bootstrap/control state in Yjs
- compact operator-facing variables and active rows in stream receivers
- details behind a `Details` action or modal
- full diagnostic evidence in disk snapshots or 360log

Stream rows are not a dumping ground for every field the skill knows. Keep row
payloads to the fields the widget renders for first interaction, avoid
duplicating the same text under multiple names, and move verbose IDs, timelines,
raw status, and diagnostics behind actions or details receivers.
Prefer widget configuration or action parameter mappings over repeated constant
fields in every row; for example, pass `$event.id` to an action instead of
copying the same identifier to both `id` and `device_id`.

Good shape:

```text
data/infrastate/state
data/infrastate/subscriptions
stream:infrastate.summary
stream:infrastate.nodes
stream:infrastate.operations.active
tool:infrastate.get_details(section="logs")
```

Bad shape:

```text
data/infrastate = <full multi-thousand-line snapshot every refresh>
```

## Tool and action responses

UI actions should return small acknowledgements.

Preferred response:

```json
{
  "ok": true,
  "accepted": true,
  "status": "refresh_scheduled",
  "trace_id": "..."
}
```

Avoid returning:

- full browser snapshots
- full log files
- full scenario materialization payloads
- data already published into Yjs or stream receivers

If the UI needs the data, publish it through the declared data plane and return
only enough metadata for the user and logs to correlate the action.

## Member-aware skills

Member skills do not own transport. The runtime, router, hub-member link, and
browser choose the best delivery path.

Skill tools and handlers should accept optional routing fields:

```python
def get_snapshot(
    webspace_id: str | None = None,
    node_id: str | None = None,
    target_node_id: str | None = None,
    _meta: dict | None = None,
    **_: object,
) -> dict:
    ...
```

Rules:

- preserve `_meta.webspace_id` and `_meta.target_node_id`
- do not infer target node from global process state if the request already
  contains explicit routing metadata
- keep node-owned Yjs state node-scoped when it enters the shared desktop
- publish member stream data with `_meta.webspace_id` and node identity

## Names, aliases, and localization

Generated skills should treat human-facing names as presentation and input
resolution data, not as routing identity.

Use canonical refs for actions and storage:

- `device:member:<node_id>`
- `device:browser:<device_id>`
- `webspace:<webspace_id>`
- `scenario:<scenario_id>`
- `skill:<skill_name>`

Do not parse or persist a localized label as the only target id.
If a skill receives a phrase such as `work browser` or `рабочий браузер`, it
should let the named-entity resolver produce the canonical ref before dispatch.

Localization rules for generated skills:

- preserve exact user-confirmed names instead of translating them
- use localized aliases as resolver input, not as storage keys
- keep language-neutral observed labels such as hostnames under `locale: "und"`
- accept `request_locale` or `preferred_locales` metadata when the runtime
  provides it
- return canonical refs plus display labels in responses when humans need to
  see what was targeted
- treat runtime alias resolution as model-training neutral: aliases should
  appear in `entity_resolution` / trace evidence, not as required Rasa or
  neural retraining inputs
- propose alias changes through `sdk.data.entities.propose_alias_add`,
  `propose_alias_remove`, or `propose_alias_deprecate` plus the matching apply
  helper instead of mutating projected registry data directly; the apply result
  returns lifecycle event envelopes that the authoritative write path can
  persist and publish
- when adding an alias for an actual browser/member device, prefer
  `sdk.data.entities.add_device_alias(device_ref, alias, locale=...)`; use
  `remove_device_alias` to stop accepting an alias, and
  `deprecate_device_alias` to keep compatibility while marking the alias as
  old vocabulary. These helpers write through the governed access-link source
  and keep Yjs as a read-only projection
- when applying an alias change from a previously read registry item, pass the
  item's `fingerprint` as `base_fingerprint`; if the result is `stale`, reread
  the registry instead of retrying blindly
- MCP clients can use `add_device_alias`, `remove_device_alias`, and
  `deprecate_device_alias` from NLUAuthoringPlane only with a write-capable
  session such as `ProfileOpsControl`; read-only sessions should use
  `get_nlu_authoring_context` and `get_named_entity_registry`

## Guarding and quarantine

The runtime may warn, throttle, block, or quarantine a skill owner when either
Yjs or stream routes apply unsafe pressure.

Generated skills must not hide that state.

Recommended behavior:

- implement `onQuarantine` or `on_quarantine` when the skill can release
  resources or record context
- accept `ttl_s`, `reason`, `metrics`, `webspace_id`, and `owner`
- write a compact skill-local incident log for later LLM repair
- return structured errors such as `skill_owner_quarantined`
- let the Web UI render disabled/quarantined state instead of silently
  pretending the action succeeded
- expose which route was guarded: `yjs`, `stream`, `tool`, or `mixed`
- include the affected slot or receiver when safe to disclose
- keep enough local context to repair the data route, not just the symptom

The runtime owns the shared quarantine projection, for example `data.yjs_qrnt`.
Skills should not write that service branch directly.

Guard responsibilities:

- Yjs guard protects the primary document from oversized, too frequent, or
  poorly attributed writes.
- Stream guard protects event delivery from oversized payloads, receiver fanout,
  snapshot request storms, and publish loops.
- Both guards should produce bounded logs and operator-visible degraded state.
- Neither guard decides the normal data route for the skill.

## Observability rules

Every skill should make failures diagnosable.

Use:

- stable error codes
- compact `trace_id` or operation id
- bounded logs
- explicit `retryable` flags
- visible `degraded` / `unavailable` states when data cannot be fetched
- disk/360log snapshot references for large evidence

Do not:

- swallow exceptions and return stale success
- fall back to another data plane without surfacing that fallback
- report a command `ack` as if browser-visible state is already delivered
- retry in tight loops
- perform expensive snapshot rebuilds for every browser poll

## LLM implementation workflow

Before coding:

- read `skill.yaml`
- read `webui.json`
- identify every browser-visible state branch
- write the data route plan for every browser-visible branch or receiver
- choose Yjs projection, stream, details tool, or skill-local storage for each
  branch, and record why
- check whether the skill is node-aware
- define size and frequency expectations
- identify hot events and define debounce/budget behavior before writing
  handlers

When coding:

- use SDK helpers instead of direct Yjs primitives
- keep tool responses small
- make updates idempotent
- fingerprint or coalesce heavy projections
- keep arrays bounded
- build stream payloads per receiver when possible, rather than rebuilding the
  whole skill snapshot
- keep raw diagnostic evidence separate from smoothed operator state
- accept routing metadata and unknown keyword args
- preserve owner attribution where helper APIs require it

Before publishing:

- verify `data_routes` exists for browser-facing Yjs, stream, details, or
  diagnostic surfaces
- verify `data_projections` exist for Yjs state
- verify every `@subscribe(...)` handler is declared in `events.subscribe` so
  packaged/runtime skill metadata stays reloadable after migration
- verify stream receivers have bounded modes and snapshot-on-subscribe behavior
- verify stream receivers have `initialState`, freshness metadata, and a
  recovery path after resubscribe
- verify load tests activate the relevant skill surface and stream receivers;
  an idle subscribed-model skill with no active receiver is not proof of low
  pressure
- verify no handler rewrites broad Yjs roots
- verify hot events have debounce/budget tests
- verify SDK projection diagnostics show the expected `by_event` pressure
  counters for dirty refresh paths before optimizing a noisy event source
- verify stream request bursts cannot rebuild every skill section by default
- verify status cards stay small and point to details instead of embedding
  detail payloads
- verify status-card compact-boundary diagnostics stay clean:
  `oversizedCardTotal == 0` and observed card bytes are comfortably below the
  card budget
- verify no action returns a large payload when a projection/stream is the
  real data path
- verify Yjs and stream guard errors are visible to the UI
- record soak evidence with active receivers visible in SDK diagnostics,
  `adaos node reliability-metrics --webspace <id> --receiver <receiver>`, and
  browser runtime-debug stream subscribe/snapshot-request records

## Anti-patterns

Treat these as defects in LLM-generated skills:

- direct skill writes to the primary Yjs document
- broad replacement of `data`, `ui`, `registry`, `data.catalog`,
  `data.installed`, or `data.desktop`
- unbounded chat/log/event arrays in Yjs
- returning a huge snapshot from `refresh_snapshot`
- polling a heavy snapshot endpoint to keep normal UI alive
- duplicating the same data in a tool response and Yjs
- duplicating the same replace-state in both eager stream publishes and Yjs
  projections on every refresh
- using HTTP/API fallback as steady-state transport for Yjs-rendered data
- hiding degraded state behind "successful" empty UI
- controlling WebRTC/Yjs channel lifecycle from business logic
- doing continuous profiling, deep JSON normalization, or full snapshot
  serialization inside hot handlers
- treating stream delivery as durable state without snapshot-on-subscribe
- letting subscription flaps rewrite Yjs on every subscribe/unsubscribe
- using runtime quarantine as the normal way to quiet a noisy skill

## Current migration priorities

The current workspace audit suggests this priority order:

1. migrate `voice_chat_skill` to declared projection/stream contracts
2. finish `browsers_skill`: its browser lists and selected-browser details now
   use snapshot-on-subscribe streams with Yjs limited to compact summary; next
   steps are hot-event status cards and churn diagnostics
3. split `infrastate_skill` into minimal summary plus details/streams
4. split `infrascope_skill` into demanded projection families
5. decide whether `mediaserver` and `prompt_engineer_skill` should remain
   tool-driven or adopt browser-facing projection contracts
