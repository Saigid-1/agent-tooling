"""Prepare a human-reviewed desk catalog and select one exact host session.

Planning is the default. Only --apply writes a configuration file, and only
--admit calls the operator admission ledger for an existing host session.
"""

from __future__ import annotations

import argparse
import json

from kp_agent_tooling._impl.service.desk_catalog_setup import (
    admit_exact_session, plan_catalog, plan_session, write_catalog, write_session,
)
from kp_agent_tooling._impl.service.desk_binding import DeskLaunchUnavailable


def _main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("roles", help="list eligible role templates; grants no session access")
    catalog = commands.add_parser("catalog", help="calculate and validate a reviewed catalog")
    for name in ("workspace-root", "catalog-path", "project-id", "tenant-id",
                 "repo-key", "source-revision", "team-approval-ref",
                 "doctrine-source-ref", "doctrine-id", "doctrine-reviewed-by",
                 "doctrine-reviewed-at"):
        catalog.add_argument("--" + name, required=True)
    catalog.add_argument("--role", action="append", required=True,
                         help='repeat exact JSON: {"role_id","label","desk_id","desk_label","template_revision","approval_ref"}')
    catalog.add_argument("--apply", action="store_true", help="write a private catalog after validation")

    session = commands.add_parser("session", help="prepare or admit one existing host session")
    for name in ("config-path", "catalog-path", "workspace-root", "state-root",
                 "provider-instance", "provider-session-id", "desk-id", "provider-id", "model-id"):
        session.add_argument("--" + name, required=True)
    action = session.add_mutually_exclusive_group()
    action.add_argument("--apply", action="store_true", help="write the exact private session config only")
    action.add_argument("--admit", action="store_true", help="admit this existing session from its written config")
    session.add_argument("--selection-output", help="exact-session recovery file; valid only with --admit")
    args = parser.parse_args(argv)
    if args.command == "roles":
        from importlib.resources import files
        result = json.loads(files("kp_agent_tooling").joinpath("assets/desk_roles.json").read_text())
        print(json.dumps(result, sort_keys=True))
        return result
    if args.command == "catalog":
        try:
            roles = [json.loads(raw) for raw in args.role]
        except json.JSONDecodeError as error:
            parser.error(f"--role requires JSON: {error}")
        document = plan_catalog(
            workspace_root=args.workspace_root, catalog_path=args.catalog_path,
            project_id=args.project_id, tenant_id=args.tenant_id,
            repo_key=args.repo_key, source_revision=args.source_revision,
            team_approval_ref=args.team_approval_ref, roles=roles,
            doctrine_source_ref=args.doctrine_source_ref, doctrine_id=args.doctrine_id,
            doctrine_reviewed_by=args.doctrine_reviewed_by,
            doctrine_reviewed_at=args.doctrine_reviewed_at,
        )
        result = {"status": "preview", "catalog_path": args.catalog_path,
                  "catalog": document, "written": False}
        if args.apply:
            written = write_catalog(args.catalog_path, document, workspace_root=args.workspace_root)
            result.update(status="written", written=True, idempotent=written["idempotent"])
    else:
        if args.selection_output and not args.admit:
            parser.error("--selection-output requires --admit")
        result = plan_session(
            config_path=args.config_path, catalog_path=args.catalog_path,
            workspace_root=args.workspace_root, state_root=args.state_root,
            provider_instance=args.provider_instance,
            provider_session_id=args.provider_session_id, desk_id=args.desk_id,
            provider_id=args.provider_id, model_id=args.model_id,
        )
        if args.apply:
            written = write_session(args.config_path, result["config"])
            result.update(status="written", idempotent=written["idempotent"])
        elif args.admit:
            admission = admit_exact_session(
                config_path=args.config_path, expected_config=result["config"],
                desk_id=args.desk_id, provider_id=args.provider_id,
                model_id=args.model_id, selection_path=args.selection_output,
            )
            result = {"status": "admitted", "config_path": args.config_path,
                      "provider_session_id": args.provider_session_id,
                      "session_identity_basis": admission["session_identity_basis"],
                      "host_existence_verification": admission["host_existence_verification"],
                      "binding_key": admission["binding_key"],
                      "selection": admission.get("selection"), "context": admission["context"]}
    print(json.dumps(result, sort_keys=True))
    return result


def main(argv=None):
    try:
        _main(argv)
        return 0
    except (OSError, ValueError, UnicodeError, DeskLaunchUnavailable) as error:
        raise SystemExit(f"desk setup: {error}") from error


if __name__ == "__main__":
    raise SystemExit(main())
