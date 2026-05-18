"""
Runtime port of the neural intent detector research notebook.

The implementation intentionally keeps Torch/FAISS optional at import time so
the service can start and abstain cleanly on nodes where the neural runtime
dependencies or artifacts have not been prepared yet.
"""

from __future__ import annotations

import json
import os
import re
import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

try:
    import torch
    import torch.nn as nn
    import torch.nn.functional as F
except Exception:  # pragma: no cover - optional runtime dependency
    torch = None
    nn = None
    F = None


@dataclass
class Config:
    PAD_IDX: int = 0
    UNK_IDX: int = 1
    BOS_IDX: int = 2
    EOS_IDX: int = 3
    SPECIALS: tuple[str, ...] = ("<pad>", "<unk>", "<bos>", "<eos>")
    EMB_DIM: int = 96
    CNN_CHANNELS: int = 128
    LSTM_HIDDEN: int = 128
    PROJ_DIM: int = 128
    MAX_LEN: int = 128
    RANK_ALPHA: float = 0.4
    RANK_BETA: float = 0.4
    RANK_GAMMA: float = 0.1
    THRESHOLD: float = 0.4
    FAISS_K: int = 5


@dataclass
class MaskResult:
    original: str
    masked: str
    slots: dict[str, list[str]] = field(default_factory=dict)


@dataclass(frozen=True)
class ExampleEntry:
    skill: str
    text: str
    masked: str


if nn is not None:

    class NLUEncoder(nn.Module):  # type: ignore[misc]
        """Char-CNN + BiLSTM encoder from the upstream notebook."""

        def __init__(self, vocab_size: int, num_labels: int, cfg: Config):
            super().__init__()
            self.emb = nn.Embedding(vocab_size, cfg.EMB_DIM, padding_idx=cfg.PAD_IDX)
            self.conv = nn.Conv1d(cfg.EMB_DIM, cfg.CNN_CHANNELS, kernel_size=5, padding=2)
            self.pool = nn.AdaptiveMaxPool1d(64)
            self.bi_lstm = nn.LSTM(cfg.CNN_CHANNELS, cfg.LSTM_HIDDEN, batch_first=True, bidirectional=True)
            self.proj = nn.Linear(2 * cfg.LSTM_HIDDEN, cfg.PROJ_DIM)
            self.cls = nn.Linear(2 * cfg.LSTM_HIDDEN, num_labels)
            self.dropout = nn.Dropout(0.1)

        def forward(self, x):
            e = self.emb(x).transpose(1, 2)
            c = F.relu(self.conv(e))
            c = self.pool(c).transpose(1, 2)
            out, _ = self.bi_lstm(c)
            feat = self.dropout(out.mean(dim=1))
            logits = self.cls(feat)
            z = F.normalize(self.proj(feat), dim=-1)
            return logits, z
else:
    NLUEncoder = None  # type: ignore[assignment]


TIME_WORDS = r"(сегодня|завтра|послезавтра|утром|дн[её]м|вечером|ночью)"
MONTHS = r"(январ[ьяе]|феврал[ьяе]|март[ае]?|апрел[ьяе]|ма[ея]|июн[ьяе]|июл[ьяе]|август[ае]?|сентябр[ьяе]|октябр[ьяе]|ноябр[ьяе]|декабр[ьяе])"
UNITS = r"(секунд(?:а|ы)?|минут(?:а|ы)?|час(?:а|ов)?|дн(?:я|ей|ь))"
CITY_LIST = ("москва", "санкт-петербург", "казань", "берлин", "лондон", "париж", "екатеринбург", "новосибирск")
CITY_PATTERNS = [(city, re.compile(re.escape(city)[:-1] + r"[а-яё]*", flags=re.I)) for city in CITY_LIST]

DEFAULT_SKILL_WEIGHTS = {
    "system.help": 0.3,
}

EXCLUSION_KEYWORDS = {
    "music.play": ("будильник", "таймер", "напоминание", "alarm"),
    "timer.start": ("будильник",),
    "music.stop": ("будильник", "таймер"),
    "time.now": ("погода", "будильник", "напомни"),
}


def _canon(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "").strip())


def _merge_spans(spans: list[tuple[int, int, str, str]]) -> list[tuple[int, int, str, str]]:
    merged: list[tuple[int, int, str, str]] = []
    for span in sorted(spans, key=lambda x: (x[0], x[1])):
        if not merged or span[0] >= merged[-1][1]:
            merged.append(span)
        elif (span[1] - span[0]) > (merged[-1][1] - merged[-1][0]):
            merged[-1] = span
    return merged


def mask_entities(text: str) -> MaskResult:
    raw = str(text or "")
    spans: list[tuple[int, int, str, str]] = []
    protected = [(m.start(), m.end()) for m in re.finditer(r"\{[a-z_]+\}", raw)]

    def is_protected(a: int, b: int) -> bool:
        return any(not (b <= x or a >= y) for x, y in protected)

    for m in re.finditer(r"\b([01]?\d|2[0-3]):[0-5]\d\b", raw):
        if not is_protected(m.start(), m.end()):
            spans.append((m.start(), m.end(), "{time}", "time"))
    for m in re.finditer(rf"\b(?:{TIME_WORDS}\s+)?[0-3]?\d[./-][01]?\d(?:[./-]\d{{2,4}})?\b", raw, flags=re.I):
        if not is_protected(m.start(), m.end()):
            spans.append((m.start(), m.end(), "{date}", "date"))
    for m in re.finditer(rf"\b\d+\s+{UNITS}\b", raw, flags=re.I):
        if not is_protected(m.start(), m.end()):
            spans.append((m.start(), m.end(), "{duration}", "duration"))
    for m in re.finditer(rf"\b\d{{1,2}}\s+{MONTHS}\b", raw, flags=re.I):
        if not is_protected(m.start(), m.end()):
            spans.append((m.start(), m.end(), "{date}", "date"))
    for m in re.finditer(r"(?<!\w)\d+(?:[.,]\d+)?(?!\w)", raw):
        a, b = m.start(), m.end()
        if not any(a < y and b > x for x, y, _, _ in spans) and not is_protected(a, b):
            spans.append((a, b, "{number}", "number"))

    music_m = re.search(r"(включи|воспроизведи|сыграй|поставь)\s+(.+)", raw, flags=re.I)
    if music_m and not is_protected(music_m.start(2), music_m.end(2)):
        tail = music_m.group(2).lower()
        if not any(keyword in tail for keyword in ("будильник", "таймер")):
            spans.append((music_m.start(2), music_m.end(2), "{song}", "song"))

    for _, pattern in CITY_PATTERNS:
        for m in pattern.finditer(raw):
            if not is_protected(m.start(), m.end()):
                spans.append((m.start(), m.end(), "{city}", "city"))

    slots: dict[str, list[str]] = {}
    out = list(raw)
    for a, b, placeholder, slot in sorted(_merge_spans(spans), key=lambda item: -item[0]):
        value = raw[a:b]
        slots.setdefault(slot, []).append(value.strip())
        slots.setdefault(f"{slot}_canon", []).append(_canon(value))
        out[a:b] = list(placeholder)
    return MaskResult(original=raw, masked=_canon("".join(out)), slots=slots)


def _json_load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _iter_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except Exception:
            continue
        if isinstance(item, dict):
            yield item


def _coerce_labels(payload: Any) -> list[str]:
    if isinstance(payload, list):
        return [str(item) for item in payload if str(item).strip()]
    if isinstance(payload, dict):
        if isinstance(payload.get("labels"), list):
            return _coerce_labels(payload.get("labels"))
        if isinstance(payload.get("id2label"), dict):
            items = sorted(payload["id2label"].items(), key=lambda kv: int(kv[0]))
            return [str(v) for _, v in items]
    return []


def _coerce_vocab(payload: Any) -> list[str]:
    if isinstance(payload, list):
        return [str(item) for item in payload]
    if isinstance(payload, dict):
        if isinstance(payload.get("vocab"), list):
            return _coerce_vocab(payload.get("vocab"))
        if isinstance(payload.get("itos"), list):
            return _coerce_vocab(payload.get("itos"))
        if isinstance(payload.get("stoi"), dict):
            return [ch for ch, _ in sorted(payload["stoi"].items(), key=lambda kv: int(kv[1]))]
    return []


def _split_paths(raw: str) -> list[Path]:
    if not raw.strip():
        return []
    out: list[Path] = []
    for part in re.split(r"[;,]", raw):
        token = part.strip().strip('"').strip("'")
        if token:
            out.append(Path(token).expanduser())
    return out


class Detector:
    def __init__(self) -> None:
        self._cfg = self._load_ranker_config()
        self._adapter = self._load_adapter()
        self._engine = self._load_neural_engine()

    @staticmethod
    def _artifacts_root() -> Path:
        base_dir = os.getenv("ADAOS_BASE_DIR", "").strip()
        if base_dir:
            return Path(base_dir).expanduser().resolve() / "state" / "nlu" / "neural"
        return Path.home() / ".adaos" / "state" / "nlu" / "neural"

    def _load_ranker_config(self) -> Config:
        cfg = Config()
        root = self._artifacts_root()
        path = Path(os.getenv("ADAOS_NEURAL_RANKER_CONFIG_PATH", "").strip() or root / "ranker_config.json")
        if not path.exists():
            return cfg
        try:
            data = _json_load(path)
        except Exception:
            return cfg
        if not isinstance(data, dict):
            return cfg
        for key in ("RANK_ALPHA", "RANK_BETA", "RANK_GAMMA", "THRESHOLD", "FAISS_K"):
            value = data.get(key) or data.get(key.lower())
            if isinstance(value, (int, float)):
                setattr(cfg, key, type(getattr(cfg, key))(value))
        return cfg

    def _load_adapter(self):
        token = os.getenv("ADAOS_NEURAL_ADAPTER", "").strip()
        if not token or ":" not in token:
            return None
        module_name, func_name = token.split(":", 1)
        module_name = module_name.strip()
        func_name = func_name.strip()
        if not module_name or not func_name:
            return None
        try:
            mod = __import__(module_name, fromlist=[func_name])
            fn = getattr(mod, func_name, None)
            if callable(fn):
                return fn
        except Exception:
            return None
        return None

    def _example_paths(self, root: Path) -> list[Path]:
        explicit = _split_paths(os.getenv("ADAOS_NEURAL_EXAMPLES_PATHS", ""))
        for key in ("ADAOS_NEURAL_TRAIN_PATH", "ADAOS_NEURAL_DEV_PATH"):
            token = os.getenv(key, "").strip()
            if token:
                explicit.append(Path(token).expanduser())
        if explicit:
            return explicit
        return [
            root / "examples_manifest.jsonl",
            root / "lbd_train_augmented.jsonl",
            root / "lbd_dev_augmented.jsonl",
        ]

    def _load_examples(self, paths: list[Path]) -> list[ExampleEntry]:
        examples: list[ExampleEntry] = []
        seen: set[tuple[str, str]] = set()
        for path in paths:
            for item in _iter_jsonl(path):
                skill = str(item.get("skill") or item.get("intent") or "").strip()
                text = str(item.get("text") or "").strip()
                if not skill or not text:
                    continue
                key = (skill, text)
                if key in seen:
                    continue
                seen.add(key)
                examples.append(ExampleEntry(skill=skill, text=text, masked=mask_entities(text).masked))
        return examples

    def _load_labels_vocab(self, *, root: Path, examples: list[ExampleEntry]) -> tuple[list[str], list[str]]:
        labels_path = Path(os.getenv("ADAOS_NEURAL_LABELS_PATH", "").strip() or root / "labels.json")
        vocab_path = Path(os.getenv("ADAOS_NEURAL_VOCAB_PATH", "").strip() or root / "vocab.json")
        labels: list[str] = []
        vocab: list[str] = []
        if labels_path.exists():
            try:
                labels = _coerce_labels(_json_load(labels_path))
            except Exception:
                labels = []
        if vocab_path.exists():
            try:
                vocab = _coerce_vocab(_json_load(vocab_path))
            except Exception:
                vocab = []
        if examples:
            if not labels:
                labels = sorted({entry.skill for entry in examples})
            if not vocab:
                charset: set[str] = set()
                for entry in examples:
                    charset.update(entry.masked)
                vocab = list(self._cfg.SPECIALS) + sorted(charset)
        return labels, vocab

    def _load_state_dict(self, path: Path) -> tuple[Any, dict[str, Any]]:
        state = torch.load(str(path), map_location="cpu")
        metadata: dict[str, Any] = {}
        if isinstance(state, dict):
            for key in ("model_id", "labels", "vocab", "config", "metrics"):
                if key in state:
                    metadata[key] = state[key]
            for key in ("model_state_dict", "state_dict", "model"):
                if isinstance(state.get(key), dict):
                    return state[key], metadata
        return state, metadata

    def _load_model_id(self, *, root: Path, metadata: dict[str, Any], model_path: Path) -> str:
        explicit = os.getenv("ADAOS_NLU_NEURAL_MODEL_ID", "").strip()
        if explicit:
            return explicit
        token = str(metadata.get("model_id") or "").strip()
        if token:
            return token
        metrics_path = root / "metrics.json"
        if metrics_path.exists():
            try:
                metrics = _json_load(metrics_path)
                if isinstance(metrics, dict):
                    token = str(metrics.get("model_id") or "").strip()
                    if token:
                        return token
            except Exception:
                pass
        return str(model_path.stem or "node-default")

    def _load_model_sha256(self, *, root: Path, metadata: dict[str, Any]) -> str:
        token = str(metadata.get("model_sha256") or "").strip()
        if token:
            return token
        metrics_path = root / "metrics.json"
        if metrics_path.exists():
            try:
                metrics = _json_load(metrics_path)
                if isinstance(metrics, dict):
                    token = str(metrics.get("model_sha256") or "").strip()
                    if token:
                        return token
            except Exception:
                pass
        return ""

    def _load_neural_engine(self):
        if torch is None or nn is None or F is None or NLUEncoder is None:
            return None
        root = self._artifacts_root()
        model_path = Path(os.getenv("ADAOS_NEURAL_MODEL_PATH", "").strip() or root / "model.pt")
        if not model_path.exists():
            return None
        examples = self._load_examples(self._example_paths(root))
        labels, vocab = self._load_labels_vocab(root=root, examples=examples)
        try:
            state, metadata = self._load_state_dict(model_path)
            if not labels:
                labels = _coerce_labels(metadata.get("labels"))
            if not vocab:
                vocab = _coerce_vocab(metadata.get("vocab"))
            if not labels or not vocab:
                return None
            model = NLUEncoder(len(vocab), len(labels), self._cfg)
            model.load_state_dict(state)
            model.eval()
            model_id = self._load_model_id(root=root, metadata=metadata, model_path=model_path)
            engine = {
                "model": model,
                "labels": labels,
                "stoi": {str(ch): idx for idx, ch in enumerate(vocab)},
                "examples": examples,
                "example_vectors": None,
                "example_index_source": None,
                "model_id": model_id,
                "model_sha256": self._load_model_sha256(root=root, metadata=metadata),
                "artifact_root": str(root),
            }
            if examples and os.getenv("ADAOS_NEURAL_DISABLE_EXAMPLE_INDEX", "0").strip().lower() not in {"1", "true", "yes", "on"}:
                vectors, source = self._load_example_index(root=root, model_id=model_id, engine=engine)
                if vectors is None:
                    vectors = self._embed_examples(model, engine["stoi"], examples)
                    source = "built" if vectors is not None else None
                    if vectors is not None:
                        self._save_example_index(root=root, model_id=model_id, engine=engine, vectors=vectors)
                engine["example_vectors"] = vectors
                engine["example_index_source"] = source
            return engine
        except Exception:
            return None

    def _example_index_path(self, root: Path) -> Path:
        token = os.getenv("ADAOS_NEURAL_EXAMPLE_INDEX_PATH", "").strip()
        return Path(token).expanduser().resolve() if token else root / "example_index.pt"

    @staticmethod
    def _examples_digest(examples: list[ExampleEntry]) -> str:
        digest = hashlib.sha256()
        for entry in examples:
            digest.update(entry.skill.encode("utf-8"))
            digest.update(b"\0")
            digest.update(entry.masked.encode("utf-8"))
            digest.update(b"\0")
            digest.update(entry.text.encode("utf-8"))
            digest.update(b"\0")
        return digest.hexdigest()

    def _load_example_index(self, *, root: Path, model_id: str, engine: dict[str, Any]) -> tuple[Any | None, str | None]:
        path = self._example_index_path(root)
        examples: list[ExampleEntry] = engine.get("examples") or []
        if not path.exists() or not examples:
            return None, None
        try:
            payload = torch.load(str(path), map_location="cpu")
        except Exception:
            return None, None
        if not isinstance(payload, dict):
            return None, None
        vectors = payload.get("vectors")
        if vectors is None or not hasattr(vectors, "shape"):
            return None, None
        if int(payload.get("example_count") or -1) != len(examples):
            return None, None
        if str(payload.get("examples_digest") or "") != self._examples_digest(examples):
            return None, None
        stored_model_id = str(payload.get("model_id") or "").strip()
        if stored_model_id and stored_model_id != str(model_id):
            return None, None
        stored_sha = str(payload.get("model_sha256") or "").strip()
        current_sha = str(engine.get("model_sha256") or "").strip()
        if stored_sha and current_sha and stored_sha != current_sha:
            return None, None
        return vectors.cpu(), "disk"

    def _save_example_index(self, *, root: Path, model_id: str, engine: dict[str, Any], vectors: Any) -> None:
        if os.getenv("ADAOS_NEURAL_SAVE_EXAMPLE_INDEX", "1").strip().lower() in {"0", "false", "no", "off"}:
            return
        examples: list[ExampleEntry] = engine.get("examples") or []
        if vectors is None or not examples:
            return
        path = self._example_index_path(root)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            torch.save(
                {
                    "schema_version": 1,
                    "backend": "torch_tensor",
                    "model_id": str(model_id),
                    "model_sha256": str(engine.get("model_sha256") or ""),
                    "example_count": len(examples),
                    "examples_digest": self._examples_digest(examples),
                    "vectors": vectors.cpu(),
                },
                str(path),
            )
        except Exception:
            pass

    def _encode_masked(self, masked_text: str, stoi: dict[str, int]) -> list[int]:
        ids = [self._cfg.BOS_IDX]
        ids.extend(stoi.get(ch, self._cfg.UNK_IDX) for ch in masked_text[: self._cfg.MAX_LEN - 2])
        ids.append(self._cfg.EOS_IDX)
        if len(ids) < self._cfg.MAX_LEN:
            ids.extend([self._cfg.PAD_IDX] * (self._cfg.MAX_LEN - len(ids)))
        return ids[: self._cfg.MAX_LEN]

    def _embed_examples(self, model: Any, stoi: dict[str, int], examples: list[ExampleEntry]):
        vectors = []
        batch_size = 128
        with torch.no_grad():
            for offset in range(0, len(examples), batch_size):
                batch = examples[offset : offset + batch_size]
                ids = [self._encode_masked(entry.masked, stoi) for entry in batch]
                x = torch.tensor(ids, dtype=torch.long)
                _logits, z = model(x)
                vectors.append(z.cpu())
        if not vectors:
            return None
        return torch.cat(vectors, dim=0)

    def _score_with_skill_weights(self, probs: list[float], labels: list[str]) -> list[float]:
        raw = os.getenv("ADAOS_NEURAL_SKILL_WEIGHTS", "").strip()
        weights_obj: dict[str, Any] = {}
        if raw:
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    weights_obj = parsed
            except Exception:
                weights_obj = {}
        weights = [float(weights_obj.get(label, DEFAULT_SKILL_WEIGHTS.get(label, 0.5))) for label in labels]
        scaled = [p * max(w, 0.0) for p, w in zip(probs, weights)]
        total = float(sum(scaled))
        if total <= 0:
            return probs
        return [v / total for v in scaled]

    def _nearest_examples(self, q_vec: Any, engine: dict[str, Any], *, query: str, clf_skill: str, clf_prob: float) -> list[dict[str, Any]]:
        vectors = engine.get("example_vectors")
        examples: list[ExampleEntry] = engine.get("examples") or []
        if vectors is None or not examples:
            return []
        try:
            sims = torch.matmul(vectors, q_vec.cpu()[0])
            k = min(max(int(self._cfg.FAISS_K), 1), len(examples))
            values, indexes = torch.topk(sims, k=k)
        except Exception:
            return []
        candidates: list[dict[str, Any]] = []
        lowered = query.lower()
        for sim, idx in zip(values.tolist(), indexes.tolist()):
            entry = examples[int(idx)]
            if any(keyword in lowered for keyword in EXCLUSION_KEYWORDS.get(entry.skill, ())):
                continue
            is_clf_match = clf_skill == entry.skill
            prob_feature = clf_prob if is_clf_match else max(0.0, 1.0 - clf_prob)
            score = (
                self._cfg.RANK_ALPHA * float(sim)
                + self._cfg.RANK_BETA * float(prob_feature)
                + self._cfg.RANK_GAMMA * float(DEFAULT_SKILL_WEIGHTS.get(entry.skill, 0.5))
            )
            candidates.append(
                {
                    "intent": entry.skill,
                    "confidence": float(max(0.0, min(1.0, score))),
                    "similarity": float(sim),
                    "matched_example": entry.masked,
                    "raw_example": entry.text,
                }
            )
        candidates.sort(key=lambda item: float(item.get("confidence") or 0.0), reverse=True)
        return candidates

    @staticmethod
    def _flatten_slots(slots: dict[str, list[str]]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key, values in slots.items():
            if not values:
                continue
            out[key] = values[0] if len(values) == 1 else list(values)
        return out

    def _neural_detect(self, *, text: str, model_text: str, entity_resolution: dict[str, Any] | None) -> dict[str, Any] | None:
        engine = self._engine
        if not isinstance(engine, dict) or torch is None:
            return None
        model = engine["model"]
        labels = engine["labels"]
        stoi = engine["stoi"]
        mask = mask_entities(model_text)
        try:
            ids = self._encode_masked(mask.masked, stoi)
            x = torch.tensor([ids], dtype=torch.long)
            with torch.no_grad():
                logits, z = model(x)
                probs = [float(v) for v in torch.softmax(logits[0], dim=-1).cpu().tolist()]
            weighted = self._score_with_skill_weights(probs, labels)
            order = sorted(range(len(weighted)), key=lambda i: weighted[i], reverse=True)
            clf_idx = int(order[0])
            clf_skill = str(labels[clf_idx])
            clf_prob = float(weighted[clf_idx])
            ranked_examples = self._nearest_examples(z, engine, query=text, clf_skill=clf_skill, clf_prob=clf_prob)
            if ranked_examples and ranked_examples[0]["confidence"] >= max(0.0, float(self._cfg.THRESHOLD)):
                top_intent = str(ranked_examples[0]["intent"])
                confidence = float(ranked_examples[0]["confidence"])
            else:
                top_intent = clf_skill
                confidence = clf_prob
            alternatives = [
                {"intent": str(labels[i]), "confidence": float(weighted[i])}
                for i in order
                if str(labels[i]) != top_intent
            ][:4]
            return {
                "top_intent": top_intent,
                "confidence": float(confidence),
                "alternatives": alternatives,
                "slots": self._flatten_slots(mask.slots),
                "via": "neural",
                "model_id": str(engine.get("model_id") or "node-default"),
                "evidence": {
                    "backend": "charcnn_bilstm",
                    "ranker": "embedding_knn" if ranked_examples else "softmax",
                    "example_index": str(engine.get("example_index_source") or "none"),
                    "softmax": clf_prob,
                    "canonicalized_text": model_text,
                    "masked_text": mask.masked,
                    "matched_examples": [item.get("matched_example") for item in ranked_examples[:3]],
                    "score_components": {
                        "rank_alpha": self._cfg.RANK_ALPHA,
                        "rank_beta": self._cfg.RANK_BETA,
                        "rank_gamma": self._cfg.RANK_GAMMA,
                    },
                    "entity_resolution": entity_resolution or {},
                },
            }
        except Exception:
            return None

    def detect(
        self,
        text: str,
        *,
        webspace_id: str | None = None,
        locale: str | None = None,
        canonicalized_text: str | None = None,
        entity_resolution: Any = None,
    ) -> dict[str, Any]:
        model_text = str(canonicalized_text or "").strip() or text
        entity_payload = entity_resolution if isinstance(entity_resolution, dict) else {}
        if self._adapter is not None:
            try:
                out = self._adapter(
                    text=text,
                    webspace_id=webspace_id,
                    locale=locale,
                    canonicalized_text=model_text,
                    entities=entity_payload,
                )
                if isinstance(out, dict):
                    return self._normalize_adapter_result(out, model_text=model_text, entity_resolution=entity_payload)
            except Exception:
                pass
        neural = self._neural_detect(text=text, model_text=model_text, entity_resolution=entity_payload)
        if isinstance(neural, dict):
            return neural
        return self._abstain(text=text, model_text=model_text, entity_resolution=entity_payload)

    def _normalize_adapter_result(self, out: dict[str, Any], *, model_text: str, entity_resolution: dict[str, Any]) -> dict[str, Any]:
        result = dict(out)
        result.setdefault("top_intent", result.get("intent") or "")
        result.setdefault("confidence", 0.0)
        result.setdefault("alternatives", [])
        result.setdefault("slots", {})
        result.setdefault("via", "neural")
        result.setdefault("model_id", os.getenv("ADAOS_NLU_NEURAL_MODEL_ID", "adapter"))
        evidence = result.get("evidence") if isinstance(result.get("evidence"), dict) else {}
        evidence.setdefault("backend", "adapter")
        evidence.setdefault("canonicalized_text", model_text)
        evidence.setdefault("entity_resolution", entity_resolution)
        result["evidence"] = evidence
        return result

    def _abstain(self, *, text: str, model_text: str, entity_resolution: dict[str, Any]) -> dict[str, Any]:
        mask = mask_entities(model_text)
        reason = "torch_unavailable" if torch is None else "model_artifacts_unavailable"
        return {
            "top_intent": "",
            "confidence": 0.0,
            "alternatives": [],
            "slots": self._flatten_slots(mask.slots),
            "via": "neural",
            "model_id": os.getenv("ADAOS_NLU_NEURAL_MODEL_ID", "unavailable"),
            "evidence": {
                "backend": "abstain",
                "reason": reason,
                "canonicalized_text": model_text,
                "masked_text": mask.masked,
                "entity_resolution": entity_resolution,
            },
        }

    def health(self) -> dict[str, Any]:
        engine = self._engine if isinstance(self._engine, dict) else {}
        return {
            "ok": True,
            "service": "neural_nlu_service_skill",
            "version": "0.2.5",
            "torch_available": torch is not None,
            "model_loaded": bool(engine),
            "model_id": engine.get("model_id") if engine else None,
            "examples_total": len(engine.get("examples") or []) if engine else 0,
            "example_index": engine.get("example_index_source") if engine else None,
            "artifact_root": str(self._artifacts_root()),
        }
