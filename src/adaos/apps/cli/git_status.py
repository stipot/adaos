from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
import subprocess
import os
import base64
import zipfile
import io


@dataclass(frozen=True, slots=True)
class GitCommitInfo:
    sha: str
    timestamp: int
    subject: str

    @property
    def iso(self) -> str:
        try:
            return datetime.fromtimestamp(self.timestamp, tz=timezone.utc).isoformat()
        except Exception:
            return ""


@dataclass(slots=True)
class GitPathStatus:
    path: str
    exists: bool
    dirty: bool
    base_ref: Optional[str] = None
    changed_vs_base: Optional[bool] = None
    ahead_count: Optional[int] = None
    behind_count: Optional[int] = None
    base_last_commit: Optional[GitCommitInfo] = None
    local_last_commit: Optional[GitCommitInfo] = None
    error: Optional[str] = None


def _run_git(workdir: Path, args: list[str], *, timeout_s: float = 10.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(workdir), *args],
        text=True,
        capture_output=True,
        timeout=timeout_s,
    )


def _git_ok(proc: subprocess.CompletedProcess[str]) -> bool:
    return proc.returncode == 0


def resolve_base_ref(workdir: Path, *, remote: str = "origin") -> Optional[str]:
    """
    Best-effort resolution of a remote-tracking base ref:
      1) refs/remotes/<remote>/HEAD symbolic-ref (e.g. origin/main)
      2) @{u} if configured
      3) <remote>/main or <remote>/master if present
    """
    proc = _run_git(workdir, ["symbolic-ref", "-q", "--short", f"refs/remotes/{remote}/HEAD"])
    if _git_ok(proc):
        ref = (proc.stdout or "").strip()
        if ref:
            return ref

    proc = _run_git(workdir, ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"])
    if _git_ok(proc):
        ref = (proc.stdout or "").strip()
        if ref:
            return ref

    for candidate in (f"{remote}/main", f"{remote}/master"):
        proc = _run_git(workdir, ["rev-parse", "--verify", "--quiet", candidate])
        if _git_ok(proc):
            return candidate

    return None


def ref_exists(workdir: Path, ref: str) -> bool:
    proc = _run_git(workdir, ["rev-parse", "--verify", "--quiet", ref])
    return _git_ok(proc)


def current_branch(workdir: Path) -> Optional[str]:
    proc = _run_git(workdir, ["rev-parse", "--abbrev-ref", "HEAD"])
    if not _git_ok(proc):
        return None
    token = (proc.stdout or "").strip()
    if not token or token == "HEAD":
        return None
    return token


def list_changed_paths(workdir: Path, *, base_ref: str, path: str | None = None) -> list[str]:
    args = ["diff", "--name-only", f"{base_ref}...HEAD"]
    if path:
        args.extend(["--", path])
    proc = _run_git(workdir, args)
    if not _git_ok(proc):
        return []
    return [line.strip() for line in (proc.stdout or "").splitlines() if line.strip()]


def ensure_remote(workdir: Path, *, name: str, url: str) -> Optional[str]:
    """
    Ensure a remote exists and points to the requested URL. Returns error string on failure.
    """
    url = (url or "").strip()
    if not url:
        return "remote url is empty"
    proc = _run_git(workdir, ["remote", "get-url", name])
    if _git_ok(proc):
        cur = (proc.stdout or "").strip()
        if cur != url:
            proc2 = _run_git(workdir, ["remote", "set-url", name, url])
            if not _git_ok(proc2):
                return (proc2.stderr or proc2.stdout or "").strip() or f"failed to set remote {name}"
        return None
    proc3 = _run_git(workdir, ["remote", "add", name, url])
    if _git_ok(proc3):
        return None
    return (proc3.stderr or proc3.stdout or "").strip() or f"failed to add remote {name}"


def ensure_repo_at(path: Path, *, url: str, remote: str = "origin") -> Optional[str]:
    """
    Ensure `path` is a git repo cloned from `url` and up to date.
    """
    path.mkdir(parents=True, exist_ok=True)
    git_dir = path / ".git"
    if not git_dir.exists():
        parent = path.parent
        parent.mkdir(parents=True, exist_ok=True)
        proc = _run_git(parent, ["clone", url, str(path.name)], timeout_s=120.0)
        if not _git_ok(proc):
            return (proc.stderr or proc.stdout or "").strip() or "git clone failed"
    err = ensure_remote(path, name=remote, url=url)
    if err:
        return err
    proc2 = _run_git(path, ["fetch", "--prune", remote], timeout_s=120.0)
    if not _git_ok(proc2):
        return (proc2.stderr or proc2.stdout or "").strip() or "git fetch failed"
    proc3 = _run_git(path, ["pull", "--ff-only", remote], timeout_s=120.0)
    if not _git_ok(proc3):
        # Non-ff is fine: keep repo as-is, status can still compare refs.
        return None
    return None


def fetch_remote(workdir: Path, *, remote: str = "origin") -> Optional[str]:
    proc = _run_git(workdir, ["fetch", "--prune", remote], timeout_s=60.0)
    if _git_ok(proc):
        return None
    err = (proc.stderr or proc.stdout or "").strip()
    return err or f"git fetch {remote} failed"


def read_last_commit(workdir: Path, *, rev: str, path: str) -> Optional[GitCommitInfo]:
    fmt = "%H%x1f%ct%x1f%s"
    proc = _run_git(workdir, ["log", "-1", f"--format={fmt}", rev, "--", path])
    if not _git_ok(proc):
        return None
    raw = (proc.stdout or "").strip()
    if not raw:
        return None
    parts = raw.split("\x1f")
    if len(parts) != 3:
        return None
    sha, ts, subject = parts
    try:
        ts_int = int(ts)
    except Exception:
        ts_int = 0
    return GitCommitInfo(sha=sha, timestamp=ts_int, subject=subject)


def read_path_divergence(workdir: Path, *, base_ref: str, path: str) -> tuple[Optional[int], Optional[int]]:
    proc = _run_git(workdir, ["rev-list", "--left-right", "--count", f"{base_ref}...HEAD", "--", path])
    if not _git_ok(proc):
        return None, None
    raw = (proc.stdout or "").strip()
    parts = raw.split()
    if len(parts) < 2:
        return None, None
    try:
        behind = int(parts[0])
        ahead = int(parts[1])
    except Exception:
        return None, None
    return ahead, behind


def compute_path_status(
    *,
    workdir: Path,
    path: Path,
    base_ref: Optional[str],
) -> GitPathStatus:
    try:
        rel = path.relative_to(workdir).as_posix()
    except Exception:
        rel = path.as_posix()
    status = GitPathStatus(path=rel, exists=path.exists(), dirty=False, base_ref=base_ref)

    proc = _run_git(workdir, ["status", "--porcelain", "--", rel])
    if _git_ok(proc):
        status.dirty = bool((proc.stdout or "").strip())
    else:
        status.error = (proc.stderr or proc.stdout or "").strip() or "git status failed"
        return status

    status.local_last_commit = read_last_commit(workdir, rev="HEAD", path=rel)
    if base_ref:
        if not ref_exists(workdir, base_ref):
            status.error = f"base ref {base_ref} is not available"
            return status
        status.base_last_commit = read_last_commit(workdir, rev=base_ref, path=rel)
        status.ahead_count, status.behind_count = read_path_divergence(workdir, base_ref=base_ref, path=rel)
        proc = _run_git(workdir, ["diff", "--name-only", base_ref, "--", rel])
        if _git_ok(proc):
            status.changed_vs_base = bool((proc.stdout or "").strip())
        else:
            status.changed_vs_base = None

    return status


def render_diff(workdir: Path, *, base_ref: str, path: str) -> str:
    proc = _run_git(workdir, ["diff", "--no-color", base_ref, "--", path], timeout_s=60.0)
    if _git_ok(proc):
        return proc.stdout or ""
    err = (proc.stderr or proc.stdout or "").strip()
    raise RuntimeError(err or "git diff failed")


@dataclass(frozen=True, slots=True)
class FsMeta:
    latest_mtime: float = 0.0
    latest_path: str = ""
    files: int = 0


def compute_fs_meta(path: Path) -> FsMeta:
    """
    Best-effort filesystem metadata for non-git directories (e.g. dev workspace).
    """
    latest_mtime = 0.0
    latest_path = ""
    files = 0
    try:
        if not path.exists():
            return FsMeta()
        for root, _dirs, filenames in os.walk(path):
            for fn in filenames:
                fp = Path(root) / fn
                try:
                    st = fp.stat()
                except Exception:
                    continue
                files += 1
                if st.st_mtime > latest_mtime:
                    latest_mtime = st.st_mtime
                    try:
                        latest_path = fp.relative_to(path).as_posix()
                    except Exception:
                        latest_path = fp.as_posix()
    except Exception:
        return FsMeta()
    return FsMeta(latest_mtime=latest_mtime, latest_path=latest_path, files=files)


def render_noindex_diff(*, left: Path, right: Path) -> tuple[bool, str]:
    """
    Returns (changed, diff_text) comparing two paths using `git diff --no-index`.
    """
    proc = subprocess.run(
        ["git", "diff", "--no-color", "--no-index", "--", str(left), str(right)],
        text=True,
        capture_output=True,
        timeout=60.0,
    )
    # exit code: 0 = no diff, 1 = diff, else error
    if proc.returncode == 0:
        return False, ""
    if proc.returncode == 1:
        return True, proc.stdout or ""
    err = (proc.stderr or proc.stdout or "").strip()
    raise RuntimeError(err or "git diff --no-index failed")


def unzip_b64_to_dir(*, archive_b64: str, dest: Path) -> None:
    raw = base64.b64decode(archive_b64.encode("ascii"))
    buf = io.BytesIO(raw)
    with zipfile.ZipFile(buf) as zf:
        zf.extractall(dest)
