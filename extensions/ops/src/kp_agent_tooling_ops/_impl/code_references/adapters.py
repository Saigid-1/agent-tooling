"""Bounded committed-source adapters used by document reference retrieval.

These adapters establish Git identities only.  They do not resolve symbols or
read document text, and they never substitute a different revision.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import subprocess
import time
from typing import Mapping


SHA = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")


class RevisionInputError(ValueError):
    """A caller supplied an invalid repository/revision selection."""


@dataclass(frozen=True)
class RevisionSelection:
    repo_key: str
    revision: str | None
    selection: str
    reason: str | None = None

    def as_dict(self) -> dict:
        value = {"repo_key": self.repo_key, "revision": self.revision,
                 "selection": self.selection}
        if self.reason is not None:
            value["reason"] = self.reason
        return value


@dataclass(frozen=True)
class BaselineSelection:
    revision: str
    available: bool
    reason: str | None = None


def _git(root: Path, *arguments: str, timeout: float = .25) -> str:
    result = subprocess.run(
        ["git", "--no-optional-locks", "-c", "core.fsmonitor=false", *arguments],
        cwd=root, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
        timeout=timeout, check=True,
    )
    return result.stdout.strip()


class GitRevisionAdapter:
    """Resolve configured refs once and verify same-repository ancestry."""

    def __init__(self, repositories: Mapping[str, Mapping[str, object]]):
        self._repositories = repositories
        self._deadline = time.monotonic() + 1.0

    def _timeout(self) -> float:
        return max(.001, min(.25, self._deadline - time.monotonic()))

    def select(self, source_repo_key: str, explicit: object | None) -> dict[str, RevisionSelection]:
        if explicit is None:
            requested = {}
        elif not isinstance(explicit, Mapping):
            raise RevisionInputError("code_revisions must be an object")
        else:
            requested = dict(explicit)
        if len(requested) > 8:
            raise RevisionInputError("code_revisions accepts at most 8 repositories")
        if source_repo_key not in self._repositories:
            raise RevisionInputError("unknown source repository")
        keys = {source_repo_key, *requested}
        if len(keys) > 8:
            raise RevisionInputError("at most 8 relevant repositories are permitted")
        unknown = keys - self._repositories.keys()
        if unknown:
            raise RevisionInputError("unknown code revision repository")
        selected: dict[str, RevisionSelection] = {}
        for key in sorted(keys):
            repo = self._repositories[key]
            root = Path(str(repo["path"]))
            if key in requested:
                revision = requested[key]
                if not isinstance(revision, str) or SHA.fullmatch(revision) is None:
                    raise RevisionInputError("code revisions require full lowercase commit IDs")
                try:
                    resolved = _git(root, "rev-parse", revision + "^{commit}",
                                    timeout=self._timeout())
                except (OSError, subprocess.SubprocessError):
                    selected[key] = RevisionSelection(key, revision, "unavailable",
                                                      "commit_not_available")
                else:
                    selected[key] = (RevisionSelection(key, revision, "explicit")
                                     if resolved == revision else
                                     RevisionSelection(key, revision, "unavailable",
                                                       "commit_identity_mismatch"))
                continue
            try:
                revision = _git(root, "rev-parse", str(repo["ref"]) + "^{commit}",
                                timeout=self._timeout())
            except (OSError, subprocess.SubprocessError):
                selected[key] = RevisionSelection(key, None, "unavailable",
                                                  "configured_revision_unavailable")
            else:
                selected[key] = (RevisionSelection(key, revision, "defaulted")
                                 if SHA.fullmatch(revision) else
                                 RevisionSelection(key, None, "unavailable",
                                                   "configured_revision_invalid"))
        return selected

    def validate_baselines(self, selected: Mapping[str, RevisionSelection],
                           baselines: object | None) -> dict[str, BaselineSelection]:
        if baselines is None:
            return {}
        if not isinstance(baselines, Mapping) or len(baselines) > 8:
            raise RevisionInputError("reference_baseline_revisions must be an object of at most 8 entries")
        result: dict[str, BaselineSelection] = {}
        for key, baseline in baselines.items():
            if key not in selected or key not in self._repositories:
                raise RevisionInputError("baseline repository requires a selected code revision")
            if not isinstance(baseline, str) or SHA.fullmatch(baseline) is None:
                raise RevisionInputError("baseline revisions require full lowercase commit IDs")
            target = selected[key]
            if target.revision is None or target.selection == "unavailable":
                raise RevisionInputError("baseline requires an available requested revision")
            root = Path(str(self._repositories[key]["path"]))
            try:
                if _git(root, "rev-parse", baseline + "^{commit}",
                        timeout=self._timeout()) != baseline:
                    raise RevisionInputError("baseline must identify its exact commit")
                completed = subprocess.run(
                    ["git", "--no-optional-locks", "merge-base", "--is-ancestor",
                     baseline, target.revision], cwd=root, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, timeout=self._timeout(),
                )
                if completed.returncode == 1:
                    raise RevisionInputError("baseline must be an ancestor of the requested revision")
                if completed.returncode != 0:
                    raise subprocess.CalledProcessError(completed.returncode, completed.args)
            except RevisionInputError:
                raise
            except (OSError, subprocess.TimeoutExpired):
                result[key] = BaselineSelection(baseline, False,
                                                "ancestry_check_unavailable")
                continue
            except subprocess.CalledProcessError:
                result[key] = BaselineSelection(baseline, False,
                                                "ancestry_check_unavailable")
                continue
            result[key] = BaselineSelection(baseline, True)
        return result


class UnavailableNavigationReadiness:
    def continuation(self, **_: object) -> None:
        return None


class FrozenNavigationReadiness:
    """Bind continuations to an operator-captured snapshot and blob inventory."""

    def __init__(self, *, snapshot_id: str, revisions: Mapping[str, str],
                 blobs: Mapping[str, Mapping[str, str]],
                 sizes: Mapping[str, Mapping[str, int]] | None = None):
        if not isinstance(snapshot_id, str) or not snapshot_id.startswith(
                "navigation-snapshot:sha256:"):
            raise ValueError("immutable navigation snapshot required")
        self.snapshot_id = snapshot_id
        self._revisions = dict(revisions)
        self._blobs = {key: dict(value) for key, value in blobs.items()}
        self._sizes = {key: dict(value) for key, value in (sizes or {}).items()}

    @classmethod
    def from_registry(cls, *, config: Mapping[str, object], registry: Path,
                      snapshot_id: str) -> "FrozenNavigationReadiness":
        """Verify a real published snapshot and precompute its blob inventory.

        Inventory work happens once at composition. Query-time readiness is then
        metadata-only and cannot silently follow a moving profile.
        """
        from kp_agent_tooling._impl.navigation_snapshot import select
        from kp_agent_tooling._impl.repository_manifest import build_manifest
        derived, selection = select(dict(config), registry, snapshot_id)
        revisions = {key: row["revision"] for key, row in selection["repos"].items()}
        blobs, sizes = {}, {}
        for key, revision in revisions.items():
            manifest = build_manifest(derived, key, revision)
            if manifest.get("status") != "ok" or manifest.get("complete") is not True:
                raise ValueError("snapshot repository inventory unavailable")
            blobs[key] = {row["path"]: row["blob_sha"] for row in manifest["entries"]
                          if row["mode"] in {"100644", "100755"}}
            sizes[key] = {row["path"]: row["size"] for row in manifest["entries"]
                          if row["mode"] in {"100644", "100755"}}
        return cls(snapshot_id=snapshot_id, revisions=revisions, blobs=blobs, sizes=sizes)

    def active_revisions(self) -> Mapping[str, str]:
        return dict(self._revisions)

    def tree_blobs(self, repo_key: str, revision: str) -> Mapping[str, str] | None:
        if self._revisions.get(repo_key) != revision:
            return None
        return dict(self._blobs.get(repo_key, {}))

    def file_size(self, repo_key: str, revision: str, path: str) -> int | None:
        if self._revisions.get(repo_key) != revision:
            return None
        return self._sizes.get(repo_key, {}).get(path)

    def continuation(self, *, repo_key: str, revision: str, path: str,
                     blob_sha: str, start_line: int, line_count: int) -> dict | None:
        if self._revisions.get(repo_key) != revision:
            return None
        if self._blobs.get(repo_key, {}).get(path) != blob_sha:
            return None
        return {"operation": "navigation.source", "arguments": {
            "repo_key": repo_key, "target_revision": revision, "path": path,
            "start_line": start_line, "line_count": line_count,
            "snapshot_id": self.snapshot_id,
        }}


class RetainedNavigationReadiness(FrozenNavigationReadiness):
    """Route exact revisions to verified snapshots retained in one registry.

    The active snapshot remains the only source of default revisions. Retained
    snapshots are used solely after a caller requests their exact revision.
    Every route is assembled at composition time from a verified selection and
    a complete committed-file manifest; request-time work is dictionary lookup.
    """

    def __init__(self, *, active: FrozenNavigationReadiness,
                 snapshots: Mapping[tuple[str, str], FrozenNavigationReadiness]):
        self.snapshot_id = active.snapshot_id
        self._active = active
        self._snapshots = dict(snapshots)

    @classmethod
    def from_registry(cls, *, config: Mapping[str, object], registry: Path,
                      active_snapshot_id: str, maximum_snapshots: int = 256
                      ) -> "RetainedNavigationReadiness":
        root = Path(registry)
        selections = sorted(root.glob("selection-*.json"))
        if len(selections) > maximum_snapshots:
            raise ValueError("retained navigation snapshot budget exceeded")
        active = FrozenNavigationReadiness.from_registry(
            config=config, registry=root, snapshot_id=active_snapshot_id)
        routes: dict[tuple[str, str], FrozenNavigationReadiness] = {}
        for selection_path in selections:
            digest = selection_path.name.removeprefix("selection-").removesuffix(".json")
            if re.fullmatch(r"[0-9a-f]{64}", digest) is None:
                continue
            snapshot_id = "navigation-snapshot:sha256:" + digest
            try:
                snapshot = (active if snapshot_id == active_snapshot_id else
                    FrozenNavigationReadiness.from_registry(
                        config=config, registry=root, snapshot_id=snapshot_id))
            except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
                continue
            for repo_key, revision in snapshot.active_revisions().items():
                key = (repo_key, revision)
                prior = routes.get(key)
                if prior is None or snapshot.snapshot_id < prior.snapshot_id:
                    routes[key] = snapshot
        for repo_key, revision in active.active_revisions().items():
            routes[(repo_key, revision)] = active
        return cls(active=active, snapshots=routes)

    def active_revisions(self) -> Mapping[str, str]:
        return self._active.active_revisions()

    def _snapshot(self, repo_key: str, revision: str) -> FrozenNavigationReadiness | None:
        return self._snapshots.get((repo_key, revision))

    def tree_blobs(self, repo_key: str, revision: str) -> Mapping[str, str] | None:
        snapshot = self._snapshot(repo_key, revision)
        return None if snapshot is None else snapshot.tree_blobs(repo_key, revision)

    def file_size(self, repo_key: str, revision: str, path: str) -> int | None:
        snapshot = self._snapshot(repo_key, revision)
        return None if snapshot is None else snapshot.file_size(repo_key, revision, path)

    def continuation(self, *, repo_key: str, revision: str, path: str,
                     blob_sha: str, start_line: int, line_count: int) -> dict | None:
        snapshot = self._snapshot(repo_key, revision)
        if snapshot is None:
            return None
        return snapshot.continuation(repo_key=repo_key, revision=revision, path=path,
            blob_sha=blob_sha, start_line=start_line, line_count=line_count)
