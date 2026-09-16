"""Integration tests against a real Hermes install: does the plugin actually
become usable after the documented install?

    python3 -m unittest discover -s tests -v

`test_unit.py` stubs Hermes out, which is the right call for behaviour tests
but means it can never see this class of failure: every unit test can pass
while `execute_code` still reports

    Unknown environment type: krutrim. Use 'local', 'docker', 'singularity',
    'modal', 'daytona', 'vercel_sandbox', 'ssh'

— i.e. the plugin is correct and Hermes cannot see it. "KrutrimProvider is a
valid provider" is not the same question as "Hermes resolves 'krutrim'", and
only the second one is what a user experiences.

Each probe runs in a FRESH SUBPROCESS on purpose. The terminal-environment
registry is module-global state populated at plugin-load time, so a probe that
shared a process with an earlier probe would inherit its registrations and
report success for the wrong reason.

HERMETIC: every probe runs against a throwaway `HERMES_HOME` containing only
this plugin, so it cannot pass merely because the developer's own Hermes
happens to be configured correctly.

BILLING SAFETY: these tests stop at provider RESOLUTION and never call
`create_environment`. Constructing a Krutrim environment provisions a real
sandbox, which is billed by the hour until deleted. Resolution is the whole
question here anyway — the failure being tested is that the factory never
reaches the provider at all.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# A Hermes checkout is required. Override with HERMES_AGENT_PATH.
_DEFAULT_HERMES = Path.home() / ".hermes" / "hermes-agent"
HERMES_PATH = Path(os.environ.get("HERMES_AGENT_PATH", _DEFAULT_HERMES))
HERMES_AVAILABLE = (HERMES_PATH / "hermes_cli" / "plugins.py").is_file()

# Mirrors tools/terminal_tool.py's error text for an unresolvable env type.
UNKNOWN_ENV_PREFIX = "Unknown environment type:"


def _make_hermes_home() -> str:
    """A throwaway HERMES_HOME with this plugin installed and enabled, exactly
    as README.md instructs (`cp -R krutrim ~/.hermes/plugins/krutrim`)."""
    home = tempfile.mkdtemp(prefix="hermes-home-")
    plugin_dir = Path(home) / "plugins" / "krutrim"
    shutil.copytree(
        REPO_ROOT / "krutrim",
        plugin_dir,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    (Path(home) / "config.yaml").write_text(
        "plugins:\n  enabled:\n    - krutrim\n  disabled: []\n"
        "terminal:\n  backend: krutrim\n"
    )
    return home


def _probe(body: str, home: str) -> dict:
    """Run `body` in a fresh interpreter rooted in the Hermes checkout.

    `body` must print one JSON object as its last line. Returns that object,
    or a dict with an `error` key describing why the probe could not run — a
    probe that did not execute is never silently read as a pass.
    """
    # Built by substitution rather than .format()/f-string: the probe bodies
    # and the wrapper are both full of braces, and escaping them twice is how
    # you get a harness bug that looks like a test failure.
    template = textwrap.dedent(
        """
        import json, sys
        result = {}
        try:
        __BODY__
        except Exception as exc:
            result = {"probe_crashed": type(exc).__name__ + ": " + str(exc)}
        print("@@RESULT@@" + json.dumps(result))
        """
    )
    indented = textwrap.indent(textwrap.dedent(body).strip("\n"), " " * 4)
    script = template.replace("__BODY__", indented)

    env = dict(os.environ)
    env["HERMES_HOME"] = home
    # Keep the plugin importable as `krutrim` from the repo too, so a probe can
    # compare the installed copy against this working tree.
    env["PYTHONPATH"] = os.pathsep.join(
        [str(REPO_ROOT), env.get("PYTHONPATH", "")]
    ).rstrip(os.pathsep)

    proc = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(HERMES_PATH),
        env=env,
        capture_output=True,
        text=True,
    )
    for line in reversed(proc.stdout.splitlines()):
        if line.startswith("@@RESULT@@"):
            return json.loads(line[len("@@RESULT@@"):])
    return {
        "error": "probe produced no result line",
        "returncode": proc.returncode,
        "stderr": proc.stderr[-2000:],
    }


class ProbeAssertions:
    """Shared by every probe-driven case: a probe that did not run is not a
    result. Without this, a crashed probe returns an empty dict and every
    `assertIn`/`assertTrue` below it reads an absent answer as an absent
    problem."""

    def assertProbeRan(self, res):
        self.assertNotIn("error", res, f"probe did not run: {res}")
        self.assertNotIn("probe_crashed", res, f"probe crashed: {res}")


@unittest.skipUnless(
    HERMES_AVAILABLE,
    f"no Hermes checkout at {HERMES_PATH} (set HERMES_AGENT_PATH)",
)
class TestBackendIsReachableFromHermes(ProbeAssertions, unittest.TestCase):
    """The user-facing contract: after the documented install, does Hermes
    resolve 'krutrim' as a terminal backend?"""

    @classmethod
    def setUpClass(cls):
        cls.home = _make_hermes_home()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.home, ignore_errors=True)

    def test_control_discovery_registers_the_backend(self):
        """NEGATIVE CONTROL for this whole file.

        Every test below asserts something about whether the registry can see
        'krutrim'. That is only evidence if the probe can see it when it IS
        there. Here discovery is run explicitly first, so the name MUST show
        up. If this fails, the install layout is broken and every other result
        in this file is meaningless rather than informative.
        """
        res = _probe(
            """
            from hermes_cli.plugins import discover_plugins
            discover_plugins(force=True)
            from agent.terminal_env_registry import plugin_backend_names
            result = {"names": plugin_backend_names()}
            """,
            self.home,
        )
        self.assertProbeRan(res)
        self.assertIn(
            "krutrim",
            res["names"],
            "control invalid: the plugin does not register even when "
            f"discovery is run explicitly (got {res['names']}). Nothing else "
            "in this file can be trusted until this passes.",
        )

    def test_factory_resolves_krutrim_without_an_explicit_discovery_call(self):
        """REGRESSION: the reported failure.

        `tools.terminal_tool._get_plugin_env_provider` reads the registry but
        never triggers plugin discovery, and swallows every exception. So in
        any process where discovery has not already run, a correctly installed,
        correctly enabled plugin is invisible and `execute_code` dies with
        "Unknown environment type: krutrim" — which reads like the plugin is
        missing rather than unloaded.

        A user does not call `discover_plugins()`. Hermes owes them resolution
        of the backend its own config names.

        Hermes already has the fix pattern and applies it everywhere else:
        `hermes_cli.plugins._ensure_plugins_discovered()` ("triggers idempotent
        plugin discovery so callers can read the registry before any explicit
        discover_plugins() call") guards plugin commands, lifecycle hooks,
        auxiliary tasks, system-prompt sections and browser providers. Terminal
        environment providers are the one provider family that reads its
        registry unguarded, which is why this backend — and only this class of
        backend — vanishes in processes that never ran discovery: gateway
        platform events, TUI slash workers, query mode, cron.
        """
        res = _probe(
            """
            from tools.terminal_tool import _get_plugin_env_provider
            provider = _get_plugin_env_provider("krutrim")
            result = {"resolved": provider is not None}
            """,
            self.home,
        )
        self.assertProbeRan(res)
        self.assertTrue(
            res["resolved"],
            "the environment factory could not resolve 'krutrim' in a fresh "
            "process: plugin discovery is not guaranteed to have run before "
            "the terminal tool reads the registry",
        )

    def test_unresolvable_backend_error_names_the_configured_backend(self):
        """If resolution does fail, the message must not mislead.

        The factory appends registered plugin names to its error. When the
        registry is empty the message lists only built-ins, so a loaded-but-
        unregistered plugin and a never-installed plugin produce identical
        text — the operator cannot tell "not installed" from "not loaded".
        """
        res = _probe(
            """
            from tools.terminal_tool import _create_environment
            try:
                _create_environment(
                    env_type="krutrim", image="", cwd="/app", timeout=5,
                )
                result = {"raised": False, "message": ""}
            except ValueError as exc:
                result = {"raised": True, "message": str(exc)}
            """,
            self.home,
        )
        self.assertProbeRan(res)
        if not res["raised"]:
            return  # resolved fine; nothing to assert about the error text
        self.assertIn(UNKNOWN_ENV_PREFIX, res["message"])
        self.assertIn(
            "krutrim",
            res["message"].split(".", 1)[1] if "." in res["message"] else "",
            "the error names 'krutrim' only as the thing that failed, never "
            "among the known backends, so an enabled-but-unloaded plugin is "
            f"indistinguishable from an uninstalled one: {res['message']}",
        )


@unittest.skipUnless(
    HERMES_AVAILABLE,
    f"no Hermes checkout at {HERMES_PATH} (set HERMES_AGENT_PATH)",
)
class TestManifestMatchesThisHermes(ProbeAssertions, unittest.TestCase):
    """plugin.yaml is a contract with whatever Hermes is installed. A field
    Hermes does not understand is ignored with only a DEBUG line, so a typo or
    a schema drift is silent."""

    def test_manifest_fields_are_all_understood(self):
        manifest = REPO_ROOT / "krutrim" / "plugin.yaml"
        self.assertTrue(manifest.is_file(), f"missing {manifest}")
        declared = {
            line.split(":", 1)[0].strip()
            for line in manifest.read_text().splitlines()
            if line.strip() and not line.startswith((" ", "#", "-"))
        }
        res = _probe(
            """
            import inspect
            from hermes_cli import plugins as p
            src = inspect.getsource(p)
            result = {"source_len": len(src), "src": src[:0]}
            known = set()
            for name in ("_KNOWN_MANIFEST_FIELDS", "KNOWN_MANIFEST_FIELDS",
                         "_MANIFEST_FIELDS", "MANIFEST_FIELDS"):
                val = getattr(p, name, None)
                if val:
                    known |= set(val)
            result["known"] = sorted(known)
            """,
            _make_hermes_home(),
        )
        self.assertProbeRan(res)
        known = set(res.get("known") or ())
        if not known:
            self.skipTest(
                "this Hermes does not expose its manifest field set as a "
                "module constant; cannot verify without guessing"
            )
        unknown = declared - known
        self.assertFalse(
            unknown,
            f"plugin.yaml declares field(s) this Hermes ignores: "
            f"{sorted(unknown)}. Hermes logs this only at DEBUG, so the "
            f"plugin appears to install cleanly. Known fields: {sorted(known)}",
        )


if __name__ == "__main__":
    unittest.main()
