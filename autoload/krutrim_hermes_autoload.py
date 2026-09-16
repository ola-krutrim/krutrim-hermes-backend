"""Make this terminal backend resolvable in Hermes processes that never run
plugin discovery.

WHY THIS EXISTS
---------------
`tools/terminal_tool.py::_get_plugin_env_provider()` reads Hermes' terminal
environment registry and never triggers plugin discovery, swallowing every
exception. In a process where discovery has not already run, a correctly
installed and correctly enabled plugin backend is therefore invisible, and
`execute_code` fails with::

    Unknown environment type: krutrim. Use 'local', 'docker', 'singularity',
    'modal', 'daytona', 'vercel_sandbox', 'ssh'

Hermes applies the guard for exactly this everywhere else -- plugin commands,
lifecycle hooks, auxiliary tasks, system-prompt sections and browser providers
all go through `_ensure_plugins_discovered()`. Terminal environment providers
are the one provider family that reads its registry unguarded. Hermes' own
notes name the affected processes: gateway platform events, TUI slash workers,
query mode, cron. A plain interactive session runs discovery during startup and
is unaffected, which is why the failure looks intermittent.

This module closes that gap from outside Hermes, with no patch to it.

HOW
---
Installed as a `.pth` so it runs at interpreter startup in every process. It
cannot simply import Hermes there -- at `.pth` time Hermes is not yet on
`sys.path`, and the naive version dies with `ModuleNotFoundError: No module
named 'hermes_cli'`. Instead it installs a `sys.meta_path` finder that waits
for Hermes' *own* terminal registry to be imported -- by which point Hermes is
importable -- runs discovery once, and returns None so it never claims the
import it piggybacked on.

SUPPORT STATUS
--------------
This depends on Hermes internals (`hermes_cli.plugins.discover_plugins`) that
carry no compatibility guarantee. `tests/test_hermes_integration.py` pins the
Hermes version it is verified against and runs in CI; if an upstream change
breaks this, that suite fails rather than the backend silently going missing
again. Set ``KRUTRIM_HERMES_NO_AUTOLOAD=1`` to disable it.

This runs in EVERY Python process on the interpreter it is installed into, so
it must never raise and never slow down a process that has nothing to do with
Hermes. The finder returns None immediately for every module name except the
two it watches, and the whole module is defensive: a failure here must leave
the interpreter exactly as it found it.
"""

import os
import sys

__all__ = ["install"]

#: Importing either of these means Hermes is loaded far enough to discover.
_TRIGGER_MODULES = frozenset({
    "agent.terminal_env_registry",
    "tools.terminal_tool",
})

_ENV_OPT_OUT = "KRUTRIM_HERMES_NO_AUTOLOAD"


class _DiscoveryTrigger:
    """A meta_path finder that never finds anything.

    It exists only for the side effect: the first time Hermes' terminal
    registry is imported, run plugin discovery so the registry is populated
    before anything reads it. It always returns None, so it never affects how
    any module is actually imported.
    """

    __slots__ = ("_fired",)

    def __init__(self):
        self._fired = False

    def find_spec(self, fullname, path=None, target=None):
        if self._fired or fullname not in _TRIGGER_MODULES:
            return None
        # Set BEFORE discovering: discovery itself imports these modules, and
        # re-entering here would recurse.
        self._fired = True
        try:
            from hermes_cli.plugins import discover_plugins

            discover_plugins()
        except Exception:
            # Never let a discovery problem break an unrelated import. If this
            # fails the backend stays missing, which the integration suite
            # catches -- an exception raised here would be strictly worse.
            pass
        return None

    # Python 2-era API some tools still probe for; harmless and explicit.
    def find_module(self, fullname, path=None):
        return None


def install():
    """Install the trigger. Idempotent; safe to call more than once."""
    if os.environ.get(_ENV_OPT_OUT):
        return False
    for finder in sys.meta_path:
        if isinstance(finder, _DiscoveryTrigger):
            return False
    sys.meta_path.insert(0, _DiscoveryTrigger())
    return True


try:
    install()
except Exception:
    # A .pth import that raises prints a traceback on EVERY interpreter start,
    # including for people who have nothing to do with Hermes. Staying silent
    # is the lesser failure: the backend is missing, not the interpreter noisy.
    pass
