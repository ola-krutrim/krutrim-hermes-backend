"""Unit tests. No network, no API key, no Hermes install required.

    python3 -m unittest discover -s tests -v

Standard library only, matching the plugin itself -- a test dependency would be
the only dependency this project has.

`_provider` imports `agent.terminal_env_provider` from Hermes, which is not
importable outside a Hermes install, so a minimal stand-in is installed in
`sys.modules` before import. The stand-in mirrors the real contract: an abstract
base with `name`, `is_available` and `create_environment` abstract, and
`is_container` defaulting True.
"""

import os
import sys
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _install_hermes_stub() -> None:
    """Stand in for the Hermes package so the provider module can be imported."""
    if "agent.terminal_env_provider" in sys.modules:
        return
    import abc

    class TerminalEnvironmentProvider(abc.ABC):
        is_container: bool = True

        @property
        @abc.abstractmethod
        def name(self) -> str: ...

        @abc.abstractmethod
        def is_available(self) -> bool: ...

        @abc.abstractmethod
        def create_environment(self, *, cwd, timeout, task_id="default",
                               image=None, container_config=None, **kwargs): ...

    agent_pkg = types.ModuleType("agent")
    agent_pkg.__path__ = []
    provider_mod = types.ModuleType("agent.terminal_env_provider")
    provider_mod.TerminalEnvironmentProvider = TerminalEnvironmentProvider
    sys.modules["agent"] = agent_pkg
    sys.modules["agent.terminal_env_provider"] = provider_mod


_install_hermes_stub()

from krutrim import _api, _env, _provider, _tools  # noqa: E402


# --------------------------------------------------------------------------
# Sandbox naming
# --------------------------------------------------------------------------
class TestSandboxNaming(unittest.TestCase):
    """The service caps derived function names at 28 chars and requires a
    DNS-1035 label. Hermes task_ids are free-form and routinely exceed it, so
    this is the failure a real Hermes session hits first."""

    import re
    DNS1035 = re.compile(r"^[a-z]([-a-z0-9]*[a-z0-9])?$")

    def assert_valid(self, name: str) -> None:
        self.assertLessEqual(len(name), 28, f"{name!r} exceeds the 28-char cap")
        self.assertRegex(name, self.DNS1035, f"{name!r} is not a DNS-1035 label")

    def test_ordinary_task_id(self):
        n = _provider._dns1035("hermes-default", "deadbeef")
        self.assert_valid(n)
        self.assertEqual(n, "hermes-default-deadbeef")

    def test_long_task_id_is_truncated_not_rejected(self):
        n = _provider._dns1035("hermes-" + "a" * 100, "deadbeef")
        self.assert_valid(n)

    def test_uniqueness_suffix_always_survives(self):
        """The prefix is truncated, never the suffix: dropping entropy would let
        two concurrent sessions collide on a name."""
        for task in ("", "x", "a" * 200, "UPPER_Case/slash", "9-leading-digit"):
            with self.subTest(task=task):
                n = _provider._dns1035(f"hermes-{task}", "cafe1234")
                self.assert_valid(n)
                self.assertTrue(n.endswith("cafe1234"), f"{n!r} lost its suffix")

    def test_illegal_characters_are_replaced(self):
        n = _provider._dns1035("hermes-Foo_Bar/Baz!", "abcd0000")
        self.assert_valid(n)

    def test_never_starts_with_a_digit(self):
        n = _provider._dns1035("123456", "abcd0000")
        self.assert_valid(n)
        self.assertTrue(n[0].isalpha())

    def test_never_ends_with_a_hyphen(self):
        for task in ("hermes-trailing-", "a-" * 30):
            with self.subTest(task=task):
                self.assert_valid(_provider._dns1035(task, "abcd0000"))


# --------------------------------------------------------------------------
# Fakes
# --------------------------------------------------------------------------
class FakeAPI:
    """Records calls instead of making them."""

    def __init__(self, run_result=None, flavors=None, raise_on_run=None):
        self.calls = []
        self._run_result = run_result if run_result is not None else {
            "stdout": "ok\n", "stderr": "", "exitCode": 0,
            "stdoutTruncated": False, "stderrTruncated": False, "timedOut": False,
        }
        self._flavors = flavors if flavors is not None else [
            {"flavor": "sandbox-nano", "region": "In-Bangalore-1", "vcpus": 0.25},
            {"flavor": "sandbox-small", "region": "In-Bangalore-1", "vcpus": 0.5},
            {"flavor": "sandbox-medium", "region": "In-Bangalore-1", "vcpus": 1.0},
            {"flavor": "sandbox-large", "region": "In-Bangalore-1", "vcpus": 2.0},
            {"flavor": "sandbox-x-large", "region": "In-Bangalore-1", "vcpus": 4.0},
            {"flavor": "sandbox-small-hyd", "region": "In-Hyderabad-1", "vcpus": 0.5},
        ]
        self._raise_on_run = raise_on_run

    def upload(self, sandbox_id, path, content):
        self.calls.append(("upload", sandbox_id, path))

    def run(self, sandbox_id, cmd, *, timeout_seconds=60, cwd=None, env=None):
        self.calls.append(("run", sandbox_id, cmd, timeout_seconds, cwd))
        if self._raise_on_run is not None:
            exc, self._raise_on_run = self._raise_on_run, None
            raise exc
        return dict(self._run_result)

    def delete(self, sandbox_id):
        self.calls.append(("delete", sandbox_id))

    def flavors(self, region=None):
        self.calls.append(("flavors", region))
        return list(self._flavors)


def err(status: int, body: bytes = b"{}", path: str = "/x") -> _api.KrutrimError:
    return _api.KrutrimError(status, body, path)


# --------------------------------------------------------------------------
# execute()
# --------------------------------------------------------------------------
class TestExecuteContract(unittest.TestCase):
    """Hermes requires execute() -> {"output", "exit_code"} and treats a raised
    exception as a harness crash, so failures must be returned, never raised."""

    def env(self, **kw):
        api = kw.pop("api", None) or FakeAPI(**kw)
        return _env.KrutrimTerminalEnvironment(api, "sb-1"), api

    def test_returns_output_and_exit_code(self):
        e, _ = self.env()
        self.assertEqual(e.execute("echo ok"), {"output": "ok\n", "exit_code": 0})

    def test_stderr_is_appended_to_output(self):
        e, _ = self.env(run_result={"stdout": "out\n", "stderr": "bad\n", "exitCode": 1})
        r = e.execute("x")
        self.assertIn("out", r["output"])
        self.assertIn("bad", r["output"])
        self.assertEqual(r["exit_code"], 1)

    def test_command_is_base64_encoded_not_sent_raw(self):
        """The three shell chaining operators are rejected by edge filtering, so
        no raw command text may reach the wire."""
        e, api = self.env()
        e.execute("pwd; id -u && echo x || echo y")
        sent = [c for c in api.calls if c[0] == "run"][0][2]
        for op in (";", "&&", "||"):
            self.assertNotIn(op, sent, f"{op!r} reached the wire in {sent!r}")
        self.assertTrue(sent.startswith("bash "))

    def test_runner_is_installed_once_not_per_command(self):
        e, api = self.env()
        for _ in range(5):
            e.execute("echo x")
        self.assertEqual(len([c for c in api.calls if c[0] == "upload"]), 1)

    def test_api_failure_is_returned_not_raised(self):
        e, _ = self.env(raise_on_run=err(500, b"boom"))
        r = e.execute("x")
        self.assertEqual(r["exit_code"], 1)
        self.assertTrue(r["output"].startswith("krutrim:"))

    def test_unexpected_exception_is_returned_not_raised(self):
        e, _ = self.env(raise_on_run=ValueError("surprise"))
        r = e.execute("x")
        self.assertEqual(r["exit_code"], 1)
        self.assertIn("ValueError", r["output"])

    def test_timeout_is_clamped_to_the_service_ceiling(self):
        """Hermes may ask for longer than the service allows; an unclamped value
        is a 400 rather than a timeout the agent can reason about."""
        e, api = self.env()
        e.execute("x", timeout=100000)
        self.assertEqual([c for c in api.calls if c[0] == "run"][0][3],
                         _api.COMMAND_TIMEOUT_MAX)

    def test_timeout_under_the_ceiling_is_passed_through(self):
        e, api = self.env()
        e.execute("x", timeout=30)
        self.assertEqual([c for c in api.calls if c[0] == "run"][0][3], 30)

    def test_timed_out_result_is_annotated(self):
        e, _ = self.env(run_result={"stdout": "", "stderr": "", "exitCode": 124, "timedOut": True})
        self.assertIn("[krutrim]", e.execute("sleep 999")["output"])

    def test_truncation_is_surfaced(self):
        e, _ = self.env(run_result={"stdout": "x", "stderr": "", "exitCode": 0,
                                    "stdoutTruncated": True})
        self.assertIn("truncated", e.execute("x")["output"])


# --------------------------------------------------------------------------
# cleanup()
# --------------------------------------------------------------------------
class TestCleanup(unittest.TestCase):
    """Sandboxes are listed per ACCOUNT, not per key. A cleanup that lists and
    sweeps would destroy a colleague's running work on a shared account."""

    def test_deletes_exactly_one_sandbox_by_id(self):
        api = FakeAPI()
        _env.KrutrimTerminalEnvironment(api, "sb-42").cleanup()
        self.assertEqual([c for c in api.calls if c[0] == "delete"], [("delete", "sb-42")])

    def test_never_lists_sandboxes(self):
        api = FakeAPI()
        e = _env.KrutrimTerminalEnvironment(api, "sb-1")
        e.execute("echo x")
        e.cleanup()
        self.assertFalse([c for c in api.calls if c[0] in ("list", "list_sandboxes")])
        self.assertFalse(hasattr(api, "list_called"))

    def test_is_idempotent(self):
        api = FakeAPI()
        e = _env.KrutrimTerminalEnvironment(api, "sb-1")
        e.cleanup()
        e.cleanup()
        self.assertEqual(len([c for c in api.calls if c[0] == "delete"]), 1)

    def test_swallows_delete_failure(self):
        """A raising cleanup surfaces as a Hermes crash at exit. The finite TTL
        set at create time is the backstop."""
        class Boom(FakeAPI):
            def delete(self, sandbox_id):
                raise err(500, b"nope")
        _env.KrutrimTerminalEnvironment(Boom(), "sb-1").cleanup()  # must not raise

    def test_execute_after_cleanup_does_not_touch_the_api(self):
        api = FakeAPI()
        e = _env.KrutrimTerminalEnvironment(api, "sb-1")
        e.cleanup()
        before = len(api.calls)
        r = e.execute("echo x")
        self.assertEqual(r["exit_code"], 1)
        self.assertEqual(len(api.calls), before)


# --------------------------------------------------------------------------
# Flavor selection
# --------------------------------------------------------------------------
class TestFlavorSelection(unittest.TestCase):
    def setUp(self):
        self.p = _provider.KrutrimProvider()
        self.api = FakeAPI()

    def pick(self, cc, region="In-Bangalore-1"):
        return self.p._pick_flavor(self.api, region, cc)

    def test_smallest_flavor_that_satisfies_the_request(self):
        for cpu, expected in [(0.25, "sandbox-nano"), (0.5, "sandbox-small"),
                              (1, "sandbox-medium"), (2, "sandbox-large"),
                              (4, "sandbox-x-large")]:
            with self.subTest(cpu=cpu):
                self.assertEqual(self.pick({"container_cpu": cpu}), expected)

    def test_request_above_the_largest_flavor_gets_the_largest(self):
        self.assertEqual(self.pick({"container_cpu": 999}), "sandbox-x-large")

    def test_no_request_uses_the_default(self):
        self.assertEqual(self.pick({}), _provider.DEFAULT_FLAVOR)

    def test_non_numeric_request_does_not_raise(self):
        self.assertIn(self.pick({"container_cpu": "not-a-number"}),
                      [f["flavor"] for f in self.api._flavors])

    def test_only_flavors_from_the_requested_region(self):
        self.assertEqual(self.pick({"container_cpu": 0.5}, "In-Hyderabad-1"),
                         "sandbox-small-hyd")

    def test_falls_back_when_the_live_endpoint_fails(self):
        """The live table is authoritative, but a create should not fail just
        because the flavor listing did."""
        class Broken(FakeAPI):
            def flavors(self, region=None):
                raise err(503, b"down")
        got = self.p._pick_flavor(Broken(), "In-Bangalore-1", {"container_cpu": 2})
        self.assertIn(got, [f for f, _ in _provider.FALLBACK_FLAVORS])


# --------------------------------------------------------------------------
# Provider surface
# --------------------------------------------------------------------------
class TestProviderSurface(unittest.TestCase):
    def setUp(self):
        self.p = _provider.KrutrimProvider()

    def test_subclasses_the_hermes_base(self):
        """Registration does an isinstance check and, on failure, logs a warning
        and returns -- `hermes plugins doctor` still reports success while the
        backend is silently absent."""
        from agent.terminal_env_provider import TerminalEnvironmentProvider
        self.assertIsInstance(self.p, TerminalEnvironmentProvider)

    def test_name_does_not_shadow_a_builtin_backend(self):
        builtin = {"local", "docker", "singularity", "modal", "daytona",
                   "vercel_sandbox", "ssh"}
        self.assertNotIn(self.p.name, builtin)

    def test_credential_is_stripped_from_agent_visible_subprocesses(self):
        self.assertIn(_api.API_KEY_ENV, self.p.strip_env_keys)

    def test_probe_surface_makes_no_network_calls(self):
        """`is_available` and `probe` run on every prompt; a network call there
        would stall the agent."""
        import urllib.request
        real = urllib.request.urlopen
        urllib.request.urlopen = lambda *a, **k: self.fail("probe hit the network")
        try:
            import os
            os.environ[_api.API_KEY_ENV] = "test-key"
            self.assertTrue(self.p.is_available())
            self.assertEqual(self.p.probe()[0], "ready")
            self.assertTrue(self.p.doctor_checks()[0][0])
            os.environ.pop(_api.API_KEY_ENV)
            self.assertFalse(self.p.is_available())
            self.assertEqual(self.p.probe()[0], "needs_setup")
        finally:
            urllib.request.urlopen = real

    def test_unknown_region_is_rejected_before_any_api_call(self):
        """A typo in KRUTRIM_SANDBOX_REGION should fail fast and name the valid
        regions, not surface later as an opaque error from the service."""
        import os
        import urllib.request
        os.environ[_api.API_KEY_ENV] = "test-key"
        os.environ["KRUTRIM_SANDBOX_REGION"] = "In-Mumbai-9"
        real = urllib.request.urlopen
        urllib.request.urlopen = lambda *a, **k: self.fail("reached the network")
        try:
            with self.assertRaises(ValueError) as ctx:
                self.p.create_environment(cwd="/app", timeout=60, task_id="t",
                                          image=None, container_config={})
            self.assertIn("In-Bangalore-1", str(ctx.exception))
        finally:
            urllib.request.urlopen = real
            os.environ.pop(_api.API_KEY_ENV, None)
            os.environ.pop("KRUTRIM_SANDBOX_REGION", None)

    def test_bangalore_is_the_only_creatable_region(self):
        """Launch is Bangalore-only. Anything else must be refused here, because
        the API itself still accepts In-Hyderabad-1 and hands back a sandbox that
        is slow to start and does not reliably delete -- and it bills until it does."""
        self.assertEqual(_provider.REGIONS, ("In-Bangalore-1",))

    def test_an_unsupported_region_is_refused_and_says_why(self):
        """A bare 'must be one of' would read as a typo. The user set this value on
        purpose, so the error has to say the region is unsupported, not unrecognised."""
        self.assertIn("In-Hyderabad-1", _provider.UNSUPPORTED_REGIONS)
        os.environ[_api.API_KEY_ENV] = "k" * 16
        os.environ["KRUTRIM_SANDBOX_REGION"] = "In-Hyderabad-1"
        try:
            with self.assertRaises(ValueError) as ctx:
                _provider.KrutrimProvider().create_environment(
                    cwd="/app", timeout=60, task_id="t", image=None, container_config={})
            message = str(ctx.exception)
            self.assertIn("In-Hyderabad-1", message)
            self.assertIn("not supported", message)
            self.assertIn("In-Bangalore-1", message)
        finally:
            os.environ.pop(_api.API_KEY_ENV, None)
            os.environ.pop("KRUTRIM_SANDBOX_REGION", None)


# --------------------------------------------------------------------------
# API client
# --------------------------------------------------------------------------
class TestAPIValidation(unittest.TestCase):
    def setUp(self):
        self.api = _api.KrutrimAPI("test-key")

    def test_ttl_bounds_are_enforced_client_side(self):
        for bad in (_api.TTL_MIN - 1, _api.TTL_MAX + 1):
            with self.subTest(ttl=bad), self.assertRaises(ValueError):
                self.api.set_ttl("sb-1", bad)

    def test_edge_rejection_gets_an_actionable_message(self):
        body = b"<HTML><HEAD> <TITLE>Access Denied</TITLE> </HEAD><BODY>..."
        self.assertIn("runner", str(_api.KrutrimError(403, body, "/commands")))

    def test_error_carries_status_and_path(self):
        e = _api.KrutrimError(404, b'{"message":"nope"}', "/omni/sandbox/v1/sandbox/x")
        self.assertEqual(e.status, 404)
        self.assertIn("/omni/sandbox/v1/sandbox/x", str(e))

    def test_base_url_is_overridable(self):
        self.assertEqual(_api.KrutrimAPI("k", "https://example.test/").base_url,
                         "https://example.test")


class TestRetryPolicy(unittest.TestCase):
    """Transient 5xx occur on healthy sandboxes and succeed on retry; 4xx never
    does, and retrying it would turn a clear error into a slow one."""

    def _api_with(self, statuses):
        import urllib.error, io
        api = _api.KrutrimAPI("test-key")
        seq = list(statuses)
        attempts = []

        def fake_urlopen(req, timeout=None):
            attempts.append(1)
            status = seq.pop(0)
            if status < 400:
                class R:
                    def read(self): return b'{"data":{"ok":true}}'
                    def __enter__(self): return self
                    def __exit__(self, *a): return False
                return R()
            raise urllib.error.HTTPError(req.full_url, status, "err", {}, io.BytesIO(b"{}"))

        import urllib.request
        real = urllib.request.urlopen
        urllib.request.urlopen = fake_urlopen
        real_sleep = _api.time.sleep
        _api.time.sleep = lambda s: None
        self.addCleanup(lambda: setattr(urllib.request, "urlopen", real))
        self.addCleanup(lambda: setattr(_api.time, "sleep", real_sleep))
        return api, attempts

    def test_5xx_is_retried_then_succeeds(self):
        api, attempts = self._api_with([502, 502, 200])
        api._request("GET", "/x")
        self.assertEqual(len(attempts), 3)

    def test_4xx_is_never_retried(self):
        api, attempts = self._api_with([400])
        with self.assertRaises(_api.KrutrimError):
            api._request("GET", "/x")
        self.assertEqual(len(attempts), 1)

    def test_retries_are_bounded(self):
        api, attempts = self._api_with([503] * 20)
        with self.assertRaises(_api.KrutrimError):
            api._request("GET", "/x")
        self.assertEqual(len(attempts), _api.RETRY_ATTEMPTS)


class TestResolveCwd(unittest.TestCase):
    """The service used to reject any cwd it did not already have, so this clamped
    everything outside /app back to /app. Krutrim-Cloud-Service-Omni#106 fixed that
    on 2026-09-17, measured live on a fresh sandbox:

        no cwd -> /app   cwd=/root -> /root   cwd=/tmp -> /tmp   cwd=/ -> /

    The clamp is now wrong rather than merely redundant: it would silently relocate
    an agent that asked for /tmp. What remains is normalisation.
    """

    def test_absent_cwd_uses_the_sandbox_root(self):
        for value in (None, "", "   "):
            self.assertEqual(_env.resolve_cwd(value), "/app")

    def test_a_path_outside_app_is_honoured_not_relocated(self):
        for path in ("/root", "/tmp", "/", "/var/log"):
            with self.subTest(path=path):
                self.assertEqual(_env.resolve_cwd(path), path)

    def test_paths_inside_the_sandbox_root_are_kept(self):
        for good in ("/app", "/app/project", "/app/a/b/c"):
            with self.subTest(path=good):
                self.assertEqual(_env.resolve_cwd(good), good)

    def test_traversal_is_normalised_rather_than_passed_through(self):
        """`..` is resolved here so the service sees one canonical path."""
        self.assertEqual(_env.resolve_cwd("/app/../root"), "/root")
        self.assertEqual(_env.resolve_cwd("/app/sub/.."), "/app")
        self.assertEqual(_env.resolve_cwd("/app//project/"), "/app/project")


class TestSandboxTools(unittest.TestCase):
    """The explicit sandbox_* tools: schema, mutation classification and approval."""

    def test_every_operation_has_a_usable_schema(self):
        for op in _tools.OPERATIONS:
            with self.subTest(tool=op.name):
                schema = op.schema
                self.assertTrue(schema["description"])
                props = schema["parameters"]["properties"]
                for key in op.required:
                    self.assertIn(key, props, f"{op.name}: required {key} missing from schema")
                self.assertFalse(schema["parameters"]["additionalProperties"])

    def test_mutation_is_derived_from_the_method_not_declared(self):
        """A new write cannot escape approval by someone forgetting a flag."""
        self.assertFalse(_tools.BY_NAME["sandbox_list_ports"].mutates)
        self.assertFalse(_tools.BY_NAME["sandbox_get_sandbox"].mutates)
        self.assertTrue(_tools.BY_NAME["sandbox_delete_sandbox"].mutates)
        self.assertTrue(_tools.BY_NAME["sandbox_open_port"].mutates)
        for op in _tools.OPERATIONS:
            self.assertEqual(op.mutates, op.method != "GET", op.name)

    def test_reads_are_not_interrupted_for_approval(self):
        self.assertIsNone(_tools.approval_for("sandbox_list_ports", {"sandbox_id": "sb1"}))
        self.assertIsNone(_tools.approval_for("not_a_tool", {}))

    def test_destructive_calls_ask_first_and_name_the_target(self):
        decision = _tools.approval_for("sandbox_delete_sandbox", {"sandbox_id": "sb1"})
        self.assertEqual(decision["action"], "approve")
        self.assertIn("sb1", decision["message"])
        self.assertIn("DESTRUCTIVE", decision["message"])

    def test_a_malformed_destructive_call_is_blocked_not_approved(self):
        decision = _tools.approval_for("sandbox_delete_sandbox", {})
        self.assertEqual(decision["action"], "block")

    def test_path_parameters_are_substituted_into_the_route(self):
        path, query, body, raw = _tools.build_request(
            _tools.BY_NAME["sandbox_close_port"], {"sandbox_id": "sb1", "port": 8080})
        self.assertTrue(path.endswith("/sb1/ports/8080"), path)
        self.assertIsNone(raw)

    def test_missing_required_argument_is_refused_before_any_request(self):
        with self.assertRaises(_tools.ToolError):
            _tools.build_request(_tools.BY_NAME["sandbox_get_sandbox"], {})

    def test_unexpected_arguments_are_refused(self):
        with self.assertRaises(_tools.ToolError):
            _tools.build_request(_tools.BY_NAME["sandbox_get_sandbox"],
                                 {"sandbox_id": "sb1", "rm_rf": "/"})

    def test_upload_decodes_base64_and_bounds_it(self):
        import base64 as b64
        _, _, _, raw = _tools.build_request(
            _tools.BY_NAME["sandbox_write_file"],
            {"sandbox_id": "sb1", "path": "/app/x", "data_base64": b64.b64encode(b"hi").decode()})
        self.assertEqual(raw, b"hi")

        with self.assertRaises(_tools.ToolError):
            _tools.build_request(_tools.BY_NAME["sandbox_write_file"],
                                 {"sandbox_id": "sb1", "path": "/app/x", "data_base64": "not!base64"})

        huge = b64.b64encode(b"x" * (_tools.MAX_UPLOAD_BYTES + 1)).decode()
        with self.assertRaises(_tools.ToolError):
            _tools.build_request(_tools.BY_NAME["sandbox_write_file"],
                                 {"sandbox_id": "sb1", "path": "/app/x", "data_base64": huge})


class TestVersionConsistency(unittest.TestCase):
    """pyproject's version is the one that ships.

    `krutrim.__version__` is cosmetic; the version in pyproject.toml is what pip
    resolves against and what `hermes plugins list` displays. When they drift, the
    symptom is silent and bad: `pip install --upgrade` decides the user is already
    current and installs nothing, so new tools never arrive, and the plugin list
    reports a version the code is not.
    """

    def _pyproject_version(self) -> str:
        text = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text()
        for line in text.splitlines():
            if line.startswith("version = "):
                return line.split("=", 1)[1].strip().strip('"')
        self.fail("no version in pyproject.toml")

    def test_packaged_version_matches_the_module(self):
        import krutrim
        self.assertEqual(
            self._pyproject_version(), krutrim.__version__,
            "pyproject.toml and krutrim.__version__ disagree; pip ships the former, "
            "so a bump to only one of them means users never receive the change",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
