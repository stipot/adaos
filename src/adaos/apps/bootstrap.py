from __future__ import annotations
from typing import Optional
from threading import RLock

from adaos.services.settings import Settings
from adaos.services.agent_context import AgentContext
from adaos.adapters.fs.path_provider import LocalPathProvider

from adaos.services.eventbus import LocalEventBus
from adaos.services.logging import setup_logging, attach_event_logger

# Остальные порты подставим позже в factory (PR-3..4).


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

        # пока оставим заглушки остальных портов, их закроем в следующих PR
        class _Nop:
            pass

        # настроим структурные логи и подпишем логгер на шину
        root_logger = setup_logging(paths)
        attach_event_logger(bus, root_logger.getChild("events"))

        return AgentContext(settings=settings, paths=paths, bus=bus, proc=_Nop(), caps=_Nop(), devices=_Nop(), kv=_Nop(), sql=_Nop(), secrets=_Nop(), net=_Nop(), updates=_Nop())


# публичные функции
def get_ctx() -> AgentContext:
    return _CtxHolder.get()


def init_ctx(settings: Optional[Settings] = None) -> AgentContext:
    return _CtxHolder.init(settings)


def reload_ctx(**overrides) -> AgentContext:
    return _CtxHolder.reload(**overrides)
