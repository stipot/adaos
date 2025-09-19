"""Utilities for executing local skill code from the AdaOS services layer.

The functions defined here are thin abstractions that encapsulate the
implementation previously living inside the CLI commands.  Moving them to the
service level makes them reusable from other entry points (tests, HTTP API,
etc.) while keeping the CLI focused on argument parsing and formatting the
output for the user.
"""

from __future__ import annotations

import asyncio
import importlib.util
from inspect import isawaitable
from pathlib import Path
from typing import Any, Mapping, Optional

from adaos.services.agent_context import AgentContext, get_ctx
from adaos.services.skill.context import SkillContextService

_MANIFEST_NAMES = ("skill.yaml", "manifest.yaml", "adaos.skill.yaml")


class SkillRuntimeError(RuntimeError):
    """Base error for problems while interacting with skill code."""


class SkillDirectoryNotFoundError(SkillRuntimeError):
    """Raised when a skill directory cannot be located on disk."""


class SkillDirectoryAmbiguousError(SkillRuntimeError):
    """Raised when multiple directories match the requested skill name."""


class SkillHandlerError(SkillRuntimeError):
    """Base class for handler-related problems."""


class SkillHandlerNotFoundError(SkillHandlerError):
    """Raised when ``handlers/main.py`` is missing."""


class SkillHandlerImportError(SkillHandlerError):
    """Raised when the handler module cannot be imported."""


class SkillHandlerMissingFunctionError(SkillHandlerError):
    """Raised when the handler module does not expose ``handle``."""


class SkillPrepError(SkillRuntimeError):
    """Raised when the preparation stage cannot be executed."""


class SkillPrepScriptNotFoundError(SkillPrepError):
    """Raised when ``prep/prepare.py`` is missing."""


class SkillPrepImportError(SkillPrepError):
    """Raised when the preparation script cannot be imported."""


class SkillPrepMissingFunctionError(SkillPrepError):
    """Raised when ``run_prep`` is not defined in ``prepare.py``."""


def find_skill_dir(skill_name: str, *, ctx: Optional[AgentContext] = None) -> Path:
    """Locate the directory with the skill sources inside ``skills_root``.

    The lookup is performed in two stages:

    1. Direct lookup by ``<skills_root>/<skill_name>``
    2. Fallback search for ``<skills_root>/**/<skill_name>`` that contains one
       of the known manifest files.

    Args:
        skill_name: Identifier of the skill (normally matches the directory
            name inside the monorepo checkout).
        ctx: Optional context override.  If omitted the global agent context is
            used via :func:`~adaos.services.agent_context.get_ctx`.

    Returns:
        ``Path`` pointing to the directory with the skill sources.

    Raises:
        SkillDirectoryNotFoundError: if no directory can be located.
        SkillDirectoryAmbiguousError: if more than one candidate is found.
    """

    agent_ctx = ctx or get_ctx()
    skills_root = Path(agent_ctx.paths.skills_dir())

    direct = skills_root / skill_name
    if direct.is_dir():
        return direct

    matches = []
    for path in skills_root.rglob("*"):
        if path.is_file() and path.name in _MANIFEST_NAMES and path.parent.name == skill_name:
            matches.append(path.parent)

    if not matches:
        raise SkillDirectoryNotFoundError(
            f"Skill '{skill_name}' was not found under {skills_root}"
        )

    if len(matches) > 1:
        found = "\n - " + "\n - ".join(str(match) for match in matches)
        raise SkillDirectoryAmbiguousError(
            f"Multiple directories match skill '{skill_name}':{found}\n"
            "Please disambiguate by renaming duplicates or specifying a path."
        )

    return matches[0]


def _load_handler(handler_file: Path):
    spec = importlib.util.spec_from_file_location("adaos_skill_handler", handler_file)
    if spec is None or spec.loader is None:
        raise SkillHandlerImportError(f"Failed to import handler from {handler_file}")

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[attr-defined]

    handle_fn = getattr(module, "handle", None)
    if handle_fn is None:
        raise SkillHandlerMissingFunctionError(
            "Handler module does not define handle(topic, payload)"
        )

    return handle_fn


async def run_skill_handler(
    skill_name: str,
    topic: str,
    payload: Mapping[str, Any],
    *,
    ctx: Optional[AgentContext] = None,
) -> Any:
    """Execute the ``handle`` function of a skill handler.

    Args:
        skill_name: Name of the skill to execute.
        topic: Event topic/intention passed to the handler.
        payload: JSON-like mapping that represents the payload.
        ctx: Optional context override.

    Returns:
        Whatever value the handler returns.

    Raises:
        SkillDirectoryNotFoundError: If the skill cannot be located.
        SkillDirectoryAmbiguousError: If multiple directories match the skill.
        SkillHandlerImportError: If the handler file is missing or invalid.
    """

    agent_ctx = ctx or get_ctx()
    skill_dir = find_skill_dir(skill_name, ctx=agent_ctx)
    handler_path = skill_dir / "handlers" / "main.py"

    if not handler_path.is_file():
        raise SkillHandlerNotFoundError(f"Handler file not found: {handler_path}")

    handle_fn = _load_handler(handler_path)

    skill_ctx_port = agent_ctx.skill_ctx
    previous = skill_ctx_port.get()
    if not skill_ctx_port.set(skill_name, skill_dir):
        raise SkillRuntimeError(f"failed to establish context for skill '{skill_name}'")
    try:
        result = handle_fn(topic, payload)
        if isawaitable(result):
            result = await result
        return result
    finally:
        if previous is None:
            skill_ctx_port.clear()
        else:
            skill_ctx_port.set(previous.name, previous.path)


def run_skill_handler_sync(
    skill_name: str,
    topic: str,
    payload: Mapping[str, Any],
    *,
    ctx: Optional[AgentContext] = None,
) -> Any:
    """Synchronously execute :func:`run_skill_handler`.

    This helper provides a convenient wrapper that can be used from synchronous
    contexts (like CLI commands).  It automatically manages the event loop by
    delegating to :func:`asyncio.run` when needed.  When executed inside an
    already running loop an explicit ``RuntimeError`` is raised to prevent
    accidental nested event loops.
    """

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(run_skill_handler(skill_name, topic, payload, ctx=ctx))
    raise RuntimeError("run_skill_handler_sync() cannot be used inside an active event loop")


def run_skill_prep(skill_name: str, *, ctx: Optional[AgentContext] = None) -> Mapping[str, Any]:
    """Execute the ``prepare.py`` helper for a given skill.

    Args:
        skill_name: Name of the skill.
        ctx: Optional context override.

    Returns:
        The dictionary returned by ``run_prep`` inside ``prepare.py``.

    Raises:
        SkillPrepError: For any issue related to locating or executing the
            preparation script.
    """

    agent_ctx = ctx or get_ctx()
    SkillContextService(agent_ctx).set_current_skill(skill_name)

    skill_dir = find_skill_dir(skill_name, ctx=agent_ctx)
    prep_script = skill_dir / "prep" / "prepare.py"

    if not prep_script.exists():
        raise SkillPrepScriptNotFoundError(
            f"Preparation script not found for skill '{skill_name}'"
        )

    spec = importlib.util.spec_from_file_location("adaos_skill_prep", prep_script)
    if spec is None or spec.loader is None:
        raise SkillPrepImportError(f"Unable to import preparation script: {prep_script}")

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[attr-defined]

    run_prep = getattr(module, "run_prep", None)
    if run_prep is None:
        raise SkillPrepMissingFunctionError(f"run_prep() is not defined in {prep_script}")

    return run_prep(skill_dir)


__all__ = [
    "SkillRuntimeError",
    "SkillDirectoryNotFoundError",
    "SkillDirectoryAmbiguousError",
    "SkillHandlerError",
    "SkillHandlerNotFoundError",
    "SkillHandlerImportError",
    "SkillHandlerMissingFunctionError",
    "SkillPrepError",
    "SkillPrepScriptNotFoundError",
    "SkillPrepImportError",
    "SkillPrepMissingFunctionError",
    "find_skill_dir",
    "run_skill_handler",
    "run_skill_handler_sync",
    "run_skill_prep",
]
