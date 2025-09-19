"""Helpers for loading skills from the local filesystem."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import sys
import uuid
from pathlib import Path


def call_fs(args: dict) -> dict:
    """Load and execute a skill handler located on the filesystem.

    Args:
        args: Mapping with keys ``skill_dir``, ``entry``, ``topic`` and ``payload``.
    Returns:
        A dictionary with ``result`` and captured ``stdout``.
    """

    skill_dir = Path(args["skill_dir"]).expanduser().resolve()
    handler_py = skill_dir / "handler.py"
    if not handler_py.exists():
        return {"error": f"handler.py not found in {skill_dir}"}

    entrypoint = args.get("entry", "handle")
    topic = args.get("topic", "")
    payload = args.get("payload") or {}

    module_name = f"_adaos_skill_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(module_name, handler_py)
    if spec is None or spec.loader is None:
        return {"error": f"failed to load spec for {handler_py}"}

    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    sys.path.insert(0, str(skill_dir))

    skill_ctx = None
    prev_skill = None
    try:
        from adaos.services.agent_context import get_ctx

        try:
            ctx = get_ctx()
        except RuntimeError:
            ctx = None
        if ctx is not None:
            skill_ctx = ctx.skill_ctx
            prev_skill = skill_ctx.get()
            skill_ctx.set(skill_dir.name, skill_dir)
    except Exception:
        skill_ctx = None
        prev_skill = None
    try:
        spec.loader.exec_module(module)  # type: ignore[call-arg]
        try:
            fn = getattr(module, entrypoint)
        except AttributeError:
            return {"error": f"entry '{entrypoint}' not found in {handler_py}"}

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            result = fn(topic, payload)
        stdout = buf.getvalue().strip()
        return {"result": result, "stdout": stdout}
    finally:
        sys.modules.pop(module_name, None)
        try:
            sys.path.remove(str(skill_dir))
        except ValueError:
            pass
        if skill_ctx is not None:
            if prev_skill is None:
                skill_ctx.clear()
            else:
                try:
                    skill_ctx.set(prev_skill.name, prev_skill.path)
                except Exception:
                    skill_ctx.clear()


__all__ = ["call_fs"]
