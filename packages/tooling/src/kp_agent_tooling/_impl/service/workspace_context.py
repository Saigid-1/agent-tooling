"""Portable project, team, desk, doctrine, and memory launch context.

The catalog is configuration, not a team-design agent.  Only human-approved role
templates can enter a launch context.  Memory is resolved through an injected
service and desk-authored records remain evidence; they are never promoted to
policy by this module.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol

from kp_agent_tooling._impl import leaf
from kp_agent_tooling._impl.service.desk_identity import binding_key, DeskNoteBinding


_REVISION = re.compile(r"[0-9a-f]{40}")
_DIGEST = re.compile(r"[0-9a-f]{64}")
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}")
_CATALOG_MAX_BYTES = 256 * 1024
_DOCTRINE_MAX_BYTES = 32 * 1024
_MEMORY_FIXTURE_MAX_BYTES = 256 * 1024
_MEMORY_TEXT_MAX_BYTES = 8 * 1024
_LAUNCH_CONTEXT_MAX_BYTES = 64 * 1024
_TOP_K_MAX = 20


class WorkspaceContextError(ValueError):
    """A workspace catalog or launch-context contract is invalid."""


@dataclass(frozen=True, slots=True)
class ProjectIdentity:
    project_id: str
    tenant_id: str
    repo_key: str
    source_revision: str


@dataclass(frozen=True, slots=True)
class RoleTemplate:
    role_id: str
    label: str
    desk_id: str
    template_revision: str
    approval_ref: str
    selection_basis: str = "human_approved"


@dataclass(frozen=True, slots=True)
class TeamSuggestion:
    suggestion_id: str
    role_ids: tuple[str, ...]
    basis: str
    status: str = "unapproved_suggestion"


@dataclass(frozen=True, slots=True)
class DoctrineRevision:
    doctrine_id: str
    revision_id: str
    source_ref: str
    content_digest: str
    reviewed_by: str
    reviewed_at: str
    authority_class: str = "human_reviewed_policy"


@dataclass(frozen=True, slots=True)
class DeskDefinition:
    desk_id: str
    label: str
    role_id: str
    binding_id: str
    memory_binding_id: str
    doctrine: DoctrineRevision


@dataclass(frozen=True, slots=True)
class WorkspaceCatalog:
    schema_version: str
    project: ProjectIdentity
    team_approval_ref: str
    roles: tuple[RoleTemplate, ...]
    desks: tuple[DeskDefinition, ...]
    team_suggestions: tuple[TeamSuggestion, ...] = ()


@dataclass(frozen=True, slots=True)
class MemoryRecord:
    note_key: str
    text: str
    content_digest: str
    trust_class: str
    provenance: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class MemoryResolution:
    status: str
    service: str
    records: tuple[MemoryRecord, ...] = ()
    reason: str | None = None

    @classmethod
    def unavailable(cls, reason: str, *, service: str = "unconfigured") -> "MemoryResolution":
        return cls(
            status="unavailable",
            service=_required_text(service, "memory.service"),
            reason=_required_text(reason, "memory.reason"),
        )


class WorkspaceMemoryResolver(Protocol):
    service_name: str

    def resolve(
        self, *, binding: DeskNoteBinding, query: str, top_k: int
    ) -> MemoryResolution: ...


@dataclass(frozen=True, slots=True)
class WorkspaceReadiness:
    ready: bool
    reasons: tuple[str, ...]
    project_id: str
    source_revision: str
    source_revision_status: str
    desk_ids: tuple[str, ...]


class ReviewedMemoryFileResolver:
    """Read explicit, desk-keyed demo records; this is fixture recall, not RAG."""

    service_name = "reviewed_file_fixture"

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)

    def resolve(
        self, *, binding: DeskNoteBinding, query: str, top_k: int
    ) -> MemoryResolution:
        _required_text(query, "memory_query")
        try:
            document = json.loads(
                _read_bounded(self._path, _MEMORY_FIXTURE_MAX_BYTES).decode("utf-8")
            )
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            return MemoryResolution.unavailable(
                "reviewed memory fixture is unavailable or invalid",
                service=self.service_name,
            )
        try:
            root = _mapping(document, "memory fixture")
            _exact_keys(
                root,
                required={"schema_version", "bindings"},
                label="memory fixture",
            )
            if root["schema_version"] != "workspace-memory-fixture/v1":
                raise WorkspaceContextError("memory fixture schema is unsupported")
            bindings = _mapping(root["bindings"], "memory fixture.bindings")
            key = binding_key(
                tenant_id=binding.tenant_id,
                role=binding.role,
                repo_key=binding.repo_key,
            )
            rows = _sequence(bindings.get(key, ()), f"memory fixture.bindings.{key}")
            records = tuple(_parse_memory_record(row, index) for index, row in enumerate(rows))
        except WorkspaceContextError as error:
            return MemoryResolution.unavailable(str(error), service=self.service_name)
        return MemoryResolution(
            status="available",
            service=self.service_name,
            records=records[:top_k],
        )


def load_workspace_catalog(path: str | Path) -> WorkspaceCatalog:
    """Load the closed JSON catalog without contacting Core or a model."""

    catalog_path = Path(path)
    try:
        document = json.loads(
            _read_bounded(catalog_path, _CATALOG_MAX_BYTES).decode("utf-8")
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise WorkspaceContextError("workspace catalog is unavailable or invalid JSON") from error
    root = _mapping(document, "catalog")
    _exact_keys(
        root,
        required={"schema_version", "project", "team", "desks"},
        optional={"team_suggestions"},
        label="catalog",
    )
    if root["schema_version"] != "workspace-context/v1":
        raise WorkspaceContextError("catalog.schema_version must be workspace-context/v1")

    project_row = _mapping(root["project"], "project")
    _exact_keys(
        project_row,
        required={"project_id", "tenant_id", "repo_key", "source_revision"},
        label="project",
    )
    project = ProjectIdentity(
        project_id=_identifier(project_row["project_id"], "project.project_id"),
        tenant_id=_identifier(project_row["tenant_id"], "project.tenant_id"),
        repo_key=_identifier(project_row["repo_key"], "project.repo_key"),
        source_revision=_revision(project_row["source_revision"]),
    )

    team = _mapping(root["team"], "team")
    _exact_keys(team, required={"selection", "approval_ref", "roles"}, label="team")
    if team["selection"] != "human_approved":
        raise WorkspaceContextError("team.selection must be human_approved")
    approval_ref = _identifier(team["approval_ref"], "team.approval_ref")
    roles = tuple(_parse_role(row, index) for index, row in enumerate(_sequence(team["roles"], "team.roles")))
    if not roles:
        raise WorkspaceContextError("team.roles must not be empty")
    role_ids = {role.role_id for role in roles}
    desk_ids = {role.desk_id for role in roles}
    if len(role_ids) != len(roles) or len(desk_ids) != len(roles):
        raise WorkspaceContextError("team roles and desk assignments must be unique")

    desks = tuple(_parse_desk(row, index, project) for index, row in enumerate(_sequence(root["desks"], "desks")))
    if {desk.desk_id for desk in desks} != desk_ids:
        raise WorkspaceContextError("desks must exactly cover the approved team desk assignments")
    for desk in desks:
        if desk.role_id not in role_ids:
            raise WorkspaceContextError(f"desk {desk.desk_id!r} names an unapproved role")
        assigned = next(role for role in roles if role.role_id == desk.role_id)
        if assigned.desk_id != desk.desk_id:
            raise WorkspaceContextError(f"desk {desk.desk_id!r} conflicts with its role assignment")

    suggestions = tuple(
        _parse_suggestion(row, index)
        for index, row in enumerate(_sequence(root.get("team_suggestions", ()), "team_suggestions"))
    )
    return WorkspaceCatalog(
        schema_version="workspace-context/v1",
        project=project,
        team_approval_ref=approval_ref,
        roles=roles,
        desks=desks,
        team_suggestions=suggestions,
    )


def validate_workspace_readiness(
    catalog: WorkspaceCatalog,
    *,
    workspace_root: str | Path,
    memory_resolver: WorkspaceMemoryResolver | None,
    require_memory: bool = False,
) -> WorkspaceReadiness:
    reasons: list[str] = []
    for desk in catalog.desks:
        try:
            _verify_doctrine(desk.doctrine, workspace_root)
        except WorkspaceContextError as error:
            reasons.append(f"desk:{desk.desk_id}:doctrine:{error}")
    if memory_resolver is None and require_memory:
        reasons.append("memory_resolver_unavailable")
    elif memory_resolver is not None and not isinstance(
        getattr(memory_resolver, "service_name", None), str
    ):
        reasons.append("memory_resolver_invalid")
    return WorkspaceReadiness(
        ready=not reasons,
        reasons=tuple(reasons),
        project_id=catalog.project.project_id,
        source_revision=catalog.project.source_revision,
        source_revision_status="declared_unverified",
        desk_ids=tuple(desk.desk_id for desk in catalog.desks),
    )


def build_launch_context(
    catalog: WorkspaceCatalog,
    *,
    desk_id: str,
    workspace_root: str | Path,
    memory_query: str,
    memory_resolver: WorkspaceMemoryResolver | None,
    top_k: int = 5,
) -> dict[str, object]:
    """Build a deterministic, digest-bound launch bundle for one approved desk."""

    selected_id = _identifier(desk_id, "desk_id")
    try:
        desk = next(candidate for candidate in catalog.desks if candidate.desk_id == selected_id)
    except StopIteration as error:
        raise WorkspaceContextError(f"desk {selected_id!r} is not in the approved team") from error
    role = next(candidate for candidate in catalog.roles if candidate.role_id == desk.role_id)
    doctrine_text = _verify_doctrine(desk.doctrine, workspace_root)
    query = _required_text(memory_query, "memory_query")
    if (
        isinstance(top_k, bool)
        or not isinstance(top_k, int)
        or top_k <= 0
        or top_k > _TOP_K_MAX
    ):
        raise WorkspaceContextError(f"top_k must be between 1 and {_TOP_K_MAX}")
    binding = DeskNoteBinding(
        tenant_id=catalog.project.tenant_id,
        role=desk.role_id,
        repo_key=catalog.project.repo_key,
    )
    memory = (
        MemoryResolution.unavailable("no configured desk-memory resolver")
        if memory_resolver is None
        else memory_resolver.resolve(binding=binding, query=query, top_k=top_k)
    )
    _validate_memory_resolution(memory)
    body: dict[str, object] = {
        "schema_version": "workspace-launch-context/v1",
        "project": asdict(catalog.project),
        "source_revision_status": "declared_unverified",
        "team_approval_ref": catalog.team_approval_ref,
        "role": asdict(role),
        "desk": asdict(desk),
        "doctrine_text": doctrine_text,
        "memory": asdict(memory),
    }
    result = {**body, "context_digest": leaf.canonical_sha256(body, ascii=True, allow_nan=True)}
    final_encoded = leaf.canonical_bytes(result, ascii=True, allow_nan=True)
    if len(final_encoded) > _LAUNCH_CONTEXT_MAX_BYTES:
        raise WorkspaceContextError("launch context exceeds the 64 KiB storage budget")
    return result


def _parse_role(value: object, index: int) -> RoleTemplate:
    row = _mapping(value, f"team.roles[{index}]")
    _exact_keys(
        row,
        required={"role_id", "label", "desk_id", "template_revision", "approval_ref", "selection_basis"},
        label=f"team.roles[{index}]",
    )
    if row["selection_basis"] != "human_approved":
        raise WorkspaceContextError(f"team.roles[{index}].selection_basis must be human_approved")
    return RoleTemplate(
        role_id=_identifier(row["role_id"], f"team.roles[{index}].role_id"),
        label=_required_text(row["label"], f"team.roles[{index}].label"),
        desk_id=_identifier(row["desk_id"], f"team.roles[{index}].desk_id"),
        template_revision=_identifier(row["template_revision"], f"team.roles[{index}].template_revision"),
        approval_ref=_identifier(row["approval_ref"], f"team.roles[{index}].approval_ref"),
    )


def _parse_desk(value: object, index: int, project: ProjectIdentity) -> DeskDefinition:
    row = _mapping(value, f"desks[{index}]")
    _exact_keys(
        row,
        required={"desk_id", "label", "role_id", "binding_id", "memory_binding_id", "doctrine"},
        label=f"desks[{index}]",
    )
    role_id = _identifier(row["role_id"], f"desks[{index}].role_id")
    expected_binding = binding_key(
        tenant_id=project.tenant_id, role=role_id, repo_key=project.repo_key
    )
    actual_binding = _required_text(row["binding_id"], f"desks[{index}].binding_id")
    memory_binding = _required_text(row["memory_binding_id"], f"desks[{index}].memory_binding_id")
    if actual_binding != expected_binding or memory_binding != expected_binding:
        raise WorkspaceContextError(
            f"desks[{index}] binding ids must match the canonical desk identity"
        )
    return DeskDefinition(
        desk_id=_identifier(row["desk_id"], f"desks[{index}].desk_id"),
        label=_required_text(row["label"], f"desks[{index}].label"),
        role_id=role_id,
        binding_id=actual_binding,
        memory_binding_id=memory_binding,
        doctrine=_parse_doctrine(row["doctrine"], index),
    )


def _parse_doctrine(value: object, index: int) -> DoctrineRevision:
    row = _mapping(value, f"desks[{index}].doctrine")
    _exact_keys(
        row,
        required={"doctrine_id", "revision_id", "source_ref", "content_digest", "reviewed_by", "reviewed_at", "authority_class"},
        label=f"desks[{index}].doctrine",
    )
    if row["authority_class"] != "human_reviewed_policy":
        raise WorkspaceContextError("doctrine authority_class must be human_reviewed_policy")
    digest = _digest(row["content_digest"], "doctrine.content_digest")
    revision_id = _required_text(row["revision_id"], "doctrine.revision_id")
    if revision_id != f"doctrine:{digest}":
        raise WorkspaceContextError("doctrine.revision_id must bind its content digest")
    reviewed_at = _required_text(row["reviewed_at"], "doctrine.reviewed_at")
    try:
        reviewed_time = datetime.fromisoformat(reviewed_at.replace("Z", "+00:00"))
    except ValueError as error:
        raise WorkspaceContextError("doctrine.reviewed_at must be ISO-8601") from error
    if reviewed_time.tzinfo is None:
        raise WorkspaceContextError("doctrine.reviewed_at must include a timezone")
    return DoctrineRevision(
        doctrine_id=_identifier(row["doctrine_id"], "doctrine.doctrine_id"),
        revision_id=revision_id,
        source_ref=_required_text(row["source_ref"], "doctrine.source_ref"),
        content_digest=digest,
        reviewed_by=_identifier(row["reviewed_by"], "doctrine.reviewed_by"),
        reviewed_at=reviewed_at,
    )


def _parse_suggestion(value: object, index: int) -> TeamSuggestion:
    row = _mapping(value, f"team_suggestions[{index}]")
    _exact_keys(
        row,
        required={"suggestion_id", "role_ids", "basis", "status"},
        label=f"team_suggestions[{index}]",
    )
    if row["status"] != "unapproved_suggestion":
        raise WorkspaceContextError("team suggestions cannot be marked approved")
    return TeamSuggestion(
        suggestion_id=_identifier(row["suggestion_id"], "team_suggestion.suggestion_id"),
        role_ids=tuple(
            _identifier(role_id, "team_suggestion.role_id")
            for role_id in _sequence(row["role_ids"], "team_suggestion.role_ids")
        ),
        basis=_required_text(row["basis"], "team_suggestion.basis"),
    )


def _verify_doctrine(doctrine: DoctrineRevision, workspace_root: str | Path) -> str:
    root = Path(workspace_root).resolve()
    relative = Path(doctrine.source_ref)
    if relative.is_absolute() or ".." in relative.parts:
        raise WorkspaceContextError("source_ref must be a relative in-workspace path")
    source = (root / relative).resolve()
    if source != root and root not in source.parents:
        raise WorkspaceContextError("source_ref escapes the workspace")
    try:
        content = _read_bounded(source, _DOCTRINE_MAX_BYTES)
        observed = hashlib.sha256(content).hexdigest()
        text = content.decode("utf-8")
    except (OSError, UnicodeError) as error:
        raise WorkspaceContextError("source_ref is unavailable") from error
    if observed != doctrine.content_digest:
        raise WorkspaceContextError("content digest does not match the reviewed doctrine file")
    return text


def _parse_memory_record(value: object, index: int) -> MemoryRecord:
    row = _mapping(value, f"memory fixture record {index}")
    _exact_keys(
        row,
        required={"note_key", "text", "content_digest", "trust_class", "provenance"},
        label=f"memory fixture record {index}",
    )
    if row["trust_class"] != "desk_authored_fact":
        raise WorkspaceContextError("fixture memory must remain a desk-authored fact")
    text = _required_text(row["text"], "memory.text")
    if len(text.encode("utf-8")) > _MEMORY_TEXT_MAX_BYTES:
        raise WorkspaceContextError("fixture memory text exceeds the 8 KiB record budget")
    digest = _digest(row["content_digest"], "memory.content_digest")
    if hashlib.sha256(text.encode("utf-8")).hexdigest() != digest:
        raise WorkspaceContextError("fixture memory content digest does not match text")
    provenance_row = _mapping(row["provenance"], "memory.provenance")
    if not provenance_row:
        raise WorkspaceContextError("fixture memory provenance must not be empty")
    provenance = {
        _identifier(key, "memory.provenance key"): _required_text(value, f"memory.provenance.{key}")
        for key, value in provenance_row.items()
    }
    return MemoryRecord(
        note_key=_identifier(row["note_key"], "memory.note_key"),
        text=text,
        content_digest=digest,
        trust_class="desk_authored_fact",
        provenance=provenance,
    )


def _validate_memory_resolution(memory: object) -> None:
    if not isinstance(memory, MemoryResolution):
        raise WorkspaceContextError("memory resolver returned an invalid result")
    if memory.status not in {"available", "unavailable"}:
        raise WorkspaceContextError("memory status must be available or unavailable")
    _required_text(memory.service, "memory.service")
    if memory.status == "unavailable" and (memory.records or not memory.reason):
        raise WorkspaceContextError("unavailable memory must have a reason and no records")
    if memory.status == "available" and memory.reason is not None:
        raise WorkspaceContextError("available memory cannot carry an unavailable reason")
    for record in memory.records:
        if record.trust_class != "desk_authored_fact":
            raise WorkspaceContextError("memory records must remain desk-authored facts")
        _digest(record.content_digest, "memory.content_digest")
        _identifier(record.note_key, "memory.note_key")
        text = _required_text(record.text, "memory.text")
        if len(text.encode("utf-8")) > _MEMORY_TEXT_MAX_BYTES:
            raise WorkspaceContextError("memory text exceeds the 8 KiB record budget")
        if hashlib.sha256(text.encode("utf-8")).hexdigest() != record.content_digest:
            raise WorkspaceContextError("memory content digest does not match text")
        if not isinstance(record.provenance, Mapping) or not record.provenance:
            raise WorkspaceContextError("memory provenance must not be empty")


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise WorkspaceContextError(f"{label} must be an object")
    return value


def _sequence(value: object, label: str) -> Sequence[object]:
    if not isinstance(value, (list, tuple)):
        raise WorkspaceContextError(f"{label} must be an array")
    return value


def _exact_keys(value: Mapping[str, object], *, required: set[str], label: str, optional: set[str] | None = None) -> None:
    allowed = required | (optional or set())
    if set(value) != required and (not required.issubset(value) or not set(value).issubset(allowed)):
        raise WorkspaceContextError(f"{label} has invalid fields")


def _required_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise WorkspaceContextError(f"{label} must be a non-empty string")
    return value


def _identifier(value: object, label: str) -> str:
    text = _required_text(value, label)
    if _ID.fullmatch(text) is None:
        raise WorkspaceContextError(f"{label} must be a bounded opaque identifier")
    return text


def _revision(value: object) -> str:
    text = _required_text(value, "project.source_revision")
    if _REVISION.fullmatch(text) is None:
        raise WorkspaceContextError("project.source_revision must be a full lowercase commit")
    return text


def _digest(value: object, label: str) -> str:
    text = _required_text(value, label)
    if _DIGEST.fullmatch(text) is None:
        raise WorkspaceContextError(f"{label} must be a lowercase SHA-256 digest")
    return text


def _read_bounded(path: Path, maximum: int) -> bytes:
    try:
        with path.open("rb") as source:
            content = source.read(maximum + 1)
    except OSError as error:
        raise WorkspaceContextError(f"{path.name} is unavailable") from error
    if len(content) > maximum:
        raise WorkspaceContextError(f"{path.name} exceeds its input budget")
    return content
