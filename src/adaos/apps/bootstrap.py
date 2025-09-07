from __future__ import annotations
from typing import Optional
from threading import RLock

from adaos.services.settings import Settings
from adaos.services.agent_context import AgentContext
from adaos.adapters.fs.path_provider import LocalPathProvider
from adaos.services.eventbus import LocalEventBus
from adaos.services.logging import setup_logging, attach_event_logger
from adaos.adapters.git.cli_git import CliGitClient
from adaos.adapters.db import SQLite, SQLiteKV
from adaos.services.runtime import AsyncProcessManager
from adaos.services.policy.capabilities import InMemoryCapabilities
from adaos.services.policy.net import NetPolicy
from adaos.adapters.git.cli_git import CliGitClient
from adaos.adapters.git.secure_git import SecureGitClient
from adaos.services.policy.fs import SimpleFSPolicy
from adaos.adapters.secrets.keyring_vault import KeyringVault
from adaos.adapters.secrets.file_vault import FileVault
from adaos.services.secrets.service import SecretsService
from adaos.services.secrets.crypto import load_or_create_master
from adaos.services.sandbox.runner import ProcSandbox
from adaos.services.sandbox.service import SandboxService


class _CtxHolder:
    _ctx: Optional[AgentContext] = None
    _lock = RLock()

    @classmethod
    def get(cls) -> AgentContext:
        with cls._lock:
            if cls._ctx is None:
                cls._ctx = cls._build(Settings.from_sources())
            return cls._ctx

    @classmethod
    def init(cls, settings: Optional[Settings] = None) -> AgentContext:
        with cls._lock:
            cls._ctx = cls._build(settings or Settings.from_sources())
            return cls._ctx

    @classmethod
    def reload(cls, **overrides) -> AgentContext:
        """Иммутабельная перегрузка (без дубликатов): создаём новый Settings и пересобираем контекст."""
        with cls._lock:
            old = cls._ctx or cls._build(Settings.from_sources())
            new_settings = old.settings.with_overrides(**overrides)
            cls._ctx = cls._build(new_settings)
            return cls._ctx

    @staticmethod
    def _build(settings: Settings) -> AgentContext:
        paths = LocalPathProvider(settings)
        bus = LocalEventBus()
        root_logger = setup_logging(paths)
        # policies
        caps = InMemoryCapabilities()
        sandbox_runner = ProcSandbox(fs_base=paths.base())
        sandbox = SandboxService(runner=sandbox_runner, caps=caps, bus=bus)
        attach_event_logger(bus, root_logger.getChild("events"))

        net = NetPolicy()

        # allow host(s) из монореп
        def _allow_host(url: str | None):
            if not url:
                return
            from urllib.parse import urlparse

            host = urlparse(url).hostname
            if not host and "@" in url and ":" in url:
                host = url.split("@", 1)[1].split(":", 1)[0]
            if host:
                net.allow(host)

        _allow_host(settings.skills_monorepo_url)
        _allow_host(settings.scenarios_monorepo_url)

        # базовые capabilities
        caps.grant("core", "proc.run", "net.git", "git.write", "skills.manage", "scenarios.manage", "secrets.read", "secrets.write")
        # ограничим сеть доменом монорепозитория навыков (если задан)
        if settings.skills_monorepo_url:
            from urllib.parse import urlparse

            host = urlparse(settings.skills_monorepo_url).hostname
            if not host and "@" in settings.skills_monorepo_url and ":" in settings.skills_monorepo_url:
                host = settings.skills_monorepo_url.split("@", 1)[1].split(":", 1)[0]  # ssh git
            if host:
                net.allow(host)

        # Git с защитой
        git_base = CliGitClient(depth=1)
        git = SecureGitClient(git_base, net)

        # FS policy: разрешённые корни — только внутри BASE_DIR
        fs = SimpleFSPolicy()
        fs.allow_root(paths.base())
        fs.allow_root(paths.skills_dir())
        fs.allow_root(paths.scenarios_dir())
        fs.allow_root(paths.state_dir())
        fs.allow_root(paths.cache_dir())
        fs.allow_root(paths.logs_dir())

        proc = AsyncProcessManager(bus=bus)
        sql = SQLite(paths)
        kv = SQLiteKV(sql, namespace="adaos")

        # Secrets: keyring primary; file vault fallback (ключ в keyring)
        try:
            secrets_backend = KeyringVault(profile=settings.profile, kv=kv)
        except Exception:
            # file vault (ключ получаем/кладём через keyring, но сам keyring может упасть; тогда ищем ENV)
            def key_get():
                try:
                    import keyring

                    return (
                        keyring.get_password(f"adaos:master:{settings.profile}", "vault.key").encode("utf-8")
                        if keyring.get_password(f"adaos:master:{settings.profile}", "vault.key")
                        else None
                    )
                except Exception:
                    return None

            def key_set(b: bytes):
                try:
                    import keyring

                    keyring.set_password(f"adaos:master:{settings.profile}", "vault.key", b.decode("utf-8"))
                except Exception:
                    pass

            secrets_backend = FileVault(base_dir=paths.base(), fs=None if False else SimpleFSPolicy(), key_get=key_get, key_set=key_set)  # fs подменим ниже

        secrets = SecretsService(secrets_backend, caps)

        # capabilities
        caps.grant("core", "net.git", "skills.manage", "scenarios.manage", "secrets.read", "secrets.write")

        # если backend FileVault — дайте ему fs
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
            sandbox=sandbox,
        )
        # TODO Doc Так ты сможешь делать paths.ctx.fs везде, где есть path
        if hasattr(paths, "ctx") is False:
            try:
                paths.ctx = ctx  # чтобы в адаптерах было paths.ctx.fs
            except Exception:
                pass
        return ctx


# публичные функции
def get_ctx() -> AgentContext:
    return _CtxHolder.get()


def init_ctx(settings: Optional[Settings] = None) -> AgentContext:
    return _CtxHolder.init(settings)


def reload_ctx(**overrides) -> AgentContext:
    return _CtxHolder.reload(**overrides)
