# Changelog

## 0.1.0

First release. Runs Hermes agent commands in an Ola Krutrim Cloud Sandbox.

- Terminal backend registered as `krutrim`; no fork of `hermes-agent`.
- Commands are dispatched through a one-time runner with a base64 argument, which
  keeps the edge WAF's blocked operators (`;`, `&&`, `||`) off the wire. Benchmarked
  45% faster than uploading a script per command.
- Flavor selection reads the live `/flavors` endpoint; `container_cpu` maps onto
  vCPUs. `container_memory` and `container_disk` are accepted and ignored because
  the service reports no per-flavor value for either.
- Bounded retry on transient 5xx from `/files` and `/commands`.
- `execute()` waits out a 409 `sandbox is not active` rather than failing.
- Per-region create timeouts (Bangalore 240s, Hyderabad 900s).
- `cleanup()` deletes only the sandbox it created, by id.
