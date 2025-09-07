# src/adaos/apps/cli/commands/tests.py
from __future__ import annotations
import os
from pathlib import Path
from typing import List, Optional
import typer

from adaos.apps.bootstrap import get_ctx
from adaos.ports.sandbox import ExecLimits
from adaos.sdk.skill_service import install_all_skills

app = typer.Typer(help="Run AdaOS test suites")


def _ensure_tmp_base_dir() -> Path:
    import tempfile

    p = Path(tempfile.mkdtemp(prefix="adaos_test_base_"))
    (p / "skills").mkdir(parents=True, exist_ok=True)
    os.environ["ADAOS_BASE_DIR"] = str(p)
    os.environ.setdefault("HOME", str(p))
    os.environ.setdefault("USERPROFILE", str(p))
    # git identity + флаг тестов
    os.environ.setdefault("GIT_AUTHOR_NAME", "AdaOS Test Bot")
    os.environ.setdefault("GIT_AUTHOR_EMAIL", "testbot@example.com")
    os.environ.setdefault("GIT_COMMITTER_NAME", "AdaOS Test Bot")
    os.environ.setdefault("GIT_COMMITTER_EMAIL", "testbot@example.com")
    os.environ["ADAOS_TESTING"] = "1"
    return p


def _prepare_skills_repo(no_clone: bool) -> None:
    """
    Гарантируем, что {BASE}/skills — корректный git-репозиторий монорепо.
    Без лишних pull/checkout: только clone при отсутствии .git.
    """
    if no_clone:
        return
    ctx = get_ctx()
    root = Path(ctx.paths.skills_dir())
    if not (root / ".git").exists():
        # ровно один раз: clone (ветка/URL из settings, allow-list в SecureGit)
        ctx.git.ensure_repo(str(root), ctx.settings.skills_monorepo_url, branch=ctx.settings.skills_monorepo_branch)
        # лёгкий exclude, чтобы не шумели временные файлы
        try:
            (root / ".git" / "info").mkdir(parents=True, exist_ok=True)
            ex = root / ".git" / "info" / "exclude"
            existing = ex.read_text(encoding="utf-8").splitlines() if ex.exists() else []
            wanted = {"*.pyc", "__pycache__/", ".venv/", "state/", "cache/", "logs/"}
            merged = sorted(set(existing).union(wanted))
            ex.write_text("\n".join(merged) + "\n", encoding="utf-8")
        except Exception:
            pass


def _collect_test_dirs(root: Path) -> List[str]:
    paths: List[str] = []
    if root.is_dir():
        for tdir in root.rglob("tests"):
            if tdir.is_dir() and any(f.name.startswith("test_") and f.suffix == ".py" for f in tdir.rglob("test_*.py")):
                paths.append(str(tdir))
    return paths


def _drive_key(path_str: str) -> str:
    # На *nix вернётся пусто, на Windows — 'C:' / 'D:' и т.п.
    import os as _os

    d, _ = _os.path.splitdrive(path_str)
    return (d or "NO_DRIVE").upper()


def _run_one_group(ctx, repo_root: Path, paths: List[str]) -> tuple[int, str, str]:
    """Запуск одной группы путей (на одном диске) через sandbox."""
    # базовые аргументы pytest
    args = ["-q", "--strict-markers", "-o", "markers=asyncio: mark asyncio tests", *paths]  # зарегистрируем 'asyncio'
    limits = ExecLimits(wall_time_sec=600, cpu_time_sec=None, max_rss_mb=None)
    res = ctx.sandbox.run(
        ["python", "-m", "pytest", *args],
        cwd=str(ctx.paths.base()),  # sandbox: только внутри BASE_DIR
        profile="tool",
        inherit_env=True,
        extra_env={
            "PYTHONUNBUFFERED": "1",
            "PYTHONPATH": str(repo_root),  # чтобы импорты из репо находились
            "ADAOS_REPO_ROOT": str(repo_root),
        },
        limits=limits,
    )
    return res.exit_code, res.stdout, res.stderr


@app.command("run")
def run_tests(
    only_sdk: bool = typer.Option(False, help="Run only SDK/API tests (./tests)."),
    only_skills: bool = typer.Option(False, help="Run only skills tests."),
    marker: Optional[str] = typer.Option(None, "--m", help="Pytest -m expression"),
    use_real_base: bool = typer.Option(False, help="Do NOT isolate ADAOS_BASE_DIR."),
    no_install: bool = typer.Option(False, help="Do not auto-install skills before running tests."),
    no_clone: bool = typer.Option(False, help="Do not clone/init skills monorepo (expect it to exist)."),
    extra: Optional[List[str]] = typer.Argument(None),
):
    """
    Runs pytest over:
      - ./tests                           (SDK/API tests)   — unless --only-skills
      - {BASE}/skills/**/tests            (skill tests)      — unless --only-sdk
    By default, when running skill tests, auto-installs all skills (in isolated BASE) unless --no-install.
    """
    try:
        import pytest  # noqa
    except Exception:
        typer.secho("pytest is not installed. Install dev deps: pip install -e .[dev]", fg=typer.colors.RED)
        raise typer.Exit(code=2)

    if not use_real_base:
        tmpdir = _ensure_tmp_base_dir()
        typer.secho(f"[AdaOS] Using isolated ADAOS_BASE_DIR at: {tmpdir}", fg=typer.colors.BLUE)

    ctx = get_ctx()
    repo_root = Path(".").resolve()
    pytest_paths: List[str] = []

    # --- SDK/API tests ---
    if not only_skills:
        sdk_tests = repo_root / "tests"
        if sdk_tests.exists():
            pytest_paths.append(str(sdk_tests))
        elif only_sdk:
            typer.secho("No SDK tests found in ./tests", fg=typer.colors.YELLOW)
            raise typer.Exit(code=1)

    # --- Skill tests ---
    if not only_sdk:
        _prepare_skills_repo(no_clone=no_clone)
        if not no_install:
            try:
                installed = install_all_skills()
                if installed:
                    typer.secho(f"[AdaOS] Installed skills: {', '.join(installed)}", fg=typer.colors.BLUE)
            except Exception as e:
                typer.secho(f"[AdaOS] Auto-install skipped/failed: {e}", fg=typer.colors.YELLOW)

        skills_root = Path(ctx.paths.skills_dir()).resolve()
        pytest_paths.extend(_collect_test_dirs(skills_root))

        # при разработке— добавим тесты из исходников, если есть
        src_skills = repo_root / "src" / "adaos" / "skills"
        pytest_paths.extend(_collect_test_dirs(src_skills))

    if not pytest_paths:
        if only_sdk:
            typer.secho("No SDK tests found. Create ./tests with test_*.py", fg=typer.colors.YELLOW)
        elif only_skills:
            typer.secho("No skill tests found. Ensure skills are installed and contain tests/ with test_*.py", fg=typer.colors.YELLOW)
        else:
            typer.secho("No test paths found. Tip: add SDK tests in ./tests, or ensure skills with tests are installed.", fg=typer.colors.YELLOW)
        raise typer.Exit(code=1)

    # ---- группируем пути по диску, чтобы избежать rootdir-коллизий на Windows
    from collections import defaultdict

    grouped: dict[str, List[str]] = defaultdict(list)
    for p in pytest_paths:
        grouped[_drive_key(p)].append(p)

    overall_code = 0
    for dk, paths in grouped.items():
        # добавим фильтры, если заданы пользователем
        run_paths = list(paths)
        args_suffix: List[str] = []
        if marker:
            args_suffix += ["-m", marker]
        if extra:
            args_suffix += extra

        # склеим: базовые аргументы внутри _run_one_group, а тут только фильтры
        # (маленький трюк: просто добавим их в конец списка путей — _run_one_group не знает про них,
        #  поэтому передадим их через PYTEST_ADDOPTS)
        # Но проще — прокинем через env:
        import os as _os

        addopts = _os.environ.get("PYTEST_ADDOPTS", "")
        if args_suffix:
            addopts = (addopts + " " + " ".join(args_suffix)).strip()
        # временно выставим переменную для этого запуска
        prev_addopts = _os.environ.get("PYTEST_ADDOPTS")
        if addopts:
            _os.environ["PYTEST_ADDOPTS"] = addopts
        try:
            code, out, err = _run_one_group(ctx, repo_root, run_paths)
            # трактуем пустую группу как успех
            if code == 5 and "no tests ran" in (out.lower() + err.lower()):
                code = 0
        finally:
            if prev_addopts is None:
                _os.environ.pop("PYTEST_ADDOPTS", None)
            else:
                _os.environ["PYTEST_ADDOPTS"] = prev_addopts

        color = typer.colors.CYAN if code == 0 else typer.colors.RED
        typer.secho(f"[pytest {dk}] exit={code} sandbox=yes\n--- stdout ---\n{out}\n--- stderr ---\n{err}", fg=color)
        overall_code = code if overall_code == 0 else overall_code

    raise typer.Exit(code=overall_code)
