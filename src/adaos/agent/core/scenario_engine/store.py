from __future__ import annotations
from pathlib import Path
from typing import Dict, Any, Optional, List
import json, shutil
from pydantic import TypeAdapter

from .dsl import Prototype, ImplementationRewrite, validate_rewrite
from adaos.sdk.context import _agent

SCEN_ROOT = _agent.scenarios_dir
TEMPL_ROOT = Path(__file__).resolve().parents[4] / "scenario_templates"  # src/adaos/scenario_templates


def proto_path(sid: str) -> Path:
    return SCEN_ROOT / sid / "scenario.json"


def impl_path(sid: str, user: str) -> Path:
    return SCEN_ROOT / sid / "impl" / user / "scenario.json"


def bindings_path(sid: str, user: str) -> Path:
    return SCEN_ROOT / sid / "bindings" / f"{user}.json"


def list_prototypes() -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    if not SCEN_ROOT.exists():
        return out
    for d in sorted(SCEN_ROOT.iterdir()):
        p = d / "scenario.json"
        if p.exists():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                out.append({"id": data.get("id") or d.name, "version": data.get("version"), "path": str(p)})
            except Exception:
                out.append({"id": d.name, "version": None, "path": str(p)})
    return out


def load_prototype(sid: str) -> Prototype:
    p = proto_path(sid)
    if not p.exists():
        raise FileNotFoundError(sid)
    data = json.loads(p.read_text(encoding="utf-8"))
    return Prototype.model_validate(data)  # <— v2


def load_impl(sid: str, user: str) -> Optional[ImplementationRewrite]:
    p = impl_path(sid, user)
    if not p.exists():
        return None
    data = json.loads(p.read_text(encoding="utf-8"))
    return ImplementationRewrite.model_validate(data)  # <— v2


def save_impl(sid: str, user: str, imp: ImplementationRewrite) -> Path:
    p = impl_path(sid, user)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(imp.model_dump_json(indent=2, ensure_ascii=False), encoding="utf-8")  # <— v2
    return p


def load_bindings(sid: str, user: str) -> Dict[str, Any]:
    p = bindings_path(sid, user)
    if not p.exists():
        return {"slots": {}, "devices": {}, "secrets": {}}
    return json.loads(p.read_text(encoding="utf-8"))


def save_bindings(sid: str, user: str, data: Dict[str, Any]) -> Path:
    p = bindings_path(sid, user)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def apply_rewrite(proto: Prototype, imp: Optional[ImplementationRewrite]) -> Prototype:
    if not imp:
        return proto
    validate_rewrite(proto, imp)
    data = proto.model_dump(by_alias=True)
    # params / io.settings
    if imp.params:
        data["params"] = {**data.get("params", {}), **imp.params}
    if imp.io:
        io = data.setdefault("io", {})
        io["settings"] = {**io.get("settings", {}), **imp.io.get("settings", {})}

    steps = list(data.get("steps", []))
    index = {s.get("id"): i for i, s in enumerate(steps) if s.get("id")}

    def find_idx(step_id: str):
        return index.get(step_id)

    for r in imp.rewrite:
        mid = r.match.get("id")
        if r.action == "drop" and mid:
            idx = find_idx(mid)
            if idx is not None:
                steps.pop(idx)
                index = {s.get("id"): i for i, s in enumerate(steps) if s.get("id")}
        elif r.action in ("move_before", "move_after") and mid and r.ref:
            src, dst = find_idx(mid), find_idx(r.ref)
            if src is not None and dst is not None:
                node = steps.pop(src)
                dst2 = find_idx(r.ref)
                insert_pos = dst2 if r.action == "move_before" else dst2 + 1
                steps.insert(insert_pos, node)
                index = {s.get("id"): i for i, s in enumerate(steps) if s.get("id")}
        elif r.action == "param" and mid and r.set:
            idx = find_idx(mid)
            if idx is not None:
                steps[idx] = {**steps[idx], **r.set}
        elif r.action == "io.set" and r.set:
            io = data.setdefault("io", {})
            io["settings"] = {**io.get("settings", {}), **r.set}
        elif r.action == "cap.narrow":
            pass

    data["steps"] = steps
    return Prototype.model_validate(data)


def install_from_template(sid: str, template: str = "template") -> Path:
    src = TEMPL_ROOT / template / "scenario.json"
    if not src.exists():
        raise FileNotFoundError(f"template not found: {src}")
    dst = proto_path(sid)
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    (dst.parent / "impl").mkdir(exist_ok=True)
    (dst.parent / "bindings").mkdir(exist_ok=True)
    return dst
