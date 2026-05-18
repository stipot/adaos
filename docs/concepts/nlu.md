# NLU in AdaOS

This document describes the current production MVP direction for intent detection in AdaOS.

## MVP baseline

- Pipeline: `regex` -> `neural (service-skill, default-installed)` -> `rasa (service-skill, long-term fallback)` -> `teacher (LLM in the loop)`
- System boundary: NLU runtime code is one; only **data** varies per scenario/skill.
- Transport: intent detection is integrated into AdaOS event bus (not CLI-only).

The target install policy is:

- Neural NLU is prepared by default during `adaos install`.
- Nodes may still disable it with `ADAOS_NLU_NEURAL=0` or by omitting the
  service skill on constrained devices.
- Rasa remains a long-term fallback, not only a temporary migration bridge.
- The hot request path must only discover/start installed service skills. It
  must not create workspace skills or A/B runtime slots on demand.

## Ownership boundaries

Core AdaOS owns orchestration, contracts, confidence policy, traces, named
entity canonicalization, and the fallback/governance loop. Core AdaOS must not
bundle concrete NLU engines, Torch/FAISS dependencies, model weights, or service
skill source trees under the Python package.

Concrete NLU engines are providers:

- `neural_nlu_service_skill` is a registry/workspace service skill sourced from
  `skills/neural_nlu_service_skill` and installed into `.adaos/workspace`.
- `rasa_nlu_service_skill` is a registry/workspace service skill.
- Model artifacts and indexes are service-owned runtime data, not core package
  data.
- Provider installation and A/B slot activation belong to install/update
  flows, not to `nlp.intent.detect.*` handling.

The historical `src/adaos/interpreter_data` package is an early experiment and
should be retired as a provider delivery mechanism.

## Current implementation status

Implemented now:

- Regex-first event pipeline with optional Rasa service-skill fallback.
- Optional neural delegation event behind `ADAOS_NLU_NEURAL`.
- Neural NLU service-skill install preparation from normal workspace/registry
  source during install flow.
- Neural bridge discovery/start of installed service only; no hot-path
  workspace mutation or A/B slot preparation.
- Neural `/parse` contract with `top_intent`, `confidence`, `alternatives`,
  `slots`, `model_id`, `evidence`, canonicalized text, and named-entity
  evidence.
- Neural usage statistics in `state/nlu/neural_usage.json`: request/fallback
  counts, latency summary, confidence bands, accept/abstain/reject counts,
  per-intent status counts, canonicalization buckets, and review samples.
- Notebook artifact preparation script for Neural NLU:
  `skills/neural_nlu_service_skill/scripts/prepare_artifacts.py`.
- Rasa NLU service-skill isolation from the hub Python environment.
- Dry-run probe API for safe phrase checks:
  - `POST /api/nlu/teacher/{webspace_id}/probe`
- Lookup API with live desktop-registry overlay:
  - `GET /api/nlu/teacher/{webspace_id}/lookups`
- Stage trace persistence in `data.nlu_trace.items[]`.
- Schema-driven NLU Teacher modal that shows missed requests, candidates, raw event payloads, and Apply actions.

Not implemented yet:

- Teacher UI Check phrase field wired to the probe API.
- Teacher UI trace/ranking/entities/action preview panel.
- Operator feedback buttons: Correct, Fix, Save example.
- Stable template inventory for regex, Rasa examples, neural labels, and lookup sets.
- Root MCP token/session flow for governed LLM-assisted authoring.

The neural stage is now sourced as a normal service skill and prepared during
install by default. Runtime dispatch still requires `ADAOS_NLU_NEURAL=1` so
nodes can roll out the neural stage deliberately while Rasa remains the
long-term fallback.

## Event flow (high level)

1. UI / Telegram / Voice publishes:
   - `nlp.intent.detect.request { text, webspace_id, request_id, _meta... }`
2. Named-entity canonicalization resolves runtime names and aliases before
   model-specific interpretation becomes final:
   - device/browser/node/webspace/scenario/skill/app/modal aliases are resolved
     to canonical refs;
   - model-facing text may be masked with placeholders such as `{device}`,
     `{scenario}`, or `{app}`;
   - `resolved_entities`, ambiguities, and unresolved spans are recorded in
     trace.
3. `nlu.pipeline` tries regex rules:
   - built-in rules (`nlu.pipeline`)
   - dynamic rules loaded centrally from:
     - workspace scenarios (`scenario.json:nlu.regex_rules`)
     - workspace skills (`skill.yaml:nlu.regex_rules`)
     - legacy per-webspace cache (`data.nlu.regex_rules`)
4. If regex does not match:
   - if `ADAOS_NLU_NEURAL=1`: emits `nlp.intent.detect.neural`
   - otherwise emits `nlp.intent.detect.rasa`
5. Neural bridge:
   - calls `neural_nlu_service_skill:/parse`
   - the service skill is installed/prepared by install/update flows, not by
     the hot parse path;
   - passes named-entity `canonicalized_text` and `resolved_entities` evidence
     into the provider request;
   - neural service can run notebook-compatible Char-CNN + BiLSTM weights plus
     FAISS ranking artifacts when installed;
   - default deployment uses one active model per node, with usage telemetry
     collected so later per-locale/webspace/profile splits can be justified by
     evidence.
   - aggregate neural usage telemetry is persisted under
     `state/nlu/neural_usage.json` for operator diagnostics and retraining
     review.
   - on high confidence -> emits `nlp.intent.detected { via: "neural" }`
   - on abstain/error -> falls back to `nlp.intent.detect.rasa`
6. Rasa bridge:
   - calls the installed `rasa_nlu_service_skill`;
   - remains a supported long-term fallback, especially for ambiguous neural
     outputs and domains where Rasa training data is already stronger;
   - can be disabled on weak devices if neural/regex coverage is sufficient.
7. If an intent is found:
   - `nlp.intent.detected { intent, confidence, slots, text, webspace_id, request_id, via }`
8. If intent is not obtained:
   - `nlp.intent.not_obtained { reason, text, via, webspace_id, request_id }`
   - Router emits a human-friendly `io.out.chat.append` and records the request for NLU Teacher.
9. If teacher is enabled:
   - `nlp.teacher.request { webspace_id, request }` is emitted for teacher runtimes.

## Runtime trace

AdaOS records NLU decisions as a stage trace so the UI can explain why a phrase worked or failed.

- `nlu.trace.stage` is emitted for `request`, `regex`, `pipeline delegate`, `rasa`, and `dispatcher action/reject`.
- Trace items are stored under `data.nlu_trace.items[]`.
- The Teacher dry-run API can emit the same trace without dispatching actions:
  - `POST /api/nlu/teacher/{webspace_id}/probe`
  - request: `{ "text": "...", "use_rasa": true, "emit_trace": true }`
  - response: `intent`, `confidence`, `slots`, `entities`, `intent_ranking`, `stages`

The implementation checklist is tracked in [nlu-roadmap.md](./nlu-roadmap.md).
Human verification steps are tracked in [nlu-human-verification.md](./nlu-human-verification.md).

## Dynamic lookup tables

AdaOS now exports baseline NLU lookup tables from workspace desktop/scenario manifests, with packaged desktop manifests as an empty-workspace
fallback. The lookup sets are:

- `modal_id`
- `node_ref`
- `app_id`
- `scenario_id`
- `webspace_id`

The Teacher/LLM inspection endpoint is:

- `GET /api/nlu/teacher/{webspace_id}/lookups`

Rasa project export consumes the same snapshot and writes:

- native Rasa lookup entries into `state/interpreter/rasa_project/data/intents_from_config.yml`
- the full inspected snapshot into `state/interpreter/rasa_project/data/lookup_tables.json`

The Teacher endpoint overlays live read-only YJS registry state on top of manifest-derived values, so runtime desktop objects can be inspected
without waiting for a training export. Rasa training continues to use the stable manifest snapshot for reproducibility.

The lookup summary participates in the Rasa training fingerprint, so changing available manifest desktop ids can mark the NLU model stale.

## Runtime entity canonicalization

Device, browser, webspace, node, skill, and scenario names should not become
permanent model behavior. AdaOS should resolve registered display names,
observed names, and aliases to canonical refs before or alongside intent
detection, then pass the model masked text such as `open weather on {device}`.

Target behavior:

- runtime aliases and observed device names do not require Rasa/neural
  retraining by default;
- NLU trace records `resolved_entities`, original spans, canonical refs, and
  ambiguity decisions;
- Teacher/probe APIs show both static lookup matches and live named-entity
  resolver matches;
- dispatch receives canonical refs such as `device:member:<node_id>` rather
  than display strings.

The target architecture and roadmap are documented in
[Named Entities and Canonical Naming](../architecture/named-entities.md).

## NLU data ownership

Curated examples should live where the behavior is owned:

- Skill-owned actions: the owning skill stores intent examples, slots, regex
  rules, and training metadata.
- Scenario-owned flows: the owning scenario stores scenario-level NLU examples
  and routing hints.
- Core/client actions such as moving, hiding, opening, pinning, switching, and
  other shell behavior should not be faked as user skills. They should be
  described in a versioned **system action catalog** with stable action ids,
  argument schemas, aliases, and training examples.

The system action catalog is still data, not provider code. Regex, Rasa,
neural, Teacher, and MCP authoring can all consume it. This lets AdaOS train
and explain built-in UI/kernel commands without baking them into a particular
NLU engine.

NLU Teacher should write accepted corrections back to the owning artifact:

- skill examples/rules for skill actions;
- scenario examples/rules for scenario flows;
- system action examples/templates for core/client commands;
- named-entity aliases through the governed named-entity write path.

## Rasa as a service-skill

Rasa is treated as a **service-type skill** (separate Python/venv, managed lifecycle) to avoid dependency conflicts with the hub runtime. AdaOS uses the NLU-only `rasa-port` package, not upstream `rasa==3.6.x` in the root venv.

Install behavior:

- `adaos install` prepares `rasa_nlu_service_skill` into an active skill slot and trains once by default.
- `--no-rasa-nlu` disables service-skill preparation.
- `--no-train-nlu` keeps the service-skill ready but skips post-install training.
- `ADAOS_RASA_PORT_PATH` can point to a local `rasa-port` checkout.
- `ADAOS_NLU_RASA=0` disables the Rasa stage at runtime.

Runtime behavior:

- `adaos api serve` does not prepare new Rasa skill slots on demand.
- Rasa parse/train bridges only discover and start an already installed service-skill through `ServiceSkillSupervisor`.
- If Rasa is missing, the bridge falls back with `rasa_base_url_unresolved` instead of mutating slot A/B.
- Creating or switching slot A/B belongs to install/update/supervisor rollout flows, not to the hot NLU parse path.

The hub supervises:

- health checks
- crash frequency
- request failures/timeouts

Issues can trigger:

- `skill.service.issue`
- `skill.service.doctor.request` -> `skill.service.doctor.report` (LLM doctor can be plugged later)

## Teacher-in-the-loop (LLM)

When `regex` and `rasa` do not produce an intent, AdaOS calls an LLM teacher to:

- propose a **dataset revision** (existing intent + new examples + slots), or
- propose a **regex rule** to improve the `regex` stage, or
- propose a **new capability** (skill / scenario candidate), or
- decide to ignore (non-actionable).

Teacher receives scenario + skill context, including:

- current scenario NLU (`scenario.json:nlu`)
- installed catalog (apps/widgets + origins)
- existing dynamic regex rules (from scenarios/skills + legacy per-webspace cache)
- built-in regex rules (`nlu.pipeline`)
- selected skill-level NLU artifacts (e.g. `interpreter/intents.yml`)
- intent routing hints (`intent_routes`: scenario intent -> callSkill topic -> skill)
- system/host actions catalog (`system_actions`, `host_actions`)

Teacher state is projected into YJS under `data.nlu_teacher.*` for UI inspection, and also persisted on disk
under `.adaos/state/skills/nlu_teacher/<webspace_id>.json` so it survives YJS reload/reset.

## Web UI: NLU Teacher

In the default web desktop scenario the current NLU Teacher UI is a schema-driven modal:

- Tabs: **User requests** / **Candidates**
- Grouping:
  - User requests: grouped by `request_id`
  - Candidates: grouped by `candidate.name`, then by `request_id`
- Logs: groups show event payloads inline (raw JSON)
- Apply actions:
  - `nlp.teacher.revision.apply`
  - `nlp.teacher.candidate.apply`:
    - for `regex_rule` candidates: persists the rule into a workspace owner (preferably a skill), then mirrors into
      `data.nlu.regex_rules` as a runtime cache so the next request matches immediately (`via="regex.dynamic"`)
    - for `skill`/`scenario` candidates: creates a development plan item
  - a successful apply emits `ui.notify` with the owner (skill/scenario) where the rule was installed

Required UI expansion:

- Check phrase field that runs the dry-run probe without dispatching actions.
- Intent ranking, entities, slots, lookup matches, confidence, and fallback reason.
- Trace timeline: `voice text -> regex/neural/rasa -> intent -> action`.
- Correct/Fix/Save example actions with explicit target selection and audit metadata.
- Current-template view with stable ids so the operator can correct existing templates instead of creating duplicates.

Until this UI expansion lands, the current implementation is human-verifiable through API/CLI using
[nlu-human-verification.md](./nlu-human-verification.md).

## Dynamic regex rules (current contract)

- Storage (source of truth):
  - skill: `.adaos/workspace/skills/<skill>/skill.yaml` -> `nlu.regex_rules[]`
  - scenario: `.adaos/workspace/scenarios/<scenario>/scenario.json` -> `nlu.regex_rules[]`
- Rule identity:
  - every rule has `id="rx.<uuid>"`
- Observability:
  - every `regex.dynamic` match appends a JSONL record into `state/nlu/regex_usage.jsonl` (webspace_id, scenario_id, rule_id, intent, slots...)
- Optional trust policy:
  - `skill.yaml: llm_policy.autoapply_nlu_teacher=true` enables automatic Apply for teacher-proposed regex candidates targeting that skill

## Later (not MVP)

- Rhasspy / offline NLU
- Retriever-style NLU (graph/context retrieval)
- Multi-step, stateful NLU workflows across scenarios
