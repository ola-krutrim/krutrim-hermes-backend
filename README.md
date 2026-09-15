# Krutrim Sandbox — Hermes terminal backend

Run [Hermes](https://github.com/NousResearch/hermes-agent) agent commands inside an
[Ola Krutrim Cloud Sandbox](https://cloud.olakrutrim.com) instead of on your own machine.
It installs as a Hermes **plugin** — no fork of `hermes-agent`, no upstream pull request.

> **Beta.** Krutrim Sandbox is in beta and has known issues. Expect rough edges, particularly
> around provisioning time in some regions and the occasional request that needs a retry. This
> plugin handles what it can on your behalf. Please report anything you hit.

```bash
export KRUTRIMCLIENT_API_KEY=<your Krutrim Cloud API key>

cp -R krutrim ~/.hermes/plugins/krutrim
hermes plugins enable krutrim
hermes config set terminal.backend krutrim
```

To go back to local execution: `hermes config set terminal.backend local`. The plugin can stay
installed — only that setting decides where commands run.

## What to expect

Measured in `In-Bangalore-1` on `sandbox-small`, driven through Hermes's own environment factory:

| | |
|---|---|
| command round-trip | median **0.28 s**, p90 0.40 s |
| new sandbox ready to use | about **5 seconds** |
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

## Licence

Apache-2.0. See [LICENSE](LICENSE), [COPYRIGHT.md](COPYRIGHT.md),
[SUPPLEMENTAL-TERMS.md](SUPPLEMENTAL-TERMS.md) and [TRADEMARK.md](TRADEMARK.md).
