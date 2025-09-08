# src/adaos/agent/ovos_adapter/__init__.py
import warnings
from adaos.integrations.ovos import *  # noqa: F401,F403

warnings.warn("adaos.agent.integrations.ovos_adapter is deprecated; use adaos.adapters.ovos", DeprecationWarning, stacklevel=2)
