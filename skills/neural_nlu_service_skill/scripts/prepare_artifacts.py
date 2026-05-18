from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

_SKILL_ROOT = Path(__file__).resolve().parents[1]
if str(_SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(_SKILL_ROOT))

from handlers.upstream_detector_port import Config, _iter_jsonl, mask_entities

_CFG = Config()


def _default_out_dir() -> Path:
    base_dir = os.getenv("ADAOS_BASE_DIR", "").strip()
    if base_dir:
        return Path(base_dir).expanduser().resolve() / "state" / "nlu" / "neural"
    return Path.home() / ".adaos" / "state" / "nlu" / "neural"


def _first_existing(candidates: Iterable[Path]) -> Path | None:
    for path in candidates:
        if path.exists():
            return path
    return None


def _json_write(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _source_candidates(source_root: Path) -> tuple[Path | None, Path | None, Path | None]:
    model = _first_existing(
        [
            source_root / "best_model.pt",
            source_root / "best_model (1).pt",
        ]
    )
    train = _first_existing(
        [
            source_root / "lbd_train_augmented.jsonl",
            source_root / "lbd_train_augmented (3).jsonl",
        ]
    )
    dev = _first_existing(
        [
            source_root / "lbd_dev_augmented.jsonl",
            source_root / "lbd_dev_augmented (3).jsonl",
        ]
    )
    return model, train, dev


def _load_rows(path: Path | None, *, split: str) -> list[dict[str, Any]]:
    if path is None:
        return []
    rows: list[dict[str, Any]] = []
    for item in _iter_jsonl(path):
        skill = str(item.get("skill") or item.get("intent") or "").strip()
        text = str(item.get("text") or "").strip()
        if not skill or not text:
            continue
        rows.append({"skill": skill, "text": text, "split": split, "masked": mask_entities(text).masked})
    return rows


def _build_vocab(rows: list[dict[str, Any]]) -> list[str]:
    charset: set[str] = set()
    for row in rows:
        charset.update(str(row.get("masked") or ""))
    return list(_CFG.SPECIALS) + sorted(charset)


def _model_id(model_path: Path, rows: list[dict[str, Any]], explicit: str | None) -> str:
    if explicit and explicit.strip():
        return explicit.strip()
    digest = hashlib.sha256()
    digest.update(_sha256(model_path).encode("ascii"))
    for row in rows:
        digest.update(str(row.get("skill") or "").encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(row.get("masked") or "").encode("utf-8"))
        digest.update(b"\0")
    return f"notebook-{digest.hexdigest()[:12]}"


def prepare_artifacts(
    *,
    source_root: Path,
    out_dir: Path,
    model_path: Path | None = None,
    train_path: Path | None = None,
    dev_path: Path | None = None,
    model_id: str | None = None,
) -> dict[str, Any]:
    source_root = source_root.expanduser().resolve()
    out_dir = out_dir.expanduser().resolve()
    default_model, default_train, default_dev = _source_candidates(source_root)
    model = (model_path or default_model)
    train = train_path or default_train
    dev = dev_path or default_dev
    if model is None or not model.exists():
        raise FileNotFoundError(f"model checkpoint not found under {source_root}")
    if train is None or not train.exists():
        raise FileNotFoundError(f"train jsonl not found under {source_root}")
    if dev is None or not dev.exists():
        raise FileNotFoundError(f"dev jsonl not found under {source_root}")

    train_rows = _load_rows(train, split="train")
    dev_rows = _load_rows(dev, split="dev")
    rows = train_rows + dev_rows
    if not rows:
        raise ValueError("no usable examples found in train/dev jsonl")

    labels = sorted({str(row["skill"]) for row in rows})
    vocab = _build_vocab(rows)
    counts = Counter(str(row["skill"]) for row in rows)
    resolved_model_id = _model_id(model, rows, model_id)

    out_dir.mkdir(parents=True, exist_ok=True)
    model_target = out_dir / "model.pt"
    if model.resolve() != model_target.resolve():
        shutil.copy2(model, model_target)
    _json_write(out_dir / "labels.json", labels)
    _json_write(out_dir / "vocab.json", vocab)
    _json_write(
        out_dir / "intents_manifest.json",
        {
            "schema_version": 1,
            "labels": labels,
            "intents": [
                {"id": label, "label": label, "examples": int(counts.get(label) or 0)}
                for label in labels
            ],
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
        },
    )

    examples_path = out_dir / "examples_manifest.jsonl"
    with examples_path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")

    metrics = {
        "schema_version": 1,
        "created_at": time.time(),
        "model_id": resolved_model_id,
        "source_root": str(source_root),
        "source_model": str(model.resolve()),
        "source_train": str(train.resolve()),
        "source_dev": str(dev.resolve()),
        "model_sha256": _sha256(model),
        "examples_total": len(rows),
        "train_examples": len(train_rows),
        "dev_examples": len(dev_rows),
        "labels_total": len(labels),
        "vocab_size": len(vocab),
        "label_counts": {label: int(counts.get(label) or 0) for label in labels},
        "artifact_files": [
            "model.pt",
            "labels.json",
            "vocab.json",
            "intents_manifest.json",
            "examples_manifest.jsonl",
            "ranker_config.json",
            "metrics.json",
        ],
    }
    _json_write(out_dir / "metrics.json", metrics)
    return metrics


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Prepare Neural NLU active model artifacts from notebook outputs.")
    parser.add_argument("--source-root", type=Path, default=Path("example"), help="Directory containing best_model*.pt and lbd_*.jsonl.")
    parser.add_argument("--out-dir", type=Path, default=_default_out_dir(), help="Active artifact output directory.")
    parser.add_argument("--model", type=Path, default=None, help="Explicit model checkpoint path.")
    parser.add_argument("--train", type=Path, default=None, help="Explicit train jsonl path.")
    parser.add_argument("--dev", type=Path, default=None, help="Explicit dev jsonl path.")
    parser.add_argument("--model-id", default=None, help="Immutable model id to write into metrics.json.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    metrics = prepare_artifacts(
        source_root=args.source_root,
        out_dir=args.out_dir,
        model_path=args.model,
        train_path=args.train,
        dev_path=args.dev,
        model_id=args.model_id,
    )
    print(json.dumps({"ok": True, "out_dir": str(Path(args.out_dir).expanduser().resolve()), "metrics": metrics}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
