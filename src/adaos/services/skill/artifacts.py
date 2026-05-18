from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path
from typing import Any, AsyncIterable, Mapping

import anyio

from adaos.services.skill.runtime_env import SkillRuntimeEnvironment


_DEFAULT_MAX_BYTES = 1024 * 1024 * 1024


def skill_upload_max_bytes() -> int:
    raw = str(os.getenv("ADAOS_SKILL_UPLOAD_MAX_BYTES") or "").strip()
    if raw:
        try:
            value = int(raw)
            if value > 0:
                return value
        except ValueError:
            pass
    return _DEFAULT_MAX_BYTES


def _clean_segment(value: str, *, fallback: str) -> str:
    token = str(value or "").strip().replace("\\", "_").replace("/", "_")
    out = []
    for ch in token:
        if ch.isalnum() or ch in {"-", "_", "."}:
            out.append(ch)
        else:
            out.append("_")
    cleaned = "".join(out).strip("._ ")
    return (cleaned or fallback)[:120]


def _safe_skill_name(value: str) -> str:
    token = str(value or "").strip()
    cleaned = _clean_segment(token, fallback="")
    if not cleaned or cleaned != token:
        raise ValueError("invalid skill name")
    return cleaned


def safe_upload_relative_path(filename: str) -> Path:
    parts = [
        _clean_segment(part, fallback="")
        for part in str(filename or "").replace("\\", "/").split("/")
        if part.strip() and part.strip() not in {".", ".."}
    ]
    parts = [part for part in parts if part]
    if not parts:
        parts = ["upload.bin"]
    return Path(*parts)


def skill_upload_dir(skills_root: Path, skill_name: str, *, purpose: str | None = None) -> Path:
    safe_skill_name = _safe_skill_name(skill_name)
    env = SkillRuntimeEnvironment(skills_root=Path(skills_root), skill_name=safe_skill_name)
    base = env.files_dir() / "uploads"
    purpose_token = _clean_segment(str(purpose or "default"), fallback="default")
    return (base / purpose_token).resolve()


def _dedupe_destination(path: Path) -> Path:
    if not path.exists():
        return path
    stamp = time.strftime("%Y%m%d-%H%M%S")
    stem = path.stem or "upload"
    suffix = path.suffix
    for idx in range(1, 1000):
        candidate = path.with_name(f"{stem}-{stamp}-{idx}{suffix}")
        if not candidate.exists():
            return candidate
    return path.with_name(f"{stem}-{stamp}-{os.getpid()}{suffix}")


async def store_skill_upload(
    *,
    skills_root: Path,
    skill_name: str,
    filename: str,
    chunks: AsyncIterable[bytes],
    purpose: str | None = None,
    content_type: str | None = None,
    max_bytes: int | None = None,
) -> dict[str, Any]:
    target_dir = skill_upload_dir(skills_root, skill_name, purpose=purpose)
    relative = safe_upload_relative_path(filename)
    target = (target_dir / relative).resolve()
    if target_dir not in target.parents and target != target_dir:
        raise ValueError("upload path escapes skill file directory")
    target.parent.mkdir(parents=True, exist_ok=True)
    target = _dedupe_destination(target)
    tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    limit = int(max_bytes or skill_upload_max_bytes())
    total = 0
    digest = hashlib.sha256()

    try:
        async with await anyio.open_file(tmp, "wb") as fh:
            async for chunk in chunks:
                if not chunk:
                    continue
                total += len(chunk)
                if limit > 0 and total > limit:
                    raise ValueError(f"upload exceeds max size: {limit} bytes")
                digest.update(chunk)
                await fh.write(chunk)
        os.replace(tmp, target)
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except Exception:
            pass

    purpose_token = _clean_segment(str(purpose or "default"), fallback="default")
    artifact_id = f"skill_file:{skill_name}:{purpose_token}:{digest.hexdigest()[:16]}"
    artifact_ref: dict[str, Any] = {
        "id": artifact_id,
        "artifact_id": artifact_id,
        "kind": "skill_file",
        "skill": skill_name,
        "purpose": purpose_token,
        "name": target.name,
        "relative_path": str(Path("uploads") / purpose_token / relative).replace("\\", "/"),
        "path": str(target),
        "uri": target.as_uri(),
        "local_path": str(target),
        "stored_path": str(target),
        "size_bytes": total,
        "sha256": digest.hexdigest(),
    }
    if content_type:
        artifact_ref["mime"] = str(content_type)

    return {
        "ok": True,
        "artifact_ref": artifact_ref,
        "file": artifact_ref,
    }


def request_upload_metadata(headers: Mapping[str, Any]) -> dict[str, Any]:
    content_type = str(headers.get("content-type") or "").strip()
    content_length = str(headers.get("content-length") or "").strip()
    size_bytes: int | None = None
    if content_length:
        try:
            size_bytes = int(content_length)
        except ValueError:
            size_bytes = None
    return {
        "content_type": content_type or None,
        "content_length": size_bytes,
    }
