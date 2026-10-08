"""Public-surface harness for the M1 order (docs/work/orders/M1-model-gateway.md).

Every product interaction goes through the console script installed next to the test
interpreter: `kp-agent-models --config <path> serve` (spoken to as a stdio MCP server
with newline-delimited JSON-RPC) and `kp-agent-models --config <path> doctor`. No
implementation module is imported. Providers are loopback stubs (m1_stub_provider);
on macOS every gateway process also runs under `sandbox-exec` with outbound network
limited to loopback, so a gateway that reaches past the configured base_url fails
instead of leaving the host.

The order leaves several shapes open. The harness reads them tolerantly and the
tolerance is stated here, once:

* Config: JSON, `schema_version: "agent-tooling.model-gateway.v1"` (the key name is
  this repository's convention for `agent-tooling.*.v1` documents), `providers` is a
  name -> provider mapping that `routes[...].provider` names, `budgets` is keyed by
  capability. Written owner-only (0600).
* Tool arguments: the order names no input field except `confirm`. Arguments are
  derived from the advertised `inputSchema` (required properties only) and validated
  against it before the call; a schema the filler cannot satisfy fails the test with
  that reason. A binary input's artifact reference goes in a required free-string
  property (name-hinted first, else the only one); when no required property can carry
  it (an either/or input), in an optional string property named `*_ref` or containing
  ref/path/file (Coordinator ruling at the M1 meet, SPEC-GAP). Enum, const and base64
  properties never carry it; with no place for it the test fails with a HarnessError.
* Results: a tool result is read through `structuredContent`, every JSON text content
  and a JSON-RPC error's `data`. "Error-shaped" means a JSON-RPC error, `isError: true`,
  or a payload with `ok: false`, a truthy `error`, or a failure-valued `status`.
* Artifact references: any string in the result that names an existing file, read as
  an absolute path, a path relative to artifact_root, a file:// URI, or `<scheme>://rel`.
* Provenance: located by value, not key name: a JSON object (in the result, or in a file
  under artifact_root) whose leaves include the capability, the model, the provider's
  name or kind, a digest-shaped string, a current timestamp and, when the provider
  reported one, the cost.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import queue
import re
import secrets
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import pytest

from m1_stub_provider import StubProvider

SCRIPT = "kp-agent-models"
SCHEMA_VERSION = "agent-tooling.model-gateway.v1"
CAPABILITIES = ("text.complete", "image.generate", "audio.transcribe", "audio.speak")
BINARY_INPUT = {"audio.transcribe"}
KINDS = ("openrouter", "openai-compatible")
PROTOCOL_VERSION = "2025-06-18"
RESPONSE_TIMEOUT = 120
DECOY_ENV_KEYS = ("OPENROUTER_API_KEY", "OPENAI_API_KEY", "KP_AGENT_MODELS_API_KEY")

SANDBOX = Path("/usr/bin/sandbox-exec")
SANDBOX_PROFILE = ('(version 1)(allow default)(deny network-outbound)'
                   '(allow network-outbound (remote ip "localhost:*"))')

FAILURE_STATUSES = {"error", "failed", "failure", "refused", "rejected", "denied", "blocked",
                    "unavailable", "unsupported", "confirmation_required", "budget_exceeded"}
_DIGEST = re.compile(r"^(?:(?:sha|sha-|blake2b?|md)\d*[:\-])?[0-9a-fA-F]{32,128}$")
_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?$")
_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*://(.*)$")
_REF_HINT = re.compile(r"(artifact|ref|path|file|audio|input|source|uri|url)", re.I)
# An OPTIONAL property may carry the reference only if its name says so (Coordinator ruling,
# M1 meet: `*_ref`, or contains ref/path/file). A base64 field never carries a reference.
_OPTIONAL_REF_NAME = re.compile(r"(ref|path|file)", re.I)
_REF_SUFFIX = re.compile(r"_?ref$", re.I)
_NOT_A_REF = re.compile(r"(base64|b64)", re.I)


class HarnessError(AssertionError):
    """The harness could not establish an observation the test needs."""


def gateway_executable() -> Path:
    """The console script next to the running interpreter (not symlink-resolved)."""
    return Path(sys.executable).parent / SCRIPT


def require_gateway() -> Path:
    path = gateway_executable()
    if not (path.is_file() and os.access(path, os.X_OK)):
        pytest.fail(f"order M1: console script {SCRIPT} is not installed next to the test interpreter "
                    f"({path} is absent)", pytrace=False)
    return path


# --------------------------------------------------------------------- values


def _is_json(text: str) -> bool:
    try:
        json.loads(text)
        return True
    except ValueError:
        return False


def iter_leaves(value):
    if isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from iter_leaves(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from iter_leaves(item)
    else:
        yield value


def iter_values(value):
    """Leaves only (dict values and list items, not keys)."""
    if isinstance(value, dict):
        for item in value.values():
            yield from iter_values(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from iter_values(item)
    else:
        yield value


def iter_dicts(value):
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from iter_dicts(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from iter_dicts(item)


def is_digest(value) -> bool:
    return isinstance(value, str) and _DIGEST.fullmatch(value.strip()) is not None


def is_current_timestamp(value, *, slack: float = 86400.0) -> bool:
    now = time.time()
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        for scale in (1.0, 1000.0, 1_000_000.0):
            if abs(value / scale - now) <= slack:
                return True
        return False
    if isinstance(value, str) and _ISO.fullmatch(value.strip()):
        text = value.strip().replace(" ", "T")
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return False
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return abs(parsed.timestamp() - now) <= slack
    return False


def cost_matches(value, cost: float) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        return abs(float(value) - cost) <= 1e-9 * max(1.0, abs(cost))
    if isinstance(value, str):
        try:
            return abs(float(value.strip().lstrip("$")) - cost) <= 1e-9 * max(1.0, abs(cost))
        except ValueError:
            return False
    return False


def provenance_nodes(document, *, capability: str, model: str, provider_names: set,
                     cost: float | None = None) -> list:
    """JSON objects whose leaves carry the full provenance the order lists."""
    found = []
    for node in iter_dicts(document):
        leaves = list(iter_values(node))
        strings = {leaf.strip() for leaf in leaves if isinstance(leaf, str)}
        if not ({capability, "model." + capability} & strings):
            continue
        if model not in strings or not (strings & set(provider_names)):
            continue
        if not any(is_digest(s) for s in strings):
            continue
        if not any(is_current_timestamp(leaf) for leaf in leaves):
            continue
        if cost is not None and not any(cost_matches(leaf, cost) for leaf in leaves):
            continue
        found.append(node)
    return found


def missing_provenance(document, *, capability, model, provider_names, cost=None) -> list[str]:
    """What the best candidate lacks (for failure messages)."""
    leaves = list(iter_values(document))
    strings = {leaf.strip() for leaf in leaves if isinstance(leaf, str)}
    missing = []
    if not ({capability, "model." + capability} & strings):
        missing.append(f"capability {capability!r}")
    if model not in strings:
        missing.append(f"model {model!r}")
    if not (strings & set(provider_names)):
        missing.append(f"provider (one of {sorted(provider_names)})")
    if not any(is_digest(s) for s in strings):
        missing.append("request digest (hex string)")
    if not any(is_current_timestamp(leaf) for leaf in leaves):
        missing.append("timestamp")
    if cost is not None and not any(cost_matches(leaf, cost) for leaf in leaves):
        missing.append(f"reported cost {cost}")
    return missing


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError):
        return False


def resolve_reference(text: str, root: Path) -> Path | None:
    """The existing file a reference string names, or None."""
    if not isinstance(text, str) or not text or len(text) > 4096 or "\n" in text or "\x00" in text:
        return None
    text = text.strip()
    candidates = []
    if text.startswith("file://"):
        candidates.append(text[len("file://"):])
    match = _SCHEME.match(text)
    if match:
        candidates += [match.group(1), str(root / match.group(1).lstrip("/"))]
    candidates += [text, str(root / text)]
    for candidate in candidates:
        try:
            path = Path(candidate)
            if path.is_file():
                return path
        except OSError:
            continue
    return None


# ------------------------------------------------------------------ results


@dataclass
class CallOutcome:
    name: str
    arguments: dict
    response: dict
    raw_line: str

    @property
    def rpc_error(self):
        return self.response.get("error")

    @property
    def result(self) -> dict:
        result = self.response.get("result")
        return result if isinstance(result, dict) else {}

    @property
    def texts(self) -> list[str]:
        texts = [item.get("text") for item in self.result.get("content") or ()
                 if isinstance(item, dict) and isinstance(item.get("text"), str)]
        if isinstance(self.rpc_error, dict) and isinstance(self.rpc_error.get("message"), str):
            texts.append(self.rpc_error["message"])
        return texts

    @property
    def payloads(self) -> list:
        payloads = []
        if "structuredContent" in self.result:
            payloads.append(self.result["structuredContent"])
        for text in self.texts:
            try:
                payloads.append(json.loads(text))
            except ValueError:
                pass
        if isinstance(self.rpc_error, dict) and "data" in self.rpc_error:
            payloads.append(self.rpc_error["data"])
        return payloads

    def blob(self) -> str:
        return self.raw_line

    def lowered(self) -> str:
        return self.raw_line.lower()

    def is_structured(self) -> bool:
        return any(isinstance(p, dict) for p in self.payloads)

    def is_error_shaped(self) -> bool:
        if self.rpc_error is not None or self.result.get("isError") is True:
            return True
        for payload in self.payloads:
            if not isinstance(payload, dict):
                continue
            if payload.get("ok") is False or payload.get("success") is False:
                return True
            if payload.get("error") or payload.get("errors"):
                return True
            for key in ("status", "outcome", "state", "result"):
                value = payload.get(key)
                if isinstance(value, str) and value.lower() in FAILURE_STATUSES:
                    return True
        return False

    def requires_confirmation(self) -> bool:
        """`confirmation_required` as a value, or as a key set to true (not merely mentioned)."""
        for payload in self.payloads:
            for node in iter_dicts(payload):
                for key, value in node.items():
                    if key == "confirmation_required" and value is True:
                        return True
                    if isinstance(value, str) and value.strip().lower() == "confirmation_required":
                        return True
            if isinstance(payload, str) and payload.strip().lower() == "confirmation_required":
                return True
        return any("confirmation_required" in text for text in self.texts if not _is_json(text))

    def strings(self) -> list[str]:
        values = [v for p in self.payloads for v in iter_leaves(p) if isinstance(v, str)]
        return values + self.texts

    def references(self, root: Path) -> list[Path]:
        found = []
        for text in self.strings():
            path = resolve_reference(text, root)
            if path is not None and path not in found:
                found.append(path)
        return found

    def describe(self) -> str:
        line = self.raw_line if len(self.raw_line) <= 3000 else self.raw_line[:3000] + "...<truncated>"
        return f"tools/call {self.name} arguments-keys={sorted(self.arguments)} -> {line}"


# ------------------------------------------------------------------ world


@dataclass
class Key:
    name: str
    path: Path
    value: str

    @property
    def fragment(self) -> str:
        middle = len(self.value) // 2
        return self.value[middle - 8: middle + 8]


class World:
    """Scratch directories, key files, stub providers, a config and gateway processes."""

    def __init__(self, root: Path):
        self.root = root.resolve()
        self.artifact_root = self.root / "artifacts"
        self.keys_dir = self.root / "keys"
        self.home = self.root / "home"
        self.tmp = self.root / "tmp"
        self.logs = self.root / "logs"
        self.outside = self.root / "outside"
        for directory in (self.artifact_root, self.keys_dir, self.home, self.tmp, self.logs, self.outside):
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.config_path = self.root / "gateway-config.json"
        self.keys: dict[str, Key] = {}
        self.decoys = {name: "sk-decoy-" + secrets.token_hex(20) for name in DECOY_ENV_KEYS}
        # One live name -> value view shared with every stub, so a stub recognises keys made later.
        self.known_keys = {f"decoy:{name}": value for name, value in self.decoys.items()}
        self.stubs: list[StubProvider] = []
        self.gateways: list[Gateway] = []
        self.doctor_runs: list[subprocess.CompletedProcess] = []
        self.nonce = secrets.token_hex(6)
        self.config: dict | None = None

    # ---------------------------------------------------------- keys & stubs

    def key(self, name: str, kind: str = "openai-compatible", mode: int = 0o600) -> Key:
        if kind == "openrouter":
            value = "sk-or-v1-" + secrets.token_hex(32)
        else:
            value = "sk-m1" + secrets.token_hex(24)
        path = self.keys_dir / f"{name}.key"
        path.write_text(value + "\n")
        path.chmod(mode)
        self.keys[name] = Key(name, path, value)
        self.known_keys[name] = value
        return self.keys[name]

    def stub(self, label: str, *, models=(), **options) -> StubProvider:
        stub = StubProvider(self.logs / f"stub-{label}.jsonl", keys=self.known_keys, models=models,
                            label=label, **options)
        stub.start()
        self.stubs.append(stub)
        return stub

    # ---------------------------------------------------------- config

    def write_config(self, *, routes: dict, providers: dict, budgets: dict | None = None,
                     artifact_root: Path | None = None) -> Path:
        if budgets is None:
            budgets = {capability: generous_budget() for capability in routes}
        self.config = {"schema_version": SCHEMA_VERSION, "routes": routes, "providers": providers,
                       "budgets": budgets, "artifact_root": str(artifact_root or self.artifact_root)}
        self.config_path.write_text(json.dumps(self.config, indent=2, sort_keys=True))
        self.config_path.chmod(0o600)
        return self.config_path

    # ---------------------------------------------------------- processes

    def env(self) -> dict:
        env = {"PATH": str(gateway_executable().parent) + os.pathsep + "/usr/bin:/bin:/usr/sbin:/sbin",
               "HOME": str(self.home), "TMPDIR": str(self.tmp) + "/",
               "XDG_CONFIG_HOME": str(self.home / ".config"), "XDG_CACHE_HOME": str(self.home / ".cache"),
               "XDG_STATE_HOME": str(self.home / ".local/state"),
               "XDG_DATA_HOME": str(self.home / ".local/share"),
               "LANG": os.environ.get("LANG", "C.UTF-8"), "PYTHONNOUSERSITE": "1",
               "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUNBUFFERED": "1",
               # Anything that honours proxies cannot leave loopback.
               "HTTP_PROXY": "http://127.0.0.1:9", "HTTPS_PROXY": "http://127.0.0.1:9",
               "ALL_PROXY": "http://127.0.0.1:9", "http_proxy": "http://127.0.0.1:9",
               "https_proxy": "http://127.0.0.1:9", "all_proxy": "http://127.0.0.1:9",
               "NO_PROXY": "127.0.0.1,localhost,::1", "no_proxy": "127.0.0.1,localhost,::1"}
        for name in ("USER", "LOGNAME", "LC_ALL"):
            if name in os.environ:
                env[name] = os.environ[name]
        env.update(self.decoys)
        return env

    @staticmethod
    def wrap(argv: list[str]) -> list[str]:
        if sys.platform == "darwin" and SANDBOX.is_file():
            return [str(SANDBOX), "-p", SANDBOX_PROFILE, *argv]
        return argv

    def gateway(self) -> "Gateway":
        require_gateway()
        gateway = Gateway(self, len(self.gateways))
        self.gateways.append(gateway)
        return gateway

    def doctor(self, *extra: str, timeout: int = 120) -> subprocess.CompletedProcess:
        script = require_gateway()
        argv = self.wrap([str(script), "--config", str(self.config_path), "doctor", *extra])
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env=self.env(),
                              cwd=str(self.tmp), stdin=subprocess.DEVNULL)
        index = len(self.doctor_runs)
        (self.logs / f"doctor-{index}.stdout").write_text(proc.stdout or "")
        (self.logs / f"doctor-{index}.stderr").write_text(proc.stderr or "")
        self.doctor_runs.append(proc)
        return proc

    def close(self):
        for gateway in self.gateways:
            gateway.stop()
        for stub in self.stubs:
            stub.stop()

    # ---------------------------------------------------------- observation

    def files_under(self, root: Path | None = None) -> dict[Path, tuple]:
        root = root or self.artifact_root
        found = {}
        for path in root.rglob("*"):
            try:
                if path.is_file() and not path.is_symlink():
                    stat = path.stat()
                    found[path] = (stat.st_size, stat.st_mtime_ns)
            except OSError:
                continue
        return found

    def changed_files(self, before: dict, root: Path | None = None) -> list[Path]:
        after = self.files_under(root)
        return [p for p, sig in after.items() if before.get(p) != sig]

    def secret_sightings(self, key: Key) -> list[str]:
        """Every place outside the key file itself where the key (or its middle) appears."""
        needles = [key.value.encode(), key.fragment.encode()]
        sightings = []
        for path in sorted(self.root.rglob("*")):
            try:
                if not path.is_file() or path.resolve() == key.path.resolve():
                    continue
                data = path.read_bytes()
            except OSError:
                continue
            if any(n in data for n in needles):
                sightings.append(f"file {path.relative_to(self.root)}")
        for gateway in self.gateways:
            if any(n.decode() in line for line in gateway.transcript for n in needles):
                sightings.append(f"stdout of gateway process #{gateway.index}")
        for index, proc in enumerate(self.doctor_runs):
            if any(n.decode() in (proc.stdout or "") + (proc.stderr or "") for n in needles):
                sightings.append(f"doctor run #{index} output")
        return sightings


def generous_budget() -> dict:
    return {"max_calls_per_hour": 1000, "max_usd_per_day": 1000.0}


def provider(stub: StubProvider, key: Key, kind: str, prefix: str | None = None) -> dict:
    if prefix is None:
        prefix = "/api/v1" if kind == "openrouter" else "/v1"
    return {"kind": kind, "base_url": stub.base_url(prefix), "api_key_file": str(key.path)}


class Gateway:
    """One `kp-agent-models --config <cfg> serve` process spoken to over stdio."""

    def __init__(self, world: World, index: int):
        self.world = world
        self.index = index
        self.process: subprocess.Popen | None = None
        self.lines: queue.Queue = queue.Queue()
        self.transcript: list[str] = []
        self.next_id = 1
        self.starts = 0
        self.stderr_paths: list[Path] = []
        self.initialize_response: dict | None = None
        self.tools_cache: list | None = None

    # ---------------------------------------------------------- lifecycle

    def start(self) -> "Gateway":
        """Start and initialize; raises HarnessError if the server does not come up."""
        outcome = self.try_start()
        if outcome is not None:
            raise HarnessError(outcome)
        return self

    def try_start(self) -> str | None:
        """Start and initialize; return None on success or a description of the refusal."""
        self.stop()
        script = require_gateway()
        argv = self.world.wrap([str(script), "--config", str(self.world.config_path), "serve"])
        stderr_path = self.world.logs / f"serve-{self.index}-{self.starts}.stderr"
        self.stderr_paths.append(stderr_path)
        self.starts += 1
        self.lines = queue.Queue()
        self.tools_cache = None
        stderr = stderr_path.open("w")
        self.process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=stderr,
                                        env=self.world.env(), cwd=str(self.world.tmp), text=True,
                                        encoding="utf-8", bufsize=1)
        stderr.close()
        stdout_log = self.world.logs / f"serve-{self.index}-{self.starts - 1}.stdout"
        process, lines = self.process, self.lines

        def pump():
            with stdout_log.open("a") as log:
                for raw in process.stdout:
                    log.write(raw)
                    log.flush()
                    lines.put(raw)
            lines.put(None)

        threading.Thread(target=pump, daemon=True).start()
        try:
            answer = self.request("initialize", {
                "protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                "clientInfo": {"name": "m1-test-arm", "version": "1"}})
        except HarnessError as failure:
            self.stop()
            return f"serve did not initialize: {failure}\n{self.stderr_tail()}"
        if "error" in answer:
            self.stop()
            return f"initialize returned an error: {answer['error']}\n{self.stderr_tail()}"
        self.initialize_response = answer
        self.notify("notifications/initialized")
        return None

    def stop(self):
        process, self.process = self.process, None
        if process is None:
            return
        try:
            if process.stdin and not process.stdin.closed:
                process.stdin.close()
        except OSError:
            pass
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)

    def restart(self) -> "Gateway":
        self.stop()
        return self.start()

    @property
    def running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def stderr_tail(self, limit: int = 3000) -> str:
        if not self.stderr_paths:
            return ""
        text = self.stderr_paths[-1].read_text(errors="replace") if self.stderr_paths[-1].exists() else ""
        return "server stderr (tail):\n" + text[-limit:]

    # ---------------------------------------------------------- JSON-RPC

    def _send(self, message: dict):
        if not self.running:
            raise HarnessError("the serve process is not running")
        try:
            self.process.stdin.write(json.dumps(message) + "\n")
            self.process.stdin.flush()
        except (BrokenPipeError, OSError) as failure:
            raise HarnessError(f"could not write to serve stdin: {failure}") from None

    def notify(self, method: str, params: dict | None = None):
        message = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        self._send(message)

    def request(self, method: str, params: dict, *, timeout: float = RESPONSE_TIMEOUT) -> dict:
        request_id = self.next_id
        self.next_id += 1
        self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        return self._await(request_id, timeout)[0]

    def _await(self, request_id: int, timeout: float):
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise HarnessError(f"no response to request {request_id} within {timeout}s")
            try:
                raw = self.lines.get(timeout=remaining)
            except queue.Empty:
                continue
            if raw is None:
                raise HarnessError(f"serve exited before answering request {request_id}")
            self.transcript.append(raw)
            try:
                message = json.loads(raw)
            except ValueError:
                raise HarnessError(f"serve wrote non-JSON to stdout: {raw[:300]!r}") from None
            if not isinstance(message, dict):
                continue
            if "method" in message and "id" in message:  # a server->client request
                self._send({"jsonrpc": "2.0", "id": message["id"],
                            "error": {"code": -32601, "message": "not supported by the M1 test client"}})
                continue
            if message.get("id") == request_id:
                return message, raw

    # ---------------------------------------------------------- MCP

    def list_tools(self) -> list[dict]:
        collected, cursor = [], None
        while True:
            answer = self.request("tools/list", {"cursor": cursor} if cursor else {})
            if "error" in answer:
                raise HarnessError(f"tools/list failed: {answer['error']}")
            collected += answer["result"]["tools"]
            cursor = answer["result"].get("nextCursor")
            if not cursor:
                break
        self.tools_cache = collected
        return collected

    def tool(self, name: str) -> dict:
        tools = self.tools_cache if self.tools_cache is not None else self.list_tools()
        for tool in tools:
            if tool.get("name") == name:
                return tool
        raise HarnessError(f"tools/list does not include {name}; it lists {[t.get('name') for t in tools]}")

    def call(self, name: str, arguments: dict, *, timeout: float = RESPONSE_TIMEOUT) -> CallOutcome:
        request_id = self.next_id
        self.next_id += 1
        self._send({"jsonrpc": "2.0", "id": request_id, "method": "tools/call",
                    "params": {"name": name, "arguments": arguments}})
        message, raw = self._await(request_id, timeout)
        return CallOutcome(name=name, arguments=arguments, response=message, raw_line=raw)

    def call_capability(self, capability: str, *, text: str | None = None, input_ref: str | None = None,
                        extra: dict | None = None, validate: bool = True) -> CallOutcome:
        name = "model." + capability
        arguments = arguments_for(self.tool(name), capability,
                                  text=text or f"M1 test request {self.world.nonce}",
                                  input_ref=input_ref, validate=validate)
        arguments.update(extra or {})
        return self.call(name, arguments)


# ------------------------------------------------------------ argument filler


def _resolve(schema: dict, root: dict) -> dict:
    seen = 0
    while isinstance(schema, dict) and "$ref" in schema and seen < 32:
        ref = schema["$ref"]
        if not ref.startswith("#/"):
            raise HarnessError(f"inputSchema uses a non-local $ref {ref!r}")
        target = root
        for part in ref[2:].split("/"):
            target = target[part.replace("~1", "/").replace("~0", "~")]
        merged = {k: v for k, v in schema.items() if k != "$ref"}
        schema = {**target, **merged}
        seen += 1
    return schema


def _choose(schema: dict, root: dict) -> dict:
    schema = _resolve(schema, root)
    for key in ("anyOf", "oneOf"):
        if key in schema:
            options = [_resolve(o, root) for o in schema[key]]
            non_null = [o for o in options if o.get("type") != "null"]
            base = {k: v for k, v in schema.items() if k != key}
            return {**base, **(non_null or options)[0]}
    if "allOf" in schema:
        merged = {k: v for k, v in schema.items() if k != "allOf"}
        for part in schema["allOf"]:
            merged = {**merged, **_resolve(part, root)}
        return merged
    return schema


def _kind(schema: dict) -> str | None:
    kind = schema.get("type")
    if isinstance(kind, list):
        kind = next((k for k in kind if k != "null"), None)
    if kind is None and "properties" in schema:
        kind = "object"
    return kind


def _clamp(value, schema):
    if "minimum" in schema:
        value = max(value, schema["minimum"])
    if "exclusiveMinimum" in schema and isinstance(schema["exclusiveMinimum"], (int, float)):
        value = max(value, schema["exclusiveMinimum"] + (1 if isinstance(value, int) else 0.5))
    if "maximum" in schema:
        value = min(value, schema["maximum"])
    return value


def _fill(schema: dict, name: str, ctx: dict, root: dict, ref_property: str | None):
    schema = _choose(schema, root)
    if "const" in schema:
        return schema["const"]
    if schema.get("enum"):
        preferred = "wav" if "audio" in ctx["capability"] else "png"
        if "format" in name.lower() and preferred in schema["enum"]:
            return preferred
        return schema["enum"][0]
    if "default" in schema and schema["default"] is not None:
        return schema["default"]
    kind = _kind(schema)
    lowered = name.lower()
    if kind == "string":
        if ctx.get("input_ref") is not None and name == ref_property:
            return ctx["input_ref"]
        if lowered == "voice":
            return "alloy"
        if "language" in lowered:
            return "en"
        if lowered in ("size", "image_size", "resolution"):
            return "1024x1024"
        if "format" in lowered:
            return "wav" if "audio" in ctx["capability"] else "png"
        if lowered == "role":
            return "user"
        return ctx["text"]
    if kind == "integer":
        return int(_clamp(1 if lowered in ("n", "count", "num_images") else 64, schema))
    if kind == "number":
        return float(_clamp(1.0, schema))
    if kind == "boolean":
        return False
    if kind == "array":
        items = _choose(schema.get("items") or {}, root)
        if "messages" in lowered or {"role", "content"} <= set(items.get("properties") or {}):
            return [{"role": "user", "content": ctx["text"]}] * max(1, schema.get("minItems", 1))
        return [_fill(items, name, ctx, root, ref_property)] * max(1, schema.get("minItems", 1))
    if kind == "object":
        return _fill_object(schema, ctx, root)
    if kind == "null":
        return None
    return ctx["text"]


def _can_carry_reference(name: str, schema: dict, root: dict) -> bool:
    """A free string property: not an enum or const, and not named as an inline base64 field."""
    schema = _choose(schema or {}, root)
    return (_kind(schema) == "string" and not schema.get("enum") and "const" not in schema
            and not _NOT_A_REF.search(name))


def _reference_property(properties: dict, required: list, root: dict) -> str | None:
    """Where the input artifact reference goes: a required carrier first (name-hinted, then
    the only free required string), otherwise an optional property named as a reference
    (`*_ref` first, then any name containing ref/path/file). None if there is no such place."""
    carriers = [r for r in required if _can_carry_reference(r, properties.get(r), root)]
    hinted = [r for r in carriers if _REF_HINT.search(r)]
    if hinted:
        return hinted[0]
    if len(carriers) == 1:
        return carriers[0]
    optional = [p for p in properties if p not in required and p != "confirm"
                and _OPTIONAL_REF_NAME.search(p) and _can_carry_reference(p, properties[p], root)]
    optional.sort(key=lambda p: _REF_SUFFIX.search(p) is None)  # stable: `*_ref` first
    return optional[0] if optional else None


def _fill_object(schema: dict, ctx: dict, root: dict) -> dict:
    schema = _choose(schema, root)
    properties = schema.get("properties") or {}
    required = [r for r in schema.get("required") or () if r != "confirm"]
    ref_property = None
    if ctx.get("input_ref") is not None:
        ref_property = _reference_property(properties, required, root)
        if ref_property is not None and ref_property not in required:
            required = required + [ref_property]
    return {prop: _fill(properties.get(prop) or {}, prop, ctx, root, ref_property) for prop in required}


def arguments_for(tool: dict, capability: str, *, text: str, input_ref: str | None = None,
                  validate: bool = True) -> dict:
    """Minimal arguments satisfying the tool's advertised inputSchema."""
    schema = tool.get("inputSchema") or {"type": "object"}
    ctx = {"capability": capability, "text": text, "input_ref": input_ref}
    arguments = _fill_object(schema, ctx, schema)
    if input_ref is not None and input_ref not in json.dumps(arguments):
        raise HarnessError(f"could not place the input artifact reference for {capability} in its "
                           f"advertised inputSchema: {json.dumps(schema)[:1500]}")
    if validate:
        import jsonschema
        validator_class = jsonschema.validators.validator_for(schema)
        errors = sorted(validator_class(schema).iter_errors(arguments), key=lambda e: list(e.path))
        if errors:
            raise HarnessError(f"arguments derived for {capability} do not satisfy the advertised "
                               f"inputSchema ({errors[0].message}); schema={json.dumps(schema)[:1500]}")
    return arguments


# ------------------------------------------------------------- input refs


def reference_forms(world: World, relative: str) -> list[str]:
    """Plausible spellings of an artifact reference to `artifact_root/relative`."""
    absolute = world.artifact_root / relative
    return [relative, str(absolute), "file://" + str(absolute)]


def spell_like(accepted: str, world: World, relative: str) -> str:
    """Spell `relative` (possibly escaping the root) the way `accepted` was spelled."""
    absolute = str(world.artifact_root) + "/" + relative
    if accepted.startswith("file://"):
        return "file://" + absolute
    if accepted.startswith("/"):
        return absolute
    match = _SCHEME.match(accepted)
    if match:
        return accepted[: len(accepted) - len(match.group(1))] + relative
    return relative


def reference_spelling(outcome: CallOutcome, world: World) -> str | None:
    """The first string in a result that names a file under artifact_root."""
    for text in outcome.strings():
        path = resolve_reference(text, world.artifact_root)
        if path is not None and _inside(path, world.artifact_root):
            return text.strip()
    return None


def write_input_audio(world: World, relative: str, stub: StubProvider) -> Path:
    path = world.artifact_root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(stub.audio)
    return path


def call_with_input(gateway: Gateway, world: World, capability: str, relative: str) -> tuple[CallOutcome, str]:
    """Call a binary-input capability, trying the plausible reference spellings in turn.

    Returns the first non-error outcome and the spelling it accepted, or the last
    outcome and '' when every spelling was refused."""
    outcome = None
    for form in reference_forms(world, relative):
        outcome = gateway.call_capability(capability, input_ref=form)
        if not outcome.is_error_shaped():
            return outcome, form
    return outcome, ""


def body_carries(stub: StubProvider, payload: bytes) -> bool:
    encoded = base64.b64encode(payload)
    return any(payload in r.body or encoded in r.body for r in stub.posts())


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def tool_names(tools: list[dict]) -> list[str]:
    return [t.get("name") for t in tools]


# ------------------------------------------------------------ provenance on disk


def documents_in(paths, *, limit: int = 8 * 1024 * 1024) -> list:
    """JSON documents (whole-file JSON, or one per JSONL line) in `paths`."""
    documents = []
    for path in paths:
        try:
            if path.stat().st_size > limit:
                continue
            data = path.read_bytes()
        except OSError:
            continue
        try:
            documents.append(json.loads(data))
            continue
        except ValueError:
            pass
        for line in data.splitlines():
            line = line.strip()
            if line[:1] in (b"{", b"["):
                try:
                    documents.append(json.loads(line))
                except ValueError:
                    pass
    return documents


def failure_marked(node: dict) -> bool:
    if node.get("ok") is False or node.get("success") is False:
        return True
    if node.get("error") or node.get("errors"):
        return True
    for key in ("status", "outcome", "state", "result"):
        value = node.get(key)
        if isinstance(value, str) and value.lower() in FAILURE_STATUSES:
            return True
    return False


def _dicts_with_ancestors(value, ancestors=()):
    if isinstance(value, dict):
        yield value, ancestors
        for item in value.values():
            yield from _dicts_with_ancestors(item, ancestors + (value,))
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _dicts_with_ancestors(item, ancestors)


def provenance_records(documents, *, capability, model, provider_names, cost=None) -> list[tuple]:
    """Minimal provenance-bearing objects, each with whether it (or an enclosing object)
    is marked as a failure: [(canonical_json, failure_marked)]."""
    records = []
    for document in documents:
        matches = provenance_nodes(document, capability=capability, model=model,
                                   provider_names=provider_names, cost=cost)
        match_ids = {id(m) for m in matches}
        for node, ancestors in _dicts_with_ancestors(document):
            if id(node) not in match_ids:
                continue
            if any(id(child) in match_ids for child in iter_dicts(node) if child is not node):
                continue  # not minimal: a descendant carries the record itself
            marked = failure_marked(node) or any(failure_marked(a) for a in ancestors)
            records.append((json.dumps(node, sort_keys=True), marked))
    return records


def provenance_on_disk(world: "World", *, capability, model, provider_names, cost=None) -> list[tuple]:
    return provenance_records(documents_in(world.files_under().keys()), capability=capability, model=model,
                              provider_names=provider_names, cost=cost)


def new_success_records(before: list[tuple], after: list[tuple]) -> list[str]:
    """Provenance records that appeared since `before` and are not marked as failures."""
    remaining = list(before)
    fresh = []
    for record in after:
        if record in remaining:
            remaining.remove(record)
        else:
            fresh.append(record)
    return [text for text, marked in fresh if not marked]
