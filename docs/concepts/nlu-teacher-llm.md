# NLU Teacher (LLM) MVP

This document describes the minimal teacher-in-the-loop implementation for AdaOS NLU.

## Pipeline (MVP)

1. Router emits `nlp.intent.detect.request` (`text` + `webspace_id` + `request_id`).
2. `nlu.pipeline` tries:
   - built-in + dynamic `regex` (fast, deterministic)
   - if not matched -> delegates to Rasa service (`nlp.intent.detect.rasa`)
3. If intent is found -> `nlp.intent.detected { via: "regex" | "regex.dynamic" | "rasa" }`.
4. If intent is not obtained -> `nlp.intent.not_obtained { reason, via, ... }`.
5. Teacher bridge reacts to `nlp.intent.not_obtained` and emits:
   - `nlp.teacher.request { webspace_id, request }`
6. Teacher runtimes store state for UI inspection (YJS, per webspace):
   - `data.nlu_teacher.events[]` (includes `llm.request` / `llm.response`)
   - `data.nlu_teacher.candidates[]` (regex rules / skill candidates / scenario candidates)
   - `data.nlu_teacher.revisions[]` (proposed dataset revisions)
   - `data.nlu_teacher.llm_logs[]` (request/response logs; debugging)
7. Teacher state is also persisted on disk so it survives YJS reload/reset:
   - `.adaos/state/skills/nlu_teacher/<webspace_id>.json`

## Enable

Set env vars on hub:

- `ADAOS_NLU_TEACHER=1`
- `ADAOS_NLU_LLM_TEACHER=1`
- optional: `ADAOS_NLU_LLM_MODEL=gpt-4o-mini`
- optional: `ADAOS_NLU_LLM_TIMEOUT_S=20`

## Teacher context (inputs)

LLM teacher receives a compact context snapshot (per webspace), including:

- current scenario id
- scenario-level NLU (`scenario.json:nlu`)
- catalog of apps/widgets (with origins) + installed ids
- built-in regex rules (`nlu.pipeline`)
- existing regex rules (from skills/scenarios + legacy per-webspace cache)
- routing hints (`intent_routes`: scenario intent -> callSkill topic -> skill)
- system actions visible in the current scenario (`system_actions`) and a published host action catalog (`host_actions`)
  with stable action ids, linked intents, slots, host event names, and training examples
- skill manifests (`skills_manifest`: tools/events/llm_policy summary for installed skills)

Goal: prefer improving existing intents (regex rule / dataset revision) over creating a new capability, when possible.

## Target UI

The NLU Teacher modal should become the operator-facing workbench for testing and curating NLU behavior.

Current UI status:

- Implemented: User requests tab, Candidates tab, raw JSON event inspection, revision Apply, and candidate Apply.
- Implemented in backend/API: dry-run phrase probe and dynamic lookup inspection.
- Missing in UI: Check phrase, trace/ranking/entities/action preview, Correct/Fix/Save example, template inventory, and patch preview.

Target controls:

- **Check phrase**: input field that sends a dry-run request through the current NLU pipeline.
- **Trace view**: shows `voice text -> regex/neural/rasa -> intent -> action`, with stage, confidence, latency, and fallback reason.
- **Candidate view**: shows intent ranking, extracted entities/slots, matched lookup values, and the proposed action target.
- **Correct**: marks the current interpretation as accepted and records the example as positive feedback.
- **Fix**: lets the operator choose or edit intent, slots, action, and storage target.
- **Save example**: persists the curated example into scenario or skill training content, without mutating code.

The first implementation can be intentionally narrow: support dry-run phrase checks, show ranking/entities for Rasa, and save examples for the
default desktop modal and runtime-backed system action intents. Broader tool/action authoring should wait until Root MCP descriptors are available.

## Current dry-run API

The first backend slice is available for the Teacher UI check-phrase field:

- `POST /api/nlu/teacher/{webspace_id}/probe`
- request:

```json
{
  "text": "open apps catalog",
  "use_rasa": true,
  "emit_trace": true
}
```

The endpoint runs a dry check through regex first and optionally Rasa second. It does not emit `nlp.intent.detected`
and does not dispatch actions, so it is safe to call from a UI preview.

Response shape:

```json
{
  "ok": true,
  "accepted": true,
  "via": "rasa",
  "intent": "desktop.open_modal",
  "confidence": 0.87,
  "slots": {"modal_id": "apps_catalog"},
  "entities": [{"entity": "modal_id", "value": "apps_catalog"}],
  "intent_ranking": [{"name": "desktop.open_modal", "confidence": 0.87}],
  "stages": [
    {"stage": "request", "status": "received"},
    {"stage": "regex", "status": "miss"},
    {"stage": "rasa", "status": "hit"}
  ]
}
```

When `emit_trace=true`, each stage is also persisted through `nlu.trace.stage` into `data.nlu_trace.items[]`.

## Current lookup API

Teacher/LLM can inspect the desktop ids that should be treated as entity candidates:

- `GET /api/nlu/teacher/{webspace_id}/lookups`
- lookup sets:
  - `modal_id`
  - `node_ref`
  - `app_id`
  - `scenario_id`
  - `webspace_id`

The backend reads workspace desktop/scenario manifests and falls back to packaged desktop manifests when the test/install workspace is still
empty. For this API, the manifest snapshot is then overlaid with live read-only YJS values from `ui.application.modals`,
`registry.merged.modals`, `data.catalog.apps`, `data.installed.apps`, `data.nodes`, and `ui.current_scenario`.

Rasa export intentionally consumes the stable manifest snapshot as native lookup-table entries and writes the full snapshot to
`state/interpreter/rasa_project/data/lookup_tables.json`. That keeps training reproducible while the Teacher API can still show the current
desktop registry.

## Human verification contract

The current implementation is considered verifiable only when the operator can reproduce the result through tests/API and, where available,
the current UI. The checklist lives in [nlu-human-verification.md](./nlu-human-verification.md).

Minimum acceptance for every NLU slice:

- A focused test or smoke command exists.
- The expected response includes intent, confidence, slots/entities, ranking when available, and stage trace.
- Fallback behavior is explicit when confidence is too low or no stage accepts the phrase.
- The operator can tell whether a value came from regex, Rasa, lookup tables, or Teacher fallback.
- Documentation states whether the behavior is available in UI now or only through API/CLI.

Teacher UI becomes the primary human control surface only after Check phrase, trace view, Correct/Fix/Save example, and template inventory are
implemented. Until then, the API/CLI checklist remains the source of truth.

## MCP-assisted teacher context

For the teacher to decide which skill/tool owns a phrase, it needs governed machine-readable context, not only free-form prompt text.
The target architecture uses `Root MCP Foundation` as the agent-facing context and authorization layer.

Token/session flow:

1. The web **MCP Server** modal exposes an **Issue token** button.
2. The operator chooses target, TTL, and capability profile, initially `NLUTeacherAuthor`.
3. Root issues a target-scoped MCP session lease or access token.
4. The browser stores only the returned bearer token/session reference needed for subsequent root requests.
5. Root resolves the token to `subnet_id`, `zone`, target, and capabilities, then routes allowed calls to root descriptors or the managed hub.

The token should accompany root requests as authorization context. It should not be embedded into LLM prompts, training examples, or generated
NLU artifacts.

Required Root MCP surfaces for NLU Teacher:

- `nlu.describe_pipeline`: regex/neural/rasa stages, thresholds, supported template types, and apply capabilities.
- `nlu.check_phrase`: dry-run phrase interpretation with trace, ranking, entities, and action preview.
- `nlu.list_templates`: current regex/Rasa/neural/lookup templates with stable ids, owners, fingerprints, and status.
- `nlu.get_template`: one current template with full editable content and provenance.
- `nlu.list_training_targets`: scenario and skill locations where examples/rules may be saved.
- `nlu.propose_templates`: LLM-facing contract for multi-engine template proposals.
- `nlu.preview_template_patch`: validate a proposed correction and return a diff without writing.
- `nlu.apply_template_patch`: apply an approved correction with audit and stale-write protection.
- `desktop.registry.lookup`: current `modal_id`, `node_ref`, `app_id`, `scenario_id`, webspace, and installed desktop objects.
- `skill.describe_tools`: skill tools, event subscriptions/publications, input schemas, and ownership hints.

This keeps the existing regex model intact: Root MCP supplies descriptors and governed operations, while the current runtime pipeline remains
`regex-first` and data-owned rules stay in scenario/skill artifacts.

## NLU authoring contract

Keep the implementation split into three layers:

- `Teacher UI`: renders trace, current templates, diffs, and approval controls. It should not decide ownership or write files directly.
- `NLU authoring service`: validates phrase checks, template patches, training targets, and safe-apply rules.
- `Root MCP`: provides governed descriptors, current template inventory, token/session resolution, routing, and audit.

The LLM should receive the current template inventory before proposing changes. This prevents duplicate regex rules, repeated Rasa examples,
and blind rewrites of training content.

Current template inventory response:

```json
{
  "templates": [
    {
      "template_id": "rx.web_desktop.desktop.open_weather.01HX...",
      "engine": "regex",
      "intent": "desktop.open_weather",
      "owner": {"type": "scenario", "id": "web_desktop"},
      "status": "active",
      "fingerprint": "sha256:...",
      "summary": "RU/EN weather phrase with optional city",
      "source": {"path": "scenarios/web_desktop/scenario.json", "json_pointer": "/nlu/regex_rules/0"}
    },
    {
      "template_id": "rasa.web_desktop.desktop.open_node_modal.example.4f9c...",
      "engine": "rasa",
      "intent": "desktop.open_node_modal",
      "owner": {"type": "scenario", "id": "web_desktop"},
      "status": "active",
      "fingerprint": "sha256:...",
      "summary": "open [apps_catalog](modal_id) on [member-1](node_ref)"
    }
  ],
  "snapshot_id": "nlu-template-snapshot.2026-05-11T12:00:00Z",
  "generated_from": ["scenario:web_desktop", "skills:*", "desktop.registry"]
}
```

Template ids should be stable enough for correction and audit:

- `regex`: use the rule id when available (`rx.<uuid>`), namespaced by owner/intent when exposed through MCP.
- `rasa`: derive a deterministic id from owner, intent, example text, and entity annotations.
- `neural`: derive from owner, intent, masked text, and label.
- `lookup`: derive from registry namespace, entity name, snapshot id, and value-set fingerprint.

Corrections are patches against existing templates, not raw file writes:

```json
{
  "template_id": "rasa.web_desktop.desktop.open_node_modal.example.4f9c...",
  "base_fingerprint": "sha256:...",
  "operation": "replace",
  "patch": {
    "example": "open [apps_catalog](modal_id) on [kitchen](node_ref)",
    "reason": "Use real node alias from desktop registry instead of hardcoded member-1"
  }
}
```

Safe apply state machine:

1. `list/get templates`: collect current ids, owners, fingerprints, and editable fields.
2. `propose patch`: LLM references an existing `template_id` or explicitly asks to create a new template.
3. `preview diff`: authoring service validates owner, capability, schema, duplicates, and `base_fingerprint`.
4. `operator approval`: UI shows before/after, affected intent/action, and expected pipeline impact.
5. `apply`: write through scenario/skill training APIs only, then record audit event and rollback pointer.
6. `verify`: run `nlu.check_phrase` and optional golden phrase regression checks.

If `base_fingerprint` no longer matches, the patch must be rejected as stale and the UI should refresh template inventory.

## Multi-engine teacher output

To avoid overfitting the first Teacher version to one NLU engine, the LLM should return a template bundle for all relevant pipeline stages.
The runtime can apply only the supported/safe subset at first.

Proposed bundle shape:

```json
{
  "phrase": "open apps catalog on kitchen display",
  "intent": "desktop.open_node_modal",
  "target": {"type": "scenario", "id": "web_desktop"},
  "slots": [
    {"name": "modal_id", "value": "apps_catalog", "source": "desktop.registry.lookup"},
    {"name": "node_ref", "value": "kitchen", "source": "desktop.registry.lookup"}
  ],
  "action": {"event": "desktop.modal.open", "params": {"modal_id": "$slot.modal_id", "target_node_id": "$slot.node_ref"}},
  "templates": {
    "regex": [{"pattern": "open\\s+(?P<modal_id>...)\\s+on\\s+(?P<node_ref>...)", "priority": "draft"}],
    "rasa": [{"example": "open [apps_catalog](modal_id) on [kitchen](node_ref)"}],
    "neural": [{"masked": "open {modal_id} on {node_ref}", "label": "desktop.open_node_modal"}],
    "lookups": [{"entity": "modal_id", "values_ref": "desktop.registry.modal_ids"}, {"entity": "node_ref", "values_ref": "desktop.registry.node_refs"}]
  },
  "rationale": "Phrase maps to the default desktop modal action and uses known registry values."
}
```

Initial apply policy:

- `regex`: apply only after explicit operator confirmation, and never overwrite existing rules.
- `rasa`: save examples and lookup references into scenario/skill training content.
- `neural`: store labels/masked examples as future training metadata; do not enable inference behavior until neural stage is explicitly enabled.
- `lookups`: generate from live desktop registry snapshots, not from hardcoded examples such as `member-1`.

## Apply

Apply can be triggered from UI or programmatically:

- apply a proposed dataset revision:
  - `nlp.teacher.revision.apply { revision_id, intent, examples[], slots }`
- apply a teacher candidate:
  - `nlp.teacher.candidate.apply { candidate_id, target? }`
  - for `regex_rule` candidates the runtime delegates to `nlp.teacher.regex_rule.apply { intent, pattern, target? }`

## Where regex rules are stored

The teacher does not "bake" regexes into the hub code. A rule is stored as data owned by a workspace artifact:

- **Skill-owned** (preferred): `.adaos/workspace/skills/<skill>/skill.yaml` -> `nlu.regex_rules[]`
- **Scenario-owned**: `.adaos/workspace/scenarios/<scenario>/scenario.json` -> `nlu.regex_rules[]`
- **Legacy runtime cache**: mirrored into YJS `data.nlu.regex_rules[]` so it starts matching immediately after Apply.

Every rule has a stable identity: `id="rx.<uuid>"`.

## Target selection (skill vs scenario)

When the teacher proposes a regex rule, it should also propose a storage target:

- Prefer the skill that actually handles the intent (derived from scenario intent `callSkill` actions + skill `events.subscribe`).
- If the intent triggers host/system behavior (`callHost`), the target is usually the scenario.

Apply supports a UI override ("Apply to Scenario"), in addition to an LLM-suggested target.

## Auto-apply policy (trusted skills)

Skills can opt into automatic application of teacher-proposed regex rules:

- `skill.yaml: llm_policy.autoapply_nlu_teacher: true`

If enabled and the candidate target is that skill, the hub auto-emits `nlp.teacher.candidate.apply` after a candidate is proposed.

## Observability: regex usage journal

Each time the dynamic regex stage matches, the hub appends a JSONL record to:

- `state/nlu/regex_usage.jsonl`

This is intended for later cleanup/optimization (identify dead rules, consolidate duplicates, etc.).

## Example: improve existing intent via regex rule

Utterance: `Покажи температуру в Берлине`

Assume built-in weather regex only matches `погода` / `weather`, so the regex stage misses the intent.

Expected teacher decision:

- `decision="propose_regex_rule"`
- `regex_rule.intent="desktop.open_weather"`
- `regex_rule.pattern` should be a Python regex with named capture groups, e.g. `(?P<city>...)`
- `target` should usually be the owning skill (e.g. `{"type":"skill","id":"weather_skill"}`)

After you click **Apply** (UI emits `nlp.teacher.candidate.apply`):

- the rule is persisted into the chosen owner (skill/scenario) and mirrored into `data.nlu.regex_rules`
- the next time the same utterance is sent, `nlu.pipeline` should resolve it as `via="regex.dynamic"` without calling the LLM

## Roadmap

### Phase 0 - Current baseline polish

- Keep `regex -> Rasa -> fallback/teacher` working through event bus.
- Preserve low-confidence fallback to `nlp.intent.not_obtained`.
- Keep service skills out of the user NLU fingerprint unless they provide training metadata.
- Maintain smoke coverage for `[homepoint] Voice -> Rasa -> desktop.modal.open`.

### Phase 1 - Trace and dry-run foundation

- Add a structured `nlu.trace` record for each phrase.
- Add dry-run `nlu.check_phrase` service/API/MCP contract.
- Surface stage decisions, confidence, ranking, entities, and action preview.

### Phase 2 - MCP token and descriptor context

- Add **Issue token** to the MCP Server modal.
- Issue target-scoped Root MCP session leases with `NLUTeacherAuthor` capability profile.
- Publish NLU pipeline, skill tool, scenario action, and desktop registry descriptors through Root MCP.
- Publish current NLU template inventory with `template_id`, owner, status, fingerprint, and provenance.

### Phase 3 - Useful Teacher UI

- Add Check phrase field.
- Show intent ranking/entities/action preview.
- Show existing templates relevant to the phrase/intent and allow selecting one for correction.
- Add Correct/Fix actions.
- Save curated examples into scenario/skill training content.

### Phase 4 - Multi-engine template application

- Accept Teacher template bundles for regex, Rasa, neural, and lookup metadata.
- Accept correction patches against existing `template_id` values with `base_fingerprint` stale-write protection.
- Apply only the supported subset safely.
- Keep regex deterministic and data-owned.
- Feed Rasa from actual desktop registry lookups.

### Phase 5 - Feedback and promotion

- Collect statistics by phrase, intent, stage, confidence, and operator feedback.
- Promote high-value examples into training sets.
- Tune confidence thresholds from observed misses and false accepts.
- Add rollout/rollback controls for neural and Rasa model updates.
