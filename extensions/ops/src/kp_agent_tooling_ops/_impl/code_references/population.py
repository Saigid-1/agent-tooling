"""Committed-source target population for the reference index (never query time)."""
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from .source_facts import DEFAULT_CACHE, committed_declarations, corroborate_scip


@dataclass(frozen=True)
class SymbolCoverage:
    repo_key: str
    revision: str
    fingerprint: str
    complete: bool
    supported_languages: frozenset[str]
    errors: tuple[dict, ...]
    files: int
    declarations: int
    nodes_created: int
    exclusions: tuple[str, ...] = ('non-Python declarations', 'assignments and dynamic declarations')
    corroboration: dict | None = None


def populate_python_targets(*, graph, root: Path, repo_key: str, revision: str, telemetry=None, source_facts=None,
                            fact_cache=DEFAULT_CACHE) -> SymbolCoverage:
    from .artifact_identity import change_lineage_key, change_occurrence_id
    from kp_agent_tooling_ops._impl.service.knowledge_telemetry import telemetry_stage
    from .models import canonical_digest

    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("explicit full commit required")
    if not isinstance(repo_key, str) or not repo_key or "\0" in repo_key:
        raise ValueError("canonical repository key required")
    with telemetry_stage(telemetry, "code_references.symbol_index", stage="compute"):
        facts = committed_declarations(root=Path(root), repo_key=repo_key, revision=revision,
                                       git=_git, cache=fact_cache)
        corroboration = corroborate_scip(facts, source_facts) if source_facts is not None else None
        created = declarations = 0
        for path, blob in facts.entries:
            change_id = change_occurrence_id(repo_key, revision, path, blob)
            attrs = {"change_key": change_lineage_key(repo_key, path),
                "change_revision_id": change_id, "repo_key": repo_key,
                "file_path": path, "commit_sha": revision, "blob_sha": blob}
            created += _ensure_node(graph, "Change", change_id, attrs)
        for path, blob, rows in facts.documents:
            change_id = change_occurrence_id(repo_key, revision, path, blob)
            for row in rows:
                sid = canonical_digest("code-symbol", [change_id, "python", row.name, row.start, row.length])
                attrs = {"symbol_revision_id": sid, "source_change_id": change_id,
                    "symbol_name": row.name, "symbol_language": "python", "byte_offset": row.start,
                    "byte_length": row.length}
                created += _ensure_node(graph, "CodeSymbol", sid, attrs)
                declarations += 1
        errors = tuple({"path_digest":p,"reason":r} for p,r in facts.errors)
        return SymbolCoverage(repo_key, revision, facts.fingerprint, not errors,
                              frozenset({"python"}), errors, len(facts.entries), declarations, created,
                              corroboration=corroboration)


def _git(root, *args):
    return subprocess.run(["git", "--no-optional-locks", "-C", str(root), *args],
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                          check=True, timeout=60).stdout


def _ensure_node(graph, kind, identity, attrs):
    current = graph.entities.get(identity)
    if current is not None:
        if current["entity_type"] != kind or any(
                current["attributes"].get(k) != v for k, v in attrs.items()):
            raise ValueError("existing target identity conflict")
        return 0
    graph.entities.create(kind, attrs, entity_id=identity)
    return 1
