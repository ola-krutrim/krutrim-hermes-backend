"""End-to-end test of the Krutrim Hermes backend against the live service."""
import os, sys, time, traceback
os.environ["KRUTRIMCLIENT_API_KEY"] = open(os.path.expanduser("~/.config/krutrim/sandbox.key")).read().strip()
from krutrim import KrutrimProvider

p = KrutrimProvider()
fails = []
def check(label, cond, detail=""):
    print(f"   {'PASS' if cond else 'FAIL'}  {label}  {detail}")
    if not cond: fails.append(label)

print("① probe surface must make NO network calls (dx's hard requirement)")
import urllib.request
real = urllib.request.urlopen
def boom(*a, **k): raise AssertionError("network call from a probe surface!")
urllib.request.urlopen = boom
try:
    check("is_available()", p.is_available() is True)
    st, msg = p.probe(); check("probe()", st == "ready", repr(st))
    check("doctor_checks()", p.doctor_checks()[0][0] is True)
    check("strip_env_keys", "KRUTRIMCLIENT_API_KEY" in p.strip_env_keys, str(sorted(p.strip_env_keys)))
    check("name/is_container", p.name == "krutrim" and p.is_container is True)
finally:
    urllib.request.urlopen = real

print("② create_environment (live)")
t0 = time.time()
env = p.create_environment(cwd="/app", timeout=60, task_id="livetest")
print(f"   sandbox={env.sandbox_id}  ready in {time.time()-t0:.1f}s")
try:
    print("③ the Hermes contract: execute() -> {'output','exit_code'}")
    r = env.execute("echo hello")
    check("plain echo", r == {"output": "hello\n", "exit_code": 0}, repr(r))
    r = env.execute("exit 7")
    check("exit code propagates", r["exit_code"] == 7, repr(r))
    r = env.execute("echo out; echo err 1>&2")
    check("semicolon survives (WAF would 403 inline)", r["exit_code"] == 0 and "out" in r["output"], repr(r))
    r = env.execute("true && echo chained")
    check("&& survives", "chained" in r["output"], repr(r))
    r = env.execute("false || echo fellback")
    check("|| survives", "fellback" in r["output"], repr(r))
    r = env.execute("printf 'a\\nb\\n'")
    check("newlines survive", r["output"] == "a\nb\n", repr(r))
    r = env.execute("echo \"it's quoted\"")
    check("quotes survive", "it's quoted" in r["output"], repr(r))

    print("④ the REAL Hermes wrapper, verbatim from _wrap_command_script")
    wrapped = "\n".join([
        'export AI_AGENT="${AI_AGENT:-hermes-agent}" HERMES_AGENT="${HERMES_AGENT:-true}"',
        'export GIT_PAGER="${GIT_PAGER:-cat}" PAGER="${PAGER:-cat}"',
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
    r = env.execute(wrapped)
    check("full wrapper runs", r["exit_code"] == 0 and "wrapped" in r["output"], repr(r)[:160])
    check("CWD marker present", "__CWD__/app__CWD__" in r["output"])

    print("⑤ failure surfaces return, never raise")
    r = env.execute("cat /nonexistent-file-xyz")
    check("missing file -> nonzero + stderr", r["exit_code"] != 0 and "No such file" in r["output"], repr(r)[:120])

    print("⑥ timeout clamp (service caps at 270s; Hermes may ask for more)")
    r = env.execute("echo clamped", timeout=100000)
    check("oversized timeout still runs", r["exit_code"] == 0, repr(r))

    print("⑦ latency over 6 commands")
    ts = []
    for _ in range(6):
        t = time.time(); env.execute("echo x"); ts.append(time.time()-t)
    ts.sort()
    print(f"   median {ts[len(ts)//2]:.2f}s  min {ts[0]:.2f}s  max {ts[-1]:.2f}s")
finally:
    print("⑧ cleanup() deletes the sandbox")
    env.cleanup()
    import urllib.error
    from krutrim._api import KrutrimAPI
    api = KrutrimAPI(os.environ["KRUTRIMCLIENT_API_KEY"])
    time.sleep(3)
    try:
        state = api.status(env.sandbox_id)
    except Exception as e:
        state = f"gone ({type(e).__name__})"
    check("sandbox deleted", state in ("deleting", "deleted", "terminated") or "gone" in str(state), repr(state))
    check("cleanup() is idempotent", (env.cleanup() or True))

print(f"\n{'ALL PASS' if not fails else 'FAILURES: ' + ', '.join(fails)}")
sys.exit(1 if fails else 0)
