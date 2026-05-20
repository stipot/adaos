# src/adaos/services/interpreter/workspace.py
from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from importlib import resources
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import yaml

from adaos.services.agent_context import AgentContext
from adaos.services.nlu_lookup_tables import collect_desktop_lookup_tables, rasa_lookup_entries, summarize_lookup_tables


def _utc_now() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _utc_filename_stamp() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _hash_payload(payload: Any) -> str:
    data = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


_RASA_ENTITY_RE = re.compile(r"\[([^\]]+)\]\([^)]+\)")


def _plain_training_text(example: str) -> str:
    return _RASA_ENTITY_RE.sub(r"\1", example).strip()


def _coerce_label_list(payload: Any) -> list[str]:
    if isinstance(payload, list):
        return sorted({str(item).strip() for item in payload if str(item).strip()})
    if isinstance(payload, dict):
        labels = payload.get("labels")
        if isinstance(labels, list):
            return _coerce_label_list(labels)
        id2label = payload.get("id2label")
        if isinstance(id2label, dict):
            items = sorted(
                id2label.items(),
                key=lambda kv: (0, int(kv[0])) if str(kv[0]).isdigit() else (1, str(kv[0])),
            )
            return [str(value).strip() for _, value in items if str(value).strip()]
    return []


def _read_json_file(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _read_jsonl_rows(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.exists():
        return rows
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except Exception:
            continue
        if isinstance(item, dict):
            rows.append(item)
    return rows


def _file_sha256(path: Path) -> str | None:
    if not path.exists():
        return None
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except Exception:
        return None
    return digest.hexdigest()


def _unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix
    for idx in range(1, 1000):
        candidate = path.with_name(f"{stem}.{idx}{suffix}")
        if not candidate.exists():
            return candidate
    return path.with_name(f"{stem}.{_utc_filename_stamp()}{suffix}")


def _compact_neural_training_payload(payload: dict[str, Any], *, out_dir: Path) -> dict[str, Any]:
    compact = dict(payload)
    report = compact.get("report")
    if isinstance(report, dict):
        def _summary(section: Any) -> dict[str, Any]:
            data = dict(section) if isinstance(section, dict) else {}
            return {
                key: data.get(key)
                for key in ("total", "passed", "failed", "accuracy", "macro_f1", "latency_ms_avg")
                if key in data
            }

        compact["report"] = {
            key: report.get(key)
            for key in (
                "schema_version",
                "created_at",
                "model_id",
                "epochs",
                "batch_size",
                "learning_rate",
                "seed",
                "split_strategy",
                "warnings",
                "examples_total",
                "train_examples",
                "dev_examples",
                "history",
                "gates",
            )
            if key in report
        }
        compact["report"]["train"] = _summary(report.get("train"))
        compact["report"]["dev"] = _summary(report.get("dev"))
        compact["report_path"] = str(out_dir / "training_report.json")
    return compact


@dataclass(slots=True)
class IntentMapping:
    intent: str
    description: str | None = None
    skill: str | None = None
    tool: str | None = None
    scenario: str | None = None
    examples: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        payload = asdict(self)
        return {k: v for k, v in payload.items() if v not in (None, [], "")}


class InterpreterWorkspace:
    CONFIG_FILENAME = "config.yaml"
    METADATA_FILENAME = "metadata.json"
    DATA_PACKAGE = "adaos.interpreter_data"
    CONFIG_TEMPLATE = "config.default.yml"
    DEFAULT_DATASET = "moodbot"
    SKILL_METADATA_DIR = "interpreter"
    SKILL_METADATA_FILE = "intents.yml"

    def __init__(self, ctx: AgentContext):
        self._ctx = ctx
        state_dir = Path(ctx.paths.state_dir())
        self.root = (state_dir / "interpreter").resolve()
        self.datasets_dir = self.root / "datasets"
        self.config_path = self.root / self.CONFIG_FILENAME
        self.metadata_path = self.root / self.METADATA_FILENAME
        self.project_dir = self.root / "rasa_project"

        self.root.mkdir(parents=True, exist_ok=True)
        self.datasets_dir.mkdir(parents=True, exist_ok=True)
        self.project_dir.mkdir(parents=True, exist_ok=True)
        self._ensure_default_files()

    @property
    def context(self) -> AgentContext:
        return self._ctx

    # ------------------------------------------------------------------ config
    def _ensure_default_files(self) -> None:
        if not self.config_path.exists():
            self._seed_config_template()

        preset = self.get_dataset_preset()
        self.sync_dataset_from_repo(preset, overwrite=False)
        self.generate_dataset_from_skills()

        custom_file = self.datasets_dir / "custom.md"
        if not custom_file.exists():
            custom_file.write_text(
                "# custom dataset\n"
                "- сюда можно добавлять свои примеры\n",
                encoding="utf-8",
            )

    def _seed_config_template(self) -> None:
        try:
            template = resources.files(self.DATA_PACKAGE) / self.CONFIG_TEMPLATE
        except Exception:
            template = None
        if template is None:
            fallback = {
                "dataset": {"preset": self.DEFAULT_DATASET},
                "intents": [],
            }
            self.config_path.write_text(yaml.safe_dump(fallback, allow_unicode=True, sort_keys=False), encoding="utf-8")
            return
        with resources.as_file(template) as src:
            shutil.copy(src, self.config_path)

    def available_presets(self) -> List[str]:
        try:
            base = resources.files(self.DATA_PACKAGE)
        except Exception:
            return []
        return sorted(item.name for item in base.iterdir() if item.is_dir() and not item.name.startswith("__"))

    def get_dataset_preset(self) -> str:
        try:
            data = yaml.safe_load(self.config_path.read_text(encoding="utf-8")) or {}
        except FileNotFoundError:
            return self.DEFAULT_DATASET
        dataset_section = data.get("dataset") or {}
        return dataset_section.get("preset") or self.DEFAULT_DATASET

    def sync_dataset_from_repo(self, preset: str, *, overwrite: bool = False) -> Path:
        try:
            preset_root = resources.files(self.DATA_PACKAGE) / preset
        except Exception as exc:
            raise ValueError(f"dataset preset '{preset}' not found in package") from exc

        dest = self.datasets_dir / preset
        if overwrite and dest.exists():
            shutil.rmtree(dest)
        if dest.exists():
            return dest
        with resources.as_file(preset_root) as src_dir:
            shutil.copytree(src_dir, dest)
        return dest

    # ----------------------------------------------------- skill metadata
    def collect_skill_intents(self) -> List[IntentMapping]:
        """Reads interpreter metadata from installed skills."""
        result: List[IntentMapping] = []
        skills_root = Path(self._ctx.paths.skills_dir())
        try:
            skills = self._ctx.skills_repo.list()
        except Exception:
            skills = []
        for meta in skills:
            skill_path = skills_root / meta.id.value
            meta_dir = skill_path / self.SKILL_METADATA_DIR
            if not meta_dir.exists():
                continue
            for fname in (self.SKILL_METADATA_FILE, "intents.yaml"):
                fpath = meta_dir / fname
                if not fpath.exists():
                    continue
                try:
                    payload = yaml.safe_load(fpath.read_text(encoding="utf-8")) or []
                except Exception:
                    payload = []
                if isinstance(payload, dict) and "intents" in payload:
                    payload = payload["intents"]
                for entry in payload:
                    intent = (entry or {}).get("intent")
                    if not intent:
                        continue
                    examples = entry.get("examples") or []
                    result.append(
                        IntentMapping(
                            intent=intent,
                            description=entry.get("description"),
                            skill=entry.get("skill") or meta.id.value,
                            tool=entry.get("tool"),
                            scenario=entry.get("scenario"),
                            examples=examples,
                        )
                    )
        return result

    def generate_dataset_from_skills(self) -> None:
        """Creates synthetic dataset fragments describing skill metadata."""
        skill_intents = self.collect_skill_intents()
        auto_dir = self.datasets_dir / "skills_auto"
        if not skill_intents:
            if auto_dir.exists():
                shutil.rmtree(auto_dir)
            return
        auto_dir.mkdir(parents=True, exist_ok=True)

        nlu_payload = {"version": "3.1", "nlu": []}
        for mapping in skill_intents:
            if not mapping.examples:
                continue
            formatted = "\n".join(f"- {ex}" for ex in mapping.examples if ex)
            if formatted.strip():
                nlu_payload["nlu"].append({"intent": mapping.intent, "examples": formatted})
        if nlu_payload["nlu"]:
            (auto_dir / "nlu.yml").write_text(
                yaml.safe_dump(nlu_payload, allow_unicode=True, sort_keys=False),
                encoding="utf-8",
            )
        meta_doc = {
            "intents": [
                {
                    "intent": m.intent,
                    "description": m.description,
                    "skill": m.skill,
                    "tool": m.tool,
                    "scenario": m.scenario,
                }
                for m in skill_intents
            ]
        }
        (auto_dir / "meta.yml").write_text(yaml.safe_dump(meta_doc, allow_unicode=True, sort_keys=False), encoding="utf-8")

    def load_config(self) -> Dict[str, Any]:
        try:
            data = yaml.safe_load(self.config_path.read_text(encoding="utf-8")) or {}
        except FileNotFoundError:
            self._ensure_default_files()
            data = yaml.safe_load(self.config_path.read_text(encoding="utf-8")) or {}
        intents = data.get("intents") or []
        data["intents"] = sorted(intents, key=lambda x: x.get("intent", ""))
        return data

    def save_config(self, config: Dict[str, Any]) -> None:
        config = dict(config)
        config["intents"] = sorted(config.get("intents", []), key=lambda x: x.get("intent", ""))
        self.config_path.write_text(yaml.safe_dump(config, allow_unicode=True, sort_keys=False), encoding="utf-8")

    def list_intents(self) -> List[IntentMapping]:
        config = self.load_config()
        mappings: List[IntentMapping] = []
        for entry in config.get("intents", []):
            mappings.append(
                IntentMapping(
                    intent=entry.get("intent", ""),
                    description=entry.get("description"),
                    skill=entry.get("skill"),
                    tool=entry.get("tool"),
                    scenario=entry.get("scenario"),
                    examples=entry.get("examples") or [],
                )
            )
        return mappings

    def upsert_intent(self, mapping: IntentMapping) -> None:
        config = self.load_config()
        intents = config.get("intents", [])
        intents = [item for item in intents if item.get("intent") != mapping.intent]
        intents.append(mapping.as_dict())
        config["intents"] = intents
        self.save_config(config)

    def remove_intent(self, intent: str) -> bool:
        config = self.load_config()
        intents = config.get("intents", [])
        new_intents = [item for item in intents if item.get("intent") != intent]
        if len(new_intents) == len(intents):
            return False
        config["intents"] = new_intents
        self.save_config(config)
        return True

    # -------------------------------------------------------------- metadata IO
    def load_metadata(self) -> Dict[str, Any]:
        if not self.metadata_path.exists():
            return {}
        try:
            return json.loads(self.metadata_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}

    def save_metadata(self, payload: Dict[str, Any]) -> None:
        self.metadata_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    # -------------------------------------------------------------- snapshots
    def _skill_snapshot(self) -> List[Dict[str, Any]]:
        skills = []
        try:
            for meta in self._ctx.skills_repo.list():
                skills.append({"id": meta.id.value, "name": meta.name, "version": meta.version})
        except Exception:
            pass
        return sorted(skills, key=lambda x: x["id"])

    def _dataset_snapshot(self) -> List[Dict[str, Any]]:
        snapshot: List[Dict[str, Any]] = []
        if not self.datasets_dir.exists():
            return snapshot
        for path in sorted(self.datasets_dir.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(self.root).as_posix()
            try:
                data = path.read_bytes()
            except OSError:
                continue
            snapshot.append({"path": rel, "hash": hashlib.sha256(data).hexdigest(), "size": len(data)})
        return snapshot

    def _auto_intents_snapshot(self) -> List[Dict[str, Any]]:
        items = []
        for mapping in self.collect_skill_intents():
            items.append(
                {
                    "intent": mapping.intent,
                    "skill": mapping.skill,
                    "tool": mapping.tool,
                    "scenario": mapping.scenario,
                    "examples_hash": _hash_payload(mapping.examples),
                }
            )
        return sorted(items, key=lambda x: x["intent"])

    def _lookup_snapshot(self) -> List[Dict[str, Any]]:
        try:
            payload = collect_desktop_lookup_tables(self._ctx)
        except Exception:
            payload = {"lookups": {}}
        return summarize_lookup_tables(payload)

    def _config_hash(self, config: Dict[str, Any]) -> str:
        return _hash_payload(config.get("intents", []))

    def _skill_hash(self, snapshot: Iterable[Dict[str, Any]]) -> str:
        return _hash_payload(list(snapshot))

    def _dataset_hash(self, snapshot: Iterable[Dict[str, Any]]) -> str:
        return _hash_payload(list(snapshot))

    def fingerprint(
        self,
        config: Dict[str, Any],
        skills: List[Dict[str, Any]],
        datasets: List[Dict[str, Any]],
        auto_intents: List[Dict[str, Any]],
        lookups: List[Dict[str, Any]],
    ) -> str:
        payload = {
            "intents": config.get("intents", []),
            "skills": skills,
            "datasets": datasets,
            "auto_intents": auto_intents,
            "lookups": lookups,
        }
        return _hash_payload(payload)

    # -------------------------------------------------------------- status info
    def describe_status(self) -> Dict[str, Any]:
        config = self.load_config()
        skills = self._skill_snapshot()
        datasets = self._dataset_snapshot()
        auto_intents = self._auto_intents_snapshot()
        lookups = self._lookup_snapshot()
        current_fp = self.fingerprint(config, skills, datasets, auto_intents, lookups)
        meta = self.load_metadata()
        needs_training = current_fp != meta.get("fingerprint")

        reasons: List[str] = []
        if not meta.get("fingerprint"):
            reasons.append("Модель ещё ни разу не обучалась.")
        else:
            if meta.get("config_hash") != self._config_hash(config):
                reasons.append("Конфигурация интентов изменилась.")
            if meta.get("skill_hash") != self._skill_hash(skills):
                reasons.append("Список скиллов изменился.")
            if meta.get("dataset_hash") != self._dataset_hash(datasets):
                reasons.append("Наборы данных изменились.")
            if meta.get("auto_intents_hash") != _hash_payload(auto_intents):
                reasons.append("Интенты из скиллов обновились.")
            if meta.get("lookup_hash") != _hash_payload(lookups):
                reasons.append("NLU lookup tables changed.")

        return {
            "needs_training": needs_training,
            "reasons": reasons,
            "intent_count": len(config.get("intents", [])),
            "trained_at": meta.get("trained_at"),
            "dataset_summary": self.dataset_summary(),
            "fingerprint": current_fp,
            "skills": skills,
            "auto_intents": auto_intents,
            "lookups": lookups,
        }

    def dataset_summary(self) -> Dict[str, Any]:
        summary = {"files": [], "total_size": 0}
        for entry in self._dataset_snapshot():
            summary["files"].append({"path": entry["path"], "size": entry["size"]})
            summary["total_size"] += entry["size"]
        return summary

    # ----------------------------------------------------- rasa project export
    def build_rasa_project(self) -> Path:
        self.generate_dataset_from_skills()
        config = self.load_config()
        manual = self.list_intents()
        auto = self.collect_skill_intents()
        combined: Dict[str, IntentMapping] = {m.intent: m for m in auto if m.intent}
        for mapping in manual:
            combined[mapping.intent] = mapping
        mappings = list(combined.values())
        dataset_files = self._dataset_snapshot()
        if not mappings and not dataset_files:
            raise RuntimeError("Нет доступных данных для обучения (интенты или датасеты).")

        preset = self.get_dataset_preset()
        self.sync_dataset_from_repo(preset, overwrite=False)

        project = self.project_dir
        data_dir = project / "data"
        packaged_dir = data_dir / "datasets"
        data_dir.mkdir(parents=True, exist_ok=True)
        packaged_dir.mkdir(parents=True, exist_ok=True)

        nlu_payload: Dict[str, Any] = {"version": "3.1", "nlu": []}
        for mapping in mappings:
            if not mapping.examples:
                continue
            formatted = "\n".join(f"- {ex}" for ex in mapping.examples if ex)
            if formatted.strip():
                nlu_payload["nlu"].append({"intent": mapping.intent, "examples": formatted})
        lookup_payload = collect_desktop_lookup_tables(self._ctx)
        for entry in rasa_lookup_entries(lookup_payload):
            nlu_payload["nlu"].append(entry)
        (data_dir / "intents_from_config.yml").write_text(
            yaml.safe_dump(nlu_payload, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
        (data_dir / "lookup_tables.json").write_text(
            json.dumps(lookup_payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        # copy datasets
        for child in self.datasets_dir.iterdir():
            dest = packaged_dir / child.name
            if dest.exists():
                if dest.is_dir():
                    shutil.rmtree(dest)
                else:
                    dest.unlink()
            if child.is_dir():
                shutil.copytree(child, dest)
            else:
                shutil.copy2(child, dest)

        rasa_pipeline = config.get("rasa_pipeline")
        if not isinstance(rasa_pipeline, list) or not rasa_pipeline:
            rasa_pipeline = [
                {"name": "WhitespaceTokenizer"},
                {"name": "RegexFeaturizer"},
                {"name": "LexicalSyntacticFeaturizer"},
                {"name": "CountVectorsFeaturizer"},
                {
                    "name": "CountVectorsFeaturizer",
                    "analyzer": "char_wb",
                    "min_ngram": 3,
                    "max_ngram": 5,
                },
                {"name": "CRFEntityExtractor"},
                {"name": "EntitySynonymMapper"},
                {"name": "LogisticRegressionClassifier", "max_iter": 200},
            ]

        config_target = project / "config.yml"
        config_target.write_text(
            yaml.safe_dump(
                {
                    "language": str(config.get("language") or "ru"),
                    "pipeline": rasa_pipeline,
                },
                allow_unicode=True,
                sort_keys=False,
            ),
            encoding="utf-8",
        )

        readme = project / "README.txt"
        readme.write_text(
            "Этот каталог формируется автоматически из данных репозитория и state/interpreter.\n",
            encoding="utf-8",
        )
        return project

    # ----------------------------------------------------- neural data export
    def export_neural_training_data(self) -> Dict[str, Any]:
        """
        Export curated interpreter examples as a Neural NLU training bundle.

        The bundle is deliberately separate from the active provider artifacts
        under ``state/nlu/neural``. It gives rebuild/reindex tooling a stable
        source of skill/scenario/system-owned examples without mutating the
        running model.
        """
        self.generate_dataset_from_skills()
        manual = self.list_intents()
        auto = self.collect_skill_intents()
        combined: Dict[str, IntentMapping] = {m.intent: m for m in auto if m.intent}
        for mapping in manual:
            combined[mapping.intent] = mapping

        out_dir = self.root / "neural_training"
        out_dir.mkdir(parents=True, exist_ok=True)

        rows: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for mapping in sorted(combined.values(), key=lambda item: item.intent):
            if not mapping.intent:
                continue
            owner: dict[str, Any]
            if mapping.skill:
                owner = {"type": "skill", "id": mapping.skill}
            elif mapping.scenario == "system":
                action_id = None
                try:
                    from adaos.services.nlu.system_actions_catalog import find_system_action_by_intent

                    action = find_system_action_by_intent(mapping.intent)
                    if isinstance(action, dict) and isinstance(action.get("id"), str):
                        action_id = action["id"]
                except Exception:
                    action_id = None
                owner = {"type": "system_action", **({"id": action_id} if action_id else {})}
            elif mapping.scenario:
                owner = {"type": "scenario", "id": mapping.scenario}
            else:
                owner = {"type": "interpreter"}
            for raw_example in mapping.examples:
                if not isinstance(raw_example, str) or not raw_example.strip():
                    continue
                text = _plain_training_text(raw_example)
                if not text:
                    continue
                key = (mapping.intent, text)
                if key in seen:
                    continue
                seen.add(key)
                rows.append(
                    {
                        "schema_version": 1,
                        "intent": mapping.intent,
                        "text": text,
                        "raw_example": raw_example.strip(),
                        "split": "train",
                        "owner": owner,
                        "source": "interpreter_workspace",
                        "example_id": "nlu." + hashlib.sha1(f"{mapping.intent}\0{text}".encode("utf-8")).hexdigest()[:16],
                    }
                )

        examples_path = out_dir / "examples_manifest.jsonl"
        with examples_path.open("w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")

        intent_counts: dict[str, int] = {}
        owners_by_intent: dict[str, dict[str, Any]] = {}
        for row in rows:
            intent = str(row.get("intent") or "")
            if not intent:
                continue
            intent_counts[intent] = intent_counts.get(intent, 0) + 1
            owner = row.get("owner")
            if isinstance(owner, dict) and intent not in owners_by_intent:
                owners_by_intent[intent] = owner
        labels = sorted(intent_counts)
        labels_path = out_dir / "labels.json"
        labels_path.write_text(json.dumps(labels, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

        intents_manifest = {
            "schema_version": 1,
            "labels": labels,
            "intents": [
                {
                    "id": intent,
                    "label": intent,
                    "examples": int(intent_counts[intent]),
                    "owner": owners_by_intent.get(intent) or {"type": "interpreter"},
                }
                for intent in labels
            ],
        }
        intents_path = out_dir / "intents_manifest.json"
        intents_path.write_text(json.dumps(intents_manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")

        summary = {
            "ok": True,
            "out_dir": str(out_dir),
            "examples_path": str(examples_path),
            "labels_path": str(labels_path),
            "intents_manifest_path": str(intents_path),
            "examples_total": len(rows),
            "intents_total": len(labels),
            "owners": {
                "skill": sum(1 for row in rows if (row.get("owner") or {}).get("type") == "skill"),
                "scenario": sum(1 for row in rows if (row.get("owner") or {}).get("type") == "scenario"),
                "system_action": sum(1 for row in rows if (row.get("owner") or {}).get("type") == "system_action"),
                "interpreter": sum(1 for row in rows if (row.get("owner") or {}).get("type") == "interpreter"),
            },
        }
        (out_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return summary

    def _neural_artifact_root(self) -> Path:
        return (Path(self._ctx.paths.state_dir()) / "nlu" / "neural").resolve()

    def plan_neural_curated_reindex(self, *, export: bool = True) -> Dict[str, Any]:
        """
        Compare the curated Neural training bundle with the active provider.

        Reindexing can safely refresh example indexes only when all curated
        labels already exist in the active model head. New labels need a full
        model rebuild/retrain before the active examples can be replaced.
        """
        export_summary = self.export_neural_training_data() if export else None
        bundle_dir = self.root / "neural_training"
        curated_examples_path = bundle_dir / "examples_manifest.jsonl"
        curated_labels_path = bundle_dir / "labels.json"
        active_root = self._neural_artifact_root()
        active_examples_path = active_root / "examples_manifest.jsonl"
        active_labels_path = active_root / "labels.json"
        active_model_path = active_root / "model.pt"

        curated_rows = _read_jsonl_rows(curated_examples_path)
        active_rows = _read_jsonl_rows(active_examples_path)
        curated_labels = _coerce_label_list(_read_json_file(curated_labels_path))
        if not curated_labels:
            curated_labels = sorted({str(row.get("intent") or row.get("skill") or "").strip() for row in curated_rows if str(row.get("intent") or row.get("skill") or "").strip()})
        active_labels = _coerce_label_list(_read_json_file(active_labels_path))
        active_example_labels = sorted({str(row.get("intent") or row.get("skill") or "").strip() for row in active_rows if str(row.get("intent") or row.get("skill") or "").strip()})

        missing_labels = [label for label in curated_labels if label not in active_labels]
        active_only_labels = [label for label in active_labels if label not in curated_labels]
        warnings: list[str] = []
        if not active_model_path.exists():
            warnings.append("active_model_missing")
        if not active_labels:
            warnings.append("active_labels_missing")
        if missing_labels:
            warnings.append("curated_labels_not_in_active_model")
        if active_only_labels:
            warnings.append("active_labels_without_curated_examples")
        if not curated_examples_path.exists() or not curated_rows:
            warnings.append("curated_examples_missing")

        compatible = bool(active_labels) and not missing_labels
        apply_allowed = bool(active_model_path.exists() and curated_rows and compatible)
        curated_digest = _file_sha256(curated_examples_path)
        active_digest = _file_sha256(active_examples_path)
        return {
            "ok": True,
            "schema_version": 1,
            "mode": "curated_neural_reindex_plan",
            "export": export_summary,
            "bundle_dir": str(bundle_dir),
            "active_root": str(active_root),
            "curated": {
                "examples_path": str(curated_examples_path),
                "labels_path": str(curated_labels_path),
                "examples_total": len(curated_rows),
                "labels_total": len(curated_labels),
                "examples_sha256": curated_digest,
            },
            "active": {
                "examples_path": str(active_examples_path),
                "labels_path": str(active_labels_path),
                "model_path": str(active_model_path),
                "model_exists": active_model_path.exists(),
                "examples_total": len(active_rows),
                "labels_total": len(active_labels),
                "examples_sha256": active_digest,
            },
            "changes": {
                "examples_delta": len(curated_rows) - len(active_rows),
                "new_labels": missing_labels,
                "active_only_labels": active_only_labels,
                "active_example_labels": active_example_labels,
                "examples_manifest_unchanged": bool(curated_digest and active_digest and curated_digest == active_digest),
            },
            "compatible_for_active_model": compatible,
            "apply_allowed": apply_allowed,
            "warnings": warnings,
        }

    def apply_neural_curated_reindex(self, *, plan: Dict[str, Any] | None = None) -> Dict[str, Any]:
        plan = plan if isinstance(plan, dict) else self.plan_neural_curated_reindex(export=True)
        if not bool(plan.get("apply_allowed")):
            return {
                "ok": False,
                "reason": "curated_bundle_incompatible_with_active_model",
                "plan": plan,
            }

        active_root = Path(str(plan["active"]["model_path"])).parent
        active_root.mkdir(parents=True, exist_ok=True)
        curated_examples_path = Path(str(plan["curated"]["examples_path"]))
        active_examples_path = active_root / "examples_manifest.jsonl"

        backup_path: Path | None = None
        if active_examples_path.exists():
            rollback_dir = active_root / "rollback"
            rollback_dir.mkdir(parents=True, exist_ok=True)
            backup_path = _unique_path(rollback_dir / f"examples_manifest.{_utc_filename_stamp()}.jsonl")
            shutil.copy2(active_examples_path, backup_path)

        shutil.copy2(curated_examples_path, active_examples_path)
        removed_indexes: list[str] = []
        for name in (
            "faiss.index",
            "faiss.index.json",
            "negative_faiss.index",
            "negative_faiss.index.json",
            "example_index.pt",
            "negative_example_index.pt",
        ):
            path = active_root / name
            if path.exists():
                try:
                    path.unlink()
                    removed_indexes.append(str(path))
                except Exception:
                    pass

        summary = {
            "ok": True,
            "applied_at": _utc_now(),
            "source_examples_path": str(curated_examples_path),
            "active_examples_path": str(active_examples_path),
            "backup_examples_path": str(backup_path) if backup_path else None,
            "removed_indexes": removed_indexes,
            "plan": plan,
        }
        (active_root / "curated_reindex.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return summary

    def _neural_train_script_path(self) -> Path:
        repo_root = Path(__file__).resolve().parents[4]
        source_script = repo_root / "skills" / "neural_nlu_service_skill" / "scripts" / "train_artifacts.py"
        if source_script.exists():
            return source_script.resolve()
        workspace_script = Path(self._ctx.paths.skills_dir()) / "neural_nlu_service_skill" / "scripts" / "train_artifacts.py"
        return workspace_script.resolve()

    def _neural_train_python(self) -> Path:
        skills_root = Path(self._ctx.paths.skills_dir())
        runtime_root = skills_root / ".runtime" / "neural_nlu_service_skill"
        candidates: list[Path] = []
        if runtime_root.exists():
            candidates.extend(sorted(runtime_root.glob("v*/venv/Scripts/python.exe"), reverse=True))
            candidates.extend(sorted(runtime_root.glob("v*/venv/bin/python"), reverse=True))
        for candidate in candidates:
            if candidate.exists():
                return candidate.resolve()
        return Path(sys.executable).resolve()

    def rebuild_neural_candidate_from_examples(
        self,
        *,
        examples_path: Path,
        candidate_dir: Path | None = None,
        model_id: str | None = None,
        epochs: int = 40,
        batch_size: int = 16,
        learning_rate: float = 0.003,
        seed: int = 13,
        min_dev_accuracy: float = 0.0,
        min_macro_f1: float = 0.0,
    ) -> Dict[str, Any]:
        candidate_root = self.root / "neural_candidates"
        out_dir = candidate_dir or _unique_path(candidate_root / f"candidate.{_utc_filename_stamp()}")
        out_dir = out_dir.expanduser().resolve()
        script = self._neural_train_script_path()
        python = self._neural_train_python()
        cmd = [
            str(python),
            str(script),
            "--examples",
            str(examples_path.expanduser().resolve()),
            "--out-dir",
            str(out_dir),
            "--epochs",
            str(int(epochs)),
            "--batch-size",
            str(int(batch_size)),
            "--learning-rate",
            str(float(learning_rate)),
            "--seed",
            str(int(seed)),
            "--min-dev-accuracy",
            str(float(min_dev_accuracy)),
            "--min-macro-f1",
            str(float(min_macro_f1)),
        ]
        if model_id:
            cmd.extend(["--model-id", model_id])
        proc = subprocess.run(cmd, cwd=str(Path(__file__).resolve().parents[4]), text=True, capture_output=True)
        stdout = proc.stdout.strip()
        payload: dict[str, Any]
        try:
            payload = json.loads(stdout) if stdout else {}
        except Exception:
            payload = {}
        compact_payload = _compact_neural_training_payload(payload, out_dir=out_dir) if payload else {}
        result = {
            "ok": bool(proc.returncode == 0 and payload.get("ok") is True),
            "returncode": int(proc.returncode),
            "candidate_dir": str(out_dir),
            "python": str(python),
            "script": str(script),
            "command": cmd,
            "stdout": "" if payload else stdout,
            "stderr": proc.stderr.strip(),
            "result": compact_payload,
        }
        if proc.returncode != 0 and not result["stderr"]:
            result["stderr"] = "neural training failed quality gates or exited non-zero"
        return result

    def promote_neural_candidate(
        self,
        *,
        candidate_dir: Path,
        reason: str | None = None,
    ) -> Dict[str, Any]:
        candidate_dir = candidate_dir.expanduser().resolve()
        required = [
            "model.pt",
            "labels.json",
            "vocab.json",
            "examples_manifest.jsonl",
            "ranker_config.json",
            "metrics.json",
        ]
        missing = [name for name in required if not (candidate_dir / name).exists()]
        if missing:
            return {"ok": False, "reason": "candidate_missing_required_artifacts", "missing": missing, "candidate_dir": str(candidate_dir)}

        active_root = self._neural_artifact_root()
        active_root.mkdir(parents=True, exist_ok=True)
        rollback_root = active_root / "rollback"
        rollback_root.mkdir(parents=True, exist_ok=True)
        backup_dir = _unique_path(rollback_root / f"model.{_utc_filename_stamp()}")
        backup_dir.mkdir(parents=True, exist_ok=True)
        backed_up: list[str] = []
        for child in active_root.iterdir():
            if child.name == "rollback":
                continue
            dest = backup_dir / child.name
            try:
                if child.is_dir():
                    shutil.copytree(child, dest)
                else:
                    shutil.copy2(child, dest)
                backed_up.append(child.name)
            except Exception:
                continue

        promoted_files: list[str] = []
        for name in [
            "model.pt",
            "labels.json",
            "vocab.json",
            "intent_map.json",
            "intents_manifest.json",
            "examples_manifest.jsonl",
            "ranker_config.json",
            "metrics.json",
            "training_report.json",
            "golden_report.json",
        ]:
            source = candidate_dir / name
            if source.exists():
                shutil.copy2(source, active_root / name)
                promoted_files.append(name)

        removed_indexes: list[str] = []
        for name in (
            "faiss.index",
            "faiss.index.json",
            "negative_faiss.index",
            "negative_faiss.index.json",
            "example_index.pt",
            "negative_example_index.pt",
        ):
            path = active_root / name
            if path.exists():
                try:
                    path.unlink()
                    removed_indexes.append(name)
                except Exception:
                    pass

        metrics = _read_json_file(active_root / "metrics.json")
        model_id = metrics.get("model_id") if isinstance(metrics, dict) else None
        pointer = {
            "schema_version": 1,
            "promoted_at": _utc_now(),
            "candidate_dir": str(candidate_dir),
            "active_root": str(active_root),
            "rollback_dir": str(backup_dir),
            "reason": reason,
            "model_id": model_id,
            "promoted_files": promoted_files,
            "backed_up_files": backed_up,
            "removed_indexes": removed_indexes,
        }
        (active_root / "active_model.json").write_text(json.dumps(pointer, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        (rollback_root / "latest.json").write_text(json.dumps(pointer, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return {"ok": True, **pointer}

    # ---------------------------------------------------------------- training
    def record_training(self, *, note: str | None = None, extra: Dict[str, Any] | None = None) -> Dict[str, Any]:
        config = self.load_config()
        skills = self._skill_snapshot()
        datasets = self._dataset_snapshot()
        auto_intents = self._auto_intents_snapshot()
        lookups = self._lookup_snapshot()
        meta = {
            "trained_at": _utc_now(),
            "fingerprint": self.fingerprint(config, skills, datasets, auto_intents, lookups),
            "config_hash": self._config_hash(config),
            "skill_hash": self._skill_hash(skills),
            "dataset_hash": self._dataset_hash(datasets),
            "auto_intents_hash": _hash_payload(auto_intents),
            "lookup_hash": _hash_payload(lookups),
            "lookup_summary": lookups,
            "skills_snapshot": skills,
            "intent_count": len(config.get("intents", [])),
            "dataset_summary": self.dataset_summary(),
        }
        if note:
            meta["note"] = note
        if extra:
            meta["extra"] = extra
        self.save_metadata(meta)
        return meta
