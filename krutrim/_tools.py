"""Explicit `sandbox_*` tools, alongside the terminal backend.

WHY BOTH. The terminal backend answers "run this command somewhere that is not my
laptop", and for that it is the whole story — the agent types shell and the shell
runs remotely. These tools cover what a shell inside the sandbox cannot reach:

    ports      exposing a service to the outside is an API operation, not a command
    lifecycle  creating, listing and deleting sandboxes other than your own
    files      moving bytes in and out without base64-ing through a command line

They are a second dispatch surface. A session with `terminal.backend=krutrim` gets
both; a session using a different backend can still drive sandboxes with these.

The operations are a table rather than eighteen hand-written functions: one row per
endpoint, with mutation derived from the HTTP method rather than restated per tool.
Eighteen functions drift apart; a table cannot. It also means a new read cannot be
marked destructive by accident, and a new write cannot escape approval because
someone forgot a flag.
"""

from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass, field
from typing import Any

from ._api import KrutrimAPI, KrutrimError

#: Refused before the request is built. The service has its own limits; these keep
#: an agent from assembling something absurd in the first place.
MAX_UPLOAD_BYTES = 1024 * 1024
MAX_RESULT_BYTES = 2 * 1024 * 1024
MAX_TTL_SECONDS = 604_800  # 7 days
SANDBOX_BASE = "/omni/sandbox/v1/sandbox"


def _text(description: str, maximum: int = 4096, minimum: int = 1,
          pattern: str | None = None) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "string", "description": description,
                              "minLength": minimum, "maxLength": maximum}
    if pattern:
        schema["pattern"] = pattern
    return schema


def _integer(description: str, minimum: int, maximum: int) -> dict[str, Any]:
    return {"type": "integer", "description": description,
            "minimum": minimum, "maximum": maximum}


#: Field definitions shared across operations, so a parameter means the same thing
#: and carries the same bounds wherever it appears.
FIELDS: dict[str, dict[str, Any]] = {
    "sandbox_id": _text("Sandbox id exactly as `create` returned it — not a name or URL.",
                        128, pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]*$"),
    "region": _text("Service region. In-Bangalore-1 is the only supported region at launch.", 128),
    "sandboxName": _text("Name for the new sandbox. Capped at 28 characters by the service.", 28),
    "flavorName": _text("Exact flavor name from sandbox_list_flavors.", 256),
    "ttlSeconds": _integer("Lifetime in seconds. The sandbox bills until it is deleted or this expires.",
                           1, MAX_TTL_SECONDS),
    "page": _integer("Page number.", 1, 1_000_000),
    "limit": _integer("Page size.", 1, 1000),
    "path": _text("Path INSIDE the sandbox, literal and unencoded. Never a path on this machine."),
    "newPath": _text("Destination path inside the sandbox, literal and unencoded."),
    "depth": _integer("How deep to list.", 0, 32),
    "data_base64": _text("Base64 of the bytes to upload. No file on this machine is read.",
                         4 * ((MAX_UPLOAD_BYTES + 2) // 3), minimum=0),
    "cmd": _text("Command run INSIDE the sandbox, never on this machine.", 32768),
    "cwd": _text("Working directory inside the sandbox."),
    "envs": {
        "type": "object",
        "description": "Environment for this remote command only.",
        "maxProperties": 100,
        "propertyNames": {"type": "string", "minLength": 1, "maxLength": 256,
                          "pattern": r"^[A-Za-z_][A-Za-z0-9_]*$"},
        "additionalProperties": _text("Value.", 32768, minimum=0),
    },
    "timeoutSeconds": _integer("Command timeout. The service caps commands at 240s.", 1, 240),
    "port": _integer("TCP port inside the sandbox.", 1, 65535),
}


@dataclass(frozen=True)
class Operation:
    """One endpoint, and how a tool call maps onto it."""

    name: str
    method: str
    route: str
    description: str
    required: tuple[str, ...] = ()
    optional: tuple[str, ...] = ()
    query: tuple[str, ...] = ()
    body: tuple[str, ...] = ()
    binary: bool = False

    @property
    def mutates(self) -> bool:
        """Anything that is not a GET changes something.

        Derived rather than declared: a new read-only operation cannot accidentally
        be marked destructive, and a new write cannot accidentally escape approval
        by someone forgetting a flag.
        """
        return self.method != "GET"

    @property
    def schema(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": {
                "type": "object",
                "properties": {key: FIELDS[key] for key in self.required + self.optional},
                "required": list(self.required),
                "additionalProperties": False,
            },
        }


_ID = ("sandbox_id",)
_PATH = _ID + ("path",)

OPERATIONS: tuple[Operation, ...] = (
    Operation("sandbox_list_flavors", "GET", "/omni/sandbox/v1/flavors",
              "List sandbox sizes with vCPU, memory and hourly cost.",
              ("region",), query=("region",)),
    Operation("sandbox_create_sandbox", "POST", SANDBOX_BASE,
              "Create a sandbox. It bills from creation until deleted or its TTL expires.",
              ("sandboxName", "region", "flavorName", "ttlSeconds"),
              body=("sandboxName", "region", "flavorName", "ttlSeconds")),
    Operation("sandbox_list_sandboxes", "GET", SANDBOX_BASE,
              "List sandboxes on this account. Others' sandboxes appear here too.",
              optional=("region", "page", "limit"), query=("region", "page", "limit")),
    Operation("sandbox_get_sandbox", "GET", SANDBOX_BASE + "/{sandbox_id}",
              "Fetch one sandbox by id, including its status.", _ID),
    Operation("sandbox_delete_sandbox", "DELETE", SANDBOX_BASE + "/{sandbox_id}",
              "DESTRUCTIVE: delete a sandbox and everything in it. Delete only ids you created.",
              _ID),
    Operation("sandbox_reset_ttl", "POST", SANDBOX_BASE + "/{sandbox_id}/ttl",
              "Change a sandbox's TTL. Extending it extends what it costs.",
              _ID + ("ttlSeconds",), body=("ttlSeconds",)),
    Operation("sandbox_write_file", "POST", SANDBOX_BASE + "/{sandbox_id}/files",
              "Upload bytes to a path in the sandbox. OVERWRITES whatever is there.",
              _PATH + ("data_base64",), query=("path",)),
    Operation("sandbox_read_file", "GET", SANDBOX_BASE + "/{sandbox_id}/files",
              "Read a file from the sandbox, returned as base64. Writes nothing locally.",
              _PATH, query=("path",), binary=True),
    Operation("sandbox_delete_path", "DELETE", SANDBOX_BASE + "/{sandbox_id}/files",
              "DESTRUCTIVE: delete a file or directory in the sandbox.", _PATH, query=("path",)),
    Operation("sandbox_list_directory", "GET", SANDBOX_BASE + "/{sandbox_id}/files/list",
              "List a directory in the sandbox.", _PATH, ("depth",), query=("path", "depth")),
    Operation("sandbox_stat_path", "GET", SANDBOX_BASE + "/{sandbox_id}/files/stat",
              "Stat a file or directory in the sandbox.", _PATH, query=("path",)),
    Operation("sandbox_move_path", "PUT", SANDBOX_BASE + "/{sandbox_id}/files/move",
              "Move or rename a path in the sandbox. May replace the destination.",
              _PATH + ("newPath",), query=("path", "newPath")),
    Operation("sandbox_create_directory", "POST", SANDBOX_BASE + "/{sandbox_id}/dirs",
              "Create a directory in the sandbox.", _PATH, query=("path",)),
    Operation("sandbox_execute_command", "POST", SANDBOX_BASE + "/{sandbox_id}/commands",
              "Run a command INSIDE the sandbox. Chained commands (`;`, `&&`, `||`) are "
              "rejected by the service edge — put multiple steps in a script and run that.",
              _ID + ("cmd", "timeoutSeconds"), ("cwd", "envs"),
              body=("cmd", "cwd", "envs", "timeoutSeconds")),
    Operation("sandbox_health", "GET", "/omni/sandbox/v1/{sandbox_id}/health",
              "Health of one sandbox. A fixed endpoint, not an arbitrary proxy target.", _ID),
    Operation("sandbox_open_port", "POST", SANDBOX_BASE + "/{sandbox_id}/ports",
              "Expose a port from the sandbox. This can make a service reachable from outside.",
              _ID + ("port",), body=("port",)),
    Operation("sandbox_list_ports", "GET", SANDBOX_BASE + "/{sandbox_id}/ports",
              "List exposed ports on a sandbox.", _ID),
    Operation("sandbox_close_port", "DELETE", SANDBOX_BASE + "/{sandbox_id}/ports/{port}",
              "Close an exposed port, cutting any connections through it.", _ID + ("port",)),
)

BY_NAME: dict[str, Operation] = {op.name: op for op in OPERATIONS}


class ToolError(Exception):
    """Refused before any request was sent."""


def build_request(operation: Operation, args: dict[str, Any] | None) -> tuple[str, dict, Any, bytes | None]:
    """Turn tool arguments into (path, query, json_body, raw_body).

    Validates here rather than at the service, so a malformed call costs nothing and
    the agent gets a reason instead of a 400.
    """
    args = dict(args or {})
    for key in operation.required:
        if args.get(key) in (None, ""):
            raise ToolError(f"{operation.name}: '{key}' is required")

    unknown = set(args) - set(operation.required + operation.optional)
    if unknown:
        raise ToolError(f"{operation.name}: unexpected argument(s) {sorted(unknown)}")

    path = operation.route
    for key in ("sandbox_id", "port"):
        token = "{" + key + "}"
        if token in path:
            path = path.replace(token, str(args[key]))

    query = {k: args[k] for k in operation.query if args.get(k) is not None}
    body = {k: args[k] for k in operation.body if args.get(k) is not None}

    raw: bytes | None = None
    if operation.name == "sandbox_write_file":
        encoded = args.get("data_base64") or ""
        try:
            raw = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ToolError("sandbox_write_file: data_base64 is not valid base64") from exc
        if len(raw) > MAX_UPLOAD_BYTES:
            raise ToolError(
                f"sandbox_write_file: {len(raw)} bytes exceeds the {MAX_UPLOAD_BYTES} byte limit"
            )
        body = {}

    return path, query, (body or None), raw


def call_operation(api: KrutrimAPI, operation: Operation, args: dict[str, Any] | None) -> str:
    """Run one operation and return a JSON string for the agent."""
    try:
        path, query, body, raw = build_request(operation, args)
    except ToolError as exc:
        return json.dumps({"ok": False, "error": str(exc)})

    try:
        if raw is not None:
            api.upload(args["sandbox_id"], args["path"], raw)
            result: Any = {"written": len(raw), "path": args["path"]}
        else:
            result = api._request(operation.method, path, body=body, query=query or None)
            # A non-JSON response comes back as {"_raw": <bytes>}; a file read is
            # the expected case, so hand it to the agent as base64 rather than as
            # an unprintable blob.
            if operation.binary and isinstance(result, dict) and "_raw" in result:
                result = {"data_base64": base64.b64encode(bytes(result["_raw"])).decode()}
    except KrutrimError as exc:
        return json.dumps({"ok": False, "status": exc.status, "error": str(exc)[:600]})
    except Exception as exc:  # noqa: BLE001 - surfaced to the agent, never raised at it
        return json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"})

    payload = json.dumps({"ok": True, "data": result}, default=str)
    if len(payload) > MAX_RESULT_BYTES:
        return json.dumps({
            "ok": False,
            "error": f"result of {len(payload)} bytes exceeds the {MAX_RESULT_BYTES} byte limit; "
                     "narrow the request (a smaller depth, page or path)",
        })
    return payload


def approval_for(tool_name: str, args: dict[str, Any] | None) -> dict[str, Any] | None:
    """Ask Hermes to confirm anything that changes or costs something.

    Returns None for reads, so the common case is not interrupted. Mutation is taken
    from the HTTP method, so a new write cannot slip past this by omitting a flag.
    """
    operation = BY_NAME.get(tool_name)
    if operation is None or not operation.mutates:
        return None
    try:
        path, query, _, _ = build_request(operation, args)
    except ToolError as exc:
        return {"action": "block", "message": str(exc)}
    target = path if not query else f"{path}?{'&'.join(sorted(query))}"
    return {
        "action": "approve",
        "message": f"{operation.description} Target: {target}. Check the arguments before approving.",
        "rule_key": f"{tool_name}:{target}",
    }
