"""Present document retrieval evidence without claiming corpus completeness."""

from __future__ import annotations


def document_retrieval_report(
    *, query: str, repo_key: str | None, top_k: int,
    results: list[dict[str, object]], failed: bool = False,
) -> dict[str, object]:
    """Build a transport-independent report; retrieval and validation live elsewhere."""
    rows = [] if failed else results
    report: dict[str, object] = {
        "schema_version": "ops.document-retrieval.v1",
        "query": query,
        "repo_keys": [repo_key] if repo_key is not None else None,
        "requested_top_k": top_k,
        "status": "error" if failed else ("ok" if rows else "no_results"),
        "returned": len(rows),
        "results": rows,
        "absence_verdict": "not-established",
        "corpus_completeness": "not-verified",
        "limitations": [
            "No results does not establish an empty corpus or an absent capability.",
            "Results cover the requested repository scope and indexed documents only.",
            "Source citations identify retrieved bytes, not the truth or currency of their claims.",
            "Ranking scores are not confidence probabilities; corpus coverage and freshness are unverified.",
        ],
    }
    if failed:
        report["error"] = {
            "code": "retrieval_failed",
            "message": "Document retrieval could not complete. Check backend availability and source access.",
        }
    return report
