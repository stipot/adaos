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
  compact reconnect-stable shared state.
- `stream_publish()` and `webio.receivers` for high-churn or append-heavy live
  data.
- skill-local storage for private durable skill state.
- tool/detail endpoints for explicit user-requested details.
- 360log or disk snapshots for later diagnostics, not browser steady-state
  rendering.

## Data-plane decision table

| Need | Use | Avoid |
| --- | --- | --- |
| Small current fact shown by widgets | Yjs projection | Returning full snapshot from every action |
| Selected ids, UI summary, latest status | Yjs projection | Rewriting `data`, `ui`, or `registry` broadly |
| Active operations, logs, telemetry, chat/event tail | Stream receiver | Unbounded arrays in Yjs |
| Big diagnostics or object inspector payload | Details tool / stream snapshot / disk snapshot | Embedding full diagnostics in primary Yjs |
| Durable private skill cache | Skill-local files or DB | Hidden browser-only state as source of truth |
| Command from UI to runtime | `callHost` / tool with small ack | Large command response used as data transport |

## Skill manifest checklist

Every browser-facing skill should make its data contract explicit.

Use `skill.yaml` to declare:

- `tools` with stable input and output schema.
- `exports.tools` for callable public tools.
- `events.subscribe` for command or domain events.
- `data_projections` for every browser-visible Yjs branch the skill owns.
- optional lifecycle hooks such as `healthcheck`, `drain`, `dispose`, and
  `onQuarantine` / `on_quarantine` when the skill can clean up or explain a
  guard action.

Example:

```yaml
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

## Stream data

Use streams for data that changes often or grows by appending.

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
from adaos.sdk.io import stream_publish

stream_publish(
    "voice_chat.messages",
    {"items": [message]},
    _meta={"webspace_id": webspace_id, "target_node_id": target_node_id},
)
```

Stream rules:

- keep payloads bounded
- dedupe events with stable ids
- provide snapshot-on-subscribe for widgets that should not open empty
- coalesce repeated snapshot requests per receiver/webspace/node
- do not eager-publish a replace stream for the same state that the widget is
  already reading from Yjs; use streams for separate high-churn state or
  snapshot-on-subscribe recovery
- do not copy stream tails back into Yjs just to make them visible

## Minimal UI plus details

The primary desktop Yjs document should contain the minimum state needed to
render the surface and explain whether it is healthy.

For heavy skills, prefer this split:

- summary in Yjs
- active rows or event tails in stream receivers
- details behind a `Details` action or modal
- full diagnostic evidence in disk snapshots or 360log

Good shape:

```text
data/infrastate/summary
data/infrastate/nodes
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

The runtime may warn, throttle, block, or quarantine a skill owner when it
applies unsafe pressure.

Generated skills must not hide that state.

Recommended behavior:

- implement `onQuarantine` or `on_quarantine` when the skill can release
  resources or record context
- accept `ttl_s`, `reason`, `metrics`, `webspace_id`, and `owner`
- write a compact skill-local incident log for later LLM repair
- return structured errors such as `skill_owner_quarantined`
- let the Web UI render disabled/quarantined state instead of silently
  pretending the action succeeded

The runtime owns the shared quarantine projection, for example `data.yjs_qrnt`.
Skills should not write that service branch directly.

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
- choose Yjs projection, stream, details tool, or skill-local storage for each
  branch
- check whether the skill is node-aware
- define size and frequency expectations

When coding:

- use SDK helpers instead of direct Yjs primitives
- keep tool responses small
- make updates idempotent
- fingerprint or coalesce heavy projections
- keep arrays bounded
- accept routing metadata and unknown keyword args
- preserve owner attribution where helper APIs require it

Before publishing:

- verify `data_projections` exist for Yjs state
- verify stream receivers have bounded modes and snapshot-on-subscribe behavior
- verify no handler rewrites broad Yjs roots
- verify no action returns a large payload when a projection/stream is the
  real data path
- verify quarantine/guard errors are visible to the UI

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

## Current migration priorities

The current workspace audit suggests this priority order:

1. migrate `voice_chat_skill` to declared projection/stream contracts
2. make `browsers_skill` projection refreshes idempotent, avoid all-webspace
   fanout for routine events, and keep streams to snapshot-on-subscribe or
   genuinely high-churn data
3. split `infrastate_skill` into minimal summary plus details/streams
4. split `infrascope_skill` into demanded projection families
5. decide whether `mediaserver` and `prompt_engineer_skill` should remain
   tool-driven or adopt browser-facing projection contracts
