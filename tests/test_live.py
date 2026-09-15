"""End-to-end tests against the real Krutrim Sandbox service.

Skipped unless BOTH are set, because they create and delete real sandboxes and
cost real money:

    KRUTRIM_LIVE_TESTS=1 KRUTRIMCLIENT_API_KEY=<key> \
        python3 -m unittest discover -s tests -v

Each test creates its own sandbox and deletes it in tearDown, by id. It never
lists sandboxes: the account is shared, and a list-and-sweep cleanup would
destroy someone else's running work.
"""

import os
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from test_unit import _install_hermes_stub  # noqa: E402

_install_hermes_stub()

from krutrim._api import KrutrimAPI  # noqa: E402
from krutrim._env import KrutrimTerminalEnvironment  # noqa: E402
from krutrim._provider import KrutrimProvider  # noqa: E402

LIVE = os.environ.get("KRUTRIM_LIVE_TESTS") == "1" and os.environ.get("KRUTRIMCLIENT_API_KEY")


@unittest.skipUnless(LIVE, "set KRUTRIM_LIVE_TESTS=1 and KRUTRIMCLIENT_API_KEY to run")
class TestLiveSandbox(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.provider = KrutrimProvider()
        cls.api = KrutrimAPI(os.environ["KRUTRIMCLIENT_API_KEY"])
        t0 = time.time()
        cls.env = cls.provider.create_environment(
            cwd="/app", timeout=60, task_id="unittest", image=None,
            container_config={"container_cpu": 1})
        cls.ready_seconds = time.time() - t0

    @classmethod
    def tearDownClass(cls):
        cls.env.cleanup()

    def test_sandbox_became_usable(self):
        self.assertLess(self.ready_seconds, 240,
                        f"create took {self.ready_seconds:.0f}s")

    def test_plain_command(self):
        self.assertEqual(self.env.execute("echo hello"),
                         {"output": "hello\n", "exit_code": 0})

    def test_exit_code_propagates(self):
        self.assertEqual(self.env.execute("exit 7")["exit_code"], 7)

    def test_chaining_operators_survive_the_edge(self):
        """`;`, `&&` and `||` are rejected if sent as raw shell. They must work
        through the runner -- this is the single most important live check."""
        for cmd, expect in [("pwd; whoami", "/app"),
                            ("true && echo chained", "chained"),
                            ("false || echo fell", "fell")]:
            with self.subTest(cmd=cmd):
                r = self.env.execute(cmd)
                self.assertEqual(r["exit_code"], 0, r)
                self.assertIn(expect, r["output"])

    def test_newlines_and_quotes_survive(self):
        self.assertEqual(self.env.execute("printf 'a\\nb\\n'")["output"], "a\nb\n")
        self.assertIn("it's", self.env.execute("echo \"it's\"")["output"])

    def test_the_real_hermes_wrapper_shape(self):
        """Hermes wraps every command in this, including the mktemp/&&/|| snapshot
        block. If this passes, a real Hermes session works."""
        wrapped = "\n".join([
            'export AI_AGENT="${AI_AGENT:-hermes-agent}" HERMES_AGENT="${HERMES_AGENT:-true}"',
            'builtin cd -- "/app" || exit 126',
            "eval 'echo wrapped && id -un'",
            "__hermes_ec=$?",
            "umask 077",
            '__hermes_snap_tmp=$(mktemp /app/.snap.XXXXXXXXXX) && '
            '{ export -p > "$__hermes_snap_tmp" && mv -f "$__hermes_snap_tmp" /app/.snap; } '
            '2>/dev/null || rm -f "$__hermes_snap_tmp" 2>/dev/null || true',
            'printf "\\n__CWD__%s__CWD__\\n" "$PWD"',
            "exit $__hermes_ec",
        ])
        r = self.env.execute(wrapped)
        self.assertEqual(r["exit_code"], 0, r)
        self.assertIn("wrapped", r["output"])
        self.assertIn("__CWD__/app__CWD__", r["output"])

    def test_failure_is_returned_not_raised(self):
        r = self.env.execute("cat /definitely-not-here")
        self.assertNotEqual(r["exit_code"], 0)
        self.assertIn("No such file", r["output"])

    def test_oversized_timeout_is_clamped_not_rejected(self):
        self.assertEqual(self.env.execute("echo ok", timeout=100000)["exit_code"], 0)

    def test_requested_size_is_applied_by_the_platform(self):
        """`nproc` inside reports the node's cores, not the limit, so verify the
        applied flavor through the API rather than from inside the sandbox."""
        data = self.api._request(
            "GET", f"/omni/sandbox/v1/sandbox/{self.env.sandbox_id}")["data"]
        self.assertEqual(int(data["noCpus"]), 1,
                         f"container_cpu=1 gave {data.get('flavorName')}")


@unittest.skipUnless(LIVE, "set KRUTRIM_LIVE_TESTS=1 and KRUTRIMCLIENT_API_KEY to run")
class TestLiveCleanup(unittest.TestCase):
    def test_cleanup_deletes_the_sandbox(self):
        env = KrutrimProvider().create_environment(
            cwd="/app", timeout=60, task_id="cleanup", image=None, container_config={})
        sid = env.sandbox_id
        env.cleanup()
        time.sleep(3)
        api = KrutrimAPI(os.environ["KRUTRIMCLIENT_API_KEY"])
        try:
            state = api.status(sid)
        except Exception:
            state = "gone"
        self.assertTrue(state in ("deleting", "deleted", "terminated", "gone", ""),
                        f"sandbox {sid} still {state!r} after cleanup")


if __name__ == "__main__":
    unittest.main(verbosity=2)
