from pathlib import Path

import pytest

from kp_agent_tooling._impl.service.desk_catalog_setup import (
    admit_exact_session,
    plan_catalog,
    plan_session,
    write_catalog,
    write_session,
)
from kp_agent_tooling._impl.service.desk_memory_runtime import initialize, read_context
from kp_agent_tooling._impl.service.workspace_context import load_workspace_catalog


def _inputs(tmp_path):
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    doctrine = tmp_path / "policy.md"
    doctrine.write_text("Human-reviewed policy.\n")
    return {
        "workspace_root": tmp_path,
        "catalog_path": private / "catalog.json",
        "project_id": "new-project",
        "tenant_id": "tenant-one",
        "repo_key": "new-repo",
        "source_revision": "a" * 40,
        "team_approval_ref": "approval:team:42",
        "roles": [{
            "role_id": "implementation", "label": "Implementation",
            "desk_id": "implementation-desk", "desk_label": "Implementation Desk",
            "template_revision": "role-template:v1", "approval_ref": "approval:role:42",
        }],
        "doctrine_source_ref": "policy.md",
        "doctrine_id": "policy-one",
        "doctrine_reviewed_by": "reviewer-one",
        "doctrine_reviewed_at": "2026-09-23T12:00:00Z",
    }


def test_fresh_catalog_to_exact_admission_and_context(tmp_path):
    inputs = _inputs(tmp_path)
    planned = plan_catalog(**inputs)
    assert not inputs["catalog_path"].exists()
    assert planned["desks"][0]["binding_id"].startswith("binding:")
    write_catalog(inputs["catalog_path"], planned, workspace_root=tmp_path)
    assert inputs["catalog_path"].stat().st_mode & 0o777 == 0o600
    assert write_catalog(inputs["catalog_path"], planned, workspace_root=tmp_path)["idempotent"]
    changed = {**planned, "project": {**planned["project"], "project_id": "other"}}
    with pytest.raises(FileExistsError):
        write_catalog(inputs["catalog_path"], changed, workspace_root=tmp_path)
    assert load_workspace_catalog(inputs["catalog_path"]).project.repo_key == "new-repo"

    state = tmp_path / "private" / "state"
    state.mkdir(mode=0o700)
    config_path = tmp_path / "private" / "session.json"
    session = plan_session(
        config_path=config_path, catalog_path=inputs["catalog_path"],
        workspace_root=tmp_path, state_root=state,
        provider_instance="claude-local", provider_session_id="existing-session-1",
        desk_id="implementation-desk", provider_id="anthropic", model_id="model-one",
    )
    assert not config_path.exists()
    assert session["session_identity_basis"] == "operator_asserted_session_id"
    assert session["host_existence_verification"] == "not-assessed"
    write_session(config_path, session["config"])
    initialize(config_path)
    selection_dir = state / "selections"
    selection_dir.mkdir(mode=0o700)
    result = admit_exact_session(
        config_path=config_path, expected_config=session["config"],
        desk_id="implementation-desk", provider_id="anthropic", model_id="model-one",
        selection_path=selection_dir / "existing-session-1.selection.json",
    )
    assert result["status"] == "admitted"
    assert result["session_identity_basis"] == "operator_asserted_session_id"
    assert read_context(config_path)["project"]["tenant_id"] == "tenant-one"
    assert result["selection"]["provider_session_id"] == "existing-session-1"
    repeated = admit_exact_session(
        config_path=config_path, expected_config=session["config"],
        desk_id="implementation-desk", provider_id="anthropic", model_id="model-one",
        selection_path=selection_dir / "existing-session-1.selection.json",
    )
    assert repeated["selection"]["idempotent"] is True


def test_catalog_requires_human_approval_and_existing_doctrine(tmp_path):
    inputs = _inputs(tmp_path)
    inputs["team_approval_ref"] = ""
    with pytest.raises(ValueError):
        plan_catalog(**inputs)
    inputs["team_approval_ref"] = "approval:team:42"
    (tmp_path / "policy.md").unlink()
    with pytest.raises(FileNotFoundError):
        plan_catalog(**inputs)
    assert not inputs["catalog_path"].exists()


def test_doctrine_unreadable_and_too_large_are_rejected(tmp_path, monkeypatch):
    inputs = _inputs(tmp_path)
    doctrine = tmp_path / "policy.md"
    original_open = Path.open

    def denied(path, *args, **kwargs):
        if path == doctrine:
            raise PermissionError("doctrine cannot be read")
        return original_open(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "open", denied)
        with pytest.raises(PermissionError):
            plan_catalog(**inputs)
    doctrine.write_bytes(b"x" * (32 * 1024 + 1))
    with pytest.raises(ValueError, match="32768"):
        plan_catalog(**inputs)
    assert not inputs["catalog_path"].exists()


def test_doctrine_must_resolve_inside_declared_workspace(tmp_path):
    inputs = _inputs(tmp_path)
    outside = tmp_path.parent / "outside-policy.md"
    outside.write_text("Outside policy")
    (tmp_path / "policy.md").unlink()
    (tmp_path / "policy.md").symlink_to(outside)
    with pytest.raises(ValueError, match="escapes"):
        plan_catalog(**inputs)
    assert not inputs["catalog_path"].exists()


def test_private_output_rejects_symlink_ancestor(tmp_path):
    inputs = _inputs(tmp_path)
    alias = tmp_path / "private-alias"
    alias.symlink_to(tmp_path / "private", target_is_directory=True)
    inputs["catalog_path"] = alias / "catalog.json"
    with pytest.raises(ValueError, match="private"):
        plan_catalog(**inputs)


def test_session_state_root_rejects_symlink_ancestor(tmp_path):
    inputs = _inputs(tmp_path)
    write_catalog(inputs["catalog_path"], plan_catalog(**inputs), workspace_root=tmp_path)
    state = tmp_path / "private" / "state"
    state.mkdir(mode=0o700)
    alias = tmp_path / "private-alias"
    alias.symlink_to(tmp_path / "private", target_is_directory=True)
    with pytest.raises(ValueError, match="physical|private"):
        plan_session(config_path=tmp_path / "private" / "session.json",
                     catalog_path=inputs["catalog_path"], workspace_root=tmp_path,
                     state_root=alias / "state", provider_instance="codex-local",
                     provider_session_id="existing-2", desk_id="implementation-desk",
                     provider_id="openai", model_id="model-a")


def test_session_preview_and_conflicts_do_not_admit(tmp_path):
    inputs = _inputs(tmp_path)
    catalog = plan_catalog(**inputs)
    write_catalog(inputs["catalog_path"], catalog, workspace_root=tmp_path)
    state = tmp_path / "private" / "state"
    state.mkdir(mode=0o700)
    config_path = tmp_path / "private" / "session.json"
    planned = plan_session(
        config_path=config_path, catalog_path=inputs["catalog_path"],
        workspace_root=tmp_path, state_root=state,
        provider_instance="codex-local", provider_session_id="existing-2",
        desk_id="implementation-desk", provider_id="openai", model_id="model-a",
    )
    assert not config_path.exists()
    write_session(config_path, planned["config"])
    with pytest.raises(Exception, match="unavailable|ledger"):
        read_context(config_path)
    with pytest.raises(FileExistsError):
        write_session(config_path, {**planned["config"], "provider_session_id": "other"})
    initialize(config_path)
    with pytest.raises(ValueError, match="different"):
        admit_exact_session(
            config_path=config_path,
            expected_config={**planned["config"], "provider_session_id": "other"},
            desk_id="implementation-desk", provider_id="openai", model_id="model-a",
        )
    with pytest.raises(Exception, match="no desk admission"):
        read_context(config_path)


def test_role_menu_is_discoverable_without_admission(capsys, tmp_path, monkeypatch):
    import json
    from kp_agent_tooling.catalog_cli import main
    monkeypatch.chdir(tmp_path)
    assert main(['roles']) == 0
    result=json.loads(capsys.readouterr().out)
    assert len(result['roles']) == 9
    assert len({row['role_id'] for row in result['roles']}) == 9
    assert all('approval_ref' not in row for row in result['roles'])
    assert not list(tmp_path.iterdir())
