"""Operator preparation of a reviewed workspace catalog and exact host session.

This module computes identities and validates supplied review facts. It never
creates an approval, initializes a store, or admits a session during planning.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path

from kp_agent_tooling._impl import leaf
from kp_agent_tooling._impl.service.desk_identity import binding_key
from kp_agent_tooling._impl.service.desk_memory_runtime import (
    admit, persist_host_selection, private_json,
)
from kp_agent_tooling._impl.service.workspace_context import (
    load_workspace_catalog, validate_workspace_readiness,
)


_HOST_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")
_SESSION_IDENTITY_NOTE = {"session_identity_basis": "operator_asserted_session_id",
                          "host_existence_verification": "not-assessed"}


def _private_parent(path: str | Path) -> Path:
    return leaf.private_parent(path, message="output requires an existing private host-owned directory (0700)")


def _required(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"human-supplied {label} is required")
    return value


def _validate_catalog(document: dict, catalog_path: Path, workspace_root: Path) -> None:
    # Use the same parser and doctrine checks used at admission. Keep the
    # validation file inside the operator's private directory.
    temporary = leaf.private_named_temp(catalog_path.parent, ".catalog-check-",
                                        lambda stream: stream.write(json.dumps(document).encode("utf-8")), fsync=False)
    try:
        catalog = load_workspace_catalog(temporary)
        readiness = validate_workspace_readiness(catalog, workspace_root=workspace_root,
                                                 memory_resolver=None)
        if not readiness.ready:
            raise ValueError("catalog doctrine is unavailable or changed: " + "; ".join(readiness.reasons))
    finally:
        temporary.unlink(missing_ok=True)


def plan_catalog(*, workspace_root: str | Path, catalog_path: str | Path,
                 project_id: str, tenant_id: str, repo_key: str,
                 source_revision: str, team_approval_ref: str, roles: list[dict],
                 doctrine_source_ref: str, doctrine_id: str,
                 doctrine_reviewed_by: str, doctrine_reviewed_at: str) -> dict:
    """Build a validated catalog from exact human-supplied roles and review facts."""
    target = _private_parent(catalog_path)
    workspace = Path(workspace_root)
    if not workspace.is_absolute() or not workspace.is_dir():
        raise ValueError("existing absolute workspace root required")
    if not isinstance(roles, list) or not roles:
        raise ValueError("at least one explicitly approved role is required")
    relative = Path(_required(doctrine_source_ref, "doctrine source"))
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("doctrine source must be relative to the workspace")
    doctrine_path = (workspace.resolve() / relative).resolve()
    if doctrine_path != workspace.resolve() and workspace.resolve() not in doctrine_path.parents:
        raise ValueError("doctrine source escapes the workspace")
    with doctrine_path.open("rb") as stream:
        doctrine_bytes = stream.read(32 * 1024 + 1)
    if not doctrine_bytes or len(doctrine_bytes) > 32 * 1024:
        raise ValueError("doctrine must contain 1 to 32768 bytes")
    doctrine_bytes.decode("utf-8")
    digest = hashlib.sha256(doctrine_bytes).hexdigest()
    doctrine = {
        "doctrine_id": _required(doctrine_id, "doctrine ID"),
        "revision_id": "doctrine:" + digest,
        "source_ref": str(relative),
        "content_digest": digest,
        "reviewed_by": _required(doctrine_reviewed_by, "doctrine reviewer"),
        "reviewed_at": _required(doctrine_reviewed_at, "doctrine review time"),
        "authority_class": "human_reviewed_policy",
    }
    role_rows = []
    desk_rows = []
    expected = {"role_id", "label", "desk_id", "desk_label", "template_revision", "approval_ref"}
    for index, role in enumerate(roles):
        if not isinstance(role, dict) or set(role) != expected:
            raise ValueError(f"role {index} requires exactly {sorted(expected)}")
        values = {key: _required(role[key], f"role {index} {key}") for key in expected}
        key = binding_key(tenant_id=tenant_id, role=values["role_id"], repo_key=repo_key)
        role_rows.append({
            "role_id": values["role_id"], "label": values["label"],
            "desk_id": values["desk_id"], "template_revision": values["template_revision"],
            "approval_ref": values["approval_ref"], "selection_basis": "human_approved",
        })
        desk_rows.append({
            "desk_id": values["desk_id"], "label": values["desk_label"],
            "role_id": values["role_id"], "binding_id": key,
            "memory_binding_id": key, "doctrine": doctrine.copy(),
        })
    document = {
        "schema_version": "workspace-context/v1",
        "project": {
            "project_id": _required(project_id, "project ID"),
            "tenant_id": _required(tenant_id, "tenant ID"),
            "repo_key": _required(repo_key, "repository key"),
            "source_revision": _required(source_revision, "source revision"),
        },
        "team": {"selection": "human_approved",
                 "approval_ref": _required(team_approval_ref, "team approval reference"),
                 "roles": role_rows},
        "desks": desk_rows,
    }
    _validate_catalog(document, target, workspace)
    return document


def _write_private_json(path: str | Path, document: dict) -> dict:
    return leaf.write_private_json_once(_private_parent(path), document, read=private_json, temp_prefix=".desk-setup-",
                                        conflict=lambda: FileExistsError("operator file already exists with different contents"))


def write_catalog(path: str | Path, document: dict, *, workspace_root: str | Path) -> dict:
    target = _private_parent(path)
    workspace = Path(workspace_root)
    if not workspace.is_absolute() or not workspace.is_dir():
        raise ValueError("existing absolute workspace root required")
    # Validate once more after preview; the doctrine may have changed.
    _validate_catalog(document, target, workspace)
    return _write_private_json(target, document)


def plan_session(*, config_path: str | Path, catalog_path: str | Path,
                 workspace_root: str | Path, state_root: str | Path,
                 provider_instance: str, provider_session_id: str,
                 desk_id: str, provider_id: str, model_id: str) -> dict:
    """Preview one exact existing host session; no ledger write occurs."""
    target = _private_parent(config_path)
    catalog_file = Path(catalog_path)
    workspace = Path(workspace_root)
    state = leaf.mark_store(state_root)
    if (not workspace.is_absolute() or not workspace.is_dir()
            or not state.is_absolute() or state.resolve() != state or not state.is_dir()
            or state.stat().st_uid != os.getuid() or leaf.shared_bits(state.stat().st_mode)):
        raise ValueError("existing absolute workspace and physical private state directory (0700) required")
    if not _HOST_ID.fullmatch(_required(provider_instance, "provider instance")):
        raise ValueError("safe provider instance required")
    if not _HOST_ID.fullmatch(_required(provider_session_id, "exact existing host session ID")):
        raise ValueError("safe exact existing host session ID required")
    private_json(catalog_file)
    catalog = load_workspace_catalog(catalog_file)
    readiness = validate_workspace_readiness(catalog, workspace_root=workspace,
                                             memory_resolver=None)
    if not readiness.ready:
        raise ValueError("catalog doctrine is unavailable or changed: " + "; ".join(readiness.reasons))
    selected = next((desk for desk in catalog.desks if desk.desk_id == desk_id), None)
    if selected is None:
        raise ValueError("select a desk in the approved catalog")
    _required(provider_id, "provider ID")
    _required(model_id, "model ID")
    config = {
        "schema_version": "ops.desk-memory.local.v1",
        "state_root": str(state), "catalog_path": str(catalog_file),
        "workspace_root": str(workspace), "provider_instance": provider_instance,
        "provider_session_id": provider_session_id,
    }
    return {"config_path": str(target), "config": config,
            "desk_id": desk_id, "binding_key": selected.binding_id,
            "provider_id": provider_id, "model_id": model_id,
            "status": "preview", "admitted": False, **_SESSION_IDENTITY_NOTE}


def write_session(path: str | Path, config: dict) -> dict:
    return _write_private_json(path, config)


def admit_exact_session(*, config_path: str | Path, expected_config: dict,
                        desk_id: str, provider_id: str, model_id: str,
                        selection_path: str | Path | None = None) -> dict:
    """Invoke the existing operator admission owner for the planned exact session."""
    if private_json(config_path) != expected_config:
        raise ValueError("session configuration is different from the exact preview")
    if selection_path is None:
        return {**admit(config_path, desk_id=desk_id, provider_id=provider_id, model_id=model_id),
                **_SESSION_IDENTITY_NOTE}
    selection = persist_host_selection(config_path, desk_id=desk_id,
                                       provider_id=provider_id, model_id=model_id,
                                       selection_path=selection_path)
    from kp_agent_tooling._impl.service.desk_memory_runtime import read_context
    return {"status": "admitted", "binding_key": selection["binding_key"],
            "context": read_context(config_path), "selection": selection,
            "dispatch": False, **_SESSION_IDENTITY_NOTE}
