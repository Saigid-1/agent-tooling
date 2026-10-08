"""Exact-revision resolvers for committed files and indexed declarations."""

from __future__ import annotations

import subprocess
import re
from collections.abc import Mapping, Sequence
from pathlib import Path

from .models import ReferenceCandidate, Resolution, ResolutionTarget, canonical_digest

RESOLUTION_POLICY_VERSION = "doc-code-reference-resolution.v1"
SUPPORTED_LANGUAGES = frozenset({"python", "javascript", "jsx", "typescript", "tsx"})


class ReferenceResolver:
    def __init__(self, *, repositories: Mapping[str, Path], graph: object,
                 symbol_index_fingerprint: str, symbol_index_complete: bool,
                 registry_bindings: Mapping[str, str] | None = None,
                 registry_fingerprint: str | None = None,
                 coverage_provider: object | None = None) -> None:
        self._repositories = {key: Path(value) for key, value in repositories.items()}
        self._graph = graph
        self._symbol_fingerprint = symbol_index_fingerprint
        self._symbol_complete = symbol_index_complete
        self._registry_bindings = dict(registry_bindings) if registry_bindings is not None else None
        self._registry_fingerprint = registry_fingerprint
        self._coverage_provider = coverage_provider
        self._blob_cache: dict[tuple[str, str], bytes] = {}

    def resolve(self, candidate: ReferenceCandidate, *, source_repo_key: str,
                target_repo_key: str, code_revision: str,
                policy_version: str = RESOLUTION_POLICY_VERSION,
                registry_view: object | None = None) -> Resolution:
        if policy_version != RESOLUTION_POLICY_VERSION:
            raise ValueError("unsupported resolution policy")
        if candidate.kind == "path":
            return self._path(candidate, target_repo_key, code_revision)
        if candidate.kind == "operation":
            bindings = getattr(registry_view, "bindings", self._registry_bindings)
            registry_fingerprint = getattr(registry_view, "fingerprint", self._registry_fingerprint)
            if bindings is None or registry_fingerprint is None:
                return Resolution("error", None, "registry_unavailable", "partial", None,
                                  registry_fingerprint or "registry:unavailable")
            handler = bindings.get(candidate.lookup_value)
            if handler is None:
                return Resolution("complete", "unresolved", "handler_not_nameable", "complete",
                                  "registry", registry_fingerprint,
                                  absence_verdict="not-established")
            candidate = ReferenceCandidate("symbol", candidate.ref_byte_offset,
                candidate.ref_byte_length, candidate.literal_digest, candidate.literal,
                handler, candidate.line_hint, candidate.target_repo_key)
            resolved = self._symbol(candidate, target_repo_key, code_revision)
            return Resolution(resolved.execution_state, resolved.status, resolved.reason,
                resolved.coverage, "registry" if resolved.status == "resolved" else resolved.resolver,
                canonical_digest("registry-resolution", [registry_fingerprint,
                    resolved.dependency_fingerprint]), resolved.targets, resolved.match_count,
                resolved.truncated, resolved.absence_verdict)
        if candidate.kind == "symbol":
            return self._symbol(candidate, target_repo_key, code_revision)
        raise ValueError("unsupported reference kind")

    def _path(self, candidate: ReferenceCandidate, repo_key: str, revision: str) -> Resolution:
        fingerprint = canonical_digest("tree-input", [repo_key, revision])
        if candidate.line_hint is not None and (candidate.line_hint[0] < 1
                or candidate.line_hint[1] < candidate.line_hint[0]):
            return Resolution("complete", "unresolved", "invalid_line_hint", "complete", "tree",
                              fingerprint, absence_verdict="not-established")
        unsafe = _unsafe_path(candidate.lookup_value)
        if unsafe:
            return Resolution("complete", "unresolved", unsafe, "complete", "tree",
                              fingerprint, absence_verdict="not-established")
        root = self._repositories.get(repo_key)
        if root is None or not _sha(revision):
            return Resolution("error", None, "target_revision_unavailable", "partial", None, fingerprint)
        proc = subprocess.run(["git", "ls-tree", "-z", revision, "--", candidate.lookup_value], cwd=root,
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False, timeout=60)
        if proc.returncode:
            return Resolution("error", None, "target_revision_unavailable", "partial", None, fingerprint)
        if not proc.stdout:
            return Resolution("complete", "unresolved", "no_exact_path", "complete", "tree",
                              fingerprint, absence_verdict="not-established")
        entry = proc.stdout.removesuffix(b"\0")
        fields = entry.decode("utf-8").split(None, 3)
        if (len(fields) != 4 or fields[3] != candidate.lookup_value
                or fields[0] not in {"100644", "100755"} or fields[1] != "blob"):
            return Resolution("complete", "unresolved", "unsupported_git_object", "complete", "tree",
                              fingerprint, absence_verdict="not-established")
        blob_sha = fields[2]
        raw = subprocess.run(["git", "cat-file", "blob", blob_sha], cwd=root, check=True,
                             stdout=subprocess.PIPE, timeout=60).stdout
        if candidate.line_hint:
            start, end = candidate.line_hint
            lines = raw.count(b"\n") + (bool(raw) and not raw.endswith(b"\n"))
            if start < 1 or end < start or end > lines:
                return Resolution("complete", "unresolved", "invalid_line_hint", "complete", "tree",
                                  fingerprint, absence_verdict="not-established")
        nodes = self._graph.entities.find("Change", {"repo_key": repo_key,
            "commit_sha": revision, "file_path": candidate.lookup_value, "blob_sha": blob_sha})
        if len(nodes) != 1:
            return Resolution("error", None, "artifact_identity_unavailable", "partial", "tree", fingerprint)
        target = ResolutionTarget(str(nodes[0]["id"]), repo_key, candidate.lookup_value,
                                  blob_sha, 0, len(raw), None,
                                  candidate.line_hint[0] if candidate.line_hint else (1 if raw else 0),
                                  candidate.line_hint[1] if candidate.line_hint else
                                  (min(200, _line_count(raw)) if raw else 0))
        return Resolution("complete", "resolved", None, "complete", "tree", fingerprint,
                          (target,), 1)

    def _symbol(self, candidate: ReferenceCandidate, repo_key: str, revision: str) -> Resolution:
        coverage = self._coverage_provider(repo_key, revision) if callable(self._coverage_provider) else None
        fingerprint = getattr(coverage, "fingerprint", self._symbol_fingerprint)
        complete = getattr(coverage, "complete", self._symbol_complete)
        supported_languages = frozenset(getattr(coverage, "supported_languages", SUPPORTED_LANGUAGES))
        if not complete:
            return Resolution("error", None, "incomplete_symbol_index", "partial", None, fingerprint)
        exact, weaker = [], []
        for node in self._graph.entities.find("CodeSymbol", {}):
            attrs = node["attributes"]
            if attrs.get("symbol_language") not in supported_languages:
                continue
            change = self._graph.entities.get(attrs.get("source_change_id"))
            if change is None or change.get("entity_type") != "Change":
                continue
            ca = change["attributes"]
            if ca.get("repo_key") != repo_key or ca.get("commit_sha") != revision or ca.get("deleted"):
                continue
            name = attrs["symbol_name"]
            requested = candidate.lookup_value
            is_dotted = "." in requested
            if (is_dotted and name == requested) or (not is_dotted and name.rsplit(".", 1)[-1] == requested):
                exact.append(self._symbol_target(node, attrs, ca, repo_key))
            elif (name.casefold() == requested.casefold()
                  or name.casefold().endswith("." + requested.casefold())
                  or name.rsplit(".", 1)[-1].casefold() == requested.rsplit(".", 1)[-1].casefold()):
                weaker.append(self._symbol_target(node, attrs, ca, repo_key))
        exact.sort(key=lambda t: (t.repo_key, t.path, t.byte_offset, t.node_id))
        weaker.sort(key=lambda t: (t.repo_key, t.path, t.byte_offset, t.node_id))
        if len(exact) == 1:
            return Resolution("complete", "resolved", None, "complete", "python_ast", fingerprint,
                              tuple(exact), 1)
        if exact:
            return Resolution("complete", "ambiguous", None, "complete", "python_ast", fingerprint,
                              tuple(exact[:20]), len(exact), len(exact) > 20)
        if weaker:
            return Resolution("complete", "candidate", "weaker_name_match", "complete", "python_ast",
                              fingerprint, tuple(weaker[:20]), len(weaker), len(weaker) > 20,
                              "not-established")
        return Resolution("complete", "unresolved", "no_exact_declaration", "complete", "python_ast",
                          fingerprint, absence_verdict="not-established")

    def _symbol_target(self, node: Mapping[str, object], attrs: Mapping[str, object],
                       change: Mapping[str, object], repo_key: str) -> ResolutionTarget:
        root = self._repositories.get(repo_key)
        if root is None:
            raise ValueError("symbol repository unavailable")
        blob_sha = str(change["blob_sha"])
        key = (repo_key, blob_sha)
        raw = self._blob_cache.get(key)
        if raw is None:
            raw = subprocess.run(["git", "cat-file", "blob", blob_sha], cwd=root,
                                 check=True, stdout=subprocess.PIPE, timeout=60).stdout
            self._blob_cache[key] = raw
        offset, length = int(attrs["byte_offset"]), int(attrs["byte_length"])
        start_line, end_line = _byte_lines(raw, offset, length)
        return ResolutionTarget(str(node["id"]), repo_key, str(change["file_path"]), blob_sha,
            offset, length, str(attrs["symbol_name"]), start_line, end_line)


def _sha(value: str) -> bool:
    return len(value) == 40 and all(char in "0123456789abcdef" for char in value)


def _unsafe_path(value: str) -> str | None:
    if not value or re.match(r"^[A-Za-z]:", value) or value.startswith(("/", "\\")) or "\\" in value or "\0" in value:
        return "unsafe_path"
    if "://" in value or "%" in value or any(part == ".." for part in value.split("/")):
        return "unsafe_path"
    return None


def _line_count(raw: bytes) -> int:
    return raw.count(b"\n") + int(bool(raw) and not raw.endswith(b"\n"))


def _byte_lines(raw: bytes, offset: int, length: int) -> tuple[int, int]:
    if offset < 0 or length <= 0 or offset + length > len(raw):
        raise ValueError("symbol coordinates outside committed blob")
    start = raw[:offset].count(b"\n") + 1
    end = raw[:offset + length - 1].count(b"\n") + 1
    return start, end
