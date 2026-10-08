# OPS extension (`kp-agent-tooling-ops`)

The OPS-specific tools, moved out of the core by [AT-0004](../../docs/adr/AT-0004-portable-kanban-suite.md) T1. The legacy source repository is being retired in favour of this repository (completed by the AT-0004 T7 cutover); this extension keeps what existing deployments still need during the transition. The core (`packages/tooling`, `kp-agent-tooling`) never imports it.

- Distribution `kp-agent-tooling-ops`, import package `kp_agent_tooling_ops`, sources under `extensions/ops/src/`.
- It depends on `kp-agent-tooling` at the same version (0.4.0).

## What it adds

**Navigation tools.** The navigation server (`kp-agent-tooling … serve`) discovers tool providers in the `kp_agent_tooling.tools` entry-point group. This distribution registers `ops = kp_agent_tooling_ops._impl.service.ops_tools:OpsToolProvider`, which serves:
- `lifecycle.evidence` and `knowledge.rationale`;
- `verification.guide`, `verification.packet`, `verification.finding`, `verification.plan`, `verification.handoff`, `verification.observations` and `verification.review`;
- the portable in-process `knowledge.*` document stack (`portable_knowledge_config`, see [PORTABLE-KNOWLEDGE.md](../../docs/PORTABLE-KNOWLEDGE.md));
- the optional legacy gateway catalog (`gateway_config`/`gateway_endpoint`).

A navigation config with an `enabled_tools` list serves only the tools it names. Every default list (the installer and `kp-agent-setup`) contains core tools only, so an operator adds these tools explicitly.

**Console scripts:**
- `kp-agent-observations`: operator-controlled immutable observation imports and reads.
- `kp-agent-capture`: sanitized raw call artifacts and a compact manifest from JSONL exports.
- `kp-agent-transcript`: private sanitized evidence for explicitly selected host transcript calls.
- `kp-agent-claude-capture`: operator-scoped Claude capture hooks (see [MEMORY.md](../../docs/MEMORY.md)).
- `kp-agent-knowledge-publish`: offline publication of maintained Git documents and reference evidence.

**Module commands** (`python -m kp_agent_tooling_ops.<module>`):
- `charter_import_cli`: import existing desk charters ([DESK-CHARTER-IMPORT.md](../../docs/DESK-CHARTER-IMPORT.md)).
- `delegation_cli`: delegated-session attribution ([DELEGATION-ATTRIBUTION.md](../../docs/memory/DELEGATION-ATTRIBUTION.md)).
- `desk_import_cli`: the former `kp-agent-desk import-history` and `import-sessions` actions.
- `handoff_cli`: the former `kp-agent-tooling … handoff` action.

**Host scripts** in [`extensions/ops/scripts/`](scripts). They run from a checkout, not from the installed package:
- `launch_claude_desk_memory.py`, `desk_session_hook.py`, `claude_capture_host.py` and `memory_tools.json`: the legacy Claude desktop launcher, the exact-session hook and the capture bridge. They use only the standard library; see [SESSION-HOOK-CONTINUITY.md](../../docs/SESSION-HOOK-CONTINUITY.md) and [CLAUDE-CAPTURE-HOST.md](../../docs/CLAUDE-CAPTURE-HOST.md).
- `reconcile_desk_history.py`: the isolated desk-history reconciliation rehearsal ([MEMORY-RECONCILIATION.md](../../docs/MEMORY-RECONCILIATION.md)).
- `prepare_tooling_docker.py`: a one-time migration of a configured local deployment to physical container paths.

**Assets:** `finding.schema.json`, `journey-registry.schema.json` and `verify-behavior.md`, under `src/kp_agent_tooling_ops/assets/`.

## Install and test

From the repository root, install the core first, then the extension:

```sh
pip install -e './packages/tooling[test]'
pip install -e extensions/ops
python -m pytest extensions/ops/tests
```

The extension suite has its own `pytest.ini` and also reads shared fixtures from the core suite's `tests/`. The core suite (`python -m pytest tests`) never needs the extension.

## Images

AT-0004 T6a plans an `ops` image target: the `product` image plus this extension, for existing deployments during the transition. It is not in this tree yet; see [DOCKER.md](../../docs/DOCKER.md#images).
