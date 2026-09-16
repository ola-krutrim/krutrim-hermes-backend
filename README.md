# Krutrim Sandbox — Hermes terminal backend

Run [Hermes](https://github.com/NousResearch/hermes-agent) agent commands inside an
[Ola Krutrim Cloud Sandbox](https://cloud.olakrutrim.com) instead of on your own machine.
It installs as a Hermes **plugin** — no fork of `hermes-agent`, no upstream pull request.

> **Beta.** Krutrim Sandbox is in beta and has known issues. Expect rough edges, particularly
> around provisioning time in some regions and the occasional request that needs a retry. This
> plugin handles what it can on your behalf.
>
> Report anything you hit via [GitHub Issues](https://github.com/ola-krutrim/krutrim-hermes-backend/issues)
> for the plugin, or **cloudsupport@olakrutrim.com** for the sandbox service itself.

```bash
git clone https://github.com/ola-krutrim/krutrim-hermes-backend
rm -rf ~/.hermes/plugins/krutrim          # only if you are reinstalling
cp -R krutrim-hermes-backend/krutrim ~/.hermes/plugins/krutrim

export KRUTRIMCLIENT_API_KEY=<your Krutrim Cloud API key>
hermes plugins enable krutrim
hermes config set terminal.backend krutrim
```

The `rm -rf` line matters on a reinstall: `cp -R` into an existing directory *nests* the copy, so
you end up with `~/.hermes/plugins/krutrim/krutrim/` and Hermes finds no plugin.

To go back to local execution: `hermes config set terminal.backend local`. The plugin can stay
installed — only that setting decides where commands run.

Enabling takes effect on your next Hermes session.

## What to expect

Measured in `In-Bangalore-1` on `sandbox-small`, driven through Hermes's own environment factory:

| | |
|---|---|
| command round-trip | median **0.28 s**, p90 0.40 s |
| new sandbox ready to use | about **5 seconds** in Bangalore; Hyderabad can take minutes |
| commands completed in a 30-command run | **30 of 30** |

A sandbox is created on the first command of a session and deleted when the session ends.

## Configuration

| Variable | Default | Notes |
|---|---|---|
| `KRUTRIMCLIENT_API_KEY` | — | Required. Declared in `strip_env_keys`, so Hermes removes it from every subprocess the agent can see. |
| `KRUTRIM_SANDBOX_REGION` | `In-Bangalore-1` | Or `In-Hyderabad-1`. Bangalore is recommended during beta — provisioning elsewhere currently takes considerably longer. |
| `KRUTRIM_SANDBOX_FLAVOR` | chosen from `container_cpu` | Pin a size, e.g. `sandbox-medium`. |
| `KRUTRIM_SANDBOX_TTL_SECONDS` | `3600` | Backstop lifetime, in case a crash skips the normal cleanup. |

Sizes are read from the live `/flavors` endpoint rather than a table baked into the plugin, so the
list cannot go stale. Hermes's `container_cpu` request selects the smallest size that satisfies it.

`container_memory` and `container_disk` are accepted and **ignored**: the service reports no
per-size value for either, so there is nothing to map them onto. Saying so is better than
pretending to honour them.

## Verifying the install

```bash
hermes doctor          # expect: ✓ Krutrim API key found
hermes plugins list    # expect: krutrim | enabled
```

`hermes plugins doctor krutrim` is **not** a sufficient check here — it counts tools and hooks and
never counts terminal backends, so it reports success whether or not the backend registered. If
`hermes doctor` says `Unknown terminal backend 'krutrim'`, it did not register.

If you are modifying this plugin, note that the provider class must *inherit* from
`TerminalEnvironmentProvider`. One that merely matches the interface is dropped with a log warning
and no error.

## If a command fails with "Unknown environment type"

```
Unknown environment type: krutrim. Use 'local', 'docker', 'singularity', 'modal',
'daytona', 'vercel_sandbox', 'ssh'
```

This means Hermes reached the plugin lookup and found nothing registered under `krutrim`. **It does
not mean Hermes cannot use plugin backends**, and it is not a bad API key, a missing sandbox, or a
provisioning failure. Read the list in the message — Hermes appends every registered plugin backend
to it. If the list ends at `'ssh'`, **no plugin backend is registered at all** in that session; if
this plugin were loaded, `'krutrim'` would appear there.

**First, start a new Hermes session.** This is the most common cause by far. Plugin discovery runs
once per session and is then cached (`discover_and_load` returns early when `self._discovered`), so
a session that was already running when you enabled the plugin keeps its old backend and cannot see
this one. Enabling takes effect on the **next** session, not the current one.

If a new session still fails, two commands separate the remaining causes:

```bash
hermes plugins list | grep krutrim
hermes doctor
```

| `plugins list` | `hermes doctor` | cause | fix |
|---|---|---|---|
| krutrim absent | — | not installed for this Hermes home | run the install above, in **that** environment |
| krutrim `enabled` | `Unknown terminal backend 'krutrim'` | loaded, but registration was dropped | the provider class does not inherit `TerminalEnvironmentProvider` — reinstall from this repo |
| krutrim `enabled` | no backend error | the plugin is fine **where you ran these commands** — so the failing session is a different one: stale, or a different home or machine | see the two notes below |

Two things that commonly explain an install you believe you did:

- **A different Hermes home.** Plugins are discovered per home directory, so a session started with
  a different `HERMES_HOME` does not see `~/.hermes/plugins/`. Check it in the failing session, not
  the one you installed from.
- **A different machine or container.** Installing on your laptop does not install into a hosted or
  containerised agent session. Install where the agent actually runs.

Set `HERMES_PLUGINS_DEBUG=1` to print plugin discovery and loading as it happens.

⚠️ `hermes plugins doctor krutrim` reports "import and registration passed" in **every** case above,
including the ones that are broken. It counts tools and hooks and never counts terminal backends.
Use `hermes doctor`.

## How commands are dispatched

Hermes wraps every command it runs in a shell script of its own. Rather than sending that script as
raw shell, the plugin installs a small runner into the sandbox once and passes each command to it
encoded. That keeps agent-generated command text away from the edge filtering in front of the
service, and it measured about twice as fast as the alternative of uploading a script file per
command:

```
upload a script per command (2 API calls)   median 0.47s
install runner once, encoded argument       median 0.26s
```

The plugin also retries transient server errors with backoff, and waits rather than failing when a
command arrives while a sandbox is still starting.

## Other agents — Claude Code, Codex, Cursor

**This plugin is Hermes-only, and necessarily so.** It implements Hermes's
`TerminalEnvironmentProvider` contract — a Hermes-specific extension point. Claude Code, Codex and
Cursor have no equivalent "swap the terminal backend" hook, so there is nothing to port.

**If you use one of those,** [ola-krutrim/Krutrim-MCP](https://github.com/ola-krutrim/Krutrim-MCP)
ships guarded Sandbox tools from v1.0.3 — lifecycle, command execution and file transfer — over MCP,
which all three speak.

> ⚠️ **It needs a different credential.** Verified against `krutrim-mcp-server` 1.0.3: API-key
> authentication is disabled, and the server refuses to start if `KRUTRIMCLIENT_API_KEY` is set:
>
> ```
> Authentication configuration error: API-key authentication is disabled in this release.
> Remove KRUTRIM_API_KEY and its legacy aliases, then configure both
> KRUTRIM_ACCESS_TOKEN and KRUTRIM_REFRESH_TOKEN.
> ```
>
> So the sandbox API key that this plugin uses will **not** get you into Krutrim-MCP — you need IAM
> access and refresh tokens instead. Worth knowing before you plan an afternoon around it.

The two are also different in kind, and it is worth knowing which you want:

| | what it does |
|---|---|
| **Krutrim-MCP** | Gives the agent sandbox *tools it can choose to call*, alongside tools for the rest of Krutrim Cloud. The agent's own shell still runs locally. |
| **This plugin** | *Redirects the agent's shell.* Every command Hermes already runs goes to the sandbox instead of your machine — no prompt changes, no new tools to learn. |

So MCP is the right shape for "let the agent manage cloud resources", and this plugin is the right
shape for "don't run agent-authored commands on my laptop".

## Using the sandbox directly

The plugin is a thin layer over a small REST API under
`https://cloud.olakrutrim.com/omni/sandbox/v1/`, with a bearer token. If you are building your own
tooling rather than using Hermes, four things are worth knowing up front:

- **Wire field names are camelCase** — `sandboxName`, `flavorName`, `ttlSeconds`, `timeoutSeconds`.
  The snake_case spellings in the Python SDK are client-side only.
- **Sandbox names are capped at 28 characters**, must start with a lowercase letter, end
  alphanumeric, and contain only lowercase letters, digits and hyphens.
- **A single command is capped at 270 seconds.**
- **Create returns while the sandbox is still starting** — poll until `status` is `active` before
  sending the first command.

## Design notes

- **No runtime dependencies.** The backend speaks four REST endpoints over the Python standard
  library. It deliberately does not import `krutrim-client`: four endpoints do not justify pushing
  an SDK into the environment of everyone who installs a Hermes plugin.
- **`cleanup()` deletes one sandbox, by id.** The plugin never lists sandboxes. Sandboxes are
  listed per *account*, not per key, so if you write your own cleanup, delete only the ids you
  created — a name-prefix sweep will destroy a colleague's running work.
- **A finite TTL is always set** so a crash that skips cleanup cannot leak a sandbox indefinitely.

## Tests

Standard library only — no test dependency, matching the plugin itself.

```bash
python3 -m unittest discover -s tests -v
```

50 tests. 40 run offline with no API key and no Hermes install; the other 10 talk to the real
service and are skipped unless you opt in:

```bash
KRUTRIM_LIVE_TESTS=1 KRUTRIMCLIENT_API_KEY=<key> python3 -m unittest discover -s tests -v
```

Live tests create and delete a real sandbox each, by id. They never list sandboxes — the account is
shared, and a list-and-sweep cleanup would destroy someone else's running work.

The offline suite is mutation-checked: eleven deliberate defects were introduced one at a time —
dropping the name-length cap, sending commands as raw shell, retrying 4xx, letting `execute()`
raise, removing the credential from `strip_env_keys`, stopping `cleanup()` from deleting — and all
eleven were caught. A suite that cannot fail is not evidence.

## Licence

Apache-2.0. See [LICENSE](LICENSE), [COPYRIGHT.md](COPYRIGHT.md),
[SUPPLEMENTAL-TERMS.md](SUPPLEMENTAL-TERMS.md) and [TRADEMARK.md](TRADEMARK.md).
