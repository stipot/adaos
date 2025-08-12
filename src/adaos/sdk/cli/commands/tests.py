# src/adaos/sdk/cli/commands/tests.py
from __future__ import annotations
import os
from pathlib import Path
from typing import List, Optional
import typer

from adaos.sdk.context import SKILLS_DIR
from adaos.sdk.skill_service import install_all_skills

app = typer.Typer(help="Run AdaOS test suites")


def _ensure_tmp_base_dir() -> Path:
    import tempfile

    p = Path(tempfile.mkdtemp(prefix="adaos_test_base_"))
    (p / "skills").mkdir(parents=True, exist_ok=True)
    os.environ["ADAOS_BASE_DIR"] = str(p)
    os.environ.setdefault("HOME", str(p))
    os.environ.setdefault("USERPROFILE", str(p))
    return p


def _collect_test_dirs(root: Path) -> List[str]:
    paths: List[str] = []
    if root.is_dir():
        for tdir in root.rglob("tests"):
            if tdir.is_dir() and any(f.name.startswith("test_") and f.suffix == ".py" for f in tdir.rglob("test_*.py")):
                paths.append(str(tdir))
    return paths


@app.command("run")
def run_tests(
    only_sdk: bool = typer.Option(False, help="Run only SDK/API tests (./tests)."),
    only_skills: bool = typer.Option(False, help="Run only skills tests."),
    marker: Optional[str] = typer.Option(None, "--m", help="Pytest -m expression"),
    use_real_base: bool = typer.Option(False, help="Do NOT isolate ADAOS_BASE_DIR."),
    no_install: bool = typer.Option(False, help="Do not auto-install skills before running tests."),
    extra: Optional[List[str]] = typer.Argument(None),
):
    """
    Runs pytest over:
      - ./tests                           (SDK/API tests)   — unless --only-skills
      - ${SKILLS_DIR}/**/tests            (skill tests)      — unless --only-sdk
    By default, when running skill tests, auto-installs all skills from the monorepo unless --no-install is set.
    """
    try:
        import pytest  # noqa
    except Exception:
        typer.secho("pytest is not installed. Install dev deps: pip install -e .[dev]", fg=typer.colors.RED)
        raise typer.Exit(code=2)

    if not use_real_base:
        tmpdir = _ensure_tmp_base_dir()
        typer.secho(f"[AdaOS] Using isolated ADAOS_BASE_DIR at: {tmpdir}", fg=typer.colors.BLUE)

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
        # авто-установка навыков перед тестами, если не отключено
        if not no_install:
            try:
                installed = install_all_skills()
                if installed:
                    typer.secho(f"[AdaOS] Installed skills: {', '.join(installed)}", fg=typer.colors.BLUE)
            except Exception as e:
                # не валим прогон — просто предупреждаем
                typer.secho(f"[AdaOS] Auto-install skipped/failed: {e}", fg=typer.colors.YELLOW)

        skills_root = Path(SKILLS_DIR).resolve()
        pytest_paths.extend(_collect_test_dirs(skills_root))

        # если в исходниках тоже есть навыки с тестами — добавим (не обязательно)
        src_skills = repo_root / "src" / "adaos" / "skills"
        pytest_paths.extend(_collect_test_dirs(src_skills))

    if not pytest_paths:
        # аккуратный, предметный месседж
        if only_sdk:
            typer.secho("No SDK tests found. Create ./tests with test_*.py", fg=typer.colors.YELLOW)
        elif only_skills:
            typer.secho("No skill tests found. Ensure skills are installed and contain tests/ with test_*.py", fg=typer.colors.YELLOW)
        else:
            typer.secho("No test paths found. Tip: add SDK tests in ./tests, or ensure skills with tests are installed.", fg=typer.colors.YELLOW)
        raise typer.Exit(code=1)

    args = ["-q", "--strict-markers", *pytest_paths]
    if marker:
        args.extend(["-m", marker])
    if extra:
        args.extend(extra)

    import pytest as _pytest

    typer.secho(f"pytest {' '.join(args)}", fg=typer.colors.CYAN)
    code = _pytest.main(args)
    raise typer.Exit(code=code)
