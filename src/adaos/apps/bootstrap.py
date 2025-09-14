# src/adaos/apps/bootstrap.py
from __future__ import annotations
from typing import Optional
from threading import RLock

from adaos.services.settings import Settings
from adaos.services.agent_context import AgentContext
from adaos.adapters.fs.path_provider import PathProvider
from adaos.services.eventbus import LocalEventBus
from adaos.services.logging import setup_logging, attach_event_logger
from adaos.adapters.git.cli_git import CliGitClient
from adaos.adapters.db import SQLite, SQLiteKV
from adaos.services.runtime import AsyncProcessManager
from adaos.services.policy.capabilities import InMemoryCapabilities
from adaos.services.policy.net import NetPolicy
from adaos.adapters.git.secure_git import SecureGitClient
from adaos.services.policy.fs import SimpleFSPolicy
from adaos.adapters.secrets.keyring_vault import KeyringVault
from adaos.adapters.secrets.file_vault import FileVault
from adaos.services.secrets.service import SecretsService
from adaos.services.secrets.crypto import load_or_create_master  # noqa: F401 (если пока не используешь)
from adaos.services.sandbox.runner import ProcSandbox
from adaos.services.sandbox.service import SandboxService

from adaos.services.agent_context import set_ctx


class _CtxHolder:
    _ctx: Optional[AgentContext] = None
    _lock = RLock()

    @classmethod
    def get(cls) -> AgentContext:
        with cls._lock:
            if cls._ctx is None:
                cls._ctx = cls._build(Settings.from_sources())
                # публикуем в ContextVar
                set_ctx(cls._ctx)
            return cls._ctx

    @classmethod
    def init(cls, settings: Optional[Settings] = None) -> AgentContext:
        with cls._lock:
            cls._ctx = cls._build(settings or Settings.from_sources())
            set_ctx(cls._ctx)  # публикуем
            return cls._ctx

    @classmethod
    def reload(cls, **overrides) -> AgentContext:
        """Иммутабельная перегрузка (без дубликатов): создаём новый Settings и пересобираем контекст."""
        with cls._lock:
            old = cls._ctx or cls._build(Settings.from_sources())
            new_settings = old.settings.with_overrides(**overrides)
            cls._ctx = cls._build(new_settings)
            set_ctx(cls._ctx)  # публикуем
            return cls._ctx

    @staticmethod
    def _build(settings: Settings) -> AgentContext:
        paths = PathProvider(settings)
        paths.ensure_tree()

        fs = SimpleFSPolicy()
        for root in (
            paths.base_dir(),
            paths.skills_dir(),
            paths.scenarios_dir(),
            paths.logs_dir(),
            paths.cache_dir(),
            paths.state_dir(),
            paths.tmp_dir(),
        ):
            fs.allow_root(root)

        bus = LocalEventBus()
        root_logger = setup_logging(paths)
        attach_event_logger(bus, root_logger.getChild("events"))

        # policies
        caps = InMemoryCapabilities()
        net = NetPolicy()

        # allow host(s) из монореп
        def _allow_host(url: str | None):
            if not url:
                return
            from urllib.parse import urlparse

            host = urlparse(url).hostname
            if not host and "@" in url and ":" in url:
                host = url.split("@", 1)[1].split(":", 1)[0]  # ssh git
            if host:
                net.allow(host)

        _allow_host(settings.skills_monorepo_url)
        _allow_host(settings.scenarios_monorepo_url)

        # базовые capabilities
        caps.grant("core", "proc.run", "net.git", "git.write", "skills.manage", "scenarios.manage", "secrets.read", "secrets.write")

        # Git с защитой
        git_base = CliGitClient(depth=1)
        git = SecureGitClient(git_base, net)

        proc = AsyncProcessManager(bus=bus)
        sql = SQLite(paths)
        kv = SQLiteKV(sql, namespace="adaos")

        # Secrets: keyring primary; file vault fallback (ключ в keyring)
        try:
            secrets_backend = KeyringVault(profile=settings.profile, kv=kv)
        except Exception:
            # file vault (ключ через keyring, но если keyring недоступен — ищем в ENV)
            def key_get():
                try:
                    import keyring

                    v = keyring.get_password(f"adaos:master:{settings.profile}", "vault.key")
                    return v.encode("utf-8") if v else None
                except Exception:
                    return None

            def key_set(b: bytes):
                try:
                    import keyring

                    keyring.set_password(f"adaos:master:{settings.profile}", "vault.key", b.decode("utf-8"))
                except Exception:
                    pass

            secrets_backend = FileVault(base_dir=paths.base, fs=None, key_get=key_get, key_set=key_set)

        secrets = SecretsService(secrets_backend, caps)

        # если backend FileVault — подставим fs
        if isinstance(secrets_backend, FileVault):
            secrets_backend.fs = fs

        ctx = AgentContext(
            settings=settings,
            paths=paths,
            bus=bus,
            proc=proc,
            caps=caps,
            devices=object(),
            kv=kv,
            sql=sql,
            secrets=secrets,
            net=net,
            updates=object(),
            git=git,
            fs=fs,
            sandbox=SandboxService(runner=ProcSandbox(fs_base=paths.base), caps=caps, bus=bus),
        )

        # чтобы в адаптерах было paths.ctx.fs (если Paths это позволяет)
        if getattr(paths, "ctx", None) is None:
            try:
                paths.ctx = ctx
            except Exception:
                pass

        return ctx


# ── публичные функции (удобные фасады) ─────────────────────────────────────────


def get_ctx() -> AgentContext:
    """Shim: проксируем на services.agent_context.get_ctx()."""
    from adaos.services.agent_context import get_ctx as _get

    return _get()


def init_ctx(settings: Optional[Settings] = None) -> AgentContext:
    """Явная инициализация приложения и публикация контекста."""
    return _CtxHolder.init(settings)


def reload_ctx(**overrides) -> AgentContext:
    """Пересборка с overrides и публикация контекста."""
    return _CtxHolder.reload(**overrides)


def bootstrap_app(settings: Optional[Settings] = None) -> AgentContext:
    """Синоним init_ctx: удобно вызывать из точек входа CLI/API."""
    return init_ctx(settings)
