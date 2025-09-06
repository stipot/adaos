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
        attach_event_logger(bus, root_logger.getChild("events"))

        # policies
        caps = InMemoryCapabilities()
        caps.grant("core", "net.git", "skills.manage")
        net = NetPolicy()

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

        proc = AsyncProcessManager(bus=bus)
        sql = SQLite(paths)
        kv = SQLiteKV(sql, namespace="adaos")

        class _Nop:
            pass

        return AgentContext(settings=settings, paths=paths, bus=bus, proc=proc, caps=caps, devices=_Nop(), kv=kv, sql=sql, secrets=_Nop(), net=net, updates=_Nop(), git=git)


# публичные функции
def get_ctx() -> AgentContext:
    return _CtxHolder.get()


def init_ctx(settings: Optional[Settings] = None) -> AgentContext:
    return _CtxHolder.init(settings)


def reload_ctx(**overrides) -> AgentContext:
    return _CtxHolder.reload(**overrides)
