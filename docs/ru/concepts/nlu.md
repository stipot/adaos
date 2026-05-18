# NLU in AdaOS

This document describes the current production MVP direction for intent detection in AdaOS.

## MVP baseline

- Pipeline: `regex` -> `neural (service-skill, optional)` -> `rasa (service-skill, optional default-on)` -> `teacher (LLM in the loop)`
- System boundary: NLU runtime code is one; only **data** varies per scenario/skill.
- Transport: intent detection is integrated into AdaOS event bus (not CLI-only).

## Event flow (high level)

1. UI / Telegram / Voice publishes:
   - `nlp.intent.detect.request { text, webspace_id, request_id, _meta... }`
2. `nlu.pipeline` tries regex rules:
   - built-in rules (`nlu.pipeline`)
   - dynamic rules loaded centrally from:
     - workspace scenarios (`scenario.json:nlu.regex_rules`)
     - workspace skills (`skill.yaml:nlu.regex_rules`)
     - legacy per-webspace cache (`data.nlu.regex_rules`)
3. If regex does not match:
   - if `ADAOS_NLU_NEURAL=1`: emits `nlp.intent.detect.neural`
   - otherwise emits `nlp.intent.detect.rasa`
4. Neural bridge:
   - calls `neural_nlu_service_skill:/parse`
   - if the skill is missing, hub falls back to Rasa; install/update flows prepare the service skill from the workspace/registry source
   - upstream detector code is ported into `handlers/upstream_detector_port.py` (service-side runtime module)
   - neural service can run notebook-compatible Char-CNN + BiLSTM weights via:
     - `ADAOS_NEURAL_MODEL_PATH` (state_dict `.pt`)
     - `ADAOS_NEURAL_LABELS_PATH` (JSON list of intents)
     - `ADAOS_NEURAL_VOCAB_PATH` (JSON char vocabulary)
   - default artifact location (if env vars are not set):
     - `<ADAOS_BASE_DIR>/state/nlu/neural/model.pt`
     - `<ADAOS_BASE_DIR>/state/nlu/neural/labels.json`
     - `<ADAOS_BASE_DIR>/state/nlu/neural/vocab.json`
   - on high confidence -> emits `nlp.intent.detected { via: "neural" }`
   - on abstain/error -> falls back to `nlp.intent.detect.rasa`
5. If an intent is found:
   - `nlp.intent.detected { intent, confidence, slots, text, webspace_id, request_id, via }`
6. If intent is not obtained:
   - `nlp.intent.not_obtained { reason, text, via, webspace_id, request_id }`
   - Router emits a human-friendly `io.out.chat.append` and records the request for NLU Teacher.
7. If teacher is enabled:
   - `nlp.teacher.request { webspace_id, request }` is emitted for teacher runtimes.

## Rasa as a service-skill

Rasa is treated as a **service-type skill** (separate Python/venv, managed lifecycle) to avoid dependency conflicts with the hub runtime. AdaOS uses the NLU-only `rasa-port` package, not upstream `rasa==3.6.x` in the root venv.

Install behavior:

- `adaos install` prepares `rasa_nlu_service_skill` into an active skill slot and trains once by default.
- `--no-rasa-nlu` disables service-skill preparation.
- `--no-train-nlu` keeps the service-skill ready but skips post-install training.
- `ADAOS_RASA_PORT_PATH` can point to a local `rasa-port` checkout.
- `ADAOS_NLU_RASA=0` disables the Rasa stage at runtime.

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

In the default web desktop scenario the NLU Teacher UI is a schema-driven modal:

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

The modal is opened through the Web UI overlay runtime, not directly by a
widget. The runtime captures the focused desktop element, releases background
focus before hiding the desktop surface, and restores focus after dismissal.
This keeps the NLU Teacher action declarative while preserving the shared
accessibility lifecycle for all schema-driven modals.

## Dynamic regex rules (current contract)

- Storage (source of truth):
  - skill: `.adaos/workspace/skills/<skill>/skill.yaml` → `nlu.regex_rules[]`
  - scenario: `.adaos/workspace/scenarios/<scenario>/scenario.json` → `nlu.regex_rules[]`
- Rule identity:
  - every rule has `id="rx.<uuid>"`
- Observability:
  - every `regex.dynamic` match appends a JSONL record into `state/nlu/regex_usage.jsonl` (webspace_id, scenario_id, rule_id, intent, slots…)
- Optional trust policy:
  - `skill.yaml: llm_policy.autoapply_nlu_teacher=true` enables automatic Apply for teacher-proposed regex candidates targeting that skill

## Later (not MVP)

- Rhasspy / offline NLU
- Retriever-style NLU (graph/context retrieval)
- Multi-step, stateful NLU workflows across scenarios
