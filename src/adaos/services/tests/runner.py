from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence

from adaos.apps.bootstrap import get_ctx
from adaos.ports.sandbox import ExecLimits


@dataclass
class TestRunResult:
    exit_code: int
    stdout: str
    stderr: str
    used_sandbox: bool


def _inside_base(p: Path, base: Path) -> bool:
    try:
        p.resolve().relative_to(base.resolve())
        return True
    except Exception:
        return False


def run_pytest(
    target: str | None = None,
    *,
    kexpr: Optional[str] = None,
    markers: Optional[str] = None,
    maxfail: Optional[int] = None,
    junit_xml: Optional[str] = None,
    use_sandbox: Optional[bool] = None,
    profile: str = "tool",
) -> TestRunResult:
    """
    target: путь к каталогу/файлу тестов. По умолчанию: {repo}/tests или {BASE_DIR}/tests (если существует).
    use_sandbox: True — запуск через SandboxService (если cwd внутри BASE_DIR), False — прямой запуск pytest.
                 None — автодетект: True, если target внутри BASE_DIR.
    """
    ctx = get_ctx()
    base = Path(ctx.paths.base())
    # разумный выбор директории по умолчанию:
    if target:
        tpath = Path(target)
    else:
        # приоритет локальным репо-тестам, если есть
        repo_tests = Path.cwd() / "tests"
        base_tests = base / "tests"
        tpath = repo_tests if repo_tests.exists() else base_tests

    args: list[str] = ["-q"]
    if kexpr:
        args += ["-k", kexpr]
    if markers:
        args += ["-m", markers]
    if maxfail is not None:
        args += ["--maxfail", str(maxfail)]
    if junit_xml:
        args += ["--junitxml", junit_xml]
    args += [str(tpath)]

    # sandbox или нет?
    if use_sandbox is None:
        use_sandbox = _inside_base(tpath, base)

    if use_sandbox:
        res = ctx.sandbox.run(
            ["python", "-m", "pytest", *args],
            cwd=str(tpath if tpath.is_dir() else tpath.parent),
            profile=profile,
            inherit_env=True,  # безопасный белый список + PYTHON*/ADAOS_*
            extra_env={"PYTHONUNBUFFERED": "1"},  # приятный stdout
            limits=ExecLimits(wall_time_sec=300, cpu_time_sec=None, max_rss_mb=None),
        )
        return TestRunResult(res.exit_code, res.stdout, res.stderr, used_sandbox=True)
    else:
        # прямой запуск (для репозиторных тестов вне BASE_DIR)
        import subprocess

        p = subprocess.run(["python", "-m", "pytest", *args], cwd=str(tpath if tpath.is_dir() else tpath.parent), capture_output=True, text=True)
        return TestRunResult(p.returncode, p.stdout, p.stderr, used_sandbox=False)
