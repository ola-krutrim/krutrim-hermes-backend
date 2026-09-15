"""Ola Krutrim Cloud Sandbox terminal backend for Hermes.

    hermes plugins enable krutrim
    hermes config set terminal.backend krutrim
"""
from ._provider import KrutrimProvider

__all__ = ["KrutrimProvider", "register"]
__version__ = "0.1.0"


def register(ctx):
    ctx.register_terminal_environment_provider(KrutrimProvider())
