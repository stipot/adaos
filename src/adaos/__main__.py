# src/adaos/__main__.py
from adaos.sdk.data.i18n import _

__all__ = ["output", "_"]
from adaos.apps.cli.app import app

if __name__ == "__main__":
    app()
