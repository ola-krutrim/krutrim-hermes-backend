"""Ola Krutrim Cloud Sandbox backend for Hermes.

    hermes plugins enable krutrim
    hermes config set terminal.backend krutrim

Registers two things:

* a terminal environment provider, so Hermes runs its commands in a sandbox; and
* explicit ``sandbox_*`` tools for what a shell inside the sandbox cannot reach --
  exposing ports, and managing sandboxes other than the session's own.

Either half is useful without the other, so each registers independently: a Hermes
build without the tool APIs still gets the backend, and an environment without
Hermes at all can still import the tools.
"""

from __future__ import annotations

import os

__all__ = ["KrutrimProvider", "register"]
__version__ = "0.2.0"

# The provider subclasses a Hermes base class, so importing it outside a Hermes
# install raises. That must not make this package unimportable: the tools below
# are plain HTTP and work anywhere, and the unit tests import this package without
# Hermes present.
_PROVIDER_IMPORT_ERROR: ImportError | None = None
try:
    from ._provider import KrutrimProvider
except ImportError as exc:  # pragma: no cover - depends on the host environment
    if exc.name not in ("agent", "agent.terminal_env_provider", "tools"):
        raise
    KrutrimProvider = None  # type: ignore[assignment]
    _PROVIDER_IMPORT_ERROR = exc


def register(ctx):
    """Install this plugin's surfaces into Hermes.

    Each half is guarded on its own. A failure to register one must never cost the
    other -- the backend is the reason this plugin exists, and the tools are useful
    even when a session's terminal backend is something else.
    """
    if KrutrimProvider is not None and hasattr(ctx, "register_terminal_environment_provider"):
        ctx.register_terminal_environment_provider(KrutrimProvider())

    if not (hasattr(ctx, "register_tool") and hasattr(ctx, "register_hook")):
        # An older Hermes without the tool/hook APIs. The backend above still works.
        return

    import json

    from ._api import API_KEY_ENV, KrutrimAPI
    from ._tools import OPERATIONS, approval_for, call_operation

    def _handler_for(operation):
        def handle(params=None, **kwargs):
            key = os.environ.get(API_KEY_ENV)
            if not key:
                return json.dumps({"ok": False, "error": f"{API_KEY_ENV} is not set"})
            return call_operation(KrutrimAPI(key), operation, params or kwargs or {})

        return handle

    for operation in OPERATIONS:
        ctx.register_tool(name=operation.name, toolset="sandbox",
                          schema=operation.schema, handler=_handler_for(operation))

    def _approve(tool_name, args=None, **_kwargs):
        return approval_for(tool_name, args)

    ctx.register_hook("pre_tool_call", _approve)
