"""Read a closed operation-to-handler registry from an exact Git commit."""

from __future__ import annotations

import ast
import hashlib
import subprocess
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class CommittedRegistryView:
    bindings: Mapping[str, str | None]
    fingerprint: str
    registry_path: str
    blob_sha: str
    namespaces: tuple[str, ...] = ()

    def __contains__(self, operation: object) -> bool:
        return operation in self.bindings

    def __iter__(self) -> Iterator[str]:
        return iter(self.bindings)


def read_committed_registry(root: Path, revision: str, path: str,
                            *, variable: str = "CODE_REFERENCE_OPERATION_BINDINGS") -> CommittedRegistryView:
    """Parse a literal binding map without importing or executing repository code."""
    if not path or path.startswith("/") or ".." in path.split("/"):
        raise ValueError("registry path must be repository-relative")
    spec = f"{revision}:{path}"
    blob = subprocess.run(["git", "rev-parse", spec], cwd=root, check=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout.decode().strip()
    raw = subprocess.run(["git", "cat-file", "blob", blob], cwd=root, check=True,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout
    try:
        tree = ast.parse(raw.decode("utf-8"), filename=path)
    except (UnicodeDecodeError, SyntaxError) as exc:
        raise ValueError("committed registry is not valid UTF-8 Python") from exc
    value = None
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(isinstance(target, ast.Name) and target.id == variable for target in targets):
                value = node.value
    if value is None:
        raise ValueError("committed operation registry declaration unavailable")
    try:
        decoded = ast.literal_eval(value)
    except (ValueError, TypeError, RecursionError) as exc:
        raise ValueError("operation registry must be a literal mapping") from exc
    if not isinstance(decoded, dict) or not decoded:
        raise ValueError("operation registry must be a nonempty mapping")
    bindings = {}
    for operation, handler in decoded.items():
        if (not isinstance(operation, str) or not operation or (handler is not None and
                (not isinstance(handler, str) or not handler or handler.startswith(".") or "." not in handler))):
            raise ValueError("operation registry entries must name operations and qualified handlers")
        bindings[operation] = handler
    fingerprint = "registry:sha256:" + hashlib.sha256(
        (blob + "\0" + variable).encode("utf-8")).hexdigest()
    return CommittedRegistryView(bindings, fingerprint, path, blob)


def read_committed_operation_declarations(
        root: Path, revision: str, path: str,
        *, variable: str = "KNOWLEDGE_OPERATION_DECLARATIONS") -> CommittedRegistryView:
    """Derive operation bindings from the literal declarations used by dispatch."""
    declaration = _read_committed_literal(root, revision, path, variable)
    if not isinstance(declaration, (tuple, list)) or not declaration:
        raise ValueError("operation declarations must be a nonempty sequence")
    bindings: dict[str, str | None] = {}
    for row in declaration:
        if not isinstance(row, dict) or set(row) != {
                "operation", "description", "handler", "dispatch_method", "binding"}:
            raise ValueError("operation declaration fields are invalid")
        operation, description = row["operation"], row["description"]
        handler, dispatch_method, binding = row["handler"], row["dispatch_method"], row["binding"]
        public_name = "knowledge." + operation if isinstance(operation, str) else ""
        if (not isinstance(operation, str) or not operation or public_name in bindings
                or not isinstance(description, str) or not description
                or not isinstance(dispatch_method, str) or not dispatch_method
                or binding not in {"nameable", "dynamic"}
                or (binding == "nameable" and (not isinstance(handler, str)
                    or handler.startswith(".") or "." not in handler))
                or (binding == "dynamic" and handler is not None)):
            raise ValueError("operation declaration values are invalid")
        bindings[public_name] = handler
    blob = _committed_blob(root, revision, path)
    fingerprint = "registry:sha256:" + hashlib.sha256(
        (blob + "\0" + variable).encode("utf-8")).hexdigest()
    return CommittedRegistryView(bindings, fingerprint, path, blob, ("knowledge",))


def load_committed_operation_declarations(
        root: Path, revision: str, path: str,
        *, variable: str = "KNOWLEDGE_OPERATION_DECLARATIONS") -> CommittedRegistryView | None:
    """Return no view when the exact revision has no readable registry blob.

    Invalid declaration content still fails closed; absence stays distinguishable
    from an authoritative declaration with an explicitly unnameable handler.
    """
    try:
        return read_committed_operation_declarations(
            root, revision, path, variable=variable)
    except subprocess.CalledProcessError:
        return None


def _committed_blob(root: Path, revision: str, path: str) -> str:
    if not path or path.startswith("/") or ".." in path.split("/"):
        raise ValueError("registry path must be repository-relative")
    return subprocess.run(["git", "rev-parse", f"{revision}:{path}"], cwd=root, check=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout.decode().strip()


def _read_committed_literal(root: Path, revision: str, path: str, variable: str) -> object:
    blob = _committed_blob(root, revision, path)
    raw = subprocess.run(["git", "cat-file", "blob", blob], cwd=root, check=True,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout
    try:
        tree = ast.parse(raw.decode("utf-8"), filename=path)
    except (UnicodeDecodeError, SyntaxError) as exc:
        raise ValueError("committed registry is not valid UTF-8 Python") from exc
    value = None
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(isinstance(target, ast.Name) and target.id == variable for target in targets):
                value = node.value
    if value is None:
        raise ValueError("committed operation registry declaration unavailable")
    try:
        return ast.literal_eval(value)
    except (ValueError, TypeError, RecursionError) as exc:
        raise ValueError("operation registry must be literal data") from exc
