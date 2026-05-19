# neural_nlu_service_skill

AdaOS Neural NLU service-skill. The hub owns orchestration, confidence policy,
named-entity canonicalization, and fallback routing. This skill owns neural
provider runtime code and model artifacts.

The skill runs in a service-owned Python venv (`runtime.env.mode: venv`) and
declares `torch`, `numpy`, and `faiss-cpu` as skill dependencies so neural
runtime packages stay out of the hub root venv.

## HTTP API

- `GET /health`
- `POST /parse`

`/parse` accepts:

```json
{
  "text": "open weather on kitchen display",
  "webspace_id": "desktop",
  "locale": "en",
  "canonicalized_text": "open weather on {device}",
  "entities": {
    "resolved_entities": []
  }
}
```

It returns the frozen provider contract under both the top-level response and
`result` for compatibility with older bridges:

```json
{
  "ok": true,
  "top_intent": "weather.get",
  "confidence": 0.91,
  "alternatives": [],
  "slots": {},
  "via": "neural",
  "model_id": "node-default",
  "evidence": {
    "canonicalized_text": "open weather on {device}",
    "backend": "charcnn_bilstm"
  },
  "result": {}
}
```

## Artifacts

Preferred node-level layout:

- `<ADAOS_BASE_DIR>/state/nlu/neural/model.pt`
- `<ADAOS_BASE_DIR>/state/nlu/neural/labels.json`
- `<ADAOS_BASE_DIR>/state/nlu/neural/intent_map.json`
- `<ADAOS_BASE_DIR>/state/nlu/neural/intents_manifest.json`
- `<ADAOS_BASE_DIR>/state/nlu/neural/vocab.json`
- `<ADAOS_BASE_DIR>/state/nlu/neural/examples_manifest.jsonl`
- `<ADAOS_BASE_DIR>/state/nlu/neural/faiss.index` (optional lazy positive-example FAISS index)
- `<ADAOS_BASE_DIR>/state/nlu/neural/faiss.index.json` (FAISS index provenance and invalidation metadata)
- `<ADAOS_BASE_DIR>/state/nlu/neural/negative_faiss.index` (optional lazy negative-example FAISS index)
- `<ADAOS_BASE_DIR>/state/nlu/neural/negative_faiss.index.json` (negative index provenance and invalidation metadata)
- `<ADAOS_BASE_DIR>/state/nlu/neural/example_index.pt` (Torch tensor k-NN fallback cache)
- `<ADAOS_BASE_DIR>/state/nlu/neural/negative_example_index.pt` (Torch tensor negative k-NN fallback cache)
- `<ADAOS_BASE_DIR>/state/nlu/neural/ranker_config.json`
- `<ADAOS_BASE_DIR>/state/nlu/neural/metrics.json`

Explicit overrides:

- `ADAOS_NEURAL_MODEL_PATH`
- `ADAOS_NEURAL_LABELS_PATH`
- `ADAOS_NEURAL_INTENT_MAP_PATH`
- `ADAOS_NEURAL_VOCAB_PATH`
- `ADAOS_NEURAL_EXAMPLES_PATHS` (`;` or `,` separated jsonl files)
- `ADAOS_NEURAL_EXAMPLE_INDEX_BACKEND` (`auto`, `faiss`, or `torch`; default `auto`)
- `ADAOS_NEURAL_FAISS_INDEX_PATH`
- `ADAOS_NEURAL_FAISS_INDEX_META_PATH`
- `ADAOS_NEURAL_NEGATIVE_FAISS_INDEX_PATH`
- `ADAOS_NEURAL_NEGATIVE_FAISS_INDEX_META_PATH`
- `ADAOS_NEURAL_NEGATIVE_EXAMPLE_INDEX_PATH`
- `ADAOS_NEURAL_RANKER_CONFIG_PATH`
- `ADAOS_NLU_NEURAL_MODEL_ID`

If `labels.json` or `vocab.json` are absent, the service can derive them from
jsonl example files using the same masking/token order as the training
notebook. That keeps the supplied `best_model.pt` usable after copying the
training/dev jsonl files into the artifact root or pointing the env vars to
them.

To prepare the active node-level layout from the research notebook outputs in
the repository-local `example` directory:

```powershell
.\.venv\Scripts\python.exe skills\neural_nlu_service_skill\scripts\prepare_artifacts.py `
  --source-root example `
  --out-dir .adaos\state\nlu\neural
```

The script copies `best_model*.pt` to `model.pt`, builds `labels.json` and
`vocab.json` with the notebook-compatible special token order, writes
`intent_map.json`, `examples_manifest.jsonl`, `intents_manifest.json`,
`ranker_config.json`, and `metrics.json`. It does not require Torch; Torch is
needed later when the service loads the model for inference.

`intent_map.json` maps model/research labels to AdaOS canonical intents and
optional action ids. The preparation script writes identity mappings by
default, and operators can edit the artifact without retraining the model:

```json
{
  "schema_version": 1,
  "intents": [
    {
      "label": "weather.get",
      "canonical_intent": "desktop.open_weather",
      "action_id": "host.open_weather",
      "target": {"kind": "system_action"}
    }
  ]
}
```

The provider returns the canonical intent in `top_intent` and keeps the model
label under `evidence.source_intent` plus the full mapping under
`evidence.intent_mapping`.

On first successful model load with examples present, the detector writes lazy
positive and negative example indexes. With `faiss` available in the service
venv it writes `faiss.index` / `negative_faiss.index` plus their JSON metadata;
otherwise it writes Torch tensor fallbacks `example_index.pt` and
`negative_example_index.pt`. Subsequent restarts validate the stored model id,
model SHA, example count, and example digest before reusing either index, so
stale notebook outputs are re-embedded automatically. Negative retrieval is
used as a contrastive signal: if the nearest other-intent example is too close
to the accepted example, the detector applies a small configurable confidence
penalty and records the evidence under `evidence.nearest_negative_examples`.
`ranker_config.json:negative_k_multiplier=0` means the negative search scans
the full example index; set it to a positive integer to cap the search at
`faiss_k * negative_k_multiplier`.

To write a golden phrase smoke report for the active artifacts:

```powershell
.adaos\workspace\skills\.runtime\neural_nlu_service_skill\v0.2\venv\Scripts\python.exe `
  skills\neural_nlu_service_skill\scripts\evaluate_golden.py `
  --min-accuracy 0.80
```

By default the report is written to
`<ADAOS_BASE_DIR>/state/nlu/neural/golden_report.json`. A custom JSONL can be
provided with `--cases`; each line should contain `text` and `expected_intent`.
