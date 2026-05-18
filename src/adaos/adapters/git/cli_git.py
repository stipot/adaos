# src\adaos\adapters\git\cli_git.py
from __future__ import annotations
import os
import re
import shutil
import subprocess
import uuid
from pathlib import Path
import logging
from typing import Optional, Final, Sequence, Union
from adaos.ports.git import GitClient


class GitError(RuntimeError): ...


StrOrPath = Union[str, Path]

_log = logging.getLogger(__name__)


def _git_command_timeout_s() -> float:
    try:
        return max(1.0, float(str(os.getenv("ADAOS_GIT_COMMAND_TIMEOUT_S") or "90").strip()))
    except Exception:
        return 90.0


def _run_git(args: list[str], cwd: Optional[StrOrPath] = None) -> str:
    if cwd is not None:
        cwd = str(Path(cwd))  # единая точка приведения к str
    timeout_s = _git_command_timeout_s()
    try:
        p = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=timeout_s)
    except subprocess.TimeoutExpired as exc:
        raise GitError(f"git {' '.join(args)} timed out after {timeout_s:.1f}s cwd={cwd or '-'}") from exc
    # TODO Проверить, git нет, но папка не пустая. Вместо операции c git даем дружественную ошибку
    # destination path 'C:\git\MUIV\adaos_test\adaos\.adaos_1\workspace' already exists and is not an empty directory
    if p.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed: {p.stderr.strip()}")
    return p.stdout.strip()


def _safe_git(dir: StrOrPath, args: list[str]) -> Optional[str]:
    try:
        return _run_git(args, cwd=dir).strip()
    except Exception:
        return None


def _is_adaos_workspace_repo(dir: StrOrPath) -> bool:
    """
    Guardrail: only apply auto-reconciliation to the AdaOS workspace monorepo.

    The original incident happens on "/root/adaos/.adaos/workspace" where
    operational code expects the worktree to be fully materialized by sync.
    """
    try:
        p = Path(dir).resolve()
    except Exception:
        p = Path(dir)
    parts = [str(x).lower() for x in p.parts]
    if not parts:
        return False
    if parts[-1] != "workspace":
        return False
    return any(part == ".adaos" for part in parts)


def _git_path_exists(dir: StrOrPath, git_path: str) -> bool:
    resolved = _safe_git(dir, ["rev-parse", "--git-path", git_path])
    if not resolved:
        return False
    return Path(dir, resolved).exists()


def _rebase_in_progress(dir: StrOrPath) -> bool:
    return _git_path_exists(dir, "rebase-merge") or _git_path_exists(dir, "rebase-apply")


def _abort_rebase_if_needed(dir: StrOrPath) -> bool:
    if not _rebase_in_progress(dir):
        return False
    try:
        _run_git(["rebase", "--abort"], cwd=dir)
        return True
    except GitError as exc:
        _log.warning("git rebase abort failed repo=%s err=%s", str(Path(dir)), exc)
        return False


def _is_rebase_conflict_error(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return any(
        marker in msg
        for marker in (
            "conflict (content)",
            "could not apply",
            "resolve all conflicts manually",
            "you have unmerged files",
            "fix them up in the work tree",
            "exiting because of an unresolved conflict",
        )
    )


def _format_rebase_push_conflict(exc: BaseException, *, aborted: bool) -> str:
    suffix = (
        "The interrupted rebase was aborted and the workspace is back at the local commit. "
        "Resolve the merge conflict or retry after the remote branch is reconciled."
        if aborted
        else "A merge conflict interrupted the rebase. Check the workspace before retrying."
    )
    return f"{exc}\n\n{suffix}"


def _truncate(text: str, *, limit: int = 12000) -> str:
    if text is None:
        return ""
    if len(text) <= limit:
        return text
    return text[:limit] + "\n... (truncated)"


def _log_git_snapshot(dir: StrOrPath) -> None:
    repo_path = str(Path(dir))
    try:
        st = _run_git(["status"], cwd=dir)
        _log.warning("git snapshot repo=%s\n%s", repo_path, _truncate(st, limit=8000))
    except Exception as exc:
        _log.warning("git snapshot status failed repo=%s err=%s", repo_path, exc)
    try:
        lg = _run_git(["log", "--oneline", "--decorate", "-5"], cwd=dir)
        _log.warning("git snapshot log repo=%s\n%s", repo_path, _truncate(lg, limit=8000))
    except Exception as exc:
        _log.warning("git snapshot log failed repo=%s err=%s", repo_path, exc)


def _log_git_replacement_diff(dir: StrOrPath, *, target_ref: str) -> None:
    """
    Emit a best-effort diff that shows what will change if we hard-reset to target_ref.
    """
    repo_path = str(Path(dir))
    head = _safe_git(dir, ["rev-parse", "HEAD"]) or ""
    target = _safe_git(dir, ["rev-parse", target_ref]) or ""
    if head and target:
        _log.warning("git reconcile reset repo=%s from=%s to=%s", repo_path, head[:12], target[:12])
    try:
        lr = _run_git(["log", "--oneline", "--left-right", "--cherry", f"HEAD...{target_ref}"], cwd=dir)
        if lr.strip():
            _log.warning("git reconcile commits repo=%s\n%s", repo_path, _truncate(lr, limit=12000))
    except Exception as exc:
        _log.warning("git reconcile commits failed repo=%s err=%s", repo_path, exc)
    for args, title in (
        (["diff", "--stat", f"HEAD..{target_ref}"], "git reconcile diff --stat"),
        (["diff", "--name-status", f"HEAD..{target_ref}"], "git reconcile diff --name-status"),
        (["diff", f"HEAD..{target_ref}"], "git reconcile diff"),
    ):
        try:
            out = _run_git(args, cwd=dir)
            if out.strip():
                _log.warning("%s repo=%s\n%s", title, repo_path, _truncate(out))
        except Exception as exc:
            _log.warning("%s failed repo=%s err=%s", title, repo_path, exc)


def _format_divergence_hint(dir: StrOrPath) -> Optional[str]:
    """
    Best-effort explanation for non-fast-forward pulls.
    Returns None if we can't compute a helpful hint.
    """
    branch = _safe_git(dir, ["rev-parse", "--abbrev-ref", "HEAD"])
    upstream = _safe_git(dir, ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"])
    if not branch or not upstream:
        return None
    counts = _safe_git(dir, ["rev-list", "--left-right", "--count", f"HEAD...{upstream}"])
    ahead = behind = None
    if counts:
        parts = counts.replace("\t", " ").split()
        if len(parts) >= 2:
            try:
                ahead = int(parts[0])
                behind = int(parts[1])
            except Exception:
                ahead = behind = None
    repo_path = str(Path(dir))
    lines: list[str] = [
        "Non fast-forward pull detected.",
        f"repo: {repo_path}",
        f"branch: {branch}",
        f"upstream: {upstream}",
    ]
    if ahead is not None and behind is not None:
        lines.append(f"ahead/behind: {ahead}/{behind}")
    lines += [
        "To resolve, choose ONE of:",
        f"  - Rebase (keeps linear history): git -C \"{repo_path}\" pull --rebase --autostash",
        f"  - Merge: git -C \"{repo_path}\" pull --no-rebase",
        f"  - Discard local commits (DANGEROUS): git -C \"{repo_path}\" reset --hard {upstream}",
    ]
    return "\n".join(lines)


def _append_exclude(dir: str, lines: list[str]) -> None:
    p = Path(dir) / ".git" / "info" / "exclude"
    existing = set()
    if p.exists():
        existing = set(p.read_text(encoding="utf-8").splitlines())
    merged = existing.union(lines)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(sorted(merged)) + "\n", encoding="utf-8")


def _sanitize_sparse_checkout_file(dir: StrOrPath) -> bool:
    sp = Path(dir) / ".git" / "info" / "sparse-checkout"
    if not sp.exists():
        return False
    try:
        lines = sp.read_text(encoding="utf-8").splitlines()
    except Exception:
        return False
    cleaned = [line for line in lines if not line.strip().startswith("--")]
    if cleaned == lines:
        return False
    content = "\n".join(cleaned)
    if content:
        content += "\n"
    sp.write_text(content, encoding="utf-8")
    _log.warning("git sparse-checkout patterns sanitized repo=%s removed_cli_flags=%s", str(Path(dir)), len(lines) - len(cleaned))
    return True


_SPARSE_OVERWRITE_RE = re.compile(
    r"Working tree file '([^']+)' would be overwritten by sparse checkout update"
)


def _sparse_checkout_overwrite_paths(message: str) -> list[str]:
    paths: list[str] = []
    for match in _SPARSE_OVERWRITE_RE.finditer(message or ""):
        rel = match.group(1).strip()
        if rel and rel not in paths:
            paths.append(rel)
    return paths


def _sparse_checkout_blocker_retry_limit() -> int:
    try:
        return max(1, int(str(os.getenv("ADAOS_SPARSE_CHECKOUT_BLOCKER_RETRIES") or "200").strip()))
    except Exception:
        return 200


def _remove_sparse_checkout_blockers(dir: StrOrPath, paths: Sequence[str]) -> list[str]:
    root = Path(dir).resolve()
    removed: list[str] = []
    for rel in paths:
        rel_path = Path(rel)
        if rel_path.is_absolute():
            continue
        target = (root / rel_path).resolve()
        try:
            target.relative_to(root)
        except ValueError:
            continue
        if target == root or ".git" in target.relative_to(root).parts:
            continue
        if not target.exists() and not target.is_symlink():
            continue
        if target.is_dir() and not target.is_symlink():
            shutil.rmtree(target)
        else:
            target.unlink()
        removed.append(rel.replace("\\", "/"))
    return removed


class CliGitClient(GitClient):
    def __init__(self, depth: int = 1) -> None:
        self._depth: Final[int] = depth

    def ensure_repo(self, dir: StrOrPath, url: str, branch: Optional[str] = None) -> None:
        d = Path(dir)
        d.mkdir(parents=True, exist_ok=True)
        git_dir = d / ".git"
        if not git_dir.exists():
            # Prefer clone into empty directory; if directory is non-empty, fall back to init+fetch
            try:
                args = ["clone", url, str(d)]
                if self._depth > 0:
                    args += [f"--depth={self._depth}"]
                if branch:
                    args += ["--branch", branch]
                _run_git(args, cwd=None)
                try:
                    _run_git(["sparse-checkout", "init", "--cone"], cwd=str(d))
                except Exception:
                    pass
            except GitError:
                # Non-empty destination — initialize in place and attach remote
                _run_git(["init"], cwd=str(d))
                try:
                    _run_git(["remote", "add", "origin", url], cwd=str(d))
                except GitError:
                    # remote may already exist — continue
                    pass
                # Fetch and checkout the desired branch (or main)
                target_branch = branch or "main"
                try:
                    fetch_args = ["fetch", "--prune", "origin"]
                    if self._depth > 0:
                        fetch_args += [f"--depth={self._depth}"]
                    fetch_args += [target_branch]
                    _run_git(fetch_args, cwd=str(d))
                except GitError:
                    # try fetching all if branch-specific fetch failed
                    _run_git(["fetch", "--prune", "origin"], cwd=str(d))
                try:
                    _run_git(["checkout", "-B", target_branch, f"origin/{target_branch}"], cwd=str(d))
                except GitError:
                    # Last resort: checkout whatever HEAD points to
                    _run_git(["checkout", target_branch], cwd=str(d))
                try:
                    _run_git(["sparse-checkout", "init", "--cone"], cwd=str(d))
                except Exception:
                    pass
        _append_exclude(
            dir,
            [
                "*.pyc",
                "__pycache__/",
                ".venv/",
                "state/",
                "cache/",
                "logs/",
                "skills/**/.skill_env.json",
                "scenarios/**/.skill_env.json",
            ],
        )

    def pull(self, dir: StrOrPath) -> None:
        try:
            _run_git(["pull", "--ff-only"], cwd=dir)
        except GitError as exc:
            msg = str(exc)
            lowered = msg.lower()
            if "no tracking information for the current branch" in lowered or "set the remote as upstream" in lowered:
                branch = _safe_git(dir, ["rev-parse", "--abbrev-ref", "HEAD"])
                if branch and branch != "HEAD":
                    # repo was likely initialized via `git init` + `fetch` and lacks upstream config.
                    # Pull explicitly from origin/<branch> as a best-effort fix.
                    _run_git(["pull", "--ff-only", "origin", branch], cwd=dir)
                    return
            if "not possible to fast-forward" in lowered or "diverging branches" in lowered or "non-fast-forward" in lowered:
                # Auto-reconcile only for AdaOS workspace monorepo.
                if _is_adaos_workspace_repo(dir):
                    env_type = str(os.getenv("ENV_TYPE", "prod") or "prod").strip().lower()
                    repo_path = str(Path(dir))
                    if env_type == "dev":
                        # In dev, keep history by rebasing and autostashing, and log a snapshot for diagnostics.
                        _log.warning("git pull divergence detected; auto-rebasing (ENV_TYPE=dev) repo=%s", repo_path)
                        _log_git_snapshot(dir)
                        _run_git(["pull", "--rebase", "--autostash"], cwd=dir)
                        return
                    # In non-dev (prod/stage), prefer a deterministic state: reset to origin/main.
                    _log.warning(
                        "git pull divergence detected; auto-resetting to origin/main (ENV_TYPE=%s) repo=%s",
                        env_type,
                        repo_path,
                    )
                    _run_git(["fetch", "origin"], cwd=dir)
                    _log_git_snapshot(dir)
                    _log_git_replacement_diff(dir, target_ref="origin/main")
                    _run_git(["reset", "--hard", "origin/main"], cwd=dir)
                    return

                hint = _format_divergence_hint(dir)
                if hint:
                    raise GitError(f"{msg}\n\n{hint}") from exc
            raise

    def fetch(self, dir: StrOrPath, remote: str = "origin", branch: Optional[str] = None, depth: Optional[int] = None) -> None:
        args = ["fetch", "--prune", remote]
        eff_depth = self._depth if depth is None else depth
        if eff_depth and eff_depth > 0:
            args += [f"--depth={eff_depth}"]
        if branch:
            args.append(branch)
        _run_git(args, cwd=dir)

    def current_commit(self, dir: StrOrPath) -> str:
        return _run_git(["rev-parse", "HEAD"], cwd=dir)

    def show(self, dir: StrOrPath, spec: str) -> str:
        return _run_git(["show", spec], cwd=dir)

    # --- sparse ---
    def sparse_init(self, dir: StrOrPath, cone: bool = True) -> None:
        args = ["sparse-checkout", "init"]
        if cone:
            args.append("--cone")
        is_workspace = _is_adaos_workspace_repo(dir)
        if is_workspace:
            env_type = str(os.getenv("ENV_TYPE", "prod") or "prod").strip().lower()
            if env_type != "dev":
                dirty = self.changed_files(dir)
                if dirty:
                    repo_path = str(Path(dir))
                    _log.warning(
                        "git sparse-checkout init with dirty worktree; auto-stashing repo=%s env_type=%s files=%s",
                        repo_path,
                        env_type,
                        len(dirty),
                    )
                    _log_git_snapshot(dir)
                    stash_ref = self.stash_push(
                        str(dir),
                        "adaos:auto-stash sparse-checkout init",
                        include_untracked=True,
                    )
                    if stash_ref:
                        _log.warning("git auto-stashed local changes repo=%s stash=%s", repo_path, stash_ref)
        _run_git(args, cwd=dir)

    def sparse_set(self, dir: StrOrPath, paths: Sequence[str], no_cone: bool = True) -> None:
        args = ["sparse-checkout", "set"]
        if no_cone:
            args.append("--no-cone")
        is_workspace = _is_adaos_workspace_repo(dir)
        env_type = str(os.getenv("ENV_TYPE", "prod") or "prod").strip().lower()
        if is_workspace:
            dirty = self.changed_files(dir)
            if dirty:
                repo_path = str(Path(dir))
                _log.warning(
                    "git sparse-checkout set with dirty worktree; auto-stashing repo=%s files=%s",
                    repo_path,
                    len(dirty),
                )
                _log_git_snapshot(dir)
                stash_ref = self.stash_push(str(dir), "adaos:auto-stash sparse-checkout set", include_untracked=True)
                if stash_ref:
                    _log.warning("git auto-stashed local changes repo=%s stash=%s", repo_path, stash_ref)
        def _apply_sparse_set() -> None:
            _run_git([*args, *paths], cwd=dir)
            if _sanitize_sparse_checkout_file(dir):
                self.sparse_reapply(dir)

        stashed_after_error = False
        removed_blockers = 0
        blocker_retry_limit = _sparse_checkout_blocker_retry_limit()
        while True:
            try:
                _apply_sparse_set()
                return
            except GitError as exc:
                lowered = str(exc).lower()
                if is_workspace and "unstaged changes" in lowered and "sparse-checkout" in lowered and not stashed_after_error:
                    repo_path = str(Path(dir))
                    _log.warning("git sparse-checkout set blocked by dirty worktree; auto-stashing repo=%s", repo_path)
                    _log_git_snapshot(dir)
                    stash_ref = self.stash_push(str(dir), "adaos:auto-stash sparse-checkout set", include_untracked=True)
                    if stash_ref:
                        _log.warning("git auto-stashed local changes repo=%s stash=%s", repo_path, stash_ref)
                    stashed_after_error = True
                    continue
                overwrite_paths = _sparse_checkout_overwrite_paths(str(exc))
                if is_workspace and overwrite_paths and env_type != "dev":
                    repo_path = str(Path(dir))
                    if removed_blockers + len(overwrite_paths) > blocker_retry_limit:
                        raise GitError(
                            f"{exc}\n\nSparse checkout blocker recovery exceeded "
                            f"{blocker_retry_limit} file(s); refusing to continue."
                        ) from exc
                    _log.warning(
                        "git sparse-checkout set blocked by stale workspace files; removing blockers repo=%s env_type=%s files=%s",
                        repo_path,
                        env_type,
                        len(overwrite_paths),
                    )
                    _log_git_snapshot(dir)
                    removed = _remove_sparse_checkout_blockers(dir, overwrite_paths)
                    if not removed:
                        raise
                    removed_blockers += len(removed)
                    _log.warning("git sparse-checkout stale blockers removed repo=%s files=%s", repo_path, removed)
                    continue
                raise

    def sparse_add(self, dir: StrOrPath, path: str) -> None:
        try:
            _run_git(["sparse-checkout", "add", path], cwd=dir)
        except GitError:
            # fallback: перечитать и расширить вручную (как в твоей логике)
            info = Path(dir) / ".git" / "info"
            sp = info / "sparse-checkout"
            lines = sp.read_text(encoding="utf-8").splitlines() if sp.exists() else []
            if path not in lines:
                info.mkdir(parents=True, exist_ok=True)
                lines.append(path)
                sp.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def sparse_reapply(self, dir: StrOrPath) -> None:
        try:
            _run_git(["sparse-checkout", "reapply"], cwd=dir)
        except GitError:
            # Non sparse worktrees raise an error — ignore silently to keep idempotent.
            pass

    def rm_cached(self, dir: StrOrPath, path: str) -> None:
        try:
            _run_git(["rm", "--cached", "-r", "--ignore-unmatch", path], cwd=dir)
        except GitError:
            # Nothing tracked for the path — ignore.
            pass

    def changed_files(self, dir: StrOrPath, subpath: Optional[str] = None) -> list[str]:
        # untracked (-o) + modified (-m), исключая игнор по .gitignore
        args = ["status", "--porcelain"]
        if subpath:
            args += ["--", subpath]
        out = _run_git(args, cwd=dir)
        files: list[str] = []
        for raw in out.splitlines():
            line = raw.rstrip("\n").rstrip("\r")
            if not line.strip():
                continue
            payload = line[3:] if len(line) > 3 else ""
            payload = payload.strip()
            if " -> " in payload:
                payload = payload.split(" -> ", 1)[1].strip()
            if payload:
                files.append(payload)
        return files

    def stash_push(self, dir: StrOrPath, message: str, include_untracked: bool = True) -> Optional[str]:
        marker = f"{message} [{uuid.uuid4().hex}]"
        args = ["stash", "push"]
        if include_untracked:
            args.append("-u")
        args += ["-m", marker]
        out = _run_git(args, cwd=dir)
        if "No local changes" in out:
            return None
        stashes = _run_git(["stash", "list"], cwd=dir)
        for line in stashes.splitlines():
            if marker in line:
                return line.split(":", 1)[0].strip()
        return None

    def stash_pop(self, dir: StrOrPath, stash_ref: str) -> None:
        if not stash_ref:
            return
        _run_git(["stash", "pop", stash_ref], cwd=dir)

    def _current_branch(self, dir: StrOrPath) -> str:
        out = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=dir).strip()
        return out or "main"

    def commit_subpath(
        self,
        dir: StrOrPath,
        subpath: str | Sequence[str],
        message: str,
        author_name: str,
        author_email: str,
        signoff: bool = False,
    ) -> str:
        # stage только подпуть
        # В sparse-checkout репозитории git add без --sparse откажется
        # индексировать пути за пределами sparse-набора. Используем
        # --sparse, чтобы корректно работать и с узкой sparse-конфигурацией.
        if isinstance(subpath, str):
            paths = [subpath]
        else:
            paths = [str(item).strip() for item in subpath if str(item).strip()]
        if not paths:
            return "nothing-to-commit"
        try:
            _run_git(["add", "--sparse", "--", *paths], cwd=dir)
        except GitError as exc:
            # На очень старых версиях git флаг --sparse может быть не поддержан.
            # В этом случае пробуем ещё раз без него, сохраняя прежнее поведение.
            if "unknown option" in str(exc) and "--sparse" in str(exc):
                _run_git(["add", "--", *paths], cwd=dir)
            else:
                raise
        # пустой ли индекс?
        status = _run_git(["diff", "--cached", "--name-only"], cwd=dir)
        if not status.strip():
            return "nothing-to-commit"
        # автор в -c для изоляции от глобальных конфигов
        args = ["-c", f"user.name={author_name}", "-c", f"user.email={author_email}", "commit", "-m", message]
        if signoff:
            args.append("--signoff")
        _run_git(args, cwd=dir)
        return _run_git(["rev-parse", "HEAD"], cwd=dir).strip()

    def push(self, dir: StrOrPath, remote: str = "origin", branch: Optional[str] = None) -> None:
        branch = branch or self._current_branch(dir)
        # 1) сначала пробуем обычный fast-forward pull (быстро и дёшево)
        try:
            _run_git(["pull", "--ff-only", remote, branch], cwd=dir)
        except GitError:
            # 2) если не вышло (non-ff), делаем rebase с автосбросом стэша
            #    но shallow-репо могут не иметь базовой истории → разшалловим и повторим
            try:
                _run_git(["-c", "rebase.autoStash=true", "pull", "--rebase", remote, branch], cwd=dir)
            except GitError as rebase_exc:
                if _is_rebase_conflict_error(rebase_exc):
                    aborted = _abort_rebase_if_needed(dir)
                    raise GitError(_format_rebase_push_conflict(rebase_exc, aborted=aborted)) from rebase_exc
                _abort_rebase_if_needed(dir)
                # попытка «расшалловить» историю и снова rebase
                try:
                    _run_git(["fetch", "--prune", "--unshallow", remote], cwd=dir)
                except GitError:
                    # если git старый и не знает --unshallow, просто увеличим глубину
                    _run_git(["fetch", "--prune", "--depth=50", remote], cwd=dir)
                try:
                    _run_git(["-c", "rebase.autoStash=true", "pull", "--rebase", remote, branch], cwd=dir)
                except GitError as retry_exc:
                    aborted = _abort_rebase_if_needed(dir)
                    if _is_rebase_conflict_error(retry_exc):
                        raise GitError(_format_rebase_push_conflict(retry_exc, aborted=aborted)) from retry_exc
                    raise
        # 3) когда локальная ветка на вершине origin/<branch> — пушим
        _run_git(["push", remote, branch], cwd=dir)
