# NLU Human Verification

This checklist is the operator-facing control loop for the current AdaOS NLU implementation. It separates what can be verified today from the target Teacher UI that still needs product work.

## Scope

Verifiable today:

- Regex-first detection and fallback behavior.
- Rasa dry-run phrase checks through the Teacher probe API.
- Intent ranking, entities, slots, confidence, and stage trace in API responses.
- Dynamic desktop lookup tables from manifests plus live read-only desktop registry overlay.
- Current NLU Teacher modal smoke behavior: missed requests, candidates, raw event payloads, and Apply.

Not yet verifiable through UI:

- Typing a phrase directly inside the NLU Teacher modal.
- Seeing ranking/entities/action preview as a first-class UI panel.
- Marking an interpretation as Correct/Fix/Save example.
- Editing existing templates by stable `template_id`.
- Root MCP token issuance and governed LLM-assisted patch apply.

## 1. Regression Tests

Run the focused NLU tests from the repository root:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_nlu_probe.py tests/test_nlu_rasa_baseline.py tests/test_nlu_lookup_tables.py
```

Expected result:

- Probe API tests pass.
- Rasa baseline export/tests pass.
- Lookup table export/API tests pass.

Neural provider readiness can be checked without dispatching an action:

```powershell
.\.venv\Scripts\adaos.exe interpreter neural-readiness --start --stop-after
```

Expected result:

- `ok=true`.
- `checks.model_loaded=true`.
- `artifacts.index_backend` is `faiss` when `faiss-cpu` is installed, or
  `torch_tensor` on fallback-only nodes.
- `artifacts.negative_index_backend` follows the same `faiss` or
  `torch_tensor` backend after the first model load.

Neural intent mapping can be checked through the same bridge path:

```powershell
.\.venv\Scripts\python.exe -m adaos interpreter neural-probe "какая погода в москве" --locale ru --no-record-stats
```

Expected result:

- `ok=true`.
- `top_intent` is the canonical intent from `intent_map.json`.
- `evidence.source_intent` shows the original model/research label.
- `evidence.intent_mapping` shows the mapping entry used for the response.
- `evidence.nearest_negative_examples` is present when the contrastive
  negative index is available.

## 2. API Smoke Check

Start the hub API in the normal development environment, then use the same bearer token configured for local API access.

Lookup inspection:

```powershell
Invoke-RestMethod `
  -Headers @{ Authorization = "Bearer $env:ADAOS_TOKEN" } `
  -Uri "http://127.0.0.1:8000/api/nlu/teacher/desktop/lookups"
```

Expected result:

- Response contains `modal_id`, `node_ref`, `app_id`, `scenario_id`, and `webspace_id`.
- Values should come from workspace/packaged manifests, plus live desktop registry overlay when the desktop is running.

Phrase probe:

```powershell
Invoke-RestMethod `
  -Method Post `
  -Headers @{ Authorization = "Bearer $env:ADAOS_TOKEN" } `
  -ContentType "application/json" `
  -Body '{"text":"open apps catalog","use_rasa":true,"emit_trace":true}' `
  -Uri "http://127.0.0.1:8000/api/nlu/teacher/desktop/probe"
```

Expected result:

- `ok=true`.
- `intent`, `confidence`, `slots`, `entities`, and `intent_ranking` are visible when a stage accepts the phrase.
- `stages[]` explains whether regex missed, Rasa accepted, or the phrase fell through to fallback.
- The call does not dispatch desktop actions.

## 3. Trace Verification

After running a real voice/text command or a probe with `emit_trace=true`, inspect the NLU trace projection:

- UI/YJS path: `data.nlu_trace.items[]`
- Expected stage names: `request`, `regex`, `pipeline delegate`, `rasa`, `dispatcher action/reject`

The trace is sufficient for debugging through developer tools today. The missing product slice is a timeline panel inside NLU Teacher.

## 4. Current UI Smoke Check

Open the NLU Teacher modal in the default web desktop scenario.

Expected current behavior:

- The modal has User requests and Candidates tabs.
- Missed NLU requests are grouped by `request_id`.
- Candidate events are grouped by candidate name and request id.
- Raw JSON payloads are visible for inspection.
- Apply actions can emit `nlp.teacher.revision.apply` or `nlp.teacher.candidate.apply`.

Known UI gap:

- There is no Check phrase field yet.
- There is no first-class ranking/entities/trace panel yet.
- Correct/Fix/Save example are not implemented as operator controls yet.

## 5. Teacher UI Acceptance Criteria

The NLU Teacher UI becomes useful for non-developer verification when an operator can complete this loop without terminal access:

1. Enter a phrase.
2. See pipeline trace, intent ranking, entities, slots, lookup matches, confidence, and action preview.
3. Confirm the interpretation as correct or open a guided fix form.
4. Select scenario/skill training target.
5. Preview the diff against the current template/example.
6. Save the approved example or template patch with audit metadata.
7. Re-run the same phrase and see the improved result.

Until those controls exist, API/CLI verification remains the source of truth.
