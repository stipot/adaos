from __future__ import annotations

import argparse
import hashlib
import json
import random
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable

_SKILL_ROOT = Path(__file__).resolve().parents[1]
if str(_SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(_SKILL_ROOT))

from handlers.upstream_detector_port import Config, NLUEncoder, mask_entities

try:
    import torch
    import torch.nn.functional as F
except Exception:  # pragma: no cover - optional runtime dependency
    torch = None
    F = None

_CFG = Config()


def _json_write(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _iter_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except Exception:
            continue
        if isinstance(item, dict):
            yield item


def _load_rows(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for item in _iter_jsonl(path):
        label = str(item.get("intent") or item.get("skill") or "").strip()
        text = str(item.get("text") or "").strip()
        if not label or not text:
            continue
        key = (label, text)
        if key in seen:
            continue
        seen.add(key)
        row = dict(item)
        row["skill"] = label
        row["intent"] = label
        row["text"] = text
        row["masked"] = mask_entities(text).masked
        rows.append(row)
    if not rows:
        raise ValueError(f"no usable examples in {path}")
    return rows


def _split_rows(rows: list[dict[str, Any]], *, seed: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str, list[str]]:
    explicit_train = [row for row in rows if str(row.get("split") or "train").lower() in {"train", "training"}]
    explicit_dev = [row for row in rows if str(row.get("split") or "").lower() in {"dev", "eval", "validation", "test"}]
    warnings: list[str] = []
    if explicit_dev:
        return explicit_train or [row for row in rows if row not in explicit_dev], explicit_dev, "explicit_split", warnings

    rng = random.Random(seed)
    train: list[dict[str, Any]] = []
    dev: list[dict[str, Any]] = []
    by_label: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_label[str(row["skill"])].append(row)
    for label, items in sorted(by_label.items()):
        shuffled = list(items)
        rng.shuffle(shuffled)
        if len(shuffled) >= 2:
            dev.append(shuffled[0])
            train.extend(shuffled[1:])
        else:
            train.extend(shuffled)
            warnings.append(f"label_without_holdout:{label}")
    if not dev:
        dev = list(train)
        warnings.append("dev_reuses_train")
        return train, dev, "train_reuse", warnings
    return train, dev, "auto_holdout", warnings


def _build_vocab(rows: list[dict[str, Any]]) -> list[str]:
    charset: set[str] = set()
    for row in rows:
        charset.update(str(row.get("masked") or ""))
    return list(_CFG.SPECIALS) + sorted(charset)


def _encode(masked_text: str, stoi: dict[str, int]) -> list[int]:
    ids = [_CFG.BOS_IDX]
    ids.extend(stoi.get(ch, _CFG.UNK_IDX) for ch in masked_text[: _CFG.MAX_LEN - 2])
    ids.append(_CFG.EOS_IDX)
    if len(ids) < _CFG.MAX_LEN:
        ids.extend([_CFG.PAD_IDX] * (_CFG.MAX_LEN - len(ids)))
    return ids[: _CFG.MAX_LEN]


def _batches(rows: list[dict[str, Any]], *, batch_size: int, seed: int) -> Iterable[list[dict[str, Any]]]:
    shuffled = list(rows)
    random.Random(seed).shuffle(shuffled)
    for offset in range(0, len(shuffled), batch_size):
        yield shuffled[offset : offset + batch_size]


def _evaluate(model: Any, rows: list[dict[str, Any]], *, labels: list[str], stoi: dict[str, int]) -> dict[str, Any]:
    label_to_idx = {label: idx for idx, label in enumerate(labels)}
    rows_out: list[dict[str, Any]] = []
    confusion: Counter[tuple[str, str]] = Counter()
    started = time.perf_counter()
    model.eval()
    with torch.no_grad():
        for row in rows:
            x = torch.tensor([_encode(str(row.get("masked") or ""), stoi)], dtype=torch.long)
            logits, _z = model(x)
            probs = torch.softmax(logits[0], dim=-1)
            pred_idx = int(torch.argmax(probs).item())
            pred = labels[pred_idx]
            expected = str(row["skill"])
            confidence = float(probs[pred_idx].item())
            confusion[(expected, pred)] += 1
            rows_out.append(
                {
                    "text": str(row.get("text") or ""),
                    "masked": str(row.get("masked") or ""),
                    "expected_intent": expected,
                    "top_intent": pred,
                    "passed": bool(pred == expected),
                    "confidence": confidence,
                }
            )
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    per_label: dict[str, dict[str, float]] = {}
    for label in labels:
        tp = confusion[(label, label)]
        fp = sum(count for (expected, pred), count in confusion.items() if pred == label and expected != label)
        fn = sum(count for (expected, pred), count in confusion.items() if expected == label and pred != label)
        precision = tp / (tp + fp) if (tp + fp) else 0.0
        recall = tp / (tp + fn) if (tp + fn) else 0.0
        f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0
        per_label[label] = {
            "precision": round(precision, 6),
            "recall": round(recall, 6),
            "f1": round(f1, 6),
            "support": float(sum(count for (expected, _pred), count in confusion.items() if expected == label)),
        }
    total = len(rows_out)
    passed = sum(1 for row in rows_out if row["passed"])
    macro_f1 = sum(item["f1"] for item in per_label.values()) / len(per_label) if per_label else 0.0
    return {
        "total": total,
        "passed": passed,
        "failed": total - passed,
        "accuracy": round((passed / total) if total else 0.0, 6),
        "macro_f1": round(macro_f1, 6),
        "latency_ms_total": round(elapsed_ms, 3),
        "latency_ms_avg": round((elapsed_ms / total) if total else 0.0, 3),
        "per_label": per_label,
        "cases": rows_out,
    }


def _model_id(rows: list[dict[str, Any]], labels: list[str], explicit: str | None) -> str:
    if explicit and explicit.strip():
        return explicit.strip()
    digest = hashlib.sha256()
    for label in labels:
        digest.update(label.encode("utf-8"))
        digest.update(b"\0")
    for row in rows:
        digest.update(str(row.get("skill") or "").encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(row.get("masked") or "").encode("utf-8"))
        digest.update(b"\0")
    return f"curated-{digest.hexdigest()[:12]}"


def train_artifacts(
    *,
    examples_path: Path,
    out_dir: Path,
    model_id: str | None = None,
    epochs: int = 40,
    batch_size: int = 16,
    learning_rate: float = 0.003,
    seed: int = 13,
    min_dev_accuracy: float = 0.0,
    min_macro_f1: float = 0.0,
) -> dict[str, Any]:
    if torch is None or F is None or NLUEncoder is None:
        raise RuntimeError("torch runtime is required for Neural NLU training")
    if epochs <= 0:
        raise ValueError("epochs must be positive")
    examples_path = examples_path.expanduser().resolve()
    out_dir = out_dir.expanduser().resolve()
    rows = _load_rows(examples_path)
    train_rows, dev_rows, split_strategy, warnings = _split_rows(rows, seed=seed)
    labels = sorted({str(row["skill"]) for row in rows})
    label_to_idx = {label: idx for idx, label in enumerate(labels)}
    vocab = _build_vocab(rows)
    stoi = {ch: idx for idx, ch in enumerate(vocab)}
    resolved_model_id = _model_id(rows, labels, model_id)

    random.seed(seed)
    torch.manual_seed(seed)
    model = NLUEncoder(len(vocab), len(labels), _CFG)
    optimizer = torch.optim.AdamW(model.parameters(), lr=float(learning_rate), weight_decay=0.001)
    history: list[dict[str, Any]] = []
    for epoch in range(1, int(epochs) + 1):
        model.train()
        losses: list[float] = []
        for batch in _batches(train_rows, batch_size=max(1, int(batch_size)), seed=seed + epoch):
            x = torch.tensor([_encode(str(row.get("masked") or ""), stoi) for row in batch], dtype=torch.long)
            y = torch.tensor([label_to_idx[str(row["skill"])] for row in batch], dtype=torch.long)
            optimizer.zero_grad()
            logits, _z = model(x)
            loss = F.cross_entropy(logits, y)
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            optimizer.step()
            losses.append(float(loss.detach().cpu().item()))
        if epoch == 1 or epoch == int(epochs) or epoch % max(1, int(epochs) // 5) == 0:
            dev = _evaluate(model, dev_rows, labels=labels, stoi=stoi)
            history.append(
                {
                    "epoch": epoch,
                    "loss": round(sum(losses) / len(losses), 6) if losses else 0.0,
                    "dev_accuracy": dev["accuracy"],
                    "dev_macro_f1": dev["macro_f1"],
                }
            )

    train_report = _evaluate(model, train_rows, labels=labels, stoi=stoi)
    dev_report = _evaluate(model, dev_rows, labels=labels, stoi=stoi)
    gates = {
        "min_dev_accuracy": float(min_dev_accuracy),
        "min_macro_f1": float(min_macro_f1),
        "dev_accuracy": dev_report["accuracy"],
        "dev_macro_f1": dev_report["macro_f1"],
        "passed": bool(dev_report["accuracy"] >= float(min_dev_accuracy) and dev_report["macro_f1"] >= float(min_macro_f1)),
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    model_path = out_dir / "model.pt"
    torch.save(
        {
            "model_state_dict": model.state_dict(),
            "model_id": resolved_model_id,
            "labels": labels,
            "vocab": vocab,
        },
        model_path,
    )
    model_sha = _sha256(model_path)
    counts = Counter(str(row["skill"]) for row in rows)
    _json_write(out_dir / "labels.json", labels)
    _json_write(out_dir / "vocab.json", vocab)
    _json_write(
        out_dir / "intent_map.json",
        {
            "schema_version": 1,
            "intents": [
                {
                    "label": label,
                    "canonical_intent": label,
                    "action_id": None,
                    "target": None,
                }
                for label in labels
            ],
        },
    )
    _json_write(
        out_dir / "intents_manifest.json",
        {
            "schema_version": 1,
            "labels": labels,
            "intents": [{"id": label, "label": label, "examples": int(counts[label])} for label in labels],
        },
    )
    _json_write(
        out_dir / "ranker_config.json",
        {
            "rank_alpha": _CFG.RANK_ALPHA,
            "rank_beta": _CFG.RANK_BETA,
            "rank_gamma": _CFG.RANK_GAMMA,
            "threshold": _CFG.THRESHOLD,
            "faiss_k": _CFG.FAISS_K,
            "negative_k_multiplier": _CFG.NEGATIVE_K_MULTIPLIER,
            "negative_margin_threshold": _CFG.NEGATIVE_MARGIN_THRESHOLD,
            "negative_penalty": _CFG.NEGATIVE_PENALTY,
        },
    )
    with (out_dir / "examples_manifest.jsonl").open("w", encoding="utf-8") as handle:
        for row in rows:
            payload = dict(row)
            payload.setdefault("split", "train")
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")

    report = {
        "schema_version": 1,
        "created_at": time.time(),
        "model_id": resolved_model_id,
        "examples_path": str(examples_path),
        "out_dir": str(out_dir),
        "epochs": int(epochs),
        "batch_size": int(batch_size),
        "learning_rate": float(learning_rate),
        "seed": int(seed),
        "split_strategy": split_strategy,
        "warnings": warnings,
        "labels": labels,
        "examples_total": len(rows),
        "train_examples": len(train_rows),
        "dev_examples": len(dev_rows),
        "history": history,
        "train": train_report,
        "dev": dev_report,
        "gates": gates,
    }
    _json_write(out_dir / "training_report.json", report)
    metrics = {
        "schema_version": 1,
        "created_at": time.time(),
        "model_id": resolved_model_id,
        "source": "curated_training",
        "source_examples": str(examples_path),
        "model_sha256": model_sha,
        "examples_total": len(rows),
        "train_examples": len(train_rows),
        "dev_examples": len(dev_rows),
        "labels_total": len(labels),
        "vocab_size": len(vocab),
        "label_counts": {label: int(counts[label]) for label in labels},
        "dev_accuracy": dev_report["accuracy"],
        "dev_macro_f1": dev_report["macro_f1"],
        "gates": gates,
        "artifact_files": [
            "model.pt",
            "labels.json",
            "vocab.json",
            "intent_map.json",
            "intents_manifest.json",
            "examples_manifest.jsonl",
            "ranker_config.json",
            "metrics.json",
            "training_report.json",
        ],
    }
    _json_write(out_dir / "metrics.json", metrics)
    return {"ok": bool(gates["passed"]), "out_dir": str(out_dir), "metrics": metrics, "report": report}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Train Neural NLU candidate artifacts from examples_manifest.jsonl.")
    parser.add_argument("--examples", type=Path, required=True, help="Input examples_manifest.jsonl.")
    parser.add_argument("--out-dir", type=Path, required=True, help="Candidate artifact output directory.")
    parser.add_argument("--model-id", default=None, help="Immutable model id.")
    parser.add_argument("--epochs", type=int, default=40)
    parser.add_argument("--batch-size", type=int, default=16)
    parser.add_argument("--learning-rate", type=float, default=0.003)
    parser.add_argument("--seed", type=int, default=13)
    parser.add_argument("--min-dev-accuracy", type=float, default=0.0)
    parser.add_argument("--min-macro-f1", type=float, default=0.0)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    result = train_artifacts(
        examples_path=args.examples,
        out_dir=args.out_dir,
        model_id=args.model_id,
        epochs=args.epochs,
        batch_size=args.batch_size,
        learning_rate=args.learning_rate,
        seed=args.seed,
        min_dev_accuracy=args.min_dev_accuracy,
        min_macro_f1=args.min_macro_f1,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
