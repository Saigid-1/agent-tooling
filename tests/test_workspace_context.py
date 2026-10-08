"""Acceptance coverage for portable workspace launch context."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest

from kp_agent_tooling._impl.service.workspace_context import (
    DoctrineRevision,
    MemoryResolution,
    ReviewedMemoryFileResolver,
    WorkspaceContextError,
    build_launch_context,
    load_workspace_catalog,
    validate_workspace_readiness,
)
from kp_agent_tooling._impl.service.desk_identity import DeskNoteBinding


ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "config/desk-context/catalog.example.json"
MEMORY = ROOT / "config/desk-context/memory.example.json"


def test_example_catalog_is_ready_and_distinguishes_approved_team_from_suggestions() -> None:
    catalog = load_workspace_catalog(CATALOG)
    readiness = validate_workspace_readiness(
        catalog,
        workspace_root=CATALOG.parent,
        memory_resolver=ReviewedMemoryFileResolver(MEMORY),
    )

    assert readiness.ready is True
    assert readiness.reasons == ()
    assert readiness.source_revision_status == "declared_unverified"
    assert [role.role_id for role in catalog.roles] == ["implementation", "verification"]
    assert all(role.selection_basis == "human_approved" for role in catalog.roles)
    assert catalog.team_suggestions[0].role_ids == ("architecture",)
    assert catalog.team_suggestions[0].status == "unapproved_suggestion"


def test_launch_context_freezes_source_desk_doctrine_and_scoped_real_fixture_memory() -> None:
    catalog = load_workspace_catalog(CATALOG)
    resolver = ReviewedMemoryFileResolver(MEMORY)

    implementation = build_launch_context(
        catalog,
        desk_id="implementation-desk",
        workspace_root=CATALOG.parent,
        memory_query="What has this desk learned?",
        memory_resolver=resolver,
    )
    verification = build_launch_context(
        catalog,
        desk_id="verification-desk",
        workspace_root=CATALOG.parent,
        memory_query="What has this desk learned?",
        memory_resolver=resolver,
    )
    repeated = build_launch_context(
        catalog,
        desk_id="implementation-desk",
        workspace_root=CATALOG.parent,
        memory_query="A browser reload may phrase the query differently",
        memory_resolver=resolver,
    )

    assert implementation["project"]["source_revision"] == (
        "a9fbfcf11c4cffa6a583c00df1b2a95bddec91db"
    )
    assert implementation["source_revision_status"] == "declared_unverified"
    assert implementation["desk"]["doctrine"]["revision_id"].startswith("doctrine:")
    assert implementation["doctrine_text"].startswith("Prefer narrow")
    assert implementation["memory"]["status"] == "available"
    assert implementation["memory"]["service"] == "reviewed_file_fixture"
    assert implementation["memory"]["records"][0]["note_key"] == "demo-note:implementation:1"
    assert verification["memory"]["records"][0]["note_key"] == "demo-note:verification:1"
    assert implementation["memory"]["records"] != verification["memory"]["records"]
    assert implementation["context_digest"] == repeated["context_digest"]


def test_missing_memory_is_explicit_and_only_blocks_when_required() -> None:
    catalog = load_workspace_catalog(CATALOG)

    optional = validate_workspace_readiness(
        catalog, workspace_root=CATALOG.parent, memory_resolver=None
    )
    required = validate_workspace_readiness(
        catalog, workspace_root=CATALOG.parent, memory_resolver=None, require_memory=True
    )
    context = build_launch_context(
        catalog,
        desk_id="implementation-desk",
        workspace_root=CATALOG.parent,
        memory_query="Prior decisions",
        memory_resolver=None,
    )

    assert optional.ready is True
    assert required.ready is False
    assert required.reasons == ("memory_resolver_unavailable",)
    assert context["memory"] == {
        "status": "unavailable",
        "service": "unconfigured",
        "records": (),
        "reason": "no configured desk-memory resolver",
    }


def test_doctrine_file_must_match_reviewed_immutable_revision(tmp_path: Path) -> None:
    catalog = load_workspace_catalog(CATALOG)
    doctrine = tmp_path / "doctrine.md"
    doctrine.write_text("changed policy\n", encoding="utf-8")
    original = catalog.desks[0].doctrine
    relocated = replace(original, source_ref="doctrine.md")
    changed_catalog = replace(
        catalog,
        desks=(replace(catalog.desks[0], doctrine=relocated), catalog.desks[1]),
    )

    readiness = validate_workspace_readiness(
        changed_catalog,
        workspace_root=tmp_path,
        memory_resolver=ReviewedMemoryFileResolver(MEMORY),
    )
    assert readiness.ready is False
    assert readiness.reasons == (
        "desk:implementation-desk:doctrine:content digest does not match the reviewed doctrine file",
        "desk:verification-desk:doctrine:content digest does not match the reviewed doctrine file",
    )


def test_catalog_refuses_algorithmic_selection_and_noncanonical_memory_binding(tmp_path: Path) -> None:
    document = json.loads(CATALOG.read_text(encoding="utf-8"))
    document["team"]["selection"] = "algorithmically_discovered"
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(WorkspaceContextError, match="team.selection must be human_approved"):
        load_workspace_catalog(path)

    document["team"]["selection"] = "human_approved"
    document["desks"][0]["memory_binding_id"] = "binding:wrong"
    path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(WorkspaceContextError, match="canonical desk identity"):
        load_workspace_catalog(path)


def test_launch_context_refuses_unbounded_recall_and_oversized_bundle() -> None:
    catalog = load_workspace_catalog(CATALOG)
    with pytest.raises(WorkspaceContextError, match="top_k must be between 1 and 20"):
        build_launch_context(
            catalog,
            desk_id="implementation-desk",
            workspace_root=CATALOG.parent,
            memory_query="prior decisions",
            memory_resolver=ReviewedMemoryFileResolver(MEMORY),
            top_k=21,
        )

    class OversizedResolver:
        service_name = "oversized-test"

        def resolve(self, *, binding, query, top_k):
            return MemoryResolution(
                status="available",
                service=self.service_name,
                records=tuple(
                    _memory_record(f"{index}:" + "x" * 7990, index)
                    for index in range(9)
                ),
            )

    with pytest.raises(WorkspaceContextError, match="64 KiB storage budget"):
        build_launch_context(
            catalog,
            desk_id="implementation-desk",
            workspace_root=CATALOG.parent,
            memory_query="prior decisions",
            memory_resolver=OversizedResolver(),
        )


def test_file_memory_requires_digest_and_provenance(tmp_path: Path) -> None:
    catalog = load_workspace_catalog(CATALOG)
    fixture = json.loads(MEMORY.read_text(encoding="utf-8"))
    binding = catalog.desks[0].binding_id
    fixture["bindings"][binding][0]["content_digest"] = hashlib.sha256(b"other").hexdigest()
    path = tmp_path / "memory.json"
    path.write_text(json.dumps(fixture), encoding="utf-8")

    result = ReviewedMemoryFileResolver(path).resolve(
        binding=_binding(catalog), query="anything", top_k=2
    )
    assert result == MemoryResolution.unavailable(
        "fixture memory content digest does not match text",
        service="reviewed_file_fixture",
    )


def _binding(catalog):
    return DeskNoteBinding(
        tenant_id=catalog.project.tenant_id,
        role=catalog.desks[0].role_id,
        repo_key=catalog.project.repo_key,
    )


def _memory_record(text: str, index: int):
    from kp_agent_tooling._impl.service.workspace_context import MemoryRecord

    return MemoryRecord(
        note_key=f"test-note:oversized:{index}",
        text=text,
        content_digest=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        trust_class="desk_authored_fact",
        provenance={"source": "test"},
    )
