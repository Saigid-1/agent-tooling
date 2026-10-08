"""Reviewed, exact Git commit-body rationale for a small first-call pilot.

The manifest is the admission boundary. Git history is never scanned to populate it.
Question aliases are reviewed routing hints; cited prose remains author-stated rationale,
not accepted doctrine, implementation proof, or observed execution.
"""

from __future__ import annotations

import hashlib
import copy
from collections import OrderedDict
from threading import RLock
import json
import os
import re
import subprocess
from datetime import datetime
from pathlib import Path

from kp_agent_tooling._impl import leaf
from kp_agent_tooling_ops._impl.tool_discovery import GitSource


_SHA = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}\Z")
_ID = re.compile(r"[a-z][a-z0-9_-]{0,79}\Z")
_PATH = re.compile(r"(?!/)(?!.*(?:^|/)\.\.?/)[^\x00-\x1f\x7f\\]+\Z")
_WORDS = re.compile(r"[a-z0-9]{3,}")
_STOP = {"the", "and", "for", "was", "were", "why", "how", "what", "with", "from", "this", "that", "into", "does"}
MAX_MANIFEST_BYTES = 65536
MAX_COMMIT_BYTES = 65536
MAX_ENTRIES = 16
MAX_COMPOUND_QUESTIONS = 8
MAX_COMPOUND_GROUPS = 4
MAX_SCOPE_OBSERVATIONS = 8
_OBSERVATION_KINDS = {"historical_intent", "author_reported_verification",
                      "reported_defect", "deployment_prerequisite", "implementation_description", "relayed_observation"}


class RationaleError(ValueError):
    """The reviewed catalog or its pinned Git source cannot be trusted."""


def _manifest_bytes(path: Path) -> bytes:
    with path.open("rb") as stream:
        raw = stream.read(MAX_MANIFEST_BYTES + 1)
    if len(raw) > MAX_MANIFEST_BYTES:
        raise RationaleError("rationale manifest exceeds 64 KiB")
    return raw


def _git(source: GitSource, *args: str) -> bytes:
    try:
        return source._git(*args)
    except Exception as exc:
        raise RationaleError("pinned rationale source unavailable") from exc


def _question(value: object) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.encode("utf-8")) > 512:
        raise RationaleError("question must be nonblank and at most 512 bytes")
    return " ".join(value.casefold().split()).rstrip("?.!")


def _path(value: object) -> str:
    if not isinstance(value, str) or not value or not _PATH.fullmatch(value) or any(
        part in {"", ".", ".."} for part in value.split("/")
    ):
        raise RationaleError("artifact reference must be a canonical repository path")
    return value


def _text(value: object, label: str, limit: int = 200) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.encode("utf-8")) > limit:
        raise RationaleError(f"{label} must be nonblank and at most {limit} bytes")
    return value


def _commit_dates(source: GitSource, commit: str) -> tuple[str, str]:
    try:
        dates = _git(source, "show", "-s", "--format=%aI%n%cI", commit).decode("ascii").splitlines()
        if len(dates) != 2:
            raise ValueError("wrong commit date count")
        for date in dates:
            parsed = datetime.fromisoformat(date)
            if parsed.tzinfo is None or parsed.utcoffset() is None:
                raise ValueError("commit date lacks a time zone")
    except (UnicodeError, ValueError) as exc:
        raise RationaleError("commit date metadata unavailable") from exc
    return dates[0], dates[1]


def _body(source: GitSource, commit: str) -> tuple[list[str], str]:
    if _git(source, "cat-file", "-t", commit).strip() != b"commit":
        raise RationaleError("rationale revision must be a commit")
    size = int(_git(source, "cat-file", "-s", commit))
    if size > MAX_COMMIT_BYTES:
        raise RationaleError("commit exceeds rationale source budget")
    raw = _git(source, "cat-file", "commit", commit)
    try:
        message = raw.split(b"\n\n", 1)[1].decode("utf-8")
    except (IndexError, UnicodeError) as exc:
        raise RationaleError("commit message must be UTF-8") from exc
    parts = message.split("\n\n", 1)
    if len(parts) != 2 or not parts[1].strip():
        raise RationaleError("commit has no body rationale")
    body_text = parts[1].rstrip("\n")
    body = body_text.split("\n")
    return body, hashlib.sha256(body_text.encode("utf-8")).hexdigest()


class RationaleCatalog:
    """A validated, immutable snapshot of explicitly reviewed commit passages."""

    def __init__(self, manifest_path: str | Path, repositories: dict[str, str | Path],
                 repository_revisions: dict[str, str] | None = None):
        try:
            raw = _manifest_bytes(Path(manifest_path))
            manifest = json.loads(raw)
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise RationaleError("rationale manifest unavailable or invalid") from exc
        self.manifest_path = Path(manifest_path)
        self.manifest_sha256 = hashlib.sha256(raw).hexdigest()
        self.repositories = dict(repositories)
        self.repository_revisions = dict(repository_revisions or {})
        if any(key not in self.repositories or not isinstance(revision, str) or not _SHA.fullmatch(revision)
               for key, revision in self.repository_revisions.items()):
            raise RationaleError("configured rationale repository revisions must be full SHAs")
        self._cache_lock = RLock()
        self._sources = OrderedDict()
        self._ancestry = OrderedDict()
        if (not isinstance(manifest, dict) or
                not {"schema_version", "entries"} <= set(manifest) or
                set(manifest) - {"schema_version", "entries", "compound_questions"} or
                manifest["schema_version"] != "ops.commit-rationale.v1"):
            raise RationaleError("unsupported rationale manifest")
        entries = manifest["entries"]
        if not isinstance(entries, list) or len(entries) > MAX_ENTRIES:
            raise RationaleError("rationale entries must be a bounded list")
        self.entries: tuple[dict, ...] = tuple(self._entry(row, repositories) for row in entries)
        ids = [row["id"] for row in self.entries]
        if len(set(ids)) != len(ids):
            raise RationaleError("duplicate rationale id")
        aliases = [(row["repo_key"], alias) for row in self.entries if row["review_state"] == "reviewed" and row["lifecycle"] == "current" for alias in row["questions"]]
        if len(set(aliases)) != len(aliases):
            raise RationaleError("ambiguous reviewed question")
        self._validate_contests()
        self.compound_questions = self._compound_questions(manifest.get("compound_questions", []))

    @staticmethod
    def _gaps(value: object) -> list[str]:
        if not isinstance(value, list) or len(value) > 6:
            raise RationaleError("evidence gaps must be a bounded list")
        gaps = [_text(item, "unestablished claim", 200) for item in value]
        if len(set(gaps)) != len(gaps):
            raise RationaleError("duplicate unestablished claim")
        return gaps

    @staticmethod
    def _evidence_scope(value: object, body: list[str], citation: list[int]) -> dict | None:
        if value is None:
            return None
        if (not isinstance(value, dict) or set(value) != {"observations", "not_established"} or
                not isinstance(value["observations"], list) or
                not 1 <= len(value["observations"]) <= MAX_SCOPE_OBSERVATIONS):
            raise RationaleError("invalid evidence scope")
        observations = []
        for item in value["observations"]:
            if (not isinstance(item, dict) or not {"kind", "body_lines"} <= set(item) or set(item) - {"kind", "body_lines", "attribution"} or
                    not isinstance(item["kind"], str) or
                    item["kind"] not in _OBSERVATION_KINDS):
                raise RationaleError("invalid evidence observation")
            span = item["body_lines"]
            if (not isinstance(span, list) or len(span) != 2 or
                    any(type(n) is not int for n in span) or
                    not citation[0] <= span[0] <= span[1] <= citation[1]):
                raise RationaleError("evidence observation must cite selected body lines")
            observed = "\n".join(body[span[0] - 1:span[1]])
            if not observed.strip() or len(observed.encode("utf-8")) > 4096:
                raise RationaleError("evidence observation excerpt unavailable")
            attribution = item.get("attribution")
            if item["kind"] == "relayed_observation" and (not isinstance(attribution, dict) or
                    set(attribution) != {"reported_by", "attributed_to"}):
                raise RationaleError("relayed observation requires reporter and attributed speaker")
            if attribution is not None:
                if not isinstance(attribution, dict) or set(attribution) != {"reported_by", "attributed_to"}:
                    raise RationaleError("invalid observation attribution")
                attribution = {key: _text(value, key) for key, value in attribution.items()}
            observations.append({"kind": item["kind"], "body_lines": span,
                                 "attribution": attribution,
                                 "direct_observation": False,
                                 "excerpt": observed,
                                 "excerpt_sha256": hashlib.sha256(observed.encode("utf-8")).hexdigest(),
                                 "proof_scope": "relayed_testimony_only" if item["kind"] == "relayed_observation" else "author_stated_only"})
        if len({(item["kind"], tuple(item["body_lines"])) for item in observations}) != len(observations):
            raise RationaleError("duplicate evidence observation")
        return {"coverage_basis": "reviewer_annotation", "observations": observations,
                "not_established": RationaleCatalog._gaps(value["not_established"])}

    @staticmethod
    def _entry(row: object, repositories: dict[str, str | Path]) -> dict:
        required = {"id", "repo_key", "commit", "body_lines", "body_sha256", "owner", "reviewer", "review_state", "lifecycle", "artifact_refs", "questions"}
        allowed = required | {"superseded_by", "current_artifact_refs", "detached_history_review", "evidence_scope", "review_provenance", "question_gaps", "contested_by"}
        if not isinstance(row, dict) or not required <= row.keys() or row.keys() - allowed:
            raise RationaleError("invalid rationale entry fields")
        if not isinstance(row["id"], str) or not _ID.fullmatch(row["id"]):
            raise RationaleError("invalid rationale id")
        key = _text(row["repo_key"], "repository key", 80)
        if key not in repositories:
            raise RationaleError("rationale repository is not configured")
        commit = row["commit"]
        if not isinstance(commit, str) or not _SHA.fullmatch(commit):
            raise RationaleError("full commit SHA required")
        lines = row["body_lines"]
        if not isinstance(lines, list) or len(lines) != 2 or any(type(n) is not int for n in lines) or not 1 <= lines[0] <= lines[1] <= 500:
            raise RationaleError("bounded one-based body line range required")
        if not isinstance(row["body_sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", row["body_sha256"]):
            raise RationaleError("full body digest required")
        _text(row["owner"], "source owner")
        _text(row["reviewer"], "reviewer")
        if row["review_state"] not in {"reviewed", "unreviewed"} or row["lifecycle"] not in {"current", "historical", "superseded", "withdrawn"}:
            raise RationaleError("invalid review or lifecycle state")
        if (row["lifecycle"] == "superseded") != ("superseded_by" in row):
            raise RationaleError("supersession requires an explicit full replacement SHA")
        if "superseded_by" in row and (not isinstance(row["superseded_by"], str) or not _SHA.fullmatch(row["superseded_by"])):
            raise RationaleError("supersession requires an explicit full replacement SHA")
        refs = row["artifact_refs"]
        if not isinstance(refs, list) or not 1 <= len(refs) <= 6 or len(set(map(str, refs))) != len(refs):
            raise RationaleError("one to six unique artifact references required")
        refs = [_path(ref) for ref in refs]
        questions = row["questions"]
        if not isinstance(questions, list) or not 1 <= len(questions) <= 6:
            raise RationaleError("reviewed question aliases required")
        questions = [_question(q) for q in questions]
        if len(set(questions)) != len(questions):
            raise RationaleError("duplicate question alias")
        try:
            source = GitSource(Path(repositories[key]), commit)
        except Exception as exc:
            raise RationaleError("pinned rationale commit unavailable") from exc
        if source.revision != commit:
            raise RationaleError("rationale commit identity mismatch")
        body, digest = _body(source, commit)
        if digest != row["body_sha256"] or lines[1] > len(body):
            raise RationaleError("rationale body changed or citation range unavailable")
        excerpt = "\n".join(body[lines[0] - 1:lines[1]])
        if len(excerpt.encode("utf-8")) > 8192:
            raise RationaleError("rationale passage exceeds 8 KiB")
        if not excerpt.strip():
            raise RationaleError("rationale citation is empty")
        for ref in refs:
            item = source.entry(ref)
            if item is None or item[0] not in {"100644", "100755"}:
                raise RationaleError("referenced artifact unavailable at commit")
        current_refs = row.get("current_artifact_refs", [])
        if not isinstance(current_refs, list) or len(current_refs) > 4:
            raise RationaleError("current artifact references must be a bounded list")
        resolved_refs = []
        for ref in current_refs:
            if (not isinstance(ref, dict) or not {"repo_key", "revision", "path"} <= set(ref)
                    or set(ref) - {"repo_key", "revision", "path", "start_line", "end_line"}):
                raise RationaleError("invalid current artifact reference")
            if ("start_line" in ref) != ("end_line" in ref):
                raise RationaleError("current artifact line range requires both endpoints")
            target_key = _text(ref["repo_key"], "current artifact repository key", 80)
            target_revision = ref["revision"]
            if target_key not in repositories or not isinstance(target_revision, str) or not _SHA.fullmatch(target_revision):
                raise RationaleError("current artifact requires a configured repository and full revision")
            target_path = _path(ref["path"])
            try:
                target = GitSource(Path(repositories[target_key]), target_revision)
                if target.revision != target_revision:
                    raise RationaleError("current artifact revision identity mismatch")
                item = target.entry(target_path)
            except Exception as exc:
                raise RationaleError("pinned current artifact unavailable") from exc
            if item is None or item[0] not in {"100644", "100755"}:
                raise RationaleError("current artifact must be a regular file at its pinned revision")
            resolved_refs.append({"repo_key": target_key, "revision": target_revision,
                                  "path": target_path, "blob_sha": item[1],
                                  "relationship": "reviewer_declared",
                                  "semantic_equivalence": "not-established",
                                  "runtime_execution": "not-assessed"})
            if "start_line" in ref:
                start, end = ref["start_line"], ref["end_line"]
                if (type(start) is not int or type(end) is not int or
                        not 1 <= start <= end or end - start + 1 > 80):
                    raise RationaleError("current artifact line range must be 1..80 lines")
                try:
                    content = target.read_bounded(item[1], 256 * 1024).decode("utf-8")
                except Exception as exc:
                    raise RationaleError("current artifact excerpt unavailable") from exc
                source_lines = content.split("\n")
                if source_lines and source_lines[-1] == "":
                    source_lines.pop()
                if end > len(source_lines):
                    raise RationaleError("current artifact line range unavailable")
                artifact_excerpt = "\n".join(source_lines[start - 1:end])
                if len(artifact_excerpt.encode("utf-8")) > 4096:
                    raise RationaleError("current artifact excerpt exceeds 4 KiB")
                resolved_refs[-1].update(start_line=start, end_line=end,
                                         excerpt=artifact_excerpt,
                                         excerpt_sha256=hashlib.sha256(artifact_excerpt.encode("utf-8")).hexdigest())
        if len({(ref["repo_key"], ref["revision"], ref["path"],
                 ref.get("start_line"), ref.get("end_line")) for ref in resolved_refs}) != len(resolved_refs):
            raise RationaleError("duplicate current artifact reference")
        detached_review = row.get("detached_history_review")
        if detached_review is not None:
            if (not isinstance(detached_review, dict) or
                    set(detached_review) != {"target_revision", "reason"} or
                    row["review_state"] != "reviewed" or row["lifecycle"] != "current" or
                    not resolved_refs):
                raise RationaleError("detached history requires reviewed current artifacts")
            detached_target = detached_review["target_revision"]
            if not isinstance(detached_target, str) or not _SHA.fullmatch(detached_target):
                raise RationaleError("detached history requires a full exact target revision")
            detached_review = {"target_revision": detached_target,
                               "reason": _text(detached_review["reason"], "detached history reason", 400)}
        author_date, committer_date = _commit_dates(source, commit)
        if "evidence_scope" in row and row["evidence_scope"] is None:
            raise RationaleError("invalid evidence scope")
        evidence_scope = RationaleCatalog._evidence_scope(row.get("evidence_scope"), body, lines)
        review = row.get("review_provenance")
        if review is not None:
            if (not isinstance(review, dict) or set(review) != {"kind", "reviewer_id", "source"}
                    or review["kind"] not in ("agent", "human")):
                raise RationaleError("explicit reviewer kind, identity and source required")
            review = {"kind": review["kind"], "reviewer_id": _text(review["reviewer_id"], "reviewer id"),
                      "source": _text(review["source"], "review source", 400), "identity_authenticated": False}
        gaps = row.get("question_gaps", {})
        if not isinstance(gaps, dict) or len(gaps) > 6:
            raise RationaleError("bounded question-specific gaps required")
        gaps = {_question(q): RationaleCatalog._gaps(value) for q, value in gaps.items()}
        if not set(gaps) <= set(questions):
            raise RationaleError("question gaps require a reviewed entry alias")
        contested = row.get("contested_by", [])
        if not isinstance(contested, list) or len(contested) > 4 or any(
                not isinstance(item, dict) or not {"entry_id", "body_lines"} <= set(item) or set(item) - {"entry_id", "body_lines", "claim_body_lines"} or
                not isinstance(item["entry_id"], str) for item in contested):
            raise RationaleError("bounded counter-statement references required")
        author = _git(source, "show", "-s", "--format=%an%x00%ae", commit).decode("utf-8").strip().split("\x00")
        actor_lines = [line for line in body if re.match(r"(?:Actor-Type|Actor-Id|Attribution-Source|Session-Id|Co-Authored-By):", line)]
        authorship = {"git_author_name": author[0], "git_author_email": author[1],
                      "actor_declarations": actor_lines[:12], "identity_authenticated": False}
        return {"id": row["id"], "repo_key": key, "commit": commit,
                "author_date": author_date, "committer_date": committer_date,
                "body_lines": lines, "body_sha256": digest, "excerpt": excerpt,
                "excerpt_sha256": hashlib.sha256(excerpt.encode("utf-8")).hexdigest(),
                "owner": row["owner"], "reviewer": row["reviewer"],
                "review_state": row["review_state"], "lifecycle": row["lifecycle"],
                "superseded_by": row.get("superseded_by"), "artifact_refs": refs,
                "current_artifact_refs": resolved_refs,
                "detached_history_review": detached_review,
                "evidence_scope": evidence_scope, "review_provenance": review,
                "question_gaps": gaps, "contested_by": contested, "source_authorship": authorship,
                "questions": questions}

    def _validate_contests(self):
        rows = {row["id"]: row for row in self.entries}
        for row in self.entries:
            for ref in row["contested_by"]:
                other = rows.get(ref["entry_id"])
                if other is None or other is row or other["review_state"] != "reviewed" or other["lifecycle"] != "current":
                    raise RationaleError("counter-statement requires another admitted reviewed entry")
                claim_span = ref.get("claim_body_lines", row["body_lines"])
                if (not isinstance(claim_span, list) or len(claim_span) != 2 or any(type(n) is not int for n in claim_span)
                        or not row["body_lines"][0] <= claim_span[0] <= claim_span[1] <= row["body_lines"][1]):
                    raise RationaleError("disputed claim must cite selected body lines")
                span = ref["body_lines"]
                if (not isinstance(span, list) or len(span) != 2 or any(type(n) is not int for n in span)
                        or not other["body_lines"][0] <= span[0] <= span[1] <= other["body_lines"][1]):
                    raise RationaleError("counter-statement must cite selected body lines")
                text = "\n".join(other["excerpt"].split("\n")[span[0]-other["body_lines"][0]:span[1]-other["body_lines"][0]+1])
                if not text.strip() or len(text.encode()) > 2048:
                    raise RationaleError("counter-statement exceeds bounded testimony scope")

    def _compound_questions(self, raw: object) -> tuple[dict, ...]:
        if not isinstance(raw, list) or len(raw) > MAX_COMPOUND_QUESTIONS:
            raise RationaleError("compound questions must be a bounded list")
        rows = {row["id"]: row for row in self.entries}
        routes = []
        seen = set()
        for route in raw:
            if (not isinstance(route, dict) or
                    set(route) != {"repo_key", "target_revision", "questions", "groups", "not_established"}):
                raise RationaleError("invalid reviewed compound question")
            anchor_key = _text(route["repo_key"], "compound repository key", 80)
            anchor_target = route["target_revision"]
            if anchor_key not in self.repositories or not isinstance(anchor_target, str) or not _SHA.fullmatch(anchor_target):
                raise RationaleError("compound question requires a configured pinned anchor")
            self._pinned_source(anchor_key, anchor_target)
            questions = route["questions"]
            if not isinstance(questions, list) or not 1 <= len(questions) <= 4:
                raise RationaleError("compound question aliases must be bounded")
            aliases = [_question(item) for item in questions]
            for alias in aliases:
                identity = (anchor_key, anchor_target, alias)
                if identity in seen:
                    raise RationaleError("ambiguous reviewed compound question")
                anchor_source = self._pinned_source(anchor_key, anchor_target)
                if any(row["repo_key"] == anchor_key and row["review_state"] == "reviewed" and
                       row["lifecycle"] == "current" and alias in row["questions"] and
                       self._lineage(anchor_source, row, anchor_target)
                       for row in self.entries):
                    raise RationaleError("compound question collides with reviewed entry alias")
                seen.add(identity)
            groups = route["groups"]
            if not isinstance(groups, list) or not 2 <= len(groups) <= MAX_COMPOUND_GROUPS:
                raise RationaleError("compound question requires two to four groups")
            validated = []
            seen_groups = set()
            seen_entries = set()
            for group in groups:
                if (not isinstance(group, dict) or
                        set(group) != {"id", "repo_key", "target_revision", "entry_ids"}):
                    raise RationaleError("invalid compound evidence group")
                group_id = group["id"]
                if not isinstance(group_id, str) or not _ID.fullmatch(group_id) or group_id in seen_groups:
                    raise RationaleError("duplicate or invalid compound group id")
                seen_groups.add(group_id)
                group_key = _text(group["repo_key"], "compound group repository key", 80)
                group_target = group["target_revision"]
                if group_key not in self.repositories or not isinstance(group_target, str) or not _SHA.fullmatch(group_target):
                    raise RationaleError("compound group requires a configured pinned target")
                source = self._pinned_source(group_key, group_target)
                entry_ids = group["entry_ids"]
                if not isinstance(entry_ids, list) or not 1 <= len(entry_ids) <= 3:
                    raise RationaleError("compound group entry ids must be bounded")
                for entry_id in entry_ids:
                    if not isinstance(entry_id, str) or entry_id not in rows or entry_id in seen_entries:
                        raise RationaleError("compound group has unknown or duplicate entry id")
                    seen_entries.add(entry_id)
                    row = rows[entry_id]
                    if (row["repo_key"] != group_key or row["review_state"] != "reviewed" or
                            row["lifecycle"] != "current" or
                            not self._lineage(source, row, group_target)):
                        raise RationaleError("compound group entry is not admitted at pinned target")
                    if (group_key != anchor_key or group_target != anchor_target) and not any(
                            ref["repo_key"] == anchor_key and ref["revision"] == anchor_target
                            for ref in row["current_artifact_refs"]):
                        raise RationaleError("cross-repository compound entry lacks pinned anchor link")
                validated.append({"id": group_id, "repo_key": group_key,
                                  "target_revision": group_target, "entry_ids": entry_ids})
            if len(seen_entries) > 5:
                raise RationaleError("compound question exceeds five cited entries")
            routes.append({"repo_key": anchor_key, "target_revision": anchor_target,
                           "questions": aliases, "groups": validated,
                           "not_established": self._gaps(route["not_established"])})
        return tuple(routes)

    def _pinned_source(self, repo_key: str, target_revision: str) -> GitSource:
        key = (repo_key, target_revision)
        if key in self._sources:
            self._sources.move_to_end(key)
            return self._sources[key]
        try:
            source = GitSource(Path(self.repositories[repo_key]), target_revision)
        except Exception as exc:
            raise RationaleError("compound target revision unavailable") from exc
        if source.revision != target_revision:
            raise RationaleError("compound target revision identity mismatch")
        self._sources[key] = source
        if len(self._sources) > 64:
            self._sources.popitem(last=False)
        return source

    def assert_current(self) -> None:
        """Fail a response assembled across an operator manifest change."""
        try:
            if hashlib.sha256(_manifest_bytes(self.manifest_path)).hexdigest() != self.manifest_sha256:
                raise RationaleError("rationale manifest changed during response")
        except OSError as exc:
            raise RationaleError("rationale manifest unavailable during response") from exc

    def search(self, repo_key: str, query: str, target_revision: str, *, top_k: int = 3, response_mode: str = "full") -> dict:
        with self._cache_lock:
            return self._search(repo_key, query, target_revision, top_k=top_k, response_mode=response_mode)

    def _search(self, repo_key: str, query: str, target_revision: str, *, top_k: int = 3, response_mode: str = "full") -> dict:
        if response_mode not in ("full", "compact"):
            raise RationaleError("response_mode must be full or compact")
        if type(top_k) is not int or not 1 <= top_k <= 5:
            raise RationaleError("top_k must be 1..5")
        if repo_key not in self.repositories:
            raise RationaleError("rationale repository is not configured")
        if not isinstance(target_revision, str) or not _SHA.fullmatch(target_revision):
            raise RationaleError("full target revision required")
        source = self._pinned_source(repo_key, target_revision)
        normalized = _question(query)
        for route in self.compound_questions:
            if (route["repo_key"] == repo_key and route["target_revision"] == target_revision and
                    normalized in route["questions"]):
                result = self._compound_result(route, target_revision, top_k)
                self.assert_current()
                return self._response(result, normalized, response_mode, repo_key, target_revision, top_k)
        terms = set(_WORDS.findall(normalized)) - _STOP
        admitted = []
        for row in self.entries:
            if row["repo_key"] != repo_key or row["review_state"] != "reviewed" or row["lifecycle"] != "current":
                continue
            # A lexical miss cannot be rescued by ancestry; do no Git work for it.
            matched = terms & (set(_WORDS.findall(row["excerpt"].casefold())) | set(_WORDS.findall(" ".join(row["questions"]))))
            if normalized not in row["questions"] and len(matched) < 2:
                continue
            lineage = self._lineage(source, row, target_revision)
            if lineage:
                admitted.append((row, lineage))
        exact = [(row, lineage) for row, lineage in admitted if normalized in row["questions"]]
        if exact:
            row, lineage = exact[0]
            result = {"status": "evidence", "evidence_kind": "author_stated_rationale",
                    "target_revision": target_revision, "manifest_sha256": self.manifest_sha256,
                    "answerability": "source_passage_only", "results": [self._result(row, lineage)],
                    "omitted": 0, "fallback": None}
            self.assert_current()
            return self._response(result, normalized, response_mode, repo_key, target_revision, top_k)
        terms = set(_WORDS.findall(normalized)) - _STOP
        candidates = []
        for row, lineage in admitted:
            matched = terms & (set(_WORDS.findall(row["excerpt"].casefold())) | set(_WORDS.findall(" ".join(row["questions"]))))
            if len(matched) >= 2:
                candidates.append((len(matched), row, lineage))
        candidates.sort(key=lambda item: (-item[0], item[1]["id"]))
        selected = [self._result(row, lineage) for _, row, lineage in candidates[:top_k]]
        result = {"status": "unverified" if selected else "missing",
                "target_revision": target_revision, "manifest_sha256": self.manifest_sha256,
                "evidence_kind": "candidate_only" if selected else None,
                "answerability": "unverified", "results": selected,
                "omitted": max(0, len(candidates) - top_k),
                "fallback": {"method": "bounded_literal_git_search", "repo_key": repo_key,
                             "query": query.strip(), "max_commits": 50,
                             "note": "Search commit messages literally, then review full SHA and body before using a finding."}}
        self.assert_current()
        return self._response(result, normalized, response_mode, repo_key, target_revision, top_k)

    def _compound_result(self, route: dict, target_revision: str, top_k: int) -> dict:
        """Return reviewed groups, allocating at least one place per group when possible.

        This is a routing aid, never a verdict that all clauses have been proven.
        """
        by_id = {row["id"]: row for row in self.entries}
        groups = []
        selected = []
        remaining = top_k
        for group in route["groups"]:
            groups.append({"id": group["id"], "repo_key": group["repo_key"],
                           "target_revision": group["target_revision"], "result_ids": [],
                           "omitted_entry_ids": list(group["entry_ids"])})
        while remaining and any(group["omitted_entry_ids"] for group in groups):
            for group in groups:
                if not remaining:
                    break
                if not group["omitted_entry_ids"]:
                    continue
                entry_id = group["omitted_entry_ids"].pop(0)
                row = by_id[entry_id]
                source = self._pinned_source(group["repo_key"], group["target_revision"])
                lineage = self._lineage(source, row, group["target_revision"])
                if lineage is None:
                    raise RationaleError("compound group admission changed during response")
                item = self._result(row, lineage)
                group["result_ids"].append(entry_id)
                selected.append(item)
                remaining -= 1
        omitted = sum(len(group["omitted_entry_ids"]) for group in groups)
        return {"status": "unverified", "target_revision": target_revision,
                "manifest_sha256": self.manifest_sha256,
                "evidence_kind": "reviewed_source_passages",
                "answerability": "claim_scope_unverified", "coverage_basis": "reviewed_compound_route",
                "results": selected, "groups": groups, "omitted": omitted,
                "not_established": route["not_established"],
                "fallback": ({"method": "read_omitted_reviewed_groups",
                              "note": "Increase top_k to include omitted entry IDs before assessing every group."}
                             if omitted else None)}

    def _lineage(self, source: GitSource, row: dict, target_revision: str) -> str | None:
        if self._ancestor(source, row["commit"], target_revision):
            return "ancestor"
        if (row["detached_history_review"] is not None and
                row["detached_history_review"]["target_revision"] == target_revision):
            return "reviewer_declared_detached_history"
        return None

    def _ancestor(self, source: GitSource, commit: str, target_revision: str) -> bool:
        key = (str(source.repository), commit, target_revision)
        if key in self._ancestry:
            self._ancestry.move_to_end(key)
            return self._ancestry[key]
        env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        env.update(GIT_TERMINAL_PROMPT="0", GIT_OPTIONAL_LOCKS="0", GIT_NO_REPLACE_OBJECTS="1")
        try:
            result = subprocess.run(["git", "-C", str(source.repository), "merge-base", "--is-ancestor", commit, target_revision],
                                    env=env, capture_output=True, timeout=10, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RationaleError("rationale ancestry unavailable") from exc
        if result.returncode not in {0, 1}:
            raise RationaleError("rationale ancestry unavailable")
        self._ancestry[key] = result.returncode == 0
        if len(self._ancestry) > 256:
            self._ancestry.popitem(last=False)
        return result.returncode == 0

    @staticmethod
    def _result(row: dict, lineage: str) -> dict:
        result = {key: row[key] for key in ("id", "repo_key", "commit", "author_date", "committer_date", "body_lines", "body_sha256", "excerpt", "excerpt_sha256", "artifact_refs", "current_artifact_refs", "owner", "reviewer", "review_state", "lifecycle", "superseded_by", "source_authorship", "review_provenance")}
        result.update(source_lineage=lineage, applicability="not-established")
        result["evidence_scope"] = row["evidence_scope"] or {"coverage_basis": "unannotated",
                                                         "observations": [], "not_established": []}
        if lineage == "reviewer_declared_detached_history":
            result["detached_history_reason"] = row["detached_history_review"]["reason"]
        return result

    def _response(self, result, query, mode, repo_key, target_revision, top_k):
        # Never mutate the cached catalog or previously delivered evidence.
        result = copy.deepcopy(result)
        result["retrieval_status"] = result["status"]
        result["question_coverage"] = {
            "status": "unverified",
            "gap": "subquestion_coverage_not_established",
            "basis": "reviewed_route" if result.get("groups") else "retrieved_passages",
            "next_action": "Check every part of the question against the cited passages; submit unresolved parts separately. A matched passage does not prove the whole question was answered.",
        }
        rows = {row["id"]: row for row in self.entries}
        group_targets = {entry_id: (group["repo_key"], group["target_revision"])
                         for group in result.get("groups", [])
                         for entry_id in group["result_ids"] + group["omitted_entry_ids"]}
        disputed = False
        for item in result["results"]:
            row = rows[item["id"]]
            review = row["review_provenance"]
            item["review_label"] = (review["kind"] + "-reviewed") if review else "reviewer-unestablished"
            item["catalog_review_state"] = item["review_state"]
            item["evidence_scope"]["entry_not_established"] = item["evidence_scope"]["not_established"]
            item["evidence_scope"]["not_established"] = row["question_gaps"].get(query)
            item["evidence_scope"]["gap_scope"] = "exact_reviewed_question" if query in row["question_gaps"] else "not_assessed_for_this_question"
            counters = []
            for ref in row["contested_by"]:
                other = rows[ref["entry_id"]]; span = ref["body_lines"]
                if other["id"] in group_targets:
                    counter_key, counter_target = group_targets[other["id"]]
                    if counter_key != other["repo_key"]:
                        raise RationaleError("counter-statement group repository mismatch")
                elif other["repo_key"] == repo_key:
                    counter_target = target_revision
                else:
                    counter_target = self.repository_revisions.get(other["repo_key"])
                    if counter_target is None:
                        raise RationaleError("counter-statement lacks a pinned counter repository target")
                counter_source = self._pinned_source(other["repo_key"], counter_target)
                counter_lineage = self._lineage(counter_source, other, counter_target)
                if not counter_lineage:
                    raise RationaleError("counter-statement is not admitted at pinned target")
                text = "\n".join(other["excerpt"].split("\n")[span[0]-other["body_lines"][0]:span[1]-other["body_lines"][0]+1])
                counters.append({"disputed_claim_body_lines": ref.get("claim_body_lines", row["body_lines"]), "counter_statement": text, "source": {
                    "entry_id": other["id"], "repo_key": other["repo_key"], "commit": other["commit"],
                    "target_revision": counter_target, "source_lineage": counter_lineage,
                    "body_lines": span, "body_sha256": other["body_sha256"],
                    "excerpt_sha256": hashlib.sha256(text.encode()).hexdigest()},
                    "source_authorship": other["source_authorship"],
                    "review_provenance": other["review_provenance"],
                    "attribution": [{"kind": obs["kind"], "attribution": obs.get("attribution")} for obs in (other["evidence_scope"] or {}).get("observations", [])
                                    if obs["body_lines"][0] <= span[1] and obs["body_lines"][1] >= span[0]],
                    "resolution": "unresolved", "runtime_execution": "not-assessed"})
            item["contested_by"] = counters
            item["claim_status"] = "disputed" if counters else "not_adjudicated"
            disputed |= bool(counters)
        if disputed:
            result["claim_status"] = result["dispute_status"] = "disputed"
            if result["retrieval_status"] == "evidence":
                result.update(status="disputed", answerability="conflicting_testimony", evidence_kind="disputed_testimony")
        else:
            result["claim_status"] = result["dispute_status"] = "not_adjudicated"
        result["response_mode"] = mode
        if mode == "compact":
            def compact(value):
                if isinstance(value, dict):
                    return {k: compact(v) for k, v in value.items() if k != "excerpt"}
                if isinstance(value, list): return [compact(v) for v in value]
                return value
            result = compact(result)
            result["expand_call"] = {"name": "knowledge.rationale", "arguments": {
                "repo_key": repo_key, "target_revision": target_revision, "query": query,
                "top_k": top_k, "response_mode": "full"}}
            result["omitted_content"] = "source excerpts omitted; hashes, ranges, attribution and counter-statements retained"
        self.assert_current()
        return result

    def snapshot(self) -> dict:
        """Stable identity map for an operator-owned incremental refresh checkpoint."""
        snapshot = {row["id"]: leaf.sorted_sha256(row)
                    for row in self.entries if row["review_state"] == "reviewed" and row["lifecycle"] == "current"}
        for route in self.compound_questions:
            route_key = "compound:" + leaf.sorted_sha256(
                [route["repo_key"], route["target_revision"], route["questions"]])
            snapshot[route_key] = leaf.sorted_sha256(route)
        return snapshot

    def refresh_plan(self, previous: dict[str, str]) -> dict:
        if not isinstance(previous, dict) or any(not isinstance(k, str) or not isinstance(v, str) or not re.fullmatch(r"[0-9a-f]{64}", v) for k, v in previous.items()):
            raise RationaleError("invalid prior rationale snapshot")
        current = self.snapshot()
        return {"upsert": sorted(k for k, v in current.items() if previous.get(k) != v),
                "remove": sorted(previous.keys() - current.keys()),
                "unchanged": sorted(k for k, v in current.items() if previous.get(k) == v),
                "snapshot": current}
