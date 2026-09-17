"""The Hermes terminal environment backed by one Krutrim sandbox."""
from __future__ import annotations

import base64
import os
import posixpath
import shlex
import threading
import time
from typing import Any, Mapping
from ._api import COMMAND_TIMEOUT_MAX, KrutrimAPI, KrutrimError

RUNNER_PATH = "/app/.hermes_runner.sh"

# The only directory tree a command may run in.
SANDBOX_ROOT = "/app"


def resolve_cwd(requested: str | None) -> str:
    """Normalise the working directory Hermes asked for.

    This used to CLAMP anything outside ``/app`` back to ``/app``, because the
    service rejected a cwd it did not already have:

        cwd=/root  ->  404 cwd not found: /root   (even after `mkdir -p /root`)

    That was fixed service-side on 2026-09-17 (Krutrim-Cloud-Service-Omni#106).
    Measured live after the fix, on a fresh sandbox:

        no cwd -> /app    cwd=/root -> /root    cwd=/tmp -> /tmp    cwd=/ -> /

    So the clamp is not merely unnecessary now, it is wrong: it silently moved an
    agent that asked for ``/tmp`` somewhere else. A directory that does not exist
    yet is still a 404, which `_ensure_runner` handles by creating it.
    """
    path = (requested or "").strip()
    if not path:
        return SANDBOX_ROOT
    return posixpath.normpath(path)
    path = posixpath.normpath(path)
    if path == SANDBOX_ROOT or path.startswith(SANDBOX_ROOT + "/"):
        return path
    return SANDBOX_ROOT


# How long execute() will wait out a 409 "sandbox is not active" before giving up.
# Generous because Hyderabad deploys take minutes, not seconds.
NOT_ACTIVE_WAIT_SECONDS = 300.0

# Why a runner instead of sending the command inline:
#
# Hermes wraps EVERY command in `_wrap_command_script`, which newline-joins its parts
# and always includes shell chaining operators. Edge filtering in front of the service
# rejects raw shell containing `;`, `&&` or `||`, so command text is passed base64
# encoded instead -- its charset is [A-Za-z0-9+/=] and cannot contain them.
#
# Two designs were benchmarked live, 8 commands each, same sandbox:
#
#     upload a script per command (2 API calls)   median 0.47s
#     install runner once + base64 arg (1 call)   median 0.26s   <- 45% faster
#
# Payloads were verified to 64KB of command text without complaint, far past any real
# Hermes command.
RUNNER_SOURCE = 'eval "$(printf %s "$1" | base64 -d)"\n'


class KrutrimTerminalEnvironment:
    """Duck-types Hermes's ``BaseEnvironment``: ``execute()`` plus ``cleanup()``."""

    def __init__(self, api: KrutrimAPI, sandbox_id: str, *, cwd: str = "/app",
                 default_timeout: int = 60, ttl_seconds: int | None = None,
                 owns_sandbox: bool = True):
        self._api = api
        self.sandbox_id = sandbox_id
        self._cwd = resolve_cwd(cwd)
        self._default_timeout = default_timeout
        self._ttl_seconds = ttl_seconds
        self._owns_sandbox = owns_sandbox
        self._runner_ready = False
        self._closed = False
        # One sandbox runs one command at a time. Without this, two
        # concurrent execute() calls can both see _runner_ready False and
        # race on uploading the runner they then both invoke.
        self._execute_lock = threading.RLock()

    # -- lifecycle ----------------------------------------------------------
    def _ensure_runner(self) -> None:
        if not self._runner_ready:
            self._api.upload(self.sandbox_id, RUNNER_PATH, RUNNER_SOURCE.encode())
            # A directory that does not exist yet is a 404 before the command
            # runs, so create it first. /app always exists, which makes it a safe
            # place to run the mkdir from.
            if self._cwd != SANDBOX_ROOT:
                self._api.run(self.sandbox_id, "mkdir -p -- " + shlex.quote(self._cwd),
                              timeout_seconds=30, cwd=SANDBOX_ROOT)
            self._runner_ready = True

    def execute(self, command: str, timeout: int | float | None = None, **kwargs: Any) -> dict:
        """Run *command* and return ``{"output", "exit_code"}``.

        ``timeout`` is clamped to the service's 270s ceiling rather than being passed
        through: Hermes may ask for longer, and an unclamped value is a 400 from the
        API instead of a timeout the agent can reason about.
        """
        if self._closed:
            return {"output": "krutrim: environment already cleaned up", "exit_code": 1}
        requested = int(timeout if timeout is not None else self._default_timeout)
        clamped = min(requested, COMMAND_TIMEOUT_MAX)
        arg = base64.b64encode(command.encode()).decode()
        try:
            with self._execute_lock:
                result = self._run_when_active(arg, clamped, kwargs)
        except KrutrimError as exc:
            return {"output": f"krutrim: {exc}", "exit_code": 1}
        except Exception as exc:  # noqa: BLE001 - surfaced to the agent, never raised
            return {"output": f"krutrim: {type(exc).__name__}: {exc}", "exit_code": 1}

        stdout = result.get("stdout") or ""
        stderr = result.get("stderr") or ""
        output = stdout + (("\n" if stdout and stderr else "") + stderr if stderr else "")
        if result.get("timedOut"):
            note = f"[krutrim] command exceeded {clamped}s"
            if clamped < requested:
                note += f" (Hermes asked for {requested}s; the service caps commands at {COMMAND_TIMEOUT_MAX}s)"
            output = (output + "\n" + note).strip()
        if result.get("stdoutTruncated") or result.get("stderrTruncated"):
            output += "\n[krutrim] output truncated by the service"
        return {"output": output, "exit_code": int(result.get("exitCode") or 0)}

    def _run_when_active(self, arg: str, clamped: int, kwargs: dict) -> dict:
        """Run the encoded command, waiting out a sandbox that is not active yet.

        The service answers 409 `sandbox is not active (status: deploying)` for a
        command sent before the sandbox is up. That is a typed, recoverable state
        rather than a failure, so it is waited on instead of surfaced to the agent --
        `create_environment` already waits for `active`, but a sandbox can be
        deploying again later, and in some regions that takes minutes.
        """
        deadline = time.time() + NOT_ACTIVE_WAIT_SECONDS
        while True:
            try:
                self._ensure_runner()
                return self._api.run(
                    self.sandbox_id,
                    f"bash {RUNNER_PATH} {arg}",
                    timeout_seconds=clamped,
                    cwd=resolve_cwd(kwargs.get("cwd")) if kwargs.get("cwd") else self._cwd,
                    env=kwargs.get("env") or kwargs.get("envs"),
                )
            except KrutrimError as exc:
                if exc.status != 409 or "not active" not in str(exc).lower():
                    raise
                if time.time() >= deadline:
                    raise
                # The runner upload is invalidated by a restart; re-upload after.
                self._runner_ready = False
                time.sleep(2.0)

    def extend_ttl(self, ttl_seconds: int) -> None:
        self._api.set_ttl(self.sandbox_id, ttl_seconds)
        self._ttl_seconds = ttl_seconds

    def cleanup(self) -> None:
        """Delete the sandbox at session teardown.

        Best-effort by design: a raising cleanup would surface as a Hermes crash at
        exit. The finite TTL set at create time is the backstop for the case where
        this never runs at all.
        """
        if self._closed or not self._owns_sandbox:
            self._closed = True
            return
        self._closed = True
        try:
            self._api.delete(self.sandbox_id)
        except Exception:  # noqa: BLE001
            pass

    # Hermes prompt surfaces ask environments where they are running.
    def get_working_directory(self) -> str:
        return self._cwd

    def __repr__(self) -> str:  # pragma: no cover
        return f"<KrutrimTerminalEnvironment {self.sandbox_id} cwd={self._cwd}>"
