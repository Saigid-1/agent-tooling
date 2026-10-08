"""Operator-only delegated-session attribution. Preview unless --apply is set.

Run with ``python -m kp_agent_tooling_ops.delegation_cli`` from the installed
tooling environment. This command records historical claims, never admission.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys

from kp_agent_tooling_ops._impl.service.delegation_attribution import (
    apply_codex_delegation, plan_codex_delegation,
)
from kp_agent_tooling._impl.service.desk_memory_runtime import components


def _main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('config', 'parent-file', 'child-file', 'agent-path', 'role-id',
                 'asserted-by', 'assignment-ref', 'recorded-at'):
        parser.add_argument('--' + name, required=True)
    parser.add_argument('--supersedes-role-claim')
    parser.add_argument('--child-source-range-json', help='explicit child native file/byte start for role and parent claims')
    parser.add_argument('--parent-source-range-json', help='explicit parent native file/byte start for contributor claim')
    parser.add_argument('--historical-parent', action='store_true',
                        help='explicitly attribute a parent other than the configured operator session')
    parser.add_argument('--apply', action='store_true', help='append catalog claims after preview verification')
    args = parser.parse_args(argv)
    config, registry, ledger, store = components(args.config)
    roles = {binding.role for binding in registry.list_bindings()}
    values = dict(parent_file=args.parent_file, child_file=args.child_file,
                  agent_path=args.agent_path, role_id=args.role_id,
                  asserted_by=args.asserted_by, assignment_ref=args.assignment_ref,
                  recorded_at=args.recorded_at, approved_role_ids=roles,
                  child_source_range=json.loads(args.child_source_range_json)
                  if args.child_source_range_json else None,
                  parent_source_range=json.loads(args.parent_source_range_json)
                  if args.parent_source_range_json else None)
    if args.supersedes_role_claim and not args.apply:
        parser.error('--supersedes-role-claim requires --apply')
    preview = plan_codex_delegation(**values)
    matches_operator = preview['parent_native_id'] == config['provider_session_id']
    if not matches_operator and not args.historical_parent:
        raise ValueError('selected native parent differs from configured operator session; use --historical-parent for explicit historical attribution')
    if args.apply:
        result = apply_codex_delegation(
            store=store,
            tenant_id=ledger.resolve(config['provider_session_id'], registry).tenant_id,
            supersedes_role_claim=args.supersedes_role_claim, **values,
        )
    else:
        result = preview
    result['applied'] = args.apply
    result['parent_identity_verification_basis'] = (
        'native_header_and_configured_operator_session' if matches_operator else
        'native_header_and_explicit_historical_parent_selection'
    )
    print(json.dumps(result, sort_keys=True))
    return result


def main(argv=None):
    try:
        _main(argv)
        return 0
    except (OSError, ValueError, sqlite3.Error, RuntimeError) as error:
        print(f'delegation attribution: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
