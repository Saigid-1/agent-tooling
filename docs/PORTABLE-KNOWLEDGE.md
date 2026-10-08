# Portable knowledge composition

The portable knowledge provider and its publisher ship with the OPS extension
(`extensions/ops`, distribution `kp-agent-tooling-ops`). The navigation server
loads it through the `kp_agent_tooling.tools` entry-point group. With the core
alone, the only `knowledge.*` tools are the local `knowledge.platform` and
`knowledge.symbol`; the document tools below need the extension.

`portable_knowledge_config` in the tooling configuration selects an in-process
knowledge provider. It replaces all eight formerly gateway-backed knowledge
operations and also exposes lexical `knowledge.discover`. Do not also configure
`local_knowledge_config`. Session memory remains a separate exact-session
provider; this knowledge configuration grants no desk or memory authority.

The operator-owned runtime JSON must be a physical, private (0600) file:

```json
{
  "schema_version": "agent-tooling.knowledge-runtime.v1",
  "tenant_id": "your-tenant",
  "catalog_path": "/config/knowledge.json",
  "lifecycle_path": "/state/knowledge/lifecycle.sqlite3",
  "documents": {
    "path": "/state/knowledge/document-corpus.json",
    "sha256": "<64 lowercase hexadecimal characters>",
    "repositories": {"your-corpus-scope": "/workspaces/your-repo"}
  },
  "embedding": {
    "model_digest": "<verified weights digest>",
    "model_id": "sentence-transformers/all-MiniLM-L6-v2",
    "dim": 384
  },
  "reference_generation": {
    "path": "/state/knowledge/reference-generation.json",
    "sha256": "<64 lowercase hexadecimal characters>"
  }
}
```

`reference_generation` is optional; missing reference evidence remains unresolved.
Under a navigation profile, `catalog_path` names the operator overlay, not the
pin (see [the catalog under a navigation profile](#the-catalog-under-a-navigation-profile)).
The document artifact has schema `agent-tooling.document-corpus.v1` and original
`chunks` and `vectors` arrays. Chunk rows retain their raw source identities;
vectors retain model/revision IDs, coordinates and digests. Operator repository
mappings relocate reads without rewriting historical provenance. Current-state,
owner/lifecycle, tenant, requested-revision and content-digest checks still apply.
The withdrawal ledger must already exist, including its lock file. It is never
silently reinitialized. A catalog rollback cannot undo ledger withdrawals.

## The catalog under a navigation profile

Without a `navigation_profile` in the tooling configuration, `catalog_path` is the
whole catalog: its `ref` and `path` pin every answer (configured pins).

With a `navigation_profile`, the file at `catalog_path` is the operator overlay, not
the pin. The knowledge tools follow the active profile's published catalog, which
`navigation_workspace.active` admits on every call (its identity digest and its
`ref`/`path` pairing with the profile). The two catalogs split by field:

- From the published generation: each served repository's `ref` and `path`, and
  the `platforms` and `scip_indexes` entries of the served repositories.
- From the operator overlay, unchanged: which repositories are served,
  `capabilities`, `tenant_ids`, `corpus_scope`, `artifacts`, `entry_symbols`,
  `default_branch_ref` and the top-level `navigation` section.

The overlay's `ref` and `path` no longer decide anything. Its `platforms` and
`scip_indexes` keys only name the entries its served repositories need; their
values come from the generation. A published repository, tenant or corpus scope
never widens what is served. Every answer carries `navigation_profile`
(`profile`, `published_at`, `profile_sha256`): the generation it was answered at.

A new generation is followed on the next call, without an operator edit or a
restart. The provider keeps its service while the admitted catalog digest
(`knowledge_config_sha256`) and the overlay stay the same, and rebuilds it when
either changes. A generation change never reopens the document corpus or the
reference store. A change to the runtime JSON still refuses with "reload
required". Overlay edits (a withdrawal, a tenant change) apply on the next call,
as they do without a profile.

A generation that cannot be served is refused as a knowledge result. It is never
mixed with the overlay and never replaced by the last good generation. The result
has `status: error`, `data.error.code: knowledge_unavailable`, a `reason` and
`data.error.refusal`:

| `refusal` | When | `reason` |
|---|---|---|
| `navigation_profile_refused` | `active()` refuses the profile or its catalog | `active()`'s own words, for example `navigation catalog identity mismatch` |
| `served_repository_not_published` | a served repository is absent from the published catalog | the refusal word |
| `published_entry_missing` | the published catalog lacks a `platforms` or `scip_indexes` entry a served repository needs | the refusal word |
| `merged_catalog_invalid` | the merged catalog fails validation, for example a published `scip_indexes` entry without a valid digest | the refusal word |

The operator's capability map still ages, and it fails loudly. It is resolved at
the generation's `ref`: each capability manifest is read from the repository at
that revision. An entry that no longer resolves is an error status, never a silent
answer from an old tree: `knowledge.context` reports the gap
`manifest_unavailable`, and `knowledge.check_references` answers
`knowledge_unavailable`.

`reference_generation` is not followed. It pins the operator's publish output
(the code-reference store written by `kp-agent-knowledge-publish`), not refresh's
generation. Reference evidence therefore still ages with the operator's publish
pin. The profile-following catalog does not mean that knowledge no longer ages.

Reference artifacts are produced with `generation_document` from an already
materialized `FrozenReferenceRetrieval`. Loading checks both the artifact hash
and serving-generation identity. Serving does not consult a graph. New reference
publication uses `kp-agent-knowledge-publish`, which reuses the existing
extractor/resolver and immutable identities with an ephemeral fact adapter,
without a persistent graph runtime. Exporting an old generation does not update
its revision or establish current reference coverage.

The embedding instrument loads lazily and is reused by the process. The current
portable document snapshot uses the existing exact cosine implementation and
has a 10,000-record/64-MiB bound; it is not a scalable ANN backend. The semantic
extra and an independently provisioned model cache are required for real queries.
Use offline model-loading settings in deployments. No lexical fallback is silently
substituted when the embedding runtime or requested embedding revision is absent.

Catalog navigation must use Serena or published SCIP. The legacy external
`kp_ops.code_navigation` subprocess is explicitly refused. With navigation
unconfigured, context reports the missing static-navigation evidence.
`knowledge.platform` reports per-member `tool_coverage`; a configured SCIP index is `digest-verified` only if it loads, validates at the member revision and matches its configured revision, digest and repository key, otherwise its row carries `gap: index_unavailable_or_mismatched`.

The portable publisher produces fresh generations from explicit committed,
maintained documents and tenant-eligible target repositories. Replay reuses
unchanged vectors; prior chunks remain retained but non-current. Completion is
sealed by a final receipt. Failed builds never activate. See
[the publication and acceptance guide](INDEPENDENT-IMAGE-ACCEPTANCE.md).
Operator activation and worker restart remain explicit; this deployment has not
yet enabled automatic document activation after navigation refresh.
