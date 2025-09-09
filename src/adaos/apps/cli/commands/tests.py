# src/adaos/apps/cli/commands/tests.py
from __future__ import annotations
import os
from pathlib import Path
from typing import List, Optional
import typer
from adaos.services.sandbox.bootstrap import ensure_dev_venv
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


def _run_one_group(
    ctx=None,  # ← опционально: AgentContext
    *,
    base_dir: Path,
    venv_python: str,
    paths: list[str],
    addopts: str = "",
) -> tuple[int, str, str]:
    """
    Запускает pytest внутри песочницы.
    - регистрирует маркер asyncio через `-o markers=...`
    - пользовательские фильтры передаются через PYTEST_ADDOPTS (addopts)
    - если передан ctx — добавляем полезные переменные окружения
    """
    if not paths:
        return 5, "no tests ran (empty path group)", ""

    pytest_args = [
        "-q",
        "--strict-markers",
        "-o",
        "markers=asyncio: mark asyncio tests",
        *paths,
    ]

    extra_env = {"PYTHONUNBUFFERED": "1"}
    # доп. окружение из контекста — безопасно и опционально
    try:
        if ctx is not None:
            extra_env.update(
                {
                    "ADAOS_LANG": getattr(ctx.settings, "lang", None) or os.environ.get("ADAOS_LANG", "en"),
                    "ADAOS_PROFILE": getattr(ctx.settings, "profile", None) or os.environ.get("ADAOS_PROFILE", "default"),
                }
            )
    except Exception:
        pass

    if addopts:
        extra_env["PYTEST_ADDOPTS"] = addopts

    return _sandbox_run([venv_python, "-m", "pytest", *pytest_args], cwd=base_dir, extra_env=extra_env)


def _mk_sandbox(base_dir: Path, profile: str = "tool"):
    """
    Создаём ProcSandbox, совместимую с разными сигнатурами конструктора:
    - ProcSandbox(fs_base=...)
    - ProcSandbox(base_dir=...) / ProcSandbox(base=...)
    - ProcSandbox(<positional>)
    Дополнительно прокидываем profile, если он поддерживается.
    """
    from adaos.services.sandbox.runner import ProcSandbox
    import inspect

    bd = Path(base_dir)
    sig = inspect.signature(ProcSandbox)
    params = sig.parameters

    # Подготовим kwargs по поддерживаемым именам
    kwargs = {}
    if "fs_base" in params:
        kwargs["fs_base"] = bd
    elif "base_dir" in params:
        kwargs["base_dir"] = bd
    elif "base" in params:
        kwargs["base"] = bd

    if "profile" in params:
        kwargs.setdefault("profile", profile)

    # 1) Попытка вызвать с подобранными kwargs
    try:
        if kwargs:
            return ProcSandbox(**kwargs)
    except TypeError:
        pass

    # 2) Попытка позиционным первым аргументом (если допустимо)
    try:
        # есть ли хотя бы один позиционный параметр без значения по умолчанию?
        positional_ok = any(p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD) and p.default is p.empty for p in params.values())
        if positional_ok:
            return ProcSandbox(bd)
    except TypeError:
        pass

    # 3) Последняя попытка: без аргументов + ручная установка базы
    try:
        sb = ProcSandbox()
    except TypeError as e:
        raise TypeError(f"ProcSandbox ctor incompatible; expected one of fs_base/base_dir/base/positional. Original: {e}") from e

    # Пробуем сеттеры/атрибуты
    for attr in ("set_fs_base", "set_base"):
        if hasattr(sb, attr):
            try:
                getattr(sb, attr)(bd)
                return sb
            except Exception:
                pass
    for attr in ("fs_base", "base_dir", "base", "_base"):
        if hasattr(sb, attr):
            try:
                setattr(sb, attr, bd)
                return sb
            except Exception:
                pass

    return sb  # как есть (но это вряд ли)


def _sandbox_run(cmd: list[str], *, cwd: Path, profile: str = "tool", extra_env: dict | None = None):
    """
    Универсальный запуск внутри песочницы:
    - авто-определяет сигнатуру ProcSandbox.run(...) и прокидывает только поддерживаемые kwargs
    - если inherit_env не поддерживается, сам мерджит окружение
    - нормализует результат к (exit_code, stdout, stderr)
    """
    import os
    import inspect
    from adaos.services.sandbox.runner import ProcSandbox

    sb = _mk_sandbox(cwd, profile=profile)

    # пытаемся импортировать ExecLimits, но делаем это опционально
    ExecLimits = None
    try:
        from adaos.services.sandbox.runner import ExecLimits as _ExecLimits

        ExecLimits = _ExecLimits
    except Exception:
        pass

    run_sig = inspect.signature(sb.run)
    params = run_sig.parameters

    kwargs = {}
    # базовые
    if "cwd" in params:
        kwargs["cwd"] = str(cwd)
    if "limits" in params and ExecLimits is not None:
        kwargs["limits"] = ExecLimits(wall_time_sec=600)

    # окружение
    if "env" in params:
        if "inherit_env" in params:
            # sandbox сам унаследует env, мы дадим только дельту
            kwargs["env"] = extra_env or {}
            kwargs["inherit_env"] = True
        else:
            # нет inherit_env — склеиваем сами c системным окружением
            merged = dict(os.environ)
            if extra_env:
                merged.update({k: v for k, v in extra_env.items() if isinstance(k, str) and isinstance(v, str)})
            kwargs["env"] = merged

    # текстовый режим вывода
    if "text" in params:
        kwargs["text"] = True

    # запускаем
    res = sb.run(cmd=cmd, **kwargs)

    # нормализуем результат
    if isinstance(res, tuple) and len(res) >= 3:
        code, out, err = res[0], res[1], res[2]
    else:
        code = getattr(res, "exit", getattr(res, "returncode", 0))
        out = getattr(res, "stdout", "")
        err = getattr(res, "stderr", "")
    return code, (out or ""), (err or "")


@app.command("run")
def run_tests(
    only_sdk: bool = typer.Option(False, help="Run only SDK/API tests (./tests)."),
    only_skills: bool = typer.Option(False, help="Run only skills tests."),
    marker: Optional[str] = typer.Option(None, "--m", help="Pytest -m expression"),
    use_real_base: bool = typer.Option(False, help="Do NOT isolate ADAOS_BASE_DIR."),
    no_install: bool = typer.Option(False, help="Do not auto-install skills before running tests."),
    no_clone: bool = typer.Option(False, help="Do not clone/init skills monorepo (expect it to exist)."),
    bootstrap: bool = typer.Option(True, help="Bootstrap dev dependencies in sandbox venv once."),
    extra: Optional[List[str]] = typer.Argument(None),
):
    # 0) изоляция BASE
    if not use_real_base:
        tmpdir = _ensure_tmp_base_dir()
        typer.secho(f"[AdaOS] Using isolated ADAOS_BASE_DIR at: {tmpdir}", fg=typer.colors.BLUE)

    # ctx уже читает настройки с учётом только что установленного ADAOS_BASE_DIR
    ctx = get_ctx()
    base_dir = Path(os.environ["ADAOS_BASE_DIR"])
    repo_root = Path(".").resolve()
    pytest_paths: List[str] = []

    # 1) Подготовка dev venv (однократно), затем используем его python
    if bootstrap:
        try:
            venv_python = str(
                ensure_dev_venv(
                    base_dir=base_dir,
                    repo_root=repo_root,
                    run=lambda c, cwd, env=None: _sandbox_run(c, cwd=cwd, extra_env=env),
                    env={"PYTHONUNBUFFERED": "1"},
                )
            )
            typer.secho(f"[AdaOS] Dev venv ready: {venv_python}", fg=typer.colors.BLUE)
        except Exception as e:
            typer.secho(f"[AdaOS] Bootstrap failed: {e}", fg=typer.colors.RED)
            raise typer.Exit(code=2)
    else:
        venv_python = "python"
        # деликатно проверим наличие pytest
        code, _, _ = _sandbox_run([venv_python, "-c", "import pytest"], cwd=base_dir)
        if code != 0:
            typer.secho("pytest is not installed. Tip: use --bootstrap or install dev deps: pip install -e .[dev]", fg=typer.colors.RED)
            raise typer.Exit(code=2)

    # 2) Подбор путей
    if not only_skills:
        sdk_tests = repo_root / "tests"
        if sdk_tests.exists():
            pytest_paths.append(str(sdk_tests))
        elif only_sdk:
            typer.secho("No SDK tests found in ./tests", fg=typer.colors.YELLOW)
            raise typer.Exit(code=1)

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

        # при разработке — добавим тесты из исходников, если есть
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

    # 3) Группировка по диску (Windows rootdir guard)
    from collections import defaultdict

    grouped: dict[str, List[str]] = defaultdict(list)
    for p in pytest_paths:
        grouped[_drive_key(p)].append(p)

    # 4) Формирование addopts (на всю группу единые)
    addopts_parts: List[str] = []
    if marker:
        addopts_parts += ["-m", marker]
    if extra:
        addopts_parts += extra
    addopts_str = " ".join(addopts_parts).strip()

    # 5) Прогон по группам (каждую — через venv_python)
    overall_code = 0
    for dk, paths in grouped.items():
        code, out, err = _run_one_group(
            base_dir=base_dir,
            venv_python=venv_python,
            paths=paths,
            addopts=addopts_str,
        )
        # трактуем пустую группу как успех
        if code == 5 and "no tests ran" in (out.lower() + err.lower()):
            code = 0

        color = typer.colors.CYAN if code == 0 else typer.colors.RED
        typer.secho(f"[pytest {dk}] exit={code} sandbox=yes\n--- stdout ---\n{out}\n--- stderr ---\n{err}", fg=color)
        overall_code = code if overall_code == 0 else overall_code

    raise typer.Exit(code=overall_code)
