# neural_nlu_service_skill

AdaOS Neural NLU service-skill. The hub owns orchestration, confidence policy,
named-entity canonicalization, and fallback routing. This skill owns neural
provider runtime code and model artifacts.

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
- `<ADAOS_BASE_DIR>/state/nlu/neural/vocab.json`
- `<ADAOS_BASE_DIR>/state/nlu/neural/examples_manifest.jsonl`
- `<ADAOS_BASE_DIR>/state/nlu/neural/ranker_config.json`
- `<ADAOS_BASE_DIR>/state/nlu/neural/metrics.json`

Explicit overrides:

- `ADAOS_NEURAL_MODEL_PATH`
- `ADAOS_NEURAL_LABELS_PATH`
- `ADAOS_NEURAL_VOCAB_PATH`
- `ADAOS_NEURAL_EXAMPLES_PATHS` (`;` or `,` separated jsonl files)
- `ADAOS_NEURAL_RANKER_CONFIG_PATH`
- `ADAOS_NLU_NEURAL_MODEL_ID`

If `labels.json` or `vocab.json` are absent, the service can derive them from
jsonl example files using the same masking/token order as the training
notebook. That keeps the supplied `best_model.pt` usable after copying the
training/dev jsonl files into the artifact root or pointing the env vars to
them.
