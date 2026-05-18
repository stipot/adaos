# NLU Roadmap Checklist

Current implementation estimate: **62%** for the practical AdaOS NLU roadmap.
The target architecture now treats Neural NLU as a default-installed provider,
but the productionization checklist remains mostly open.

## Phase 1: Baseline Runtime

- [x] Regex-first pipeline with dynamic scenario/skill regex rules.
- [x] Optional neural delegation event (`nlp.intent.detect.neural`) behind
  `ADAOS_NLU_NEURAL`.
- [x] Rasa NLU service-skill isolated from the hub Python environment.
- [x] Rasa service-skill prepared in A/B skill runtime slots.
- [x] Confidence/fallback path to `nlp.intent.not_obtained`.
- [x] Baseline desktop intents for opening modals and node-scoped modals.
- [x] Remove Neural NLU runtime-provider delivery through `src/adaos/interpreter_data`.
- [x] Ensure Neural NLU parse bridge only discovers/starts installed service skills and does
  not mutate workspace skills or A/B slots on demand.

## Phase 2: Operator Feedback Loop

- [x] NLU Teacher stores not-obtained requests per webspace.
- [x] Teacher can apply regex candidates into scenario/skill-owned artifacts.
- [x] Teacher can apply dataset revisions into scenario training content.
- [x] Dry-run phrase probe API for Teacher UI:
  - `POST /api/nlu/teacher/{webspace_id}/probe`
  - regex-first, optional Rasa fallback
  - returns `intent_ranking`, `entities`, `slots`, `stages`
  - does not dispatch actions
- [x] Human verification checklist separates current API/CLI checks from target UI behavior.
- [ ] UI field for "check phrase" wired to the probe endpoint.
- [ ] UI buttons: "correct", "fix", "save example".
- [ ] Operator-approved positive feedback stored with audit metadata.
- [ ] Route accepted feedback to the owning artifact:
  skill, scenario, system action catalog, or named-entity source.
- [ ] Add explicit correction targets for core/client actions that are not
  implemented as skills.

## Phase 3: Observability

- [x] `data.nlu_trace.items[]` stores request/detected/not-obtained events.
- [x] Stage trace event `nlu.trace.stage` records:
  - `request`
  - `regex`
  - `pipeline delegate`
  - `rasa`
  - `dispatcher action/reject`
- [ ] Trace UI should show `voice text -> regex/neural/rasa -> intent -> action`.
- [ ] Add latency per stage and service timing.
- [ ] Add golden phrase regression reports.
- [x] Add neural usage statistics: request count, latency, confidence
  histogram, accept/abstain/reject counts, fallback ratio, and per-intent
  status evidence.
- [x] Add named-entity canonicalization statistics: hit/miss/ambiguity counts
  and unresolved spans.

## Human Verification Gates

- [x] Current implemented behavior has a manual checklist: [nlu-human-verification.md](./nlu-human-verification.md).
- [x] Documentation marks which NLU Teacher behaviors are current UI, backend/API only, or target architecture.
- [ ] NLU Teacher UI can run a phrase probe without terminal access.
- [ ] NLU Teacher UI shows stage trace, ranking, entities, slots, lookup matches, confidence, and action preview.
- [ ] NLU Teacher UI supports Correct/Fix/Save example with target selection and audit metadata.
- [ ] Template correction flow uses stable ids and stale-write fingerprints.

## Phase 4: Dynamic Lookups and Template Inventory

- [x] Export baseline desktop lookup tables from workspace/packaged desktop manifests:
  - `modal_id`
  - `node_ref`
  - `app_id`
  - `scenario_id`
  - `webspace_id`
- [x] Feed lookup tables into Rasa training data.
- [x] Expose lookup tables for Teacher/LLM inspection:
  - `GET /api/nlu/teacher/{webspace_id}/lookups`
- [x] Overlay live YJS desktop registry values on top of manifest lookups for Teacher API.
- [ ] Expose stable template ids for regex, Rasa examples, neural labels, and lookup sets.
- [ ] Implement stale-write protection using template fingerprints.
- [ ] Define the system action catalog for core/client commands such as move,
  hide, open, pin, switch, and other shell actions.
- [ ] Include system action examples in NLU authoring context without treating
  those actions as user skills.

## Phase 4a: Runtime Named Entities and Canonicalization

- [x] Add a named-entity read model over devices, nodes, browsers, webspaces,
  scenarios, skills, apps, and modals.
- [x] Add a deterministic resolver that maps display names, observed names, and
  aliases to canonical refs before model dispatch.
- [x] Add entity masking so model-facing text can use placeholders such as
  `{device}`, `{webspace}`, and `{scenario}`.
- [x] Add ambiguity handling instead of silently choosing between conflicting
  aliases.
- [x] Add Teacher/probe output for resolved entities, unresolved spans,
  canonical refs, and ambiguity evidence.
- [x] Add regression tests proving alias and device-name changes do not require
  Rasa/neural retraining.
- [x] Track the full target design in
  [Named Entities and Canonical Naming](../architecture/named-entities.md).
- [x] Feed canonicalized text and entity evidence into the neural provider
  contract.
- [ ] Ensure Rasa and neural training fingerprints exclude runtime aliases by
  default.

## Phase 5: MCP-Assisted Authoring

- [ ] MCP Server modal issues scoped NLU authoring token.
- [ ] Root resolves token to subnet/zone/capabilities.
- [ ] Root MCP surfaces:
  - `nlu.describe_pipeline`
  - `nlu.check_phrase`
  - `nlu.list_templates`
  - `nlu.get_template`
  - `nlu.preview_template_patch`
  - `nlu.apply_template_patch`
  - `desktop.registry.lookup`
  - `skill.describe_tools`
- [ ] LLM receives current template inventory before proposing changes.
- [ ] Template patches are previewed and operator-approved before apply.

## Phase 6: Neural NLU Provider

### Provider Boundary

- [x] Move `neural_nlu_service_skill` out of `src/adaos/interpreter_data` into
  normal registry/workspace skill delivery.
- [x] Add default-on `adaos install` preparation for Neural NLU.
- [x] Add `--no-neural-nlu` install option for constrained devices.
- [x] Make the neural bridge discover/start only installed service skills.
- [x] Remove hot-path workspace mutation/bootstrap from neural parse handling.
- [x] Keep provider dependencies (`torch`, `faiss-cpu`, etc.) out of the hub
  root venv.

### Inference Contract

- [x] Freeze `/parse` request/response schema with `top_intent`,
  `confidence`, `alternatives`, `slots`, `model_id`, and `evidence`.
- [x] Pass named-entity canonicalization evidence into `/parse`.
- [x] Return matched examples, score components, and canonicalized text in
  `evidence`.
- [x] Add confidence gates for accept/abstain/reject.
- [x] Add neural abstain/error fallback to Rasa.
- [ ] Route Rasa miss/low confidence to NLU Teacher.

### Notebook Approach Port

- [x] Port masking logic into provider-owned runtime code.
- [x] Port Char-CNN + BiLSTM model loader.
- [x] Fix and test special-token compatibility between training and runtime.
- [x] Port supervised-contrastive embedding projection usage.
- [ ] Add FAISS positive example index.
- [ ] Add FAISS negative example indexes.
- [x] Add weighted ranker over softmax, k-NN similarity, and action/skill
  priors.
- [ ] Add intent/action id mapping from research labels to AdaOS canonical
  intents and system actions.

### Artifacts and ModelOps

- [x] Define node-level active model layout owned by the service skill runtime.
- [ ] Store `model.pt`, `labels.json`/`intents_manifest.json`, `vocab.json`,
  `faiss.index`, `examples_manifest.jsonl`, `ranker_config.json`, and
  `metrics.json`.
- [ ] Add immutable `model_id` and model provenance metadata.
- [ ] Add rollback pointer for the node-level active model.
- [ ] Add golden phrase regression report before model promotion.
- [ ] Add quality gates using accuracy, macro-F1, abstain rate, and latency.
- [ ] Defer per-locale/webspace/profile models until usage statistics justify
  the added operational complexity.

### Usage Statistics

- [x] Record neural request count and latency per stage.
- [x] Record confidence distributions and threshold bands.
- [x] Record accept/abstain/reject counts per intent.
- [x] Record fallback ratio `neural -> Rasa`.
- [x] Record canonicalization hit/miss/ambiguity/unresolved counts for neural
  requests.
- [x] Record abstained/rejected samples for Teacher review and retraining.
- [ ] Link final Rasa miss/low-confidence outcomes back to the neural fallback
  sample so `neural -> Rasa -> Teacher` can be measured end to end.

### Training Data Feedback

- [ ] Export skill-owned examples from skills.
- [ ] Export scenario-owned examples from scenarios.
- [ ] Export core/client command examples from the system action catalog.
- [ ] Export named-entity classes as masks, not as local alias training data.
- [ ] Let Teacher-approved corrections update regex, Neural, and Rasa datasets
  through the owning artifact.
- [ ] Rebuild/reindex the neural provider from curated examples after
  approved changes.

## Immediate Next Steps

1. Add persisted FAISS positive/negative indexes for the service-owned artifact
   layout; the current runtime has a Torch in-memory k-NN ranker fallback.
2. Define the system action catalog for core/client commands and include it in
   NLU authoring context.
3. Link neural usage samples to downstream Rasa/Teacher outcomes and expose the
   aggregate stats in operator diagnostics.
4. Wire the Teacher UI Check phrase flow to show canonicalization, neural,
   Rasa, and action-preview evidence.
5. Add "save correct example" backend action with skill/scenario/system-action
   target selection and audit metadata.
6. Add golden phrase reports and model promotion gates.

## Last Completed Slice

- Rasa is packaged as an optional default-on service-skill and installed into skill runtime slots.
- NLU Teacher has a dry-run phrase probe API with regex-first and optional Rasa fallback.
- NLU Teacher exposes baseline desktop lookup tables for `modal_id`, `node_ref`, `app_id`, `scenario_id`, and `webspace_id`.
- Teacher lookup API overlays live YJS values from `ui.application.modals`, `registry.merged.modals`, `data.catalog.apps`,
  `data.installed.apps`, `data.nodes`, and `ui.current_scenario`.
- Rasa export writes native lookup tables and `data/lookup_tables.json`; lookup summary is included in the training fingerprint.
- Runtime emits stage trace events for regex, pipeline delegation, Rasa, and dispatcher actions/rejects.
- Trace items are persisted to `data.nlu_trace.items[]` for the future UI timeline.
- Neural bridge records node-local aggregate usage stats in `state/nlu/neural_usage.json`, including latency,
  confidence bands, accept/abstain/reject counts, fallback ratio, canonicalization buckets, and review samples.
- NLU documentation now includes a human verification checklist and clearly separates current UI, backend/API-only behavior, and target UI.
