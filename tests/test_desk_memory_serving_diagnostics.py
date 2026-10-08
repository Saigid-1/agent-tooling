"""Observable serving route and model reuse without loading production weights."""

from __future__ import annotations
import pytest

import importlib
import json
import threading
import time
from types import SimpleNamespace

from kp_agent_tooling._impl.embeddings.embedders import RealEmbedder


def test_real_embedder_loads_once_for_concurrent_warm_calls(monkeypatch) -> None:
    constructions: list[str] = []

    class FakeModel:
        def __init__(self, model_id: str) -> None:
            constructions.append(model_id)
            time.sleep(0.02)

        def encode(self, texts: list[str], **_kwargs: object) -> list[list[float]]:
            return [[1.0, 0.0] for _ in texts]

    real_import = importlib.import_module

    def load_module(name: str):
        if name == "sentence_transformers":
            return SimpleNamespace(SentenceTransformer=FakeModel)
        return real_import(name)

    monkeypatch.setattr("kp_agent_tooling._impl.embeddings.embedders.importlib.import_module", load_module)
    embedder = RealEmbedder(model_digest="sha256:fixture", dim=2)
    threads = [threading.Thread(target=embedder.embed, args=(["query"],)) for _ in range(5)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=2)
        assert not thread.is_alive()
    assert len(constructions) == 1
    assert embedder.diagnostics["model_load_count"] == 1
    assert embedder.diagnostics["embed_call_count"] == 5
    assert embedder.diagnostics["last_batch_size"] == 1
    assert isinstance(embedder.diagnostics["model_load_ms"], float)


@pytest.mark.skip(reason='S5 excluded: needs OPS-only scripts/desk_memory_cli.py and kp_ops.service.desk_memory_daemon (desk_memory* legacy)')
def test_cli_reports_resident_or_cold_fallback_on_stderr_only(monkeypatch, capsys) -> None:
    cli = importlib.import_module("scripts.desk_memory_cli")
    daemon = importlib.import_module("kp_agent_tooling._impl.service.desk_memory_daemon")
    binding = {"tenant_id": "tenant-a", "role": "desk", "repo_key": "org/repo"}
    hit = SimpleNamespace(shared=False, score=0.9, text="A fact")
    calls: list[dict[str, object]] = []

    class Client:
        def __init__(self) -> None:
            self.unavailable = False

        def recall(self, **kwargs):
            calls.append(kwargs)
            if self.unavailable:
                raise daemon.RecallTransportError("socket failed")
            return (hit,)

        def diagnostics(self):
            return {"model_load_count": 1, "model_load_ms": 20.0,
                    "embed_call_count": 5, "last_embed_ms": 1.0,
                    "last_batch_size": 1}

    client = Client()
    monkeypatch.setattr(cli, "_remote_client", lambda _args: client)
    monkeypatch.setattr(cli, "_binding", lambda _args: binding)

    class Reader:
        _embedder = SimpleNamespace(diagnostics={
            "model_load_count": 1, "model_load_ms": 20.0,
            "embed_call_count": 1, "last_embed_ms": 1.0,
            "last_batch_size": 1,
        })

        def recall(self, **kwargs):
            calls.append(kwargs)
            return (hit,)

    reader = Reader()
    monkeypatch.setattr(cli, "_reader_writer", lambda: (reader, None, None, None))
    monkeypatch.setattr(cli, "_resolved_binding", lambda _args, _reader: binding)
    args = SimpleNamespace(query="question", top_k=1, scope="shared", diagnostics=True, no_daemon=False)

    assert cli._cmd_recall(args) == 0
    first = capsys.readouterr()
    assert first.out == "[desk 0.900] A fact\n"
    resident_diagnostic = json.loads(first.err)
    assert resident_diagnostic["route"] == "resident"
    assert resident_diagnostic["embedder"]["model_load_count"] == 1
    assert calls[-1]["scope"] == "shared"

    client.unavailable = True
    assert cli._cmd_recall(args) == 0
    second = capsys.readouterr()
    assert second.out == first.out
    lines = second.err.splitlines()
    assert "using cold reader" in lines[0]
    diagnostics = json.loads(lines[1])
    assert diagnostics["route"] == "cold_fallback"
    assert diagnostics["embedder"]["model_load_count"] == 1
    assert calls[-1]["scope"] == "shared"


@pytest.mark.skip(reason='S5 excluded: needs OPS-only kp_ops.service.desk_memory_daemon (desk_memory* legacy)')
def test_resident_diagnostics_only_exposes_bounded_counters(monkeypatch) -> None:
    daemon = importlib.import_module("kp_agent_tooling._impl.service.desk_memory_daemon")
    emitted: list[bytes] = []

    class Connection:
        def settimeout(self, _timeout: float) -> None:
            pass

        def sendall(self, data: bytes) -> None:
            emitted.append(data)

    monkeypatch.setattr(daemon, "_receive_line", lambda *_args, **_kwargs: b'{"action":"diagnostics"}')
    private = {
        "model_load_count": 1, "model_load_ms": 20.0,
        "embed_call_count": 5, "last_embed_ms": 1.0,
        "last_batch_size": 1,
    }
    reader = SimpleNamespace(_embedder=SimpleNamespace(diagnostics=private))
    daemon._serve_request(Connection(), reader=reader, run_reader=object())
    assert json.loads(emitted[0]) == {"embedder": private}

    reader._embedder.diagnostics = {**private, "secret": "query text"}
    daemon._serve_request(Connection(), reader=reader, run_reader=object())
    assert json.loads(emitted[1]) == {"embedder": None}
