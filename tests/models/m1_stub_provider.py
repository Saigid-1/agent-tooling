"""A loopback OpenAI/OpenRouter-compatible provider stub for the M1 tests.

The order (docs/work/orders/M1-model-gateway.md) does not say which provider endpoints
the gateway calls for each capability, so the stub answers every endpoint a gateway
plausibly uses for `text.complete`, `image.generate`, `audio.transcribe` and
`audio.speak` through an `openrouter` or `openai-compatible` provider:

* POST .../chat/completions (plain, `modalities` image/audio, `input_audio` parts,
  `stream: true` as server-sent events), .../responses, .../completions,
  .../images/generations, .../audio/transcriptions, .../audio/speech;
* GET .../models (OpenRouter shape, with pricing and architecture), .../generation,
  .../key, .../auth/key, .../credits and .../files/<id> (image URLs it handed out).

Every request is recorded: method, path (with query), headers minus every credential
header, which configured key (by name) the credential header carried, body SHA-256 and
length. Records go to a JSONL log under the test's TMPDIR-based directory; the raw body
is kept in memory only, for assertions. The log never holds a key: a credential header
is recorded by name only, and any other header whose value contains a configured key
is dropped too.

Every POST is treated as a paid call. GETs are metadata reads.
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import random
import struct
import threading
import time
import wave
import zlib
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

CREDENTIAL_HEADERS = {"authorization", "x-api-key", "api-key", "openai-api-key",
                      "proxy-authorization", "x-goog-api-key"}

# Modes for POST requests.
OK = "ok"
UNSUPPORTED = "unsupported-modality"
ERROR_MODES = ("status:500", "status:429", "status:503-then-ok", "error-envelope-200",
               "non-json-200")


@dataclass
class Recorded:
    method: str
    path: str
    headers: dict
    credential_headers: list
    key_name: str | None
    authorization_key: str | None
    authorization_scheme: str | None
    body_sha256: str
    body_len: int
    status: int
    body: bytes  # memory only, never logged

    @property
    def route(self) -> str:
        return urlsplit(self.path).path

    def json_body(self):
        try:
            return json.loads(self.body)
        except ValueError:
            return None


def _png(width: int, height: int, seed: int, text: str) -> bytes:
    """A valid, incompressible RGB PNG carrying `text` in a tEXt chunk."""
    rng = random.Random(seed)
    rows = b"".join(b"\x00" + rng.randbytes(width * 3) for _ in range(height))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + kind + data
                + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"tEXt", b"Comment\x00" + text.encode())
            + chunk(b"IDAT", zlib.compress(rows, 0))
            + chunk(b"IEND", b""))


def _wav(marker: str, frames: int = 4000) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(8000)
        handle.writeframes(marker.encode().ljust(frames * 2, b"\x00"))
    return buffer.getvalue()


def _text_payload(marker: str, size: int | None) -> str:
    if not size:
        return marker
    pieces, total, index = [], 0, 0
    while total < size:
        piece = f"{marker}:{index:08d};"
        pieces.append(piece)
        total += len(piece)
        index += 1
    return "".join(pieces)[:size]


class StubProvider:
    """One loopback provider. Start with `with StubProvider(...) as stub:` or start()."""

    def __init__(self, log_path: Path, *, keys: dict | None = None, models=(), mode: str = OK,
                 cost: float | None = 0.0137, text_size: int | None = None,
                 image_side: int = 8, pricing: dict | None = None, echo_key_in_errors: bool = False,
                 label: str = "stub"):
        self.log_path = Path(log_path)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.keys = keys if keys is not None else {}  # live view: keys made later are known too
        self.models = list(models)
        self.mode = mode
        self.cost = cost
        self.label = label
        self.nonce = hashlib.sha256(f"{label}{time.time_ns()}{id(self)}".encode()).hexdigest()[:16]
        self.text_marker = f"M1-STUB-TEXT-{self.nonce}"
        self.text = _text_payload(self.text_marker, text_size)
        self.image_marker = f"M1-STUB-IMAGE-{self.nonce}"
        self.image = _png(image_side, image_side, int(self.nonce, 16) % (2 ** 31), self.image_marker)
        self.audio_marker = f"M1-STUB-AUDIO-{self.nonce}"
        self.audio = _wav(self.audio_marker)
        self.error_marker = f"M1-STUB-ERROR-{self.nonce}"
        self.pricing = dict(pricing or {"prompt": "0", "completion": "0", "request": "0", "image": "0"})
        self.echo_key_in_errors = echo_key_in_errors
        self.requests: list[Recorded] = []
        self._lock = threading.Lock()
        self._posts_seen = 0
        self._files: dict[str, bytes] = {}
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    # ------------------------------------------------------------ lifecycle

    @property
    def port(self) -> int:
        return self.server.server_address[1]

    def base_url(self, prefix: str = "/v1") -> str:
        return f"http://127.0.0.1:{self.port}{prefix}"

    def start(self):
        self.thread.start()
        return self

    def stop(self):
        self.server.shutdown()
        self.server.server_close()

    def __enter__(self):
        return self.start()

    def __exit__(self, *_):
        self.stop()

    # ------------------------------------------------------------ observations

    def posts(self) -> list[Recorded]:
        with self._lock:
            return [r for r in self.requests if r.method == "POST"]

    def post_count(self) -> int:
        return len(self.posts())

    def settled_post_count(self, grace: float = 0.5) -> int:
        """POST count after a grace period, so a background retry is counted too."""
        time.sleep(grace)
        return self.post_count()

    # ------------------------------------------------------------ handler

    def _record(self, handler, body: bytes, status: int) -> Recorded:
        headers, credential_headers, key_name = {}, [], None
        authorization_key = authorization_scheme = None
        for name, value in handler.headers.items():
            lowered = name.lower()
            carried = [key_id for key_id, key in list(self.keys.items()) if key and key in value]
            if lowered in CREDENTIAL_HEADERS or carried:
                credential_headers.append(lowered)
                if carried:
                    key_name = carried[0]
                elif lowered in CREDENTIAL_HEADERS and key_name is None:
                    key_name = "<unrecognised>"
                if lowered == "authorization":
                    authorization_key = carried[0] if carried else "<unrecognised>"
                    authorization_scheme = value.split(" ", 1)[0] if " " in value else None
                continue
            headers[lowered] = value
        record = Recorded(method=handler.command, path=handler.path, headers=headers,
                          credential_headers=credential_headers, key_name=key_name,
                          authorization_key=authorization_key, authorization_scheme=authorization_scheme,
                          body_sha256=hashlib.sha256(body).hexdigest(), body_len=len(body),
                          status=status, body=body)
        line = {k: getattr(record, k) for k in ("method", "path", "headers", "credential_headers",
                                                 "key_name", "authorization_key", "authorization_scheme",
                                                 "body_sha256", "body_len", "status")}
        line["stub"] = self.label
        line["t"] = time.time()
        with self._lock:
            self.requests.append(record)
            with self.log_path.open("a") as log:
                log.write(json.dumps(line, sort_keys=True) + "\n")
        return record

    def _handler(self):
        stub = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, *_):
                pass

            def _body(self) -> bytes:
                if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
                    data = b""
                    while True:
                        size = int(self.rfile.readline().split(b";")[0].strip() or b"0", 16)
                        if size == 0:
                            while self.rfile.readline() not in (b"\r\n", b"\n", b""):
                                pass
                            return data
                        data += self.rfile.read(size)
                        self.rfile.readline()
                length = int(self.headers.get("Content-Length") or 0)
                return self.rfile.read(length) if length else b""

            def _send(self, status: int, body: bytes, content_type: str):
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _json(self, status: int, value):
                self._send(status, json.dumps(value).encode(), "application/json")

            def do_GET(self):  # noqa: N802
                status, value, raw = stub._get(self.path)
                stub._record(self, b"", status)
                if raw is not None:
                    self._send(status, raw[0], raw[1])
                else:
                    self._json(status, value)

            def do_POST(self):  # noqa: N802
                body = self._body()
                credential = " ".join(v for k, v in self.headers.items() if k.lower() in CREDENTIAL_HEADERS)
                status, payload, content_type = stub._post(self.path, body, self.headers, credential)
                stub._record(self, body, status)
                if content_type == "text/event-stream":
                    self.send_response(status)
                    self.send_header("Content-Type", content_type)
                    self.send_header("Cache-Control", "no-cache")
                    self.end_headers()
                    self.wfile.write(payload)
                    self.wfile.flush()
                    self.close_connection = True
                else:
                    self._send(status, payload, content_type)

        return Handler

    # ------------------------------------------------------------ responses

    def _usage(self, extra=None) -> dict:
        usage = {"prompt_tokens": 11, "completion_tokens": 22, "total_tokens": 33}
        if self.cost is not None:
            usage["cost"] = self.cost
        usage.update(extra or {})
        return usage

    def _modalities(self, supported: bool) -> dict:
        if supported:
            return {"input_modalities": ["text", "image", "audio", "file"],
                    "output_modalities": ["text", "image", "audio"], "modality": "text+image+audio->text+image+audio"}
        return {"input_modalities": ["text"], "output_modalities": ["text"], "modality": "text->text"}

    def _get(self, raw_path: str):
        parts = urlsplit(raw_path)
        path = parts.path.rstrip("/")
        if path.endswith("/models") or path == "/models":
            data = [{"id": model, "name": model, "created": 1_700_000_000, "context_length": 128000,
                     "pricing": dict(self.pricing),
                     "architecture": self._modalities(self.mode != UNSUPPORTED),
                     "top_provider": {"context_length": 128000, "max_completion_tokens": 16384,
                                      "is_moderated": False},
                     "supported_parameters": ["max_tokens", "temperature", "modalities"]}
                    for model in self.models]
            return 200, {"data": data}, None
        if path.endswith("/generation"):
            ident = (parse_qs(parts.query).get("id") or [""])[0]
            return 200, {"data": {"id": ident, "total_cost": self.cost or 0, "usage": self.cost or 0,
                                  "model": self.models[0] if self.models else "", "is_byok": False}}, None
        if path.endswith("/auth/key") or path.endswith("/key"):
            return 200, {"data": {"label": "m1-stub", "usage": 0, "limit": None, "is_free_tier": False}}, None
        if path.endswith("/credits"):
            return 200, {"data": {"total_credits": 100, "total_usage": 0}}, None
        if "/files/" in path:
            blob = self._files.get(path.rsplit("/", 1)[-1])
            if blob is not None:
                return 200, None, (blob, "image/png")
        return 404, {"error": {"message": "not found", "code": 404}}, None

    def _error_body(self, status: int, credential: str) -> bytes:
        message = f"M1 stub provider failure {self.error_marker}"
        if self.echo_key_in_errors:
            message += f" (received credential: {credential})"
        return json.dumps({"error": {"message": message, "code": status,
                                     "metadata": {"provider_name": "m1-stub"}}}).encode()

    def _post(self, raw_path: str, body: bytes, headers, credential: str):
        path = urlsplit(raw_path).path.rstrip("/")
        with self._lock:
            self._posts_seen += 1
            ordinal = self._posts_seen
        mode = self.mode
        if mode == "status:503-then-ok":
            mode = OK if ordinal > 1 else "status:503"
        if mode.startswith("status:"):
            status = int(mode.split(":")[1])
            return status, self._error_body(status, credential), "application/json"
        if mode == "error-envelope-200":
            return 200, self._error_body(502, credential), "application/json"
        if mode == "non-json-200":
            return 200, b"<html><body>upstream returned something that is not JSON</body></html>", "text/html"
        try:
            request = json.loads(body) if body[:1] in (b"{", b"[") else None
        except ValueError:
            request = None
        request = request if isinstance(request, dict) else {}
        model = request.get("model") if isinstance(request.get("model"), str) else (
            self.models[0] if self.models else "unknown")
        unsupported = mode == UNSUPPORTED
        now = int(time.time())
        ident = f"gen-{self.nonce}-{ordinal}"

        if path.endswith("/chat/completions"):
            modalities = request.get("modalities") if isinstance(request.get("modalities"), list) else []
            wants_image = "image" in modalities
            wants_audio = "audio" in modalities or isinstance(request.get("audio"), dict)
            sends_audio = b"input_audio" in body or b'"type": "input_audio"' in body
            if unsupported and (wants_image or wants_audio or sends_audio):
                return 400, json.dumps({"error": {"message": "this model does not support the requested "
                                                  "modality", "code": 400}}).encode(), "application/json"
            message = {"role": "assistant", "content": self.text}
            if wants_image:
                message["images"] = [{"type": "image_url", "image_url": {
                    "url": "data:image/png;base64," + base64.b64encode(self.image).decode()}}]
            if wants_audio:
                message["audio"] = {"id": f"audio-{ident}", "data": base64.b64encode(self.audio).decode(),
                                    "transcript": self.text_marker, "expires_at": now + 3600, "format": "wav"}
            if request.get("stream") is True:
                chunks = [{"id": ident, "object": "chat.completion.chunk", "created": now, "model": model,
                           "provider": "m1-stub",
                           "choices": [{"index": 0, "delta": dict(message), "finish_reason": None}]},
                          {"id": ident, "object": "chat.completion.chunk", "created": now, "model": model,
                           "provider": "m1-stub",
                           "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                           "usage": self._usage()}]
                stream = b"".join(b"data: " + json.dumps(c).encode() + b"\n\n" for c in chunks)
                return 200, stream + b"data: [DONE]\n\n", "text/event-stream"
            value = {"id": ident, "object": "chat.completion", "created": now, "model": model,
                     "provider": "m1-stub",
                     "choices": [{"index": 0, "finish_reason": "stop", "native_finish_reason": "stop",
                                  "message": message}],
                     "usage": self._usage()}
            return 200, json.dumps(value).encode(), "application/json"
        if path.endswith("/responses"):
            value = {"id": ident, "object": "response", "created_at": now, "model": model,
                     "status": "completed",
                     "output": [{"type": "message", "id": f"msg-{ident}", "status": "completed",
                                 "role": "assistant",
                                 "content": [{"type": "output_text", "text": self.text, "annotations": []}]}],
                     "output_text": self.text,
                     "usage": self._usage({"input_tokens": 11, "output_tokens": 22})}
            return 200, json.dumps(value).encode(), "application/json"
        if path.endswith("/completions"):
            value = {"id": ident, "object": "text_completion", "created": now, "model": model,
                     "choices": [{"index": 0, "text": self.text, "finish_reason": "stop"}],
                     "usage": self._usage()}
            return 200, json.dumps(value).encode(), "application/json"
        if unsupported:
            return 404, json.dumps({"error": {"message": "this provider does not offer this modality",
                                              "code": 404}}).encode(), "application/json"
        if path.endswith("/images/generations"):
            if request.get("response_format") == "url":
                name = f"{ident}.png"
                self._files[name] = self.image
                item = {"url": f"http://127.0.0.1:{self.port}/files/{name}"}
            else:
                item = {"b64_json": base64.b64encode(self.image).decode()}
            item["revised_prompt"] = request.get("prompt") if isinstance(request.get("prompt"), str) else ""
            value = {"created": now, "data": [item], "usage": self._usage()}
            return 200, json.dumps(value).encode(), "application/json"
        if path.endswith("/audio/transcriptions") or path.endswith("/audio/translations"):
            if b'name="response_format"\r\n\r\ntext' in body or request.get("response_format") == "text":
                return 200, self.text.encode(), "text/plain"
            value = {"text": self.text, "usage": self._usage({"type": "tokens"})}
            return 200, json.dumps(value).encode(), "application/json"
        if path.endswith("/audio/speech"):
            return 200, self.audio, "audio/wav"
        return 404, json.dumps({"error": {"message": f"unknown endpoint {path}", "code": 404}}).encode(), \
            "application/json"
