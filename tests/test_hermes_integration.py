"""Integration tests against a real Hermes install: is the backend actually
usable, and is the shim that makes it usable still doing its job?

    python3 -m unittest discover -s tests -v

`test_unit.py` stubs Hermes out, which is right for behaviour tests but means
it can never see this class of failure: every unit test can pass while
`execute_code` still reports "Unknown environment type: krutrim". "The provider
is valid" and "Hermes resolves it" are different questions, and only the second
is what a user experiences.

WHY SOME TESTS ASSERT A BUG EXISTS
----------------------------------
Two upstream defects are treated as PERMANENT and worked around on our side
rather than waited on:

1. `tools/terminal_tool.py::_get_plugin_env_provider` reads Hermes' terminal
   registry without triggering plugin discovery, so a correctly installed
   backend is invisible in any process that never ran discovery.
2. The "Unknown environment type" error never lists loaded-but-unregistered
   plugin backends, so "not installed" and "not loaded" are indistinguishable.

The tests below therefore assert the CURRENT upstream behaviour, not the
behaviour we wish for. Each one says what it means if it starts failing --
usually "upstream fixed this, delete the workaround". A test that fails forever
teaches nobody anything; a test that passes today and flips the day reality
changes is a working sensor.

What must genuinely work is OUR end of it: with the package installed, the
backend resolves. `test_shim_makes_the_backend_resolvable` is that test, and it
is the one that protects users.

NEVER SILENTLY INERT
--------------------
These tests need a Hermes checkout. Absent one they skip -- and a skipped test
is not a passing test. Set ``KRUTRIM_REQUIRE_HERMES=1`` (CI does) to turn that
skip into a failure, so this suite can never quietly stop running.

HERMETIC: every probe builds a throwaway `HERMES_HOME` containing only this
plugin, so nothing passes because of how a developer's own Hermes is set up.

FRESH SUBPROCESS PER PROBE: the terminal registry is module-global state
populated at plugin-load time. Probes sharing a process would inherit each
other's registrations and pass for the wrong reason.

BILLING SAFETY: every probe stops at provider RESOLUTION and never calls
`create_environment`, which would provision a real sandbox billed by the hour.
Resolution is the whole question anyway -- the failure under test is that the
factory never reaches the provider.
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
AUTOLOAD_DIR = REPO_ROOT / "autoload"

_DEFAULT_HERMES = Path.home() / ".hermes" / "hermes-agent"
HERMES_PATH = Path(os.environ.get("HERMES_AGENT_PATH", _DEFAULT_HERMES))
HERMES_AVAILABLE = (HERMES_PATH / "hermes_cli" / "plugins.py").is_file()

#: CI sets this. Without it a missing Hermes is a skip; with it, a failure.
REQUIRE_HERMES = bool(os.environ.get("KRUTRIM_REQUIRE_HERMES"))

UNKNOWN_ENV_PREFIX = "Unknown environment type:"


def _hermes_guard(cls):
    """Skip without a Hermes checkout -- unless CI demands one, then fail."""
    if HERMES_AVAILABLE:
        return cls
    if REQUIRE_HERMES:
        return cls  # tests run and fail loudly in setUpClass
    return unittest.skip(
        f"no Hermes checkout at {HERMES_PATH} (set HERMES_AGENT_PATH)"
    )(cls)


def _make_hermes_home() -> str:
    """A throwaway HERMES_HOME with this plugin installed and enabled."""
    home = tempfile.mkdtemp(prefix="hermes-home-")
    shutil.copytree(
        REPO_ROOT / "krutrim",
        Path(home) / "plugins" / "krutrim",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    (Path(home) / "config.yaml").write_text(
        "plugins:\n  enabled:\n    - krutrim\n  disabled: []\n"
        "terminal:\n  backend: krutrim\n"
    )
    return home


def _probe(body: str, home: str, *, with_shim: bool = False) -> dict:
    """Run `body` in a fresh interpreter rooted in the Hermes checkout.

    `with_shim` reproduces what the shipped `.pth` does -- `import
    krutrim_hermes_autoload` at startup -- without needing a built wheel, so
    the mechanism is tested from source.

    Returns the probe's JSON result, or a dict carrying an `error` /
    `probe_crashed` key. A probe that did not execute is never read as a pass.
    """
    # Substitution rather than .format()/f-string: probe bodies and wrapper are
    # both full of braces, and escaping them twice is how you get a harness bug
    # that looks like a test failure.
    template = textwrap.dedent(
        """
        import json, sys
        result = {}
        try:
        __PRE__
        __BODY__
        except Exception as exc:
            result = {"probe_crashed": type(exc).__name__ + ": " + str(exc)}
        print("@@RESULT@@" + json.dumps(result))
        """
    )
    pre = "    import krutrim_hermes_autoload  # noqa: F401" if with_shim else "    pass"
    script = template.replace("__PRE__", pre).replace(
        "__BODY__", textwrap.indent(textwrap.dedent(body).strip("\n"), " " * 4)
    )

    env = dict(os.environ)
    env["HERMES_HOME"] = home
    env.pop("KRUTRIM_HERMES_NO_AUTOLOAD", None)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(REPO_ROOT), str(AUTOLOAD_DIR), env.get("PYTHONPATH", "")]
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


RESOLVE_PROBE = """
from tools.terminal_tool import _get_plugin_env_provider
result = {"resolved": _get_plugin_env_provider("krutrim") is not None}
"""


class ProbeCase(unittest.TestCase):
    """Shared setup plus the assertion that a probe actually ran."""

    @classmethod
    def setUpClass(cls):
        if not HERMES_AVAILABLE:
            raise AssertionError(
                f"KRUTRIM_REQUIRE_HERMES is set but no Hermes checkout exists "
                f"at {HERMES_PATH}. These tests would otherwise SKIP, and a "
                f"skipped test reports success while checking nothing. Install "
                f"Hermes or set HERMES_AGENT_PATH."
            )
        cls.home = _make_hermes_home()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(getattr(cls, "home", ""), ignore_errors=True)

    def assertProbeRan(self, res):
        self.assertNotIn("error", res, f"probe did not run: {res}")
        self.assertNotIn("probe_crashed", res, f"probe crashed: {res}")


@_hermes_guard
class TestBackendIsReachableFromHermes(ProbeCase):

    def test_control_discovery_registers_the_backend(self):
        """NEGATIVE CONTROL for this whole file.

        The tests here assert what the registry can and cannot see. That is
        only evidence if the probe can see the backend when it IS there. Run
        discovery explicitly and the name must appear. If this fails, the
        install layout is broken and every other result here is meaningless
        rather than informative.
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
            "control invalid: the plugin does not register even when discovery "
            f"is run explicitly (got {res['names']}). Nothing else in this file "
            "can be trusted until this passes.",
        )

    def test_shim_makes_the_backend_resolvable(self):
        """THE TEST THAT PROTECTS USERS.

        With the shim loaded -- exactly what the shipped `.pth` does at
        interpreter startup -- the environment factory resolves 'krutrim' in a
        process that never called `discover_plugins()`.

        If this fails, Hermes users cannot run commands in a sandbox, whatever
        the rest of the suite says.
        """
        res = _probe(RESOLVE_PROBE, self.home, with_shim=True)
        self.assertProbeRan(res)
        self.assertTrue(
            res["resolved"],
            "the shim did not make the backend resolvable -- Hermes users are "
            "broken. Most likely an upstream change moved "
            "hermes_cli.plugins.discover_plugins or the registry import path "
            "the trigger watches.",
        )

    def test_without_the_shim_upstream_still_cannot_resolve_it(self):
        """Characterises upstream defect #1, and tells us when it is gone.

        This documents WHY the shim is shipped. It asserts the bug, so it
        passes today.

        If this test FAILS, that is good news: upstream now discovers plugins
        before reading the terminal registry, the shim is dead weight, and it
        (plus this test) should be deleted.
        """
        res = _probe(RESOLVE_PROBE, self.home, with_shim=False)
        self.assertProbeRan(res)
        self.assertFalse(
            res["resolved"],
            "upstream resolved the backend without our shim -- the workaround "
            "is no longer needed. Delete the shim, its packaging in "
            "pyproject.toml, and this test.",
        )

    def test_error_message_still_cannot_distinguish_missing_from_unloaded(self):
        """Characterises upstream defect #2.

        The factory appends registered plugin backends to its error. With an
        empty registry the message lists only built-ins, so a never-installed
        plugin and a loaded-but-unregistered one produce identical text. The
        README quotes this message verbatim; if the wording changes, the docs
        need updating, so this pins it.
        """
        res = _probe(
            """
            from tools.terminal_tool import _create_environment
            try:
                _create_environment(env_type="krutrim", image="", cwd="/app", timeout=5)
                result = {"raised": False, "message": ""}
            except ValueError as exc:
                result = {"raised": True, "message": str(exc)}
            """,
            self.home,
            with_shim=False,
        )
        self.assertProbeRan(res)
        self.assertTrue(res["raised"], "expected the unresolved-backend ValueError")
        self.assertIn(UNKNOWN_ENV_PREFIX, res["message"])
        known = res["message"].split(".", 1)[1] if "." in res["message"] else ""
        self.assertNotIn(
            "krutrim",
            known,
            "upstream now names the configured backend among the known ones, "
            "so the error is self-diagnosing. Update the README troubleshooting "
            "section, which currently tells users to read the list instead.",
        )


@_hermes_guard
class TestManifestMatchesThisHermes(ProbeCase):
    """plugin.yaml is a contract with whatever Hermes is installed. A field
    Hermes does not understand is ignored with only a DEBUG line, so a typo or
    schema drift is otherwise silent."""

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
            from hermes_cli import plugins as p
            known = set()
            for name in ("_KNOWN_MANIFEST_FIELDS", "KNOWN_MANIFEST_FIELDS",
                         "_MANIFEST_FIELDS", "MANIFEST_FIELDS"):
                val = getattr(p, name, None)
                if val:
                    known |= set(val)
            result = {"known": sorted(known)}
            """,
            self.home,
        )
        self.assertProbeRan(res)
        known = set(res.get("known") or ())
        if not known:
            self.skipTest(
                "this Hermes does not expose its manifest field set as a module "
                "constant; cannot verify without guessing"
            )
        unknown = declared - known
        self.assertFalse(
            unknown,
            f"plugin.yaml declares field(s) this Hermes ignores: "
            f"{sorted(unknown)}. Hermes logs this only at DEBUG, so the plugin "
            f"appears to install cleanly. Known fields: {sorted(known)}",
        )


class TestShimIsPackaged(unittest.TestCase):
    """The shim only helps if it actually ships. These need no Hermes."""

    @staticmethod
    def _pyproject() -> str:
        return (REPO_ROOT / "pyproject.toml").read_text()

    def test_entry_point_is_declared(self):
        """Without this group a pip install is invisible to Hermes, however
        correct the package is."""
        text = self._pyproject()
        self.assertIn('[project.entry-points."hermes_agent.plugins"]', text)
        self.assertRegex(text, r"krutrim\s*=\s*[\"']krutrim[\"']")

    def test_pth_and_module_are_shipped_into_site_packages(self):
        """The `.pth` is what runs the shim at interpreter startup. Shipping
        the module without it produces a package that looks complete and does
        nothing."""
        text = self._pyproject()
        self.assertIn("force-include", text)
        self.assertIn('= "krutrim_hermes_autoload.py"', text)
        self.assertIn('= "krutrim_hermes_autoload.pth"', text)
        self.assertTrue((AUTOLOAD_DIR / "krutrim_hermes_autoload.py").is_file())
        pth = AUTOLOAD_DIR / "krutrim_hermes_autoload.pth"
        self.assertTrue(pth.is_file())
        self.assertEqual(
            pth.read_text().strip(),
            "import krutrim_hermes_autoload",
            "a .pth line only executes when it starts with 'import'",
        )

    def test_shim_never_raises_and_can_be_opted_out(self):
        """It runs in EVERY process on the interpreter it is installed into. A
        traceback here would hit people who have nothing to do with Hermes."""
        sys.path.insert(0, str(AUTOLOAD_DIR))
        try:
            import krutrim_hermes_autoload as shim
        finally:
            sys.path.pop(0)
        self.assertFalse(
            shim.install(),
            "install() must be idempotent -- the module body already ran it",
        )
        os.environ["KRUTRIM_HERMES_NO_AUTOLOAD"] = "1"
        try:
            self.assertFalse(shim.install(), "opt-out must prevent installation")
        finally:
            os.environ.pop("KRUTRIM_HERMES_NO_AUTOLOAD", None)

    def test_shim_module_does_not_import_the_plugin_package(self):
        """At `.pth` time Hermes is not importable, and `krutrim/__init__.py`
        imports `agent.terminal_env_provider`. Importing the package from the
        shim would raise on every interpreter start."""
        src = (AUTOLOAD_DIR / "krutrim_hermes_autoload.py").read_text()
        code = "\n".join(
            line for line in src.splitlines() if not line.lstrip().startswith("#")
        )
        self.assertNotIn("import krutrim", code)
        self.assertNotIn("from krutrim", code)


if __name__ == "__main__":
    unittest.main()
