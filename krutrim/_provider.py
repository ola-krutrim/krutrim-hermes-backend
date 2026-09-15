"""Hermes TerminalEnvironmentProvider for Ola Krutrim Cloud Sandbox."""
from __future__ import annotations

import os
import uuid
from typing import Any, Dict, List, Optional, Tuple

from agent.terminal_env_provider import TerminalEnvironmentProvider

from ._api import API_KEY_ENV, KrutrimAPI, TTL_MAX, TTL_MIN
from ._env import KrutrimTerminalEnvironment

DEFAULT_REGION = "In-Bangalore-1"
DEFAULT_FLAVOR = "sandbox-small"
DEFAULT_TTL_SECONDS = 3600
DEFAULT_CWD = "/app"

# Regions the sandbox service exposes today, with how long to wait for a create.
#
# Provisioning time differs enough between regions that a single timeout does not fit
# both: Bangalore reaches `active` in seconds, Hyderabad currently takes considerably
# longer. Hyderabad therefore gets a much larger budget, and the failure message names
# the region rather than reading as a generic timeout.
REGION_CREATE_TIMEOUT = {"In-Bangalore-1": 240.0, "In-Hyderabad-1": 900.0}
REGIONS = tuple(REGION_CREATE_TIMEOUT)

# Fallback only. The live /flavors endpoint is authoritative and is consulted at
# create time; this table exists so a create can still pick a sane flavor if that
# call fails, and is deliberately not used for anything else.
FALLBACK_FLAVORS = (
    ("sandbox-nano", 0.25), ("sandbox-small", 0.5), ("sandbox-medium", 1.0),
    ("sandbox-large", 2.0), ("sandbox-x-large", 4.0),
)


# The service derives a function name from the sandbox name and caps it at 28
# characters: a longer name is rejected with
#   400 "sandbox name: function name must be 28 characters or less"
# Hermes task_ids are free-form and routinely push past that, so the suffix is
# what must survive truncation -- it is the part that makes the name unique.
MAX_SANDBOX_NAME = 28


def _dns1035(prefix: str, suffix: str) -> str:
    """Build a name matching ^[a-z]([-a-z0-9]*[a-z0-9])?$ within the 28-char cap.

    Truncates the PREFIX, never the suffix: dropping entropy would let two
    concurrent sessions collide on a name.
    """
    def clean(v: str) -> str:
        return "".join(c if c.isalnum() else "-" for c in v.lower()).strip("-")

    suffix = clean(suffix)
    room = MAX_SANDBOX_NAME - len(suffix) - 1  # -1 for the joining hyphen
    head = clean(prefix)[:max(room, 1)].rstrip("-") or "h"
    if not head[0].isalpha():
        head = "h" + head[: max(room - 1, 1)]
    name = f"{head}-{suffix}"[:MAX_SANDBOX_NAME].rstrip("-")
    return name if name[0].isalpha() else "h" + name[1:]


class KrutrimProvider(TerminalEnvironmentProvider):
    """Registered via ``ctx.register_terminal_environment_provider`` in __init__.py.

    Subclassing is MANDATORY and silently enforced: `PluginContext.
    register_terminal_environment_provider` does an `isinstance` check against
    `TerminalEnvironmentProvider` and, on failure, emits a `logger.warning` and
    returns. Registration then "succeeds" from the plugin's point of view --
    `hermes plugins doctor` reports import and registration OK -- while
    `hermes doctor` reports `Unknown terminal backend 'krutrim'`. A standalone
    class that merely duck-types the protocol is dropped without an error.
    """

    name = "krutrim"
    display_name = "Krutrim Sandbox"

    # Own filesystem rooted away from the host -> Hermes routes container resource
    # config to us and skips its host-path container guards.
    is_container = True

    @property
    def description(self) -> str:
        return "Run commands in an Ola Krutrim Cloud Sandbox (India regions)."

    @property
    def env_description(self) -> str:
        return "an Ola Krutrim Cloud Sandbox (Linux)"

    @property
    def cache_path_base(self) -> Optional[str]:
        return DEFAULT_CWD

    @property
    def strip_env_keys(self) -> frozenset:
        """Never let the vendor credential reach a model-visible subprocess."""
        return frozenset({API_KEY_ENV, "KRUTRIM_API_KEY"})

    # -- availability: MUST NOT touch the network -------------------------------
    def is_available(self) -> bool:
        return bool(os.environ.get(API_KEY_ENV) or os.environ.get("KRUTRIM_API_KEY"))

    def check_requirements(self, config: Dict[str, Any]) -> bool:
        return self.is_available()

    def probe(self) -> Tuple[str, str]:
        if self.is_available():
            return ("ready", "")
        return ("needs_setup", f"{self.display_name} needs {API_KEY_ENV} in the environment.")

    def setup_instructions(self) -> List[str]:
        return [
            f"export {API_KEY_ENV}=<your Krutrim Cloud API key>",
            "hermes plugins enable krutrim",
            "hermes config set terminal.backend krutrim",
        ]

    def doctor_checks(self) -> List[Tuple[bool, str, str]]:
        ok = self.is_available()
        return [(ok, "Krutrim API key",
                 "found" if ok else f"{API_KEY_ENV} is not set")]

    # -- creation ---------------------------------------------------------------
    def _api(self) -> KrutrimAPI:
        key = os.environ.get(API_KEY_ENV) or os.environ.get("KRUTRIM_API_KEY")
        if not key:
            raise RuntimeError(f"{API_KEY_ENV} is not set")
        return KrutrimAPI(key)

    def _pick_flavor(self, api: KrutrimAPI, region: str, cc: Dict[str, Any]) -> str:
        """Map Hermes's ``container_cpu`` onto the smallest flavor that satisfies it.

        ``container_memory`` and ``container_disk`` are accepted and ignored: the
        service reports ram_size 0 and a flat 20GB disk for every flavor, so there is
        nothing to map them onto. Silently pretending to honour them would be worse
        than saying so here.
        """
        want_cpu = cc.get("container_cpu") or cc.get("cpu")
        try:
            rows = [(f["flavor"], f["vcpus"]) for f in api.flavors(region) if f["region"] == region]
        except Exception:  # noqa: BLE001
            rows = list(FALLBACK_FLAVORS)
        if not rows:
            rows = list(FALLBACK_FLAVORS)
        rows.sort(key=lambda r: r[1])
        if want_cpu:
            try:
                need = float(want_cpu)
            except (TypeError, ValueError):
                need = 0.0
            for flavor, vcpus in rows:
                if vcpus >= need:
                    return flavor
            return rows[-1][0]
        for flavor, _ in rows:
            if flavor == DEFAULT_FLAVOR:
                return flavor
        return rows[0][0]

    def create_environment(self, *, cwd: str, timeout: int, task_id: str = "default",
                           image: Optional[str] = None,
                           container_config: Optional[Dict[str, Any]] = None,
                           **kwargs: Any) -> KrutrimTerminalEnvironment:
        cc = dict(container_config or {})
        api = self._api()
        region = os.environ.get("KRUTRIM_SANDBOX_REGION") or DEFAULT_REGION
        if region not in REGIONS:
            raise ValueError(f"KRUTRIM_SANDBOX_REGION must be one of {REGIONS}, got {region!r}")

        ttl = int(os.environ.get("KRUTRIM_SANDBOX_TTL_SECONDS") or DEFAULT_TTL_SECONDS)
        ttl = max(TTL_MIN, min(TTL_MAX, ttl))

        flavor = os.environ.get("KRUTRIM_SANDBOX_FLAVOR") or self._pick_flavor(api, region, cc)
        name = _dns1035(f"hermes-{task_id}", uuid.uuid4().hex[:8])

        sandbox_id = api.create(name=name, region=region, flavor=flavor, ttl_seconds=ttl)
        # `create` answers 202 while the sandbox is still deploying; commands sent
        # before it is active fail. Never skip this.
        try:
            api.wait_active(sandbox_id, timeout=REGION_CREATE_TIMEOUT.get(region, 240.0),
                            region=region)
        except Exception:
            try:
                api.delete(sandbox_id)
            except Exception:  # noqa: BLE001
                pass
            raise
        return KrutrimTerminalEnvironment(
            api, sandbox_id,
            cwd=cwd or DEFAULT_CWD,
            default_timeout=timeout or 60,
            ttl_seconds=ttl,
        )
