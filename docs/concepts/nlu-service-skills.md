# NLU as Service Skills

AdaOS treats some NLU components as long-running **service skills** instead of in-process Python modules.
General service-skill docs: `docs/concepts/service-skills.md`.

## Why service skills for NLU?

- Avoid Python/ABI conflicts (pin Python per component when needed).
- Isolate heavy dependencies per component.
- Uniform lifecycle: install -> start -> health -> restart -> observe.

## Provider packaging boundary

NLU engines are providers, not core package data. The AdaOS core owns event
contracts, confidence policy, tracing, named-entity canonicalization, and
fallback orchestration. Concrete engines such as Neural NLU and Rasa are normal
workspace/registry service skills.

Provider source trees, Torch/FAISS/Rasa dependencies, model weights, indexes,
and training artifacts must not be bundled under `src/adaos/interpreter_data`.
That package is treated as an early experiment to retire. A runtime bridge may
discover and start an installed service skill, but it must not create workspace
skills or prepare A/B slots on the hot parse path.

## Event interface

The hub NLU pipeline uses:

- `nlp.intent.detect.request { text, webspace_id, request_id, _meta }`
- `nlp.intent.detected { intent, confidence, slots, text, webspace_id, request_id, via }`
- `nlp.intent.not_obtained { reason, text, webspace_id, request_id, via }`

## Neural NLU service skill

Target skill:

- Source skill: `skills/neural_nlu_service_skill`
- Installed skill: `.adaos/workspace/skills/neural_nlu_service_skill`
- Active source: `.adaos/workspace/skills/.runtime/neural_nlu_service_skill/v<major>.<minor>/slots/<A|B>/src/skills/neural_nlu_service_skill`
- Supervisor: `src/adaos/services/skill/service_supervisor.py`
- Bridge: `src/adaos/services/nlu/neural_service_bridge.py`

Target install policy:

- `adaos install` prepares Neural NLU by default.
- `--no-neural-nlu` or `ADAOS_NLU_NEURAL=0` may disable the stage on weak
  devices.
- The bridge starts only an already installed/active service skill.
- The bridge does not copy templates, create workspace skills, or prepare A/B
  slots on `nlp.intent.detect.neural`.
- If the service skill is missing or unhealthy, the bridge falls back to Rasa.

Runtime / environment:

- `runtime.kind: service`
- `runtime.env.mode: venv` by default for Torch/FAISS isolation
- dependencies are declared by the skill, not by the hub root venv

HTTP API:

- `GET /health`
- `POST /parse` `{ "text": "...", "webspace_id": "...", "locale": "ru", "canonicalized_text": "...", "entities": {...} }`
- optional `POST /reindex`
- optional `POST /train` or an offline artifact build command

Responses include `top_intent`, `confidence`, `alternatives`, `slots`,
`model_id`, and `evidence`, with the same payload mirrored under `result` for
older bridge compatibility. The evidence includes canonicalized text,
model-facing masked text, score components, and matched examples when the
example index is available.

Target artifacts are service-owned runtime data:

- `model.pt`
- `labels.json` / `intents_manifest.json`
- `vocab.json`
- `faiss.index` and `faiss.index.json` for the optional lazy positive-example
  FAISS index
- `examples_manifest.jsonl`
- `example_index.pt` as the Torch tensor k-NN fallback cache when FAISS is not
  installed in the service venv
- `ranker_config.json`
- `metrics.json`

Notebook outputs can be normalized into this layout with:

```powershell
.\.venv\Scripts\python.exe skills\neural_nlu_service_skill\scripts\prepare_artifacts.py `
  --source-root example `
  --out-dir .adaos\state\nlu\neural
```

The script copies `best_model*.pt`, derives labels/vocab with the same masking
and special-token order as the notebook, writes example and intent manifests,
and records provenance in `metrics.json`.

On first successful model load, the neural skill validates and reuses an
existing positive-example index when the model id, model SHA, example count,
and example digest still match. If `faiss` is importable in the service venv,
the preferred `auto` backend writes `faiss.index`; otherwise it writes
`example_index.pt`. `ADAOS_NEURAL_EXAMPLE_INDEX_BACKEND=torch` forces the
Torch fallback, while `ADAOS_NEURAL_EXAMPLE_INDEX_BACKEND=faiss` requires the
FAISS backend.

The active artifacts can be smoke-tested with
`skills/neural_nlu_service_skill/scripts/evaluate_golden.py`, which writes
`golden_report.json` and can fail the command with `--min-accuracy`.
The hub-side bridge can be probed with
`adaos interpreter neural-probe "какая погода в москве" --locale ru`; this
uses the same service discovery/start, confidence gates, canonicalized payload,
and usage-stat recording as runtime neural dispatch.

The first production policy is one active model per node. The service records
usage statistics so later splits by locale, webspace, profile, or hardware class
can be justified by observed drift, latency, confidence, and fallback patterns.
The bridge persists those node-local aggregates in
`state/nlu/neural_usage.json`: request and fallback counts, latency summary,
confidence bands, accept/abstain/reject counts, per-intent status counts,
canonicalization hit/miss/ambiguity/unresolved buckets, and bounded samples for
Teacher review.

The intended detector algorithm is the full research-notebook approach:

- named-entity canonicalization before model inference;
- entity masking for model-facing text;
- Char-CNN + BiLSTM encoder;
- supervised-contrastive embedding projection;
- FAISS positive and negative example retrieval;
- weighted ranker over softmax, k-NN similarity, and skill/action priors.

## Rasa NLU service skill

- Skill: `.adaos/workspace/skills/rasa_nlu_service_skill`
- Config: `.adaos/workspace/skills/rasa_nlu_service_skill/skill.yaml`
- Active source: `.adaos/workspace/skills/.runtime/rasa_nlu_service_skill/v<major>.<minor>/slots/<A|B>/src/skills/rasa_nlu_service_skill`
- Supervisor: `src/adaos/services/skill/service_supervisor.py`
- Bridge: `src/adaos/services/nlu/rasa_service_bridge.py`
- Installer: `src/adaos/services/nlu/rasa_skill_installer.py`

### Runtime / environment

- `runtime.kind: service`
- `runtime.env.mode: venv`
- `runtime.env.python: 3.11`

The supervisor discovers the active runtime slot and uses the bucket-level service venv:

- `.adaos/workspace/skills/.runtime/rasa_nlu_service_skill/v<major>.<minor>/venv`

Patch A/B updates reuse this venv. A minor/major skill update creates a new runtime bucket and therefore a fresh service venv.

When AdaOS is started directly from the project root with `adaos api serve`, runtime bridges must not prepare or switch
skill slots. In that mode the service supervisor either:

- uses the already active skill runtime slot, if one exists, or
- uses the workspace service-skill source and a non-slot service venv under `state/services/rasa_nlu_service_skill`.

Preparing a new A/B slot is reserved for install/update flows (`adaos install`, skill runtime refresh, or a
supervisor-managed candidate rollout). A plain `api serve` should not create slot B just because Rasa is parsed or
trained.

Dependencies are installed from:

- `skill.yaml: dependencies`
- optional `requirements.in` in the skill root

For Rasa, `skill.yaml: dependencies` is generated by the installer:

- local checkout: `--no-deps -e file:///.../src/adaos/integrations/rasa-port`
- fallback: `--no-deps "adaos-rasa-nlu @ git+https://github.com/stipot-com/rasa-port.git@main"`

The hub root venv must not install upstream `rasa==3.6.x`.

### HTTP API (provided by the service)

- `GET /health`
- `POST /parse` `{ "text": "..." }`
- `POST /train` `{ "project_dir": "...", "out_dir": "...", "fixed_model_name": "interpreter_latest" }`

### Self-management (issues)

Rasa bridge records service issues when parsing fails or times out:
- issue types: `rasa_failed`, `rasa_timeout`
- storage: `state/services/rasa_nlu_service_skill/issues.json`

If `service.self_managed.doctor.enabled: true`, these issues can also trigger:
- `skill.service.doctor.request` events (with log tail)
- persisted reports via `state/services/rasa_nlu_service_skill/doctor_reports.json`

## Training flow

1. `adaos.services.nlu.data_registry.sync_from_scenarios_and_skills()` syncs NLU data into interpreter workspace files.
2. `adaos.services.nlu.rasa_training_bridge` triggers training by calling the service `/train`.
3. `adaos install` prepares the service-skill/runtime slot and runs one post-install train by default. Use
   `--no-train-nlu` to skip training after preparation.

The parse and train bridges do not install or prepare Rasa. If the service-skill is missing, they return fallback
reasons such as `rasa_base_url_unresolved` and let the operator run the install/update path intentionally.

Controls:

- `ADAOS_NLU_RASA=0` disables the Rasa stage.
- `ADAOS_NLU_AUTOTRAIN=1` enables event-driven retraining after scenario/skill changes.
- `ADAOS_RASA_PORT_PATH` points to a local `rasa-port` checkout.
- `ADAOS_RASA_PORT_REQUIREMENT` overrides the fallback git requirement.
