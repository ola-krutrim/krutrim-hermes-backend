"""Minimal Krutrim Sandbox REST client. Standard library only.

Deliberately does NOT depend on `krutrim-client`: the plugin needs four endpoints,
and vendoring an SDK into a Hermes plugin adds a dependency Hermes users did not ask
for. The wire contract below was read out of krutrim-client 0.6.1 and then verified
live against https://cloud.olakrutrim.com.

Wire note: request fields are camelCase (`sandboxName`, `flavorName`, `ttlSeconds`,
`timeoutSeconds`). The snake_case names in krutrim-client are client-side only and
are rejected by the service -- a snake_case create returns
`400 "sandbox name: function name is required"`, which reads like a missing field
rather than a misspelled one.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Mapping

DEFAULT_BASE_URL = "https://cloud.olakrutrim.com"
API_KEY_ENV = "KRUTRIMCLIENT_API_KEY"

# Service-side limits, mirrored from krutrim-client's validators so we fail with a
# clear message instead of a 400.
COMMAND_TIMEOUT_MIN, COMMAND_TIMEOUT_MAX = 1, 270
TTL_MIN, TTL_MAX = 60, 604800

# Transient 5xx retry. Bounded: a real outage should surface to the agent quickly,
# not be hidden behind a long stall.
RETRY_ATTEMPTS = 4
RETRY_BACKOFF = 0.4


class KrutrimError(RuntimeError):
    def __init__(self, status: int, body: bytes, path: str):
        self.status = status
        self.body = body
        snippet = body[:300].decode("utf-8", "replace").strip()
        if b"<TITLE>Access Denied" in body or b"Access Denied" in body[:200]:
            snippet = ("blocked by the edge WAF (403 Access Denied). A command containing "
                       "';', '&&' or '||' was sent inline -- it must go through the runner.")
        super().__init__(f"{path} -> HTTP {status}: {snippet}")


class KrutrimAPI:
    def __init__(self, api_key: str, base_url: str | None = None, *, request_timeout: float = 180.0):
        self.api_key = api_key
        self.base_url = (base_url or os.environ.get("KRUTRIM_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
        self.request_timeout = request_timeout

    def _request(self, method: str, path: str, *, body: Any = None, raw: bytes | None = None,
                 query: Mapping[str, str] | None = None,
                 content_type: str = "application/json") -> Any:
        url = self.base_url + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", f"Bearer {self.api_key}")
        req.add_header("Accept", "application/json")
        if data is not None:
            req.add_header("Content-Type", content_type)
        # The service returns transient 5xx on both /files and /commands under normal
        # use -- observed live as 502 "failed to upload file to sandbox" and 502
        # "failed to run command in sandbox" on a healthy, active sandbox, roughly
        # 2 in 4 calls in one unlucky run. They succeed on retry. Without this a
        # terminal backend is unusable, so 5xx is retried and 4xx never is.
        last: KrutrimError | None = None
        for attempt in range(RETRY_ATTEMPTS):
            try:
                with urllib.request.urlopen(req, timeout=self.request_timeout) as resp:
                    payload = resp.read()
                break
            except urllib.error.HTTPError as exc:
                err = KrutrimError(exc.code, exc.read(), path)
                if exc.code < 500 or attempt == RETRY_ATTEMPTS - 1:
                    raise err from None
                last = err
                time.sleep(RETRY_BACKOFF * (2 ** attempt))
            except urllib.error.URLError:
                if attempt == RETRY_ATTEMPTS - 1:
                    raise
                time.sleep(RETRY_BACKOFF * (2 ** attempt))
        else:  # pragma: no cover - loop always breaks or raises
            raise last if last else RuntimeError(f"{path}: exhausted retries")
        if not payload:
            return {}
        try:
            return json.loads(payload)
        except ValueError:
            return {"_raw": payload}

    # --- sandbox lifecycle -------------------------------------------------
    def create(self, *, name: str, region: str, flavor: str, ttl_seconds: int | None = None,
               env: Mapping[str, str] | None = None) -> str:
        body: dict[str, Any] = {"sandboxName": name, "region": region, "flavorName": flavor}
        if ttl_seconds is not None:
            if not TTL_MIN <= ttl_seconds <= TTL_MAX:
                raise ValueError(f"ttl_seconds must be {TTL_MIN}..{TTL_MAX}, got {ttl_seconds}")
            body["ttlSeconds"] = ttl_seconds
        if env:
            body["environmentVariables"] = dict(env)
        return self._request("POST", "/omni/sandbox/v1/sandbox", body=body)["data"]["id"]

    def status(self, sandbox_id: str) -> str:
        data = self._request("GET", f"/omni/sandbox/v1/sandbox/{sandbox_id}").get("data", {})
        return str(data.get("status") or data.get("state") or "").lower()

    def wait_active(self, sandbox_id: str, *, timeout: float = 240.0, interval: float = 2.0,
                    region: str | None = None) -> None:
        """`create` returns 202 while the sandbox is still `deploying`; the first command
        against a deploying sandbox fails. Callers must not skip this."""
        deadline = time.time() + timeout
        state = ""
        while time.time() < deadline:
            state = self.status(sandbox_id)
            if state in ("active", "running"):
                return
            if state in ("failed", "error", "deleted", "terminated"):
                raise RuntimeError(f"sandbox {sandbox_id} entered terminal state {state!r}")
            time.sleep(interval)
        where = f" in {region}" if region else ""
        raise TimeoutError(
            f"sandbox {sandbox_id} still {state!r} after {timeout:.0f}s{where}. "
            "Hyderabad provisioning is known to be slow; try In-Bangalore-1 "
            "(KRUTRIM_SANDBOX_REGION) if this persists.")

    def set_ttl(self, sandbox_id: str, ttl_seconds: int) -> None:
        if not TTL_MIN <= ttl_seconds <= TTL_MAX:
            raise ValueError(f"ttl_seconds must be {TTL_MIN}..{TTL_MAX}, got {ttl_seconds}")
        self._request("POST", f"/omni/sandbox/v1/sandbox/{sandbox_id}/ttl",
                      body={"ttlSeconds": ttl_seconds})

    def delete(self, sandbox_id: str) -> None:
        self._request("DELETE", f"/omni/sandbox/v1/sandbox/{sandbox_id}")

    # --- files and commands ------------------------------------------------
    def upload(self, sandbox_id: str, path: str, content: bytes) -> None:
        self._request("POST", f"/omni/sandbox/v1/sandbox/{sandbox_id}/files",
                      raw=content, query={"path": path},
                      content_type="application/octet-stream")

    def run(self, sandbox_id: str, cmd: str, *, timeout_seconds: int = 60,
            cwd: str | None = None, env: Mapping[str, str] | None = None) -> dict:
        ts = max(COMMAND_TIMEOUT_MIN, min(COMMAND_TIMEOUT_MAX, int(timeout_seconds)))
        body: dict[str, Any] = {"cmd": cmd, "timeoutSeconds": ts}
        if cwd:
            body["cwd"] = cwd
        if env:
            body["envs"] = dict(env)
        return self._request("POST", f"/omni/sandbox/v1/sandbox/{sandbox_id}/commands",
                             body=body).get("data", {})

    def flavors(self, region: str | None = None) -> list[dict]:
        """Live flavor table. krutrim-client 0.6.1's `list_flavors()` returns empty
        objects, which is a client bug -- the REST endpoint returns full rows, so
        there is no need to hardcode a table that would go stale."""
        q = {"region": region} if region else None
        rows = self._request("GET", "/omni/sandbox/v1/flavors", query=q).get("data", []) or []
        out = []
        for row in rows:
            g = row.get("groupBy", row) or {}
            out.append({"flavor": g.get("flavorid") or g.get("flavorname"),
                        "region": row.get("subject"),
                        "vcpus": float(g.get("vcpus") or 0),
                        "cost_per_hour": g.get("cost")})
        return [f for f in out if f["flavor"]]
