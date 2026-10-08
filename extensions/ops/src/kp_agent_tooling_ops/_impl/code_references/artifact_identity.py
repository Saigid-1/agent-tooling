"""Original Git artifact identities; no connector runtime dependency."""
from kp_agent_tooling._impl import leaf

def _identity_text(name: str, value: str) -> str:
    if not isinstance(value, str) or not value or "\0" in value:
        raise ValueError(f"{name} must be a non-empty NUL-free string")
    return value

def change_lineage_key(repo_key: str, file_path: str) -> str:
    """Return the deterministic repository/path identity for one file lineage."""

    repository = _identity_text("repo_key", repo_key)
    path = _identity_text("file_path", file_path)
    digest = leaf.sha256_hex(f"{repository}\0{path}".encode("utf-8"))
    return f"change-key:sha256:{digest}"

def change_occurrence_id(
    repo_key: str,
    commit_sha: str,
    file_path: str,
    blob_sha: str | None,
) -> str:
    """Return a Change id bound to its exact repository occurrence coordinates."""

    repository = _identity_text("repo_key", repo_key)
    commit = _identity_text("commit_sha", commit_sha)
    path = _identity_text("file_path", file_path)
    if blob_sha is None:
        # Preserve every existing content-occurrence id while using an encoding
        # no NUL-free blob coordinate can collide with.
        encoded = f"{repository}\0{commit}\0{path}\0\0deleted".encode("utf-8")
    else:
        blob = _identity_text("blob_sha", blob_sha)
        encoded = f"{repository}\0{commit}\0{path}\0{blob}".encode("utf-8")
    digest = leaf.sha256_hex(encoded)
    return f"change:sha256:{digest}"
