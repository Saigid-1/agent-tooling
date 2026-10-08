# T11a census at the census commit (appendix to the T11a order)

An AST walk of the product modules at the census commit, an earlier main, by a read-only census agent, 2026-10-03. The T11a order's "Re-census" section corrects it where they differ: (c) helpers are 30, not 29; the fifth-variant site list changed; and (g) has 6 definitions the raw word count did not flag. Path prefixes: T/ = packages/tooling/src/kp_agent_tooling/, O/ = extensions/ops/src/kp_agent_tooling_ops/, D/ = deploy/image/.

# T11 leaf-module census: a clean worktree at the census commit

**Scope and method.** I read only tracked files under `packages/tooling/src/kp_agent_tooling/**`, `extensions/ops/src/**` and `deploy/image/*.py`: 160 Python modules. Counts come from AST walks, not grep, with import aliases resolved: `import hashlib as _hashlib, json as _json` at T/_impl/service/scip_entry.py:34, and `from hashlib import sha256` at O/_impl/service/delegation_attribution.py:11.

String-embedded Python scripts were parsed separately and their sites are marked **e**. They are `_INSPECT`, `_OWN`, `_CLEAR`, `_STORE_LIB` and `_MIGRATE` in runtime_install.py:1122–1487, and dependency_identity.py:124–130. They must stay stdlib-only: runtime_install.py:1118-1119 says they run with `python3 -I -c` and use only the standard library.

Path abbreviations:
- **T/** = `packages/tooling/src/kp_agent_tooling/`
- **O/** = `extensions/ops/src/kp_agent_tooling_ops/`
- **D/** = `deploy/image/`
- **H** = the site is a helper definition; **I** = an inline use.

---

## (a) Canonical JSON serialisation

**All `json.dump`/`json.dumps` call sites: 240** (229 in module code, 11 embedded).

**Canonical sites** (`sort_keys=True` and `separators=(',',':')`): **49**. Of these, **26 are helper definitions and 23 are inline**. They fall into 6 argument combinations, which reduce to 4 distinct outputs.

- **A: sort + compact, ensure_ascii and allow_nan left at defaults (ASCII, NaN allowed). 19 sites (6 H, 13 I).**
  - O/_impl/code_references/manifest.py:90 I, :111 I
  - O/_impl/embeddings/store.py:748 I, :866 I (dead code), :872 I (dead code)
  - O/_impl/verification_plan.py:32 I
  - O/charter_import_cli.py:16 I
  - O/knowledge_publish_cli.py:31 H `_write`
  - T/_impl/scip_navigation.py:17 H `_bytes`, :134 H `digest`
  - T/_impl/service/desk_memory_runtime.py:149 I, :216 I, :316 I, :372 I
  - T/_impl/service/desk_write_scope.py:10 H `_digest`
  - T/_impl/service/session_bindings.py:35 H `_digest`
  - T/_impl/service/workspace_context.py:337 I, :339 I
  - T/refresh_cli.py:97 H `digest_json`
- **B: as A plus explicit `ensure_ascii=True`. Output is byte-identical to A. 17 sites (11 H, 6 I).**
  - O/_impl/code_references/manifest.py:65 I
  - O/_impl/code_references/models.py:12 H `canonical_digest`
  - T/_impl/navigation_search_pages.py:42 H `_canonical`
  - T/_impl/navigation_snapshot.py:16 H `_canonical`
  - T/_impl/platform_snapshot.py:90 I
  - T/_impl/semantic_index.py:56 H `_canonical`
  - T/_impl/service/claude_memory_hook.py:34 I
  - T/_impl/service/episodic_handoff.py:6 H `encoded_size`
  - T/_impl/service/episodic_memory.py:70 H `_bytes`
  - T/_impl/service/episodic_queue.py:22 H `_json`
  - T/_impl/service/episodic_summarizer.py:64 I
  - T/_impl/service/host_card_bridge.py:57 I
  - T/_impl/service/memory_budget.py:33 H `_digest`
  - T/_impl/service/model_gateway.py:512 I
  - T/_impl/service/model_gateway_providers.py:114 H `_json`
  - T/_impl/service/session_import_job.py:56 H `_json`
  - T/_impl/service/summary_contract.py:28 H `request_bytes`
- **C: `allow_nan=False`, `ensure_ascii=True`, sort, compact (ASCII, NaN refused). 6 sites (5 H, 1 I).**
  - O/_impl/host_transcript_capture.py:34 H `_canonical`
  - O/_impl/journey_registry.py:155 I
  - O/_impl/observations.py:14 H `encoded`
  - O/_impl/review_ledger.py:20 H `canonical_digest`
  - O/_impl/trial_response_capture.py:33 H `_canonical`
  - O/_impl/verification_finding.py:21 H `finding_digest`
- **D: `allow_nan=False`, sort, compact. Output equals C. 3 sites (3 H).**
  - O/_impl/behavior_model.py:15 H `digest`
  - T/_impl/runtime_install.py:167 H `canonical`
  - T/_impl/workspace_setup.py:17 H `encoded`
- **E: `allow_nan=False`, `ensure_ascii=False`, sort, compact (UTF-8, NaN refused). 2 sites.**
  - O/_impl/embeddings/store.py:200 I
  - T/_impl/embeddings/revision.py:20 H `_canonical_json`
- **F: `ensure_ascii=False`, sort, compact (UTF-8, NaN allowed). 2 sites, both I.**
  - T/_impl/service/host_adapter.py:479 and T/_impl/service/launch_binding.py:155.
  - Both are `codex_trust_entry` and must match Kanban's TypeScript hash.
  - The two functions are duplicates: host_adapter.py:471-480 and launch_binding.py:145-157. Their signatures differ, and so does `HOOK_TIMEOUT_SECONDS`: 5 at host_adapter.py:54, 30 at launch_binding.py:49.

**Duplicated helper bodies:**
- `_canonical` is identical at navigation_search_pages.py:41, navigation_snapshot.py:15 and semantic_index.py:55, and `episodic_memory._bytes`:69 is the same.
- `_json` is identical at episodic_queue.py:21 and session_import_job.py:55.
- `_canonical` is identical at host_transcript_capture.py:33 and trial_response_capture.py:32.

**Non-compact "sorted" forms that feed sha256.** These are a distinct canonical form; moving them to compact separators would change stored digests.
- `sort_keys` only:
  - O/_impl/commit_rationale.py:689, 693, 695
  - O/_impl/verification_adjacency.py:80
  - O/_impl/verification_correctness.py:29
  - O/_impl/verification_packet.py:63
  - T/_impl/service/agent_tooling.py:66
  - T/refresh_cli.py:460
- `ensure_ascii` plus `sort_keys`: O/_impl/verification_packet.py:17 H `encoded`.
- Unsorted and hashed: T/_impl/service/scip_entry.py:41.

**The other 191 (non-canonical) sites, by combination:**
- **Plain `dumps(x)`, 120:**
  - D/container.py:231, 263
  - O/_impl/service/gateway_transport.py:134
  - O/charter_import_cli.py:81, 83
  - O/claude_hook_cli.py:73, 75, 79, 83, 95, 99
  - O/desk_import_cli.py:32
  - O/handoff_cli.py:27
  - O/knowledge_publish_cli.py:164
  - O/observation_cli.py:35
  - T/_impl/dependency_identity.py:130e
  - T/_impl/runtime_install.py:399, 426, 575, 1088, 1129e, 1140e, 1162e, 1205e, 1209e, 1405e, 1457e, 1458e, 1566, 1747, 1888, 1958
  - T/_impl/service/claude_episode_capture.py:171, 175, 176, 370, 371, 415, 417
  - T/_impl/service/desk_catalog_setup.py:53
  - T/_impl/service/desk_memory_runtime.py:27, 276, 290
  - T/_impl/service/desk_write_scope.py:24
  - T/_impl/service/episodic_search.py:138
  - T/_impl/service/host_adapter.py:371, 710 (two calls on line 710)
  - T/_impl/service/launch_binding.py:66, 718
  - T/_impl/service/model_gateway.py:222, 231, 622
  - T/_impl/service/scip_entry.py:41
  - T/_impl/service/serena_navigation.py:269, 383, 454
  - T/_impl/service/session_bindings.py:136, 139
  - T/_impl/service/session_sources.py:299, 303, 317, 326, 762, 813 (two calls), 814, 974 (two calls), 982, 1018, 1020, 1022, 1032, 1040, 1070, 1088, 1170, 1209, 1248
  - T/_impl/service/spool_ingest.py:696
  - T/_impl/service/workspace_capture.py:585 (two calls)
  - T/_impl/typescript_context.py:26
  - T/assistant_host_cli.py:129, 131
  - T/cli.py:42
  - T/desk_cli.py:83
  - T/desk_registry_cli.py:111, 114
  - T/host_bridge_cli.py:17
  - T/host_cli.py:76, 79, 91, 93, 131, 144, 146
  - T/launch_cli.py:52, 54, 92, 95, 97, 102, 105
  - T/memory_cli.py:82
  - T/queue_cli.py:91, 96, 98
  - T/refresh_cli.py:591, 609, 781, 789, 795, 799
  - T/session_import_cli.py:89, 93, 98, 105
  - T/workspace_capture_cli.py:91
- **`sort_keys=True` only, 25:**
  - O/_impl/behavior_model.py:58
  - O/_impl/commit_rationale.py:689, 693, 695
  - O/_impl/verification_adjacency.py:80
  - O/_impl/verification_correctness.py:29
  - O/_impl/verification_packet.py:63
  - O/delegation_cli.py:60
  - O/knowledge_publish_cli.py:162
  - T/_impl/runtime_install.py:1184e, 1439e, 1660
  - T/_impl/service/agent_tooling.py:66
  - T/_impl/service/claude_episode_capture.py:377
  - T/_impl/service/launch_binding.py:699
  - T/_impl/service/spool_ingest.py:109, 283, 694
  - T/_impl/service/workspace_capture.py:264
  - T/catalog_cli.py:44, 90
  - T/refresh_cli.py:460
  - T/workspace_capture_cli.py:53, 86, 88
- **`ensure_ascii=True` only, 12:**
  - O/_impl/code_references/diagnostics.py:70
  - O/_impl/code_references/recovery.py:112
  - O/_impl/code_references/retrieval.py:551, 579
  - O/_impl/journey_registry.py:219
  - O/_impl/service/knowledge.py:171
  - O/_impl/service/knowledge_context.py:19
  - O/_impl/verification_plan.py:78
  - T/_impl/service/episodic_memory.py:521
  - T/memory_cli.py:64, 101
  - T/models_cli.py:49
- **`indent=2` plus `sort_keys`, 9:**
  - O/capture_cli.py:50, 58
  - O/transcript_cli.py:36, 43
  - T/_impl/runtime_install.py:525
  - T/_impl/service/desk_catalog_setup.py:134
  - T/install_cli.py:95, 99, 102
- **Compact separators only, 5:**
  - D/container.py:270
  - O/capture_cli.py:60
  - O/transcript_cli.py:46
  - T/_impl/service/episodic_summarizer.py:343
  - T/_impl/service/model_gateway_providers.py:134
- **`ensure_ascii` plus `sort_keys`, 3:** O/_impl/verification_packet.py:17; T/models_cli.py:26; T/session_import_cli.py:86.
- **`indent=2` only, 3:** O/claude_hook_cli.py:56; T/refresh_cli.py:48; T/setup_cli.py:60.
- **`ensure_ascii=False` plus `indent=2`, 2:** T/_impl/service/desk_registry.py:143; T/_impl/service/host_adapter.py:669.
- **`indent=1` plus `sort_keys`, 2:** T/_impl/service/host_adapter.py:125, 813.
- **`ensure_ascii=False` plus compact, 2:** T/_impl/service/host_adapter.py:294, 297.
- **`ensure_ascii=False` only, 2:** T/_impl/service/launch_binding.py:133, 142.
- **One site each:**
  - `ascii`, `indent=2`, `sort`: O/_impl/tool_discovery.py:176 (dead code)
  - `ascii` plus compact: T/_impl/service/episodic_summarizer.py:325
  - `default=str` plus `sort`: T/_impl/service/host_adapter.py:721
  - `ascii`, `indent=1`, `sort`: T/_impl/service/model_gateway.py:487
  - `allow_nan=False`, `ascii`, compact: T/_impl/tool_delivery.py:38
  - `allow_nan=False` plus compact: T/_impl/workspace_setup_server.py:93

---

## (b) sha256 hashing

**`hashlib.sha256` call sites: 176** (172 in module code, 4 embedded). By file:

- O/_impl/behavior_model.py:15
- O/_impl/code_references/artifact_identity.py:14, 35
- O/_impl/code_references/extraction.py:80
- O/_impl/code_references/models.py:13
- O/_impl/code_references/portable_generation.py:26
- O/_impl/code_references/registry.py:62, 92
- O/_impl/code_references/retrieval.py:35
- O/_impl/code_references/source_facts.py:89
- O/_impl/commit_rationale.py:108, 122, 192, 302, 347, 469, 657, 689, 692, 695
- O/_impl/embeddings/desk_docs.py:108
- O/_impl/host_transcript_capture.py:39
- O/_impl/journey_registry.py:156, 209
- O/_impl/lifecycle_matrix.py:254, 302, 323
- O/_impl/observations.py:18
- O/_impl/review_ledger.py:20, 157
- O/_impl/service/delegation_attribution.py:48 (from-import)
- O/_impl/service/document_coordinates.py:30
- O/_impl/service/document_corpus.py:29, 61
- O/_impl/service/legacy_desk_import.py:62, 73, 108, 140
- O/_impl/service/ops_tools.py:254, 374
- O/_impl/service/published_navigation.py:52, 58
- O/_impl/tool_discovery.py:152
- O/_impl/trial_response_capture.py:38
- O/_impl/verification_adjacency.py:71, 80, 83
- O/_impl/verification_correctness.py:15, 16, 29, 31, 32
- O/_impl/verification_finding.py:21
- O/_impl/verification_packet.py:29, 45, 56, 59, 61, 63, 103
- O/capture_cli.py:53
- O/charter_import_cli.py:17, 20, 78
- O/knowledge_publish_cli.py:35, 122, 152
- O/transcript_cli.py:39, 46
- T/_impl/dependency_identity.py:89, 106, 113
- T/_impl/embeddings/embedders.py:79
- T/_impl/embeddings/revision.py:60, 122
- T/_impl/navigation_discovery.py:201
- T/_impl/navigation_environment.py:22
- T/_impl/navigation_search_pages.py:54, 91, 142, 332
- T/_impl/navigation_snapshot.py:61, 66, 77, 83, 88, 93, 104, 109, 124, 133, 145
- T/_impl/navigation_workspace.py:115, 133, 311
- T/_impl/platform_snapshot.py:77, 91
- T/_impl/repository_manifest.py:48
- T/_impl/runtime_install.py:171, 1296e, 1361e, 1372e, 1477e
- T/_impl/scip_navigation.py:59, 81, 134
- T/_impl/semantic_index.py:52
- T/_impl/service/agent_tooling.py:38, 66
- T/_impl/service/claude_episode_capture.py:36, 153
- T/_impl/service/desk_catalog_setup.py:87
- T/_impl/service/desk_identity.py:20
- T/_impl/service/desk_memory_runtime.py:149, 216, 372
- T/_impl/service/desk_profiles.py:255
- T/_impl/service/desk_write_scope.py:10
- T/_impl/service/episodic_memory.py:74
- T/_impl/service/episodic_queue.py:26
- T/_impl/service/episodic_search.py:107, 162
- T/_impl/service/episodic_summarizer.py:65, 346, 350
- T/_impl/service/host_adapter.py:150, 480
- T/_impl/service/host_card_bridge.py:60
- T/_impl/service/import_context.py:305
- T/_impl/service/launch_binding.py:157, 508, 633
- T/_impl/service/memory_budget.py:34
- T/_impl/service/model_gateway.py:650, 654, 699
- T/_impl/service/model_gateway_providers.py:119
- T/_impl/service/native_history_import.py:35
- T/_impl/service/scip_entry.py:41 (`_hashlib` alias)
- T/_impl/service/serena_navigation.py:114, 184
- T/_impl/service/session_bindings.py:35
- T/_impl/service/session_import_job.py:60, 80
- T/_impl/service/session_sources.py:140
- T/_impl/service/spool_ingest.py:101, 196, 217, 605
- T/_impl/service/workspace_capture.py:106
- T/_impl/service/workspace_context.py:338, 451, 473, 509
- T/_impl/source_citations.py:57
- T/_impl/tool_delivery.py:47, 48, 65, 88
- T/_impl/typescript_context.py:18, 30
- T/_impl/workspace_setup.py:21, 127, 136
- T/refresh_cli.py:97, 101, 455, 456, 457, 458, 460, 529, 550, 632, 669, 677

**Generic helper definitions: 24.** Caller counts are product references unless noted.

Raw bytes or file → hex (10). Each `_sha` body below is `return hashlib.sha256(x).hexdigest()`; they are identical copies.
- O/_impl/host_transcript_capture.py:38 `_sha`: 6
- O/_impl/trial_response_capture.py:37 `_sha`: 5
- T/_impl/runtime_install.py:170 `sha256`: 6. It shadows the name; the calls at :711, 877, 945, 950, 1894, 2006 are not hashlib calls.
- T/_impl/semantic_index.py:51 `_digest`: 12
- T/_impl/service/host_adapter.py:149 `_sha`: 5
- T/_impl/service/native_history_import.py:34 `_sha`: 7
- T/_impl/service/session_import_job.py:59 `_sha`: 8
- T/_impl/service/session_sources.py:139 `_sha`: 3, including episodic_search.py:302 through a function-local import at :211
- T/_impl/service/workspace_capture.py:105 `_sha`: 7
- T/refresh_cli.py:100 `digest_file`: 6

Canonical JSON → hex or prefixed ID (14):
- O/_impl/behavior_model.py:14 `digest`: 16 (lifecycle_matrix ×8, verification_packet ×2)
- O/_impl/code_references/models.py:11 `canonical_digest(prefix, value)`: 11
- O/_impl/observations.py:17 `digest`: 6
- O/_impl/review_ledger.py:18 `canonical_digest`: 2
- O/_impl/verification_finding.py:19 `finding_digest`: 3, plus 5 in tests
- T/_impl/embeddings/revision.py:59 `_sha256_identity(namespace, value)`: 2
- T/_impl/scip_navigation.py:133 `digest`: 4, plus 11 in tests
- T/_impl/service/desk_write_scope.py:9 `_digest`: 2
- T/_impl/service/episodic_memory.py:73 `_id(kind, value)`: 38
- T/_impl/service/episodic_queue.py:25 `_digest`: 3, plus 3 in tests
- T/_impl/service/memory_budget.py:32 `_digest`: 1
- T/_impl/service/session_bindings.py:34 `_digest`: 3
- T/_impl/workspace_setup.py:20 `digest`: 1
- T/refresh_cli.py:96 `digest_json`: 3

**Domain identity hashers: 12.** These keep their schemes but would call the leaf.
- O/_impl/code_references/artifact_identity.py:9 `change_lineage_key` and :17 `change_occurrence_id`.
- O/_impl/code_references/retrieval.py:32 `_chunk_id` and O/_impl/service/document_coordinates.py:28 `_chunk_id` implement the same `deskdoc:` scheme twice.
- T/_impl/repository_manifest.py:46 `_identity`
- T/_impl/service/desk_identity.py:16 `binding_key`: 11 product references, 7 in tests
- T/_impl/navigation_search_pages.py:140 `_manifest_cache_path`
- T/_impl/service/claude_episode_capture.py:35 `_digest` (hash chain)
- The two duplicate `codex_trust_entry` functions: host_adapter.py:475 and launch_binding.py:149
- T/_impl/embeddings/revision.py:112 `embedding_identity`
- O/knowledge_publish_cli.py:30 `_write` (writes and returns the hash)

**Streaming and constant uses** (not leaf helpers):
- `hashlib.sha256()` with `.update`: T/_impl/service/claude_episode_capture.py:153; launch_binding.py:633; spool_ingest.py:217, 605; session_import_job.py:80; runtime_install.py:1296e, 1361e, 1372e.
- Constants: launch_binding.py:508 `_EMPTY_SHA`; spool_ingest.py:101.

---

## (c) Private-file open and write

**Primitive sites: 144.** The 12 O_NOFOLLOW markers are counted separately below and overlap these.

- **`os.open` with O_CREAT: 14.**
  - O/_impl/service/knowledge_lifecycle.py:27 (no NOFOLLOW), :34 (EXCL, no NOFOLLOW)
  - O/knowledge_publish_cli.py:32 (EXCL, no NOFOLLOW)
  - T/_impl/runtime_install.py:1033
  - T/_impl/service/host_adapter.py:123, 135, 298, 594, 786
  - T/_impl/service/launch_binding.py:92
  - T/_impl/service/model_gateway.py:467
  - T/_impl/service/spool_ingest.py:77, 91
  - T/_impl/service/workspace_capture.py:327 (no NOFOLLOW)
  - Read-only `os.open`, excluded from the count: refresh_retention.py:118, desk_registry.py:132, model_gateway.py:265, 444.
- **O_NOFOLLOW: 12.** As an attribute: host_adapter.py:123, 135, 298, 594, 786; launch_binding.py:92; spool_ingest.py:77, 91. Through `getattr(os,'O_NOFOLLOW',0)`: runtime_install.py:1033; model_gateway.py:261, 442, 467.
- **Exclusive `'x'`/`'xb'` opens: 10.**
  - claude_episode_capture.py:180, 183
  - claude_memory_hook.py:21
  - desk_memory_runtime.py:274
  - episodic_queue.py:41
  - launch_binding.py:64, 536, 539
  - runtime_install.py:1231e, 1362e
  - Append-mode opens, not counted: refresh_cli.py:568, 786.
- **chmod family: 35.**
  - `os.chmod` (19):
    - O/capture_cli.py:46, 52, 59
    - O/transcript_cli.py:31, 38, 45
    - runtime_install.py:1138e, 1150e, 1343e, 1468e, 1865
    - desk_catalog_setup.py:52, 141
    - desk_memory_runtime.py:275, 335
    - host_adapter.py:141
    - launch_binding.py:65
    - model_gateway.py:340
    - tool_delivery.py:57 (0o400)
  - `Path.chmod` (13): claude_episode_capture.py:182, 185; claude_memory_hook.py:23; desk_memory_runtime.py:266; episodic_memory.py:104; episodic_queue.py:43; episodic_search.py:66; launch_binding.py:538, 541; session_import_job.py:286; workspace_capture.py:270; tool_delivery.py:34; workspace_setup.py:179.
  - `os.fchmod` (3): runtime_install.py:995, 1035; desk_registry.py:148.
- **Private directories at 0o700: 22.**
  - `Path.mkdir(mode=0o700)` (17):
    - D/container.py:267
    - O/capture_cli.py:45
    - O/knowledge_publish_cli.py:93
    - O/transcript_cli.py:30
    - agent_tooling.py:81
    - host_adapter.py:213, 574, 777
    - launch_binding.py:81, 272
    - model_gateway.py:336, 476, 481
    - tool_delivery.py:31
    - workspace_setup.py:159
    - assistant_host_cli.py:42, 46
  - `os.mkdir` (4): runtime_install.py:1414e, 1423e, 1432e, 1863.
  - `os.makedirs` (1): runtime_install.py:1251e.
  - Not counted: these mkdirs use no mode, so the directory gets the umask default, including store parents: desk_binding.py:86, episodic_memory.py:95, episodic_search.py:58, knowledge_lifecycle.py:23.
- **tempfile: 15.**
  - `mkstemp` (10): observations.py:71; navigation_search_pages.py:62, 169; navigation_snapshot.py:30; runtime_install.py:993; scip_navigation.py:21; semantic_index.py:247; desk_registry.py:146; tool_delivery.py:51; refresh_cli.py:45.
  - `NamedTemporaryFile(delete=False)` (3): desk_catalog_setup.py:49, 139; desk_memory_runtime.py:333.
  - `mkdtemp` (2): workspace_setup.py:174; refresh_cli.py:556.
- **Publication: 19.**
  - `os.replace` (6): runtime_install.py:1000; scip_navigation.py:27; semantic_index.py:250; desk_registry.py:153; host_adapter.py:142; refresh_cli.py:49.
  - `os.link` (publish-once, 7): observations.py:75; navigation_search_pages.py:69, 176; navigation_snapshot.py:39; desk_catalog_setup.py:146; desk_memory_runtime.py:339; tool_delivery.py:59.
  - `os.rename` (5): refresh_retention.py:211; runtime_install.py:1461e, 1664, 1673; model_gateway.py:488.
  - `Path.rename` (1): workspace_setup.py:180.
- **`os.fsync`: 16.**
  - observations.py:74
  - knowledge_publish_cli.py:34
  - navigation_search_pages.py:67, 174
  - navigation_snapshot.py:35
  - runtime_install.py:999, 1367e
  - scip_navigation.py:26
  - semantic_index.py:249
  - desk_catalog_setup.py:144
  - desk_registry.py:152
  - host_adapter.py:128, 140, 316
  - tool_delivery.py:56
  - refresh_cli.py:48
  - None of these fsyncs a parent directory.
- **`st_mode & 0o077` permission checks: 13.**
  - O/_impl/code_references/portable_generation.py:21
  - O/_impl/service/document_corpus.py:24
  - O/_impl/service/legacy_desk_import.py:93
  - runtime_install.py:953
  - desk_catalog_setup.py:35, 176
  - desk_memory_runtime.py:34, 227, 307
  - host_adapter.py:107
  - launch_binding.py:85
  - model_gateway.py:278
  - assistant_host_cli.py:43
  - Related but different: D/container.py:207 compares `& 0o7777` to 0o700.

**Helper definitions: 29** (all are definitions; callers in brackets).

Write helpers (16):
- T/_impl/service/launch_binding.py:61 `write_private`: opens `'x'`, then chmod 0600. The window between create and chmod is open, and there is no fsync. [11: launch_binding ×6, assistant_host_cli ×4, spool_ingest `lb.` ×1]
- launch_binding.py:69 `_write_once` [6: launch_binding ×2, spool_ingest `lb.` ×4]
- host_adapter.py:121 `_write_new`: O_EXCL, NOFOLLOW, fsync [4]
- host_adapter.py:131 `_replace`: temp file with EXCL and NOFOLLOW, then fsync, chmod, replace [3]
- desk_catalog_setup.py:132 `_write_private_json`: temp file, fsync, link [2]
- model_gateway.py:466 `_write_private`: EXCL, NOFOLLOW, no fsync [2]
- runtime_install.py:992 `_write`: mkstemp, fchmod, fsync, replace [4]
- runtime_install.py:1031 `_placeholder` [1]
- scip_navigation.py:20 `_atomic` [2]
- semantic_index.py:241 `_write_once`: replace [4]
- navigation_snapshot.py:26 `_write_once`: link [3]
- refresh_cli.py:43 `atomic` [11]
- O/knowledge_publish_cli.py:30 `_write` [4]
- desk_registry.py:139 `RoleRoster._write` [2]
- navigation_search_pages.py:52 `_save` [1] and :164 `_store_manifest` [1]: domain functions with a publish-once write inside

Directory and lock helpers (4):
- launch_binding.py:78 `private_dir` [4: launch_binding ×1, spool_ingest ×3]
- launch_binding.py:91 `_locked` [5]
- host_adapter.py:105 `_private_dir` [1]
- desk_catalog_setup.py:30 `_private_parent` [4]

Read and validate helpers (9):
- desk_memory_runtime.py:19 `private_json` [31 product references across 13 modules: portable_knowledge ×3, charter_import_cli ×1, knowledge_publish_cli ×3, assistant_memory_policy ×1, desk_catalog_setup ×4, desk_memory_runtime ×8, desk_registry ×1, harness_profiles ×1, host_card_bridge ×1, launch_binding ×3, spool_ingest ×1, assistant_host_cli ×3, workspace_capture_cli ×1]
- desk_memory_runtime.py:30 `_private_json` [2]
- desk_registry.py:42 `_private_json`, a lazy-import shim [2]
- host_adapter.py:94 `_owned` [3]
- host_adapter.py:112 `_read_json` [2]
- model_gateway.py:259 `read_api_key` [3]
- navigation_snapshot.py:47 `_read_regular` [8]
- native_history_import.py:38 `_regular` [2]

**Inline write sequences** (not helpers):
- desk_memory_runtime.py:333-341 (`persist_host_selection`, no fsync)
- tool_delivery.py:51-64
- observations.py:71-77
- desk_catalog_setup.py:49-52
- capture_cli.py:45-59
- transcript_cli.py:30-45
- model_gateway.py:476-488
- workspace_setup.py:154-180
- Store `initialize` methods that create with `'xb'` and then chmod: claude_episode_capture.py:180-185, claude_memory_hook.py:21-23, episodic_queue.py:41-43, launch_binding.py:536-541, desk_memory_runtime.py:274-275.

---

## (d) SQLite open

**`sqlite3.connect` sites: 39** (36 in module code, 3 embedded). 10 are inside per-store connection helpers and 26 are inline.

**Per-store connection helpers (10):**
- claude_episode_capture.py:197 `_db`
- claude_memory_hook.py:27 `_db`
- episodic_memory.py:108 `_connect`
- episodic_queue.py:59 `_db`
- episodic_search.py:52 `_connect`
- launch_binding.py:549 `_db`
- model_gateway.py:331 `_connect`
- session_import_job.py:273 `_db`
- spool_ingest.py:72 `Cursors._db`
- workspace_capture.py:272 `_db`

**Connection profiles (9 distinct):**
- **Plain path, no options (13):**
  - O/_impl/service/knowledge_lifecycle.py:36
  - claude_episode_capture.py:186
  - claude_memory_hook.py:24
  - desk_binding.py:87
  - episodic_memory.py:96
  - episodic_queue.py:44
  - episodic_search.py:59
  - launch_binding.py:542
  - session_import_job.py:279
  - spool_ingest.py:80
  - runtime_install.py:1263e, 1383e, 1389e
- **`timeout=30.0, check_same_thread=False`, plus `PRAGMA foreign_keys = ON` and `PRAGMA busy_timeout = 30000` (1):** O/_impl/code_references/manifest.py:41, 43, 44.
- **`timeout=30, isolation_level=None` (1):** model_gateway.py:338.
- **`timeout=10` (1):** workspace_capture.py:279.
- **URI `?mode=rw`, `uri=True` (10):** knowledge_lifecycle.py:78; claude_episode_capture.py:200; claude_memory_hook.py:30; desk_binding.py:127; desk_memory_runtime.py:268, 289; desk_write_scope.py:21; episodic_queue.py:62; launch_binding.py:552; session_bindings.py:109.
- **URI `?mode=ro` (8):** knowledge_lifecycle.py:64; desk_binding.py:103, 168; desk_memory_runtime.py:367; desk_registry.py:213; desk_write_scope.py:30; launch_binding.py:219; session_bindings.py:151.
- **URI with the mode computed at runtime (2):** episodic_memory.py:112; episodic_search.py:70.
- **URI computed plus `timeout=10` (1):** session_import_job.py:289.
- **`:memory:` (2):** model_gateway.py:371; workspace_capture.py:276.

**The 21 URI sites use three different spellings:**
- `.as_uri()+'?mode='` (7): knowledge_lifecycle 64, 78; desk_memory_runtime 268, 289, 367; desk_write_scope 21, 30.
- f-string `f'{p.as_uri()}?mode='` (6): desk_binding 103, 127, 168; launch_binding 219; session_bindings 109, 151.
- `.resolve().as_uri()+'?mode='` (8): claude_episode_capture 200; claude_memory_hook 30; desk_registry 213; episodic_memory 112; episodic_queue 62; episodic_search 70; launch_binding 552; session_import_job 289.

**ATTACH (1):** episodic_search.py:255, `ATTACH DATABASE ? AS idx` with a `resolve().as_uri()+'?mode=ro'` parameter.

**Pragmas:**
- `journal_mode`: 0 product sites. Only tests set it: tests/install/t9b_harness.py:414, 832 set WAL.
- `foreign_keys`, `busy_timeout`: only manifest.py:43-44.
- `user_version`: set at episodic_queue.py:56 (in DDL), checked at :64.
- `quick_check`: desk_binding.py:104; session_sources.py:1119; runtime_install.py:1219e.
- `table_info`: desk_binding.py:106; runtime_install.py:1289e.
- `pragma_table_list`: runtime_install.py:1285e.

**Other connection settings:**
- `row_factory = sqlite3.Row`: claude_episode_capture.py:201; episodic_queue.py:63; launch_binding.py:553; session_import_job.py:432.
- `text_factory = bytes`: runtime_install.py:1264e, 1390e.
- `BEGIN IMMEDIATE`, 37 sites:
  - O manifest.py:70
  - O charter_import_cli.py:61
  - claude_episode_capture.py:111, 168, 288, 410
  - desk_binding.py:128
  - desk_profiles.py:163, 212
  - desk_registry.py:281
  - episodic_memory.py:164, 216
  - episodic_queue.py:86, 138, 164, 187, 196
  - episodic_search.py:100, 125, 167
  - launch_binding.py:592, 712
  - model_gateway.py:379
  - session_bindings.py:110
  - session_import_job.py:588, 705, 728
  - session_sources.py:512, 574, 609, 1055, 1095, 1146, 1162, 1227, 1239
  - workspace_capture.py:444
- desk_binding.py:87, 103, 127 and 168 use `with sqlite3.connect(...) as db`, which commits but never closes the connection.

---

## (e) Store-path rule: every store path under /state/memory

**The rule exists twice; there is no shared implementation.**
- D/container.py: constants at :105-109 (`STORE`, `LEGACY_STORES`, `STORE_KEYS`, `TEMPLATE_KEY`); `outside_store` at :141-152; `operator_store_paths` at :155-187, which also follows a `config_template` found under /state/memory (:184-186); `store_preflight` at :189-227, called at :252.
- T/_impl/runtime_install.py: constants at :123-133 (`MEMORY_TARGET`, `MEMORY_HOST`, `LEGACY_STORES`, `STORE_KEYS`, `TEMPLATE_KEY`); `_state_path` at :724-728, which also rejects control characters (container.py does not); `required_store_path` at :731-748; `operator_store_paths` at :751-783, which returns dicts where container.py returns strings; `store_directories` from :786.

**Literal `/state/memory` in executable code: 2** — container.py:105 and runtime_install.py:123. Related: runtime_install.py:124 (`'state/memory'`) and :1097 (a message).

**No product site checks /state/memory when it opens a store.** Enforcement happens only at role start (container.py) and in host-side planning (runtime_install).

**Sites that read `state_root` and derive store paths: 14.**
- `config['state_root']` reads (11):
  - T/_impl/service/desk_memory_runtime.py:226 (`components()`, which derives sessions.sqlite3 at :256, episodes.sqlite3 at :257 and assistant-owner.json at :234) and :271
  - T/_impl/service/launch_binding.py:266 (launches dir at :267; ledgers via `_ledgers` :223-231) and :434
  - T/_impl/service/spool_ingest.py:366, 483 (launches dir at :484) and 563 (`_state_root`)
  - T/assistant_host_cli.py:35 and :84
  - T/queue_cli.py:49
  - O/claude_hook_cli.py:33
- `state_root` passed as a parameter (3):
  - desk_memory_runtime.py:162/167, `RegistryDeskAuthority.store_path`
  - spool_ingest.py:68-70, `Cursors(state_root)`
  - desk_catalog_setup.py:166/173, which validates the directory and writes it into the config at :195
  - catalog_cli.py:70 only passes the CLI argument through.

**`components()` is the single opener.** It has 30 call sites:
- O/charter_import_cli.py:79
- O/claude_hook_cli.py:32
- O/delegation_cli.py:31
- O/desk_import_cli.py:27, 31
- desk_memory_runtime.py:262, 281, 301, 353, 360
- host_adapter.py:407
- host_card_bridge.py:33
- launch_binding.py:253, 431
- model_gateway.py:507
- session_bindings.py:90
- spool_ingest.py:364, 482, 562
- assistant_host_cli.py:37, 81
- desk_cli.py:45, 57, 62, 66, 72
- desk_registry_cli.py:107
- queue_cli.py:48
- session_import_cli.py:60
- workspace_capture_cli.py:71
- runtime_install.py:910 calls an unrelated function that is also named `components`.

**`roster_path` sites (5):** desk_registry.py:66 (checks it is absolute); desk_memory_runtime.py:166; desk_profiles.py:127; desk_registry.py:276; desk_registry_cli.py:93.

**`config_template` sites (2):** assistant_host_cli.py:29, and :41, where the launches directory is `template.parent/'launches'`, not under `state_root`.

**Store files derived from `EpisodeStore.path.with_name()` (13):** the 11 `with_name` episode-search sites in (h), plus session_import_job.py:266 and workspace_capture.py:258.

**Containment checks (5):**
- queue_cli.py:50-52 and O/claude_hook_cli.py:34-36 both raise "state must remain in the configured private state root".
- assistant_host_cli.py:85-88.
- launch_binding.py:434 and spool_ingest.py:366 check for a "receipt … of this state root".

**Private state-directory validation is duplicated:** desk_memory_runtime.py:226-230 and desk_catalog_setup.py:173-177.

---

## (f) The duplicated `_page` predicate

**Current lines:**
- T/_impl/service/launch_binding.py:624-708, `RolloutCapture._page` (85 lines).
- T/_impl/service/spool_ingest.py:206-292, `ChildRolloutCapture._page` (87 lines).
- `ChildRolloutCapture(lb.RolloutCapture)` (spool_ingest.py:199) overrides only `__init__` (:202-204) and `_page`.

**They are not byte-identical.** In the previously reported ranges (launch_binding.py:628-708 against spool_ingest.py:212-292, 81 lines each), **70 lines are byte-identical and 11 differ:**
- **7 differ only in the exception spelling**, `RolloutCaptureConflict(` against `conflict(`; spool_ingest.py:210 aliases `conflict = lb.RolloutCaptureConflict`. Line pairs (launch_binding / spool_ingest): 640/224, 644/228, 650/234, 667/251, 676/260, 684/268, 690/274.
- **2 differ in the predicate:** launch_binding.py:663 and :666 call `codex_meta_matches(value, session, workspace)`; spool_ingest.py:247 and :250 call `child_meta_matches(value, session, self.parent, workspace)`. The predicates are defined at launch_binding.py:384 and spool_ingest.py:145.
- **2 differ in the message as well as the exception:** launch_binding.py:655/664 say "…this session metadata"; spool_ingest.py:239/248 say "…this child session metadata".

Preamble differences: spool_ingest.py:207 adds the comment "The same page as RolloutCapture._page; only the session_meta predicate differs" and the alias line :210. launch_binding.py:627 has a trailing comment that spool_ingest.py:211 lacks.

---

## (g) Dead code

**How I checked.**
1. Collected all 1,714 FunctionDef, AsyncFunctionDef and ClassDef nodes in the 160 product modules.
2. Indexed references from every tracked `.py` file in the repo (product, tests, scripts): every `ast.Name`, `ast.Attribute.attr` and `ast.alias`, plus every identifier token inside string constants. This covers `getattr` strings, `__all__` (code_references/__init__.py:7), MCP tool tables keyed by string, and `-m` strings.
3. Indexed word tokens from every tracked non-`.py` file except apps/kanban: docs, both pyproject.toml files (including `[project.scripts]` and the `kp_agent_tooling.tools` entry point `OpsToolProvider`), the Dockerfile, compose.yaml and the CSV inventories.
4. A definition is dead when it has no reference outside its own line span. For module-level definitions, only files that mention the module's name count. I iterated to a fixpoint, ignoring references from inside already-dead code.
5. Excluded dunders and the stdlib framework overrides in T/_impl/workspace_setup_server.py: `get_request`:48, `log_message`:75, `do_GET`:124, `do_POST`:144.
6. Cross-check: a raw word count over all tracked files, including apps/kanban, flags 19 definitions; all 19 appear below.
7. Limit: attribute matching is by name, so a dead method that shares a name with a live attribute would be missed. The method gives false negatives, not false positives.

**No references anywhere: 24 definitions, 514 lines, in 11 files.**
- O/_impl/embeddings/store.py:
  - `VectorStore.replace_provenance` 101-105 (5)
  - `VectorStore.desk_note_candidates` 116-128 (13)
  - `MemoryVectorStore.replace_provenance` 429-453 (25)
  - `MemoryVectorStore.desk_note_candidates` 505-553 (49)
  - `PgVectorStore.replace_provenance` 834-891 (58)
  - `PgVectorStore.desk_note_candidates` 971-1046 (76)
  - These six total **226 lines**. Add `_desk_note_scope_matches` 160-173 (14), used only by them, for **240 lines** in this file. They contain the (a) sites :866 and :872.
- O/_impl/embeddings/desk_docs.py: `retrieve_desk_docs` 172-215 (44); `DeskDocRetrievalResult` 52-56 (5, used only by it); `_repo_scope` 249-257 (9, used only by it).
- O/_impl/tool_discovery.py: `render_bounded` 194-209 (16); `_render` 174-191 (18, used only by it, contains the dumps at :176); `_markdown_text` 169-171 (3).
- O/_impl/code_references/adapters.py:157-161: `NavigationReadiness` Protocol (5), not used even as an annotation.
- O/_impl/code_references/indexing.py:39-44: `DocumentReferenceIndexer.prepare_targets` (6).
- O/_impl/code_references/manifest.py:165-175: `SQLiteReferenceManifestStore.export_receipt` (11).
- O/_impl/service/knowledge_lifecycle.py:109-113: `default_lifecycle` (5).
- T/_impl/embeddings/embedders.py:198-277: `ProductionLocalEmbedding` (80, including `embed_one` 239-277). Its name appears only in its own error strings at :228 and :233.
- T/_impl/embeddings/revision.py:129-182: `EmbeddingRunManifest` (54).
- T/_impl/service/session_sources.py: `capture_identity` 379-381 (3); `scope_ids` 782-790 (9).
- T/_impl/service/summary_contract.py:23-24: `SummaryProposer` Protocol (2).
- T/_impl/service/workspace_context.py: `WorkspaceCatalog.to_dict` 94-95 (2); `WorkspaceReadiness.to_dict` 140-141 (2).

**Referenced only by tests: 10 definitions** (not dead under the rule, listed for the Principal):
- O/_impl/code_references/registry.py:97-109 `load_committed_operation_declarations`
- O/_impl/commit_rationale.py:698-705 `refresh_plan`
- O/_impl/evidence_references.py:65-66 `observation_reference`
- O/_impl/observation_adapters.py:50-65 `runtime_observation`
- O/_impl/service/knowledge.py:348-356 `permits_tenant`
- O/_impl/service/knowledge_lifecycle.py:102-106 `withdraw_documents` and :116-142 `reconcile_catalog`
- T/_impl/embeddings/embedders.py:43-88 `DeterministicEmbedder` (21 test references; a test double living in product code)
- T/_impl/service/workspace_context.py:144-188 `ReviewedMemoryFileResolver`
- T/_impl/service/openrouter_models.py:248-260 `OpenRouterModelRegistryClient.fitting_models`

**Modules:** none is unreferenced. O/handoff_cli.py is referenced only by extensions/ops/README.md:29; it has no script entry and no test.

---

## (h) Hard-coded store filenames (product `.py` files)

- **`episode-search.sqlite3`: 12, and no constant exists.**
  - O/claude_hook_cli.py:62
  - T/_impl/service/episodic_memory_tools.py:381
  - host_card_bridge.py:89
  - launch_binding.py:487
  - model_gateway.py:520
  - native_history_import.py:423
  - session_import_job.py:570
  - spool_ingest.py:411
  - workspace_capture.py:424
  - T/desk_cli.py:59, 63
  - T/assistant_host_cli.py:101
  - All except assistant_host_cli.py:101 use `store.path.with_name(...)`; that one uses `root/'…'`.
- **`episodes.sqlite3`: 2.** desk_memory_runtime.py:167, 257.
- **`sessions.sqlite3`: 2.** desk_memory_runtime.py:256; assistant_host_cli.py:39.
- **`workspace-capture.sqlite3`: 1.** workspace_capture.py:258.
- **Other store filenames: 10.**
  - session_import_job.py:266 `session-import-jobs.sqlite3`
  - spool_ingest.py:46 `LEDGER='spool-ingest.sqlite3'` (also in the docstring at :19)
  - launch_binding.py:229 `capture.sqlite3` and `queue.sqlite3`; :230 `telemetry.sqlite3`; :231 `rollout-capture.sqlite3` and `queue.sqlite3`
  - assistant_host_cli.py:56 builds the name as `name + '.sqlite3'`, so a grep for the literal misses it
  - model_gateway.py:326 `ledger.sqlite3`
  - O/knowledge_publish_cli.py:107 `references.sqlite3`
- **Outside `.py` and outside scope:**
  - The packaged asset T/assets/MEMORY.md:153, 155, 156, 157.
  - Embedded suffix tests at runtime_install.py:1214e and 1253e.
  - extensions/ops/scripts/reconcile_desk_history.py:52, 65.

---

## Partial leaves

**No `canonical.py`, `hashing.py`, `private_files.py`, `sqlite_open` or `leaf.py` module exists.**

Stdlib-only modules that already serve these categories:
- **O/_impl/code_references/models.py:** `canonical_digest`:11, 11 references. Importers: indexing.py:218 (local import), manifest.py:13, population.py:28 (local import), resolution.py:10, source_facts.py:13.
- **O/_impl/behavior_model.py:** `digest`:14, 16 references. Importers: lifecycle_matrix.py:16, verification_packet.py:9.
- **O/_impl/observations.py:** `encoded`:13 (7 product references, 2 test) and `digest`:17 (6). Importer: observation_adapters.py:2.
- **O/_impl/trial_response_capture.py:** `_canonical`:32 and `_sha`:37, internal only.
- **O/_impl/document_identity.py:** `document_identity`:5, 4 references. A clean example of a leaf, though not in these categories.
- **T/_impl/service/desk_identity.py:** `binding_key`:16, 11 product references and 7 test, 6 importers.
- **T/_impl/service/summary_contract.py:** `request_bytes`:27, 3 references; importer episodic_queue.py:14.
- **T/_impl/embeddings/revision.py:** `_canonical_json`:16 and `_sha256_identity`:59.
- **T/_impl/workspace_setup.py:** `encoded`:16 and `digest`:20.
- **T/_impl/tool_delivery.py:** `DeliveryStore.encode`.
- **desk_write_scope.py, memory_budget.py, claude_episode_capture.py:** private `_digest` helpers.

Shared today but living in heavy modules:
- **desk_memory_runtime.py:** `private_json`:19, with 31 references across 13 modules.
- **episodic_memory.py:** `_bytes`:69 (21 references; importers legacy_desk_import, desk_profiles, desk_registry by local import, session_sources) and `_id`:73 (38 references; importers desk_profiles, desk_registry local, host_card_bridge, native_history_import local, session_import_job, session_sources).
- **session_sources.py:** `_sha`:139, imported locally by episodic_search.py:211.
- **launch_binding.py:** `write_private`, `_write_once`, `private_dir` and `_locked`, used by assistant_host_cli.py:14 and by spool_ingest through the `lb.` alias.

---

## Import graph

**Hubs by top-level fan-in** (top-level importers / all importers including function-local):
- `service.episodic_memory`: 15/16. It imports desk_binding, episodic_provenance and summary_contract.
- `_impl.source_citations`: 14/15. Stdlib only.
- `service.desk_binding`: 13/13. It imports desk_identity.
- `service.desk_memory_runtime`: 13/23. It imports desk_identity, desk_binding, episodic_memory and desk_registry.
- `code_references.models`: 7/8.
- `service.session_sources`: 7/13.
- `service.desk_identity`: 6/6.

**Function-local imports that exist only to reach these helpers.** A leaf would remove all of them:
- desk_registry.py:43 imports `private_json`. desk_memory_runtime.py:16 imports desk_registry at top level, so a top-level import here would be a cycle.
- desk_registry.py:259 imports `_bytes` and `_id`.
- assistant_memory_policy.py:21 and harness_profiles.py:169 import `private_json`.
- native_history_import.py:384 imports `_id`.
- episodic_search.py:211 imports `_sha`.

**Candidate path: `packages/tooling/src/kp_agent_tooling/_impl/leaf.py`.** Everything it needs is stdlib: json, hashlib, os, stat, tempfile, sqlite3, fcntl, contextlib, pathlib and re. Nothing it would need imports a product module.
- `kp_agent_tooling/__init__.py` and `_impl/__init__.py` are docstring-only, so importing the leaf runs nothing else.
- The ops extension already imports the core at top level (for example legacy_desk_import.py:16 and portable_knowledge.py:5).
- tests/boundary/test_t1_p1_core_closed.py forbids only core→ops imports.
- tests/test_import_closure.py only requires imports to resolve, so a new module satisfies it.

**Constraints:**
1. D/container.py is stdlib-only today. It is installed as `tooling-container` (deploy/Dockerfile:114) in product-base, where kp-agent-tooling is installed (Dockerfile:101-105), so it *can* import the leaf, but that would be a policy change.
2. The runtime_install and dependency_identity embedded scripts must stay stdlib-only (runtime_install.py:1118-1119).
3. tests/t10_instruments.py:298-341 wraps `sqlite3.connect` on `sqlite3`, on `sqlite3.dbapi2` and on any `kp_agent_tooling*` module attribute named `connect`. It injects `factory=` and `cached_statements=0` unless the caller passes them. So `sqlite_open` must call `sqlite3.connect` by attribute at call time and must not pass `factory` or `cached_statements`. Any pragma added per connection appears in T10 traces: P1 compares counts between runs and allows at most one `quick_check` per database per call.

---

## Tests (counts and paths only)

**Test files defining their own canonical-JSON or `sqlite3.connect` helper: 8.**
- tests/install/t9b_harness.py
- tests/t10_measure.py
- tests/t10_oracle.py (canonical `_id`:25 and connect helpers)
- tests/t10_tamper.py
- tests/t10_world.py (`canonical`:39 and connect helpers)
- tests/test_episodic_search_incremental.py
- tests/test_t10_p2_no_scans.py
- tests/test_t10_p6_upgrade.py

**Of those, canonical-JSON wrapper helpers: 2 files** — t10_world.py and t10_oracle.py. Two more files use canonical dumps inline in non-test functions: t10_tamper.py and tests/fixtures/t10-read-path-projection/generate.py.

**Files with any `sqlite3.connect`:** 17 by AST (46 calls), or 19 by grep (54 lines, which includes embedded script strings in t9b_harness.py, test_t9b_p3_migration_image.py and t10_instruments.py).

---

## Proposed API (not implemented)

Module: `kp_agent_tooling/_impl/leaf.py`, stdlib-only.

```python
# (a) Two flags cover the four observed output variants: ascii × allow_nan.
def canonical_json(value, *, ascii: bool = True, allow_nan: bool = False) -> str
def canonical_bytes(value, *, ascii: bool = True, allow_nan: bool = False) -> bytes   # .encode('utf-8')
def sorted_json(value) -> str   # legacy sort_keys-only form used in persisted digests (commit_rationale etc.)

# (b)
def sha256_hex(data: bytes | str) -> str            # str → utf-8
def sha256_file(path) -> str
def canonical_sha256(value, *, ascii=True, allow_nan=False) -> str
def content_id(prefix: str, value, **canonical_kw) -> str   # f'{prefix}:sha256:{…}' (episodic_memory._id, models.canonical_digest, revision._sha256_identity)

# (c)
def read_private_json(path, *, max_bytes=262_144)                 # desk_memory_runtime._private_json semantics
def ensure_private_dir(path) -> Path                              # launch_binding.private_dir semantics
def write_private_new(path, data: bytes, *, mode=0o600) -> None   # O_CREAT|O_EXCL|O_NOFOLLOW|O_CLOEXEC + fsync; never overwrite
def publish_private_once(path, data: bytes) -> bool              # temp + fsync + os.link; identical existing → False; different → raise
def replace_private(path, data: bytes, *, mode=0o600) -> None    # temp (EXCL|NOFOLLOW) + fsync + chmod + os.replace
@contextmanager
def private_lock(path)                                           # O_RDWR|O_CREAT|O_NOFOLLOW 0o600 + flock

# (d)
def sqlite_open(path, mode: Literal['create', 'rw', 'ro'], *, timeout: float | None = None) -> sqlite3.Connection
    # One URI spelling (Path(path).resolve().as_uri() + '?mode=…', uri=True). No implicit pragmas.
    # Calls sqlite3.connect by attribute (T10). 'create' pre-creates the file via write_private_new(path, b'').
def sqlite_attach_ro(db, path, alias) -> None

# (e) / (h)
STORE_ROOT = PurePosixPath('/state/memory')
STORE_KEYS = ('state_root', 'roster_path')
TEMPLATE_KEY = 'config_template'
LEGACY_STORES = ('registry', 'assistant', 'desk-memory')
def required_store_path(key, value) -> str | None   # replaces container.outside_store and runtime_install.required_store_path
EPISODES_DB = 'episodes.sqlite3'; SESSIONS_DB = 'sessions.sqlite3'; SEARCH_INDEX_DB = 'episode-search.sqlite3'
WORKSPACE_CAPTURE_DB = 'workspace-capture.sqlite3'; SESSION_IMPORT_JOBS_DB = 'session-import-jobs.sqlite3'
SPOOL_INGEST_DB = 'spool-ingest.sqlite3'   # plus capture/queue/telemetry/rollout-capture
def store_file(state_root, name) -> Path
```

**Behaviour changes the Principal should rule on:**
- **`allow_nan`:** variants A, B and F currently allow NaN (38 sites), while C, D and E refuse it. Making refusal the default changes behaviour only for NaN or Infinity inputs.
- **`.resolve()` in URIs:** 13 URI sites do not resolve today; resolving everywhere would change how symlinked roots behave.
- **fsync:** `write_private` (launch_binding.py:61), `model_gateway._write_private` (:466) and `persist_host_selection` (desk_memory_runtime.py:333-341) gain fsync.

---

## Guard-test design

`tests/test_t11_leaf_single_home.py` would:
- Walk `git ls-files` product `.py` files with `ast`, skipping `_impl/leaf.py`.
- Resolve aliases per file: `import json as X`, `from json import dumps as Y`, likewise for hashlib and sqlite3. It must not match bare names, because runtime_install.py:170 defines its own `sha256`.
- Parse string constants that contain an `import` statement as embedded code, and check them against a frozen allowlist (runtime_install `_INSPECT`, `_OWN`, `_CLEAR`, `_STORE_LIB`, `_MIGRATE`; dependency_identity:124).
- Assert each category count outside the leaf equals 0. During migration it can assert against a ratcheting baseline.

**Patterns per category:**

- **(a)**
  - Site: a `Call` to `json.dumps` or `json.dump` with keyword `sort_keys=Constant(True)` and `separators=Tuple(Constant(','), Constant(':'))`.
  - Second pattern: `sort_keys=True` dumps nested under `hashlib.sha256(<dumps>.encode())`, which catches the non-compact identity forms.
  - Definition: a `FunctionDef` with at most 3 body statements (docstring ignored) whose `Return` contains such a call.
  - Expected false positives: none for the strict pattern. The second pattern hits CLI display output only if it is hashed. Compact-without-sort wire encodings (tool_delivery.py:38, workspace_setup_server.py:93, episodic_summarizer.py:325/343) are not matched. The two `codex_trust_entry` sites need `ascii=False` support in the leaf, or an allowlist entry.
- **(b)**
  - Site: a `Call` that resolves to `hashlib.sha256`.
  - Definition: a `FunctionDef` with at most 3 statements returning `…sha256(…).hexdigest()`, optionally behind a string prefix (`BinOp(Add)` or `JoinedStr`).
  - Expected false positives: streaming `hashlib.sha256()` with `.update` (the 8 sites in (b)), the empty-digest constants (launch_binding.py:508, spool_ingest.py:101), and domain hashers that keep their schemes but should call `leaf.sha256_hex`. Allow zero-argument `sha256()` calls.
- **(c)**
  - Sites:
    - `os.open` whose flags `BinOp` contains `O_CREAT`
    - `Attribute(attr='O_NOFOLLOW')` or `getattr(os, 'O_NOFOLLOW', …)`
    - `open` or `*.open` whose mode `Constant` contains `x`
    - `os.chmod`, `*.chmod` or `os.fchmod` whose mode argument is `Constant` in {0o600, 0o700, 0o400} or `Name` in {FILE_MODE, DIRECTORY_MODE}
    - `*.mkdir(mode=0o700)`, `os.mkdir(_, 0o700)`, `os.makedirs(mode=…)`
    - `tempfile.mkstemp`, `tempfile.NamedTemporaryFile`, `tempfile.mkdtemp`
    - `os.replace`, `os.link`, `os.fdopen`
    - `BinOp(BitAnd, Attribute(attr='st_mode'), Constant(0o077))`
  - Expected false positives:
    - Bare 384 or 63 literals (store.py:641 and embedders.py:20 are dimensions; embedders.py:82-83 are a bit shift), so match modes only in argument position.
    - Directory renames that are not private files (model_gateway.py:488, refresh_retention.py:211, runtime_install.py:1664, 1673).
    - The 0o400 read-only delivery at tool_delivery.py:57.
    - `Path.replace` cannot be told apart from `str.replace` by AST, so match only `os.replace`; there are no `Path.replace` sites today.
- **(d)**
  - Sites: a `Call` resolving to `sqlite3.connect`; a `keyword(arg='uri')`; strings or `JoinedStr` containing `?mode=`; `Constant` matching `(?i)^\s*pragma\b` passed to `.execute` or `.executescript`, or inside an executescript literal (episodic_queue.py:56); `Constant` matching `(?i)^\s*attach\b`.
  - Expected false positives: the `:memory:` scratch connections (model_gateway.py:371, workspace_capture.py:276), the embedded runtime_install connects (allowlist them), and integrity pragmas (`quick_check`, `table_info`). Decide whether "pragma" in this category means configuration pragmas only.
- **(e)**
  - Sites: `Subscript(slice=Constant('state_root' | 'roster_path' | 'config_template'))`; `Constant('/state/memory')`; definitions named `outside_store` or `required_store_path`.
  - Expected false positives: docstrings (exclude the first `Expr` `Constant` of a body); the key-set literal at desk_memory_runtime.py:222 and the dict key at desk_catalog_setup.py:195 are not `Subscript`, so they are not matched.
- **(h)**
  - Sites: a `Constant` string matching `[\w-]+\.sqlite3`, plus `BinOp(Add, *, Constant('.sqlite3'))`, which catches assistant_host_cli.py:56.
  - Expected false positives: the spool_ingest.py:19 docstring and the runtime_install.py:1214e/1253e suffix tests, which are embedded and allowlisted.
- **(f)**
  - For every product `FunctionDef` with at least 15 statements, take `ast.dump` per statement after renaming local aliases (for example `conflict` ↔ `RolloutCaptureConflict`) and dropping string constants. Fail when two functions share at least 90% of their statements under `difflib.SequenceMatcher`.
  - Expected false positives: Protocol stubs (excluded by the size floor) and intentionally parallel `initialize`/`_db` pairs, which are well under the floor.
- **(g)**
  - Reuse the method in (g): Name, Attribute and alias references plus string-literal tokens across all tracked `.py` files, word tokens across tracked non-`.py` files, a fixpoint over dead spans, and an allowlist of `{do_GET, do_POST, log_message, get_request}`.
  - Expected false positives: framework hooks and Protocol stubs, which need either annotations or an allowlist.
