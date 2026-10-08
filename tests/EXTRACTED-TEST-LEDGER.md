# Extracted-module OPS test ledger (order S5)

Source: OPS `tests/` at the extraction commit. Order: `docs/work/orders/S5-restore-extracted-tests.md` at its freeze commit.
Every OPS test file that names an extracted module appears below exactly once, with one status: `ported`, `excluded`, `already-covered` or `covered-by-S1`.

## Locations after AT-0004 T1

This ledger records order S5. AT-0004 T1 ([order](../docs/work/orders/T1-extension-seam.md)) later split the tests:
- Every test that imported a module T1 moved to the OPS extension moved to `extensions/ops/tests/`, and the paths below name that location. The other ported and already-covered tests stay in `tests/`.
- T1 deleted only tests whose sole subject is a `REMOVE` module in [T1-module-dispositions.csv](../docs/work/inventory/T1-module-dispositions.csv). The rows that lost tests say so: `test_package_lineage.py` (the whole ported file), `test_review_ledger.py` (3 tests) and `test_knowledge_withdrawal.py` (1 test).
- The counts and "collected" figures are as recorded at S5.
- A path written directly after `OPS` or `OPS-only` is a path in the OPS source repository at the extraction commit, not in this tree. `tests/docs/test_doc_paths.py` checks that every other path here exists.

## Rules applied

- **Extracted module.** A module file under `packages/tooling/src/kp_agent_tooling/_impl` whose source came from OPS `kp_ops/`. It includes `code_references/__init__.py`.
  - It also includes the CLIs that OPS `portable_tooling/setup.py` wrote from OPS scripts: `agent_tooling_cli`→`cli`, `episodic_memory_cli`→`memory_cli`, `episodic_queue_cli`→`queue_cli`, `claude_memory_hook`→`claude_hook_cli`, `refresh_navigation`→`refresh_cli`, `workspace_setup_cli`→`setup_cli`, `capture_trial_responses`→`capture_cli`, `extract_host_transcript_calls`→`transcript_cli`.
  - The package `__init__` files that setup.py synthesized (`_impl`, `service`, `embeddings`, `queries`) are not extracted modules.
- **Direct scope (129 files).** The test imports an extracted module, patches one by string, reads one by path, or loads an extracted CLI script.
- **Indirect scope (24 files).** Listed only so that P1 holds under the broader reading "names an extracted module". Every file here is excluded.
  - Some reach an extracted module only through an OPS package re-export (`from kp_ops.embeddings import DeterministicEmbedder`).
  - Some go through an OPS-only script, or name one only as a file name.
  - One (`test_knowledge_navigation.py`) targets a module that was extracted and later retired.
- **Port.** `kp_ops.` → `kp_agent_tooling._impl.` in import statements, `importlib`/`monkeypatch` module strings and extracted-CLI imports. Extracted-CLI script paths are relocated (list below). No assertion was edited.
- **Excluded.** The file needs something that exists only in OPS: the graph or board, the legacy gateway, `desk_memory*` legacy, PostgreSQL, `kp_core`, another OPS-only `kp_ops` module, an OPS-only script, or OPS `records/`/`migrations/` data.
  - Per the Coordinator's A2 ruling, this means the test's subject or its assertions need OPS-only product code. Borrowing a self-contained test fixture does not count.
  - A borrowed helper that itself builds the OPS-only system under test (kp_core, the OPS graph) still counts.
  - Inside a ported file, a test with such a need is kept verbatim and marked `@pytest.mark.skip(reason="S5 excluded: …")`. Each one is listed under EXCLUDED TESTS.
- **Divergence.** A ported test that fails for any other reason is kept verbatim and marked `@pytest.mark.xfail(strict=True, reason="S5 divergence: …")`. Each one is listed under DIVERGENCES.
  - Each divergence was also run against the OPS source tree at the extraction commit (a `git archive` copy, same interpreter). The result is recorded to separate pre-existing OPS red from agent-tooling change.

## Counts

- Ledger rows: 153 (129 direct + 24 indirect).
- ported 64, excluded 57, already-covered 30, covered-by-S1 2.
- Ported-file test items: 554.
  - 482 pass.
  - 24 are strict xfail (20 test functions): DIVERGENCES.
  - 38 are excluded-test skips: EXCLUDED TESTS.
  - 8 are skips inherited from the OPS originals: INHERITED OPS SKIPS.
  - 2 are covered-by-S1 skips: COVERED-BY-S1 TESTS.

## Direct scope

| OPS file | extracted modules named | status | detail |
| --- | --- | --- | --- |
| test_agent_tooling.py | dependency_identity, observation_contract, observations, service.agent_tooling, service.import_context | ported: extensions/ops/tests/test_agent_tooling.py | 18 collected |
| test_agent_tooling_stdio_text.py | tool_delivery | ported: extensions/ops/tests/test_agent_tooling_stdio_text.py | 1 collected; mechanical edit: OPS 'scripts/agent_tooling_cli.py' -> 'packages/tooling/src/kp_agent_tooling/cli.py' |
| test_behavior_model.py | behavior_model | excluded | kp_core MemoryAdapter and the OPS graph kp_ops.persist.graph, imported at module level |
| test_claim_evidence.py | source_citations | excluded | its subject, kp_ops.claim_evidence, is OPS-only (module-level import). The borrowed repo fixture is now available from tests/ops_repo_fixture.py, which does not change this |
| test_claude_capture_integration.py | service.claude_episode_capture, service.desk_memory_runtime, service.episodic_memory, service.episodic_memory_tools, service.episodic_queue | already-covered: tests/test_claude_capture_integration.py | identical after rename |
| test_claude_episode_capture.py | service.claude_episode_capture | already-covered: tests/test_claude_episode_capture.py | same assertions plus one; rotation uses rename instead of unlink, and 4 tests are added. The OPS form also passes here |
| test_claude_memory_hook.py | service.claude_memory_hook, service.episodic_memory_tools | already-covered: extensions/ops/tests/test_claude_memory_hook.py | same assertions plus two; the CLI test drops the external-volume-mount simulation that setup.py removed from claude_hook_cli (see ADAPTED EQUIVALENTS) |
| test_code_reference_cli.py | code_references.manifest | excluded | kp_core, the OPS graph kp_ops.persist.graph and OPS-only scripts/desk_memory_cli.py |
| test_code_reference_population.py | code_references.population | excluded | kp_core, the OPS graph kp_ops.persist.graph and kp_ops.connectors.s5_git |
| test_code_reference_publication.py | code_references.manifest | ported: extensions/ops/tests/test_code_reference_publication.py | 8 collected |
| test_code_reference_publication_postgres.py | code_references.indexing, code_references.manifest, code_references.models | excluded | PostgreSQL, kp_core and the OPS graph kp_ops.persist.graph |
| test_code_reference_retrieval.py | code_references.adapters, code_references.extraction, code_references.manifest, code_references.resolution, navigation_snapshot, navigation_workspace, service.agent_tooling, service.knowledge | already-covered: extensions/ops/tests/test_code_reference_retrieval.py | service assertions identical; the MCP half of test_enabled_real_service_and_mcp_select_exact_manifest (kp_ops.service.desk_documents, kp_ops.service.app, fastapi) is OPS-only and not carried |
| test_code_reference_retrieval_mutations.py | code_references.adapters, code_references.extraction, code_references.manifest, code_references.resolution, code_references.retrieval, service.knowledge_context | already-covered: extensions/ops/tests/test_code_reference_retrieval_mutations.py | identical after rename |
| test_code_reference_telemetry.py | service.knowledge_telemetry, service.telemetry_runtime | ported: extensions/ops/tests/test_code_reference_telemetry.py | 14 collected |
| test_code_reference_telemetry_otel.py | service.knowledge_telemetry | ported: extensions/ops/tests/test_code_reference_telemetry_otel.py | whole module skips here: OPS importorskip('opentelemetry.sdk') (2 tests) |
| test_code_references.py | code_references.extraction, code_references.indexing, code_references.manifest, code_references.models, code_references.registry, service.knowledge | ported: extensions/ops/tests/test_code_references.py | 15 collected |
| test_commit_memory_evidence.py | service.episodic_memory_tools | excluded | OPS-only scripts/commit_session_attribution.py |
| test_commit_rationale.py | commit_rationale, tool_discovery | already-covered: extensions/ops/tests/test_commit_rationale.py | identical after rename |
| test_controlled_failure_records.py | observations | excluded | OPS-only records/claude-review-lifecycle-failures (register.py module and retained registry) |
| test_cross_repo_imports.py | navigation_workspace, service.agent_tooling, service.import_context | ported: tests/test_cross_repo_imports.py | 23 collected |
| test_dependency_identity.py | dependency_identity, source_citations | ported: tests/test_dependency_identity.py | 5 collected |
| test_desk_catalog_setup.py | service.desk_catalog_setup, service.desk_memory_runtime, service.workspace_context | already-covered: tests/test_desk_catalog_setup.py | OPS tests identical; agent-tooling adds one test |
| test_desk_doc_corpus.py | embeddings.desk_docs, embeddings.revision | excluded | kp_core, the OPS graph (kp_ops.persist), OPS-only embeddings cards, desk_notes and desk_bookends, kp_ops.service.desk_documents, and OPS scripts/desk_memory_cli.py |
| test_desk_memory_serving_diagnostics.py | embeddings.embedders | ported: tests/test_desk_memory_serving_diagnostics.py | 3 collected; 2 excluded-test skip |
| test_desk_memory_session_surface.py | embeddings.embedders, embeddings.store | excluded | kp_core, desk_memory* legacy (desk_memory, desk_memory_daemon, desk_memory_lifecycle, desk_run, binding_registry) and PostgreSQL |
| test_desk_session_hook.py | service.desk_memory_runtime | already-covered: extensions/ops/tests/test_desk_session_hook.py | identical after rename |
| test_doc_code_reference_acceptance.py | code_references.adapters, code_references.extraction, code_references.manifest, code_references.resolution, code_references.retrieval | excluded | borrows system/enrich from test_doc_code_reference_contracts. That helper is not self-contained: it builds a kp_core MemoryAdapter, the OPS graph, desk_documents ingestion and an OPS embedder/store, and the assertions run against that system (A2 ruling: still excluded) |
| test_doc_code_reference_contracts.py | code_references.adapters, code_references.indexing, code_references.manifest, code_references.models, code_references.population, code_references.resolution, code_references.retrieval, service.agent_tooling, service.knowledge, service.knowledge_telemetry | excluded | kp_core, the OPS graph kp_ops.persist.graph, kp_ops.service.desk_documents and OPS-only scripts/doc_code_reference_scenarios.py |
| test_doc_code_reference_delivery.py | code_references.extraction, code_references.models, code_references.population, code_references.resolution | ported: extensions/ops/tests/test_doc_code_reference_delivery.py | 21 collected; 12 excluded-test skip |
| test_docker_deployment.py | service.agent_tooling, service.desk_memory_runtime, service.gateway_transport | ported: extensions/ops/tests/test_docker_deployment.py | 4 collected; mechanical edit: the `python -c` program's import statements `import kp_ops.service.…` -> `import kp_agent_tooling._impl.service.…` |
| test_document_token_coverage.py | embeddings.complete_text, embeddings.desk_docs, embeddings.embedders | excluded | borrows _api/_memory_system/_fixture_repo/_ingest from test_desk_doc_corpus. Those helpers build the kp_core/OPS-graph desk-document system under test (A2 ruling: still excluded) |
| test_document_token_review.py | embeddings.complete_text | excluded | OPS-only kp_ops.embeddings.cards (board/card); imports test_document_token_coverage |
| test_episodic_capture_policy.py | service.episodic_capture_policy | ported: tests/test_episodic_capture_policy.py | 14 collected |
| test_episodic_cross_desk_reads.py | service.episodic_memory, service.episodic_memory_tools | ported: tests/test_episodic_cross_desk_reads.py | 3 collected |
| test_episodic_handoff.py | service.episodic_handoff, service.episodic_memory, service.episodic_memory_tools | already-covered: tests/test_episodic_handoff.py | identical after rename |
| test_episodic_memory.py | service.episodic_memory | already-covered: tests/test_episodic_memory.py | identical after rename |
| test_episodic_memory_tools.py | service.desk_memory_runtime, service.episodic_memory, service.episodic_memory_tools | already-covered: tests/test_episodic_memory_tools.py | same assertions; OPS scripts/episodic_memory_cli.py relocated to kp_agent_tooling.memory_cli (2 tests) |
| test_episodic_queue.py | service.episodic_memory_tools, service.episodic_queue, service.memory_budget | already-covered: tests/test_episodic_queue.py | same assertions; the CLI test drops the external-volume-mount simulation that setup.py removed from queue_cli (see ADAPTED EQUIVALENTS) |
| test_episodic_runtime_setup.py | service.episodic_summarizer, service.memory_budget | excluded | OPS-only scripts/episodic_runtime_setup.py, OPS scripts/opencode_observer.py, kp_ops.service.opencode_compaction and test_desk_memory_claims (kp_core) |
| test_episodic_search.py | service.episodic_memory, service.episodic_search | already-covered: tests/test_episodic_search.py | identical after rename |
| test_episodic_summarizer.py | service.episodic_summarizer, service.memory_budget, service.summary_contract | already-covered: tests/test_episodic_summarizer.py | same assertions; helper configured() observes the profile 1 minute earlier. The OPS helper also passes here |
| test_evidence_references.py | evidence_references, observations, verification_packet | ported: extensions/ops/tests/test_evidence_references.py | 5 collected |
| test_feature_intake_enrichment.py | embeddings.store | excluded | OPS-only feature_intake* services (graph/board), kp_ops.embeddings.retrieval and kp_ops.service.graph_packs |
| test_feature_intake_production_instruments.py | embeddings.embedders | excluded | kp_core, the OPS graph, feature_intake* services and PostgreSQL |
| test_feature_intake_vector_chunk_bound.py | embeddings.store | excluded | OPS-only kp_ops.embeddings.retrieval, feature_intake_enrichment(_postgres) and PostgreSQL |
| test_gateway_failures.py | service.agent_tooling, service.gateway_transport | ported: extensions/ops/tests/test_gateway_failures.py | 24 collected |
| test_generic_journeys.py | evidence_references, journey_registry, observations, verification_finding, verification_packet, verification_plan | excluded | OPS-only kp_ops.synthetic_evidence, imported at module level |
| test_host_card_bridge.py | service.desk_binding, service.desk_memory_runtime, service.episodic_memory, service.episodic_memory_tools, service.episodic_search, service.host_card_bridge, service.session_sources | ported: tests/test_host_card_bridge.py | 9 collected |
| test_host_transcript_capture.py | host_transcript_capture, trial_response_capture | ported: extensions/ops/tests/test_host_transcript_capture.py | 9 collected |
| test_import_context.py | service.import_context | ported: tests/test_import_context.py | 7 collected |
| test_imported_desk_runtime.py | service.desk_identity, service.desk_memory_runtime, service.episodic_search, service.legacy_desk_import | already-covered: extensions/ops/tests/test_imported_desk_runtime.py | identical after rename |
| test_knowledge_catalog_states.py | service.knowledge | already-covered: extensions/ops/tests/test_knowledge_catalog_states.py | identical after rename (its fastapi test skips in both) |
| test_knowledge_context.py | service.knowledge, service.knowledge_context, service.serena_navigation | already-covered: extensions/ops/tests/test_knowledge_context.py | 16 of 18 OPS tests identical; test_context_cli_matches_shared_service (OPS-only scripts/knowledge_cli.py) and test_context_mcp_real_composition_and_authority (legacy app, fastapi) are OPS-only |
| test_knowledge_lifecycle.py | service.knowledge | ported: extensions/ops/tests/test_knowledge_lifecycle.py | 11 collected; 3 excluded-test skip |
| test_knowledge_mcp.py | service.knowledge, service.knowledge_operations | excluded | the legacy gateway (knowledge_mcp, mcp_gateway, mcp_transport, service.app), fastapi and kp_core |
| test_knowledge_service.py | service.knowledge, tool_discovery | already-covered: extensions/ops/tests/test_knowledge_service.py | 17 of 20 OPS tests identical; test_reference_manifest_is_read_from_configured_revision, test_cli_uses_shared_service_and_stays_lazy and test_cli_subprocess_discovery need OPS-only scripts/knowledge_cli.py |
| test_knowledge_telemetry.py | service.knowledge_telemetry | ported: extensions/ops/tests/test_knowledge_telemetry.py | whole module skips here: OPS importorskip('opentelemetry.sdk') (3 tests) |
| test_knowledge_withdrawal.py | embeddings.store, service.knowledge, service.knowledge_lifecycle, service.knowledge_lifecycle_transport | already-covered: extensions/ops/tests/test_knowledge_withdrawal.py | 11 of 12 OPS tests identical; test_ingestion_refuses_withdrawn_blob_before_backend needs OPS-only scripts/desk_memory_cli.py and test_desk_doc_corpus (kp_core). T1 deleted test_withdrawal_waits_for_final_asgi_response_send, whose subject, `knowledge_lifecycle_transport`, is a `REMOVE` module |
| test_launch_claude_desk_memory.py | service.desk_binding, service.desk_identity, service.desk_memory_runtime, service.episodic_memory | ported: extensions/ops/tests/test_launch_claude_desk_memory_ops_original.py | 2 OPS tests whose tool-list assertions agent-tooling rewrote ported unchanged as strict xfail; the other 14 are in extensions/ops/tests/test_launch_claude_desk_memory.py (identical, or the desk_cli.py path relocated) |
| test_legacy_desk_import.py | service.desk_identity, service.episodic_provenance, service.episodic_search, service.legacy_desk_import | already-covered: extensions/ops/tests/test_legacy_desk_import.py | identical after rename |
| test_lifecycle_matrix.py | behavior_model, lifecycle_matrix | ported: extensions/ops/tests/test_lifecycle_matrix.py | 5 collected; 1 inherited OPS skip |
| test_memory_budget.py | service.episodic_memory, service.memory_budget | ported: tests/test_memory_budget_ops_original.py | 3 OPS tests whose inputs agent-tooling changed (context_length 1000/1500/1200 -> 2400) ported unchanged as strict xfail; the other 9 are identical in tests/test_memory_budget.py (already-covered) |
| test_memory_eval_boundaries.py | embeddings.store | excluded | kp_core, desk_memory* legacy and the OPS graph |
| test_native_history_import.py | service.episodic_memory, service.episodic_search, service.native_history_import, service.session_sources | already-covered: tests/test_native_history_import.py | identical after rename |
| test_navigation_discovery_snapshot.py | navigation_discovery, navigation_snapshot | ported: tests/test_navigation_discovery_snapshot.py | 10 collected |
| test_navigation_environment.py | navigation_environment | ported: tests/test_navigation_environment.py | 3 collected |
| test_navigation_incremental_refresh.py | embeddings.embedders, repository_manifest, scip_navigation, semantic_index | ported: tests/test_navigation_incremental_refresh.py | 23 collected; 1 excluded-test skip; mechanical edit: OPS 'scripts/refresh_navigation.py' -> 'packages/tooling/src/kp_agent_tooling/refresh_cli.py' |
| test_navigation_integration.py | navigation_discovery, review_ledger, service.agent_tooling | ported: extensions/ops/tests/test_navigation_integration.py | 8 collected; mechanical edit: OPS 'scripts/agent_tooling_cli.py' -> 'packages/tooling/src/kp_agent_tooling/cli.py' |
| test_navigation_limits_l1.py | navigation_search_pages | ported: tests/test_navigation_limits_l1.py | 3 collected |
| test_navigation_limits_l3.py | navigation_search_pages, service.agent_tooling, service.repository_coverage | ported: tests/test_navigation_limits_l3.py | 7 collected |
| test_navigation_pattern_errors.py | navigation_discovery, service.agent_tooling | ported: tests/test_navigation_pattern_errors.py | 8 collected; mechanical edit: OPS 'scripts/agent_tooling_cli.py' -> 'packages/tooling/src/kp_agent_tooling/cli.py' |
| test_navigation_refresh.py | scip_navigation | ported: tests/test_navigation_refresh.py | 5 collected; mechanical edit: OPS 'scripts/refresh_navigation.py' -> 'packages/tooling/src/kp_agent_tooling/refresh_cli.py' (3 places) |
| test_navigation_search_pages.py | git_batch, navigation_search_pages, repository_manifest | already-covered: tests/test_navigation_search_pages.py | identical after rename |
| test_navigation_workspace.py | navigation_workspace, service.agent_tooling, verification_finding | ported: extensions/ops/tests/test_navigation_workspace.py | 26 collected |
| test_observations.py | observation_adapters, observations | ported: extensions/ops/tests/test_observations.py | 16 collected; 1 excluded-test skip |
| test_opencode_compaction_factory.py | service.episodic_memory, service.memory_budget | excluded | OPS-only kp_ops.service.opencode_compaction, OPS scripts/opencode_observer.py and test_desk_memory_claims (kp_core) |
| test_opencode_desk_launch.py | embeddings.embedders, embeddings.store | excluded | fastapi, kp_core, desk_memory* legacy, opencode_* services and PostgreSQL |
| test_openrouter_models.py | service.openrouter_models | ported: tests/test_openrouter_models.py | 4 collected |
| test_operation_navigation.py | source_citations | ported: tests/test_operation_navigation.py | 12 collected; 1 excluded-test skip; ported under the A2 ruling for its source_citations tests; mechanical edit: the inline repo fixture (OPS lines 7-14) is replaced by `from ops_repo_fixture import repo`, which holds it verbatim. The module-level `from kp_agent_tooling._impl.operation_navigation import project, validate_view` is commented out with an S5 note, because that module is OPS-only |
| test_package_lineage.py | package_lineage | ported; deleted by T1 | 2 collected at S5. T1 deleted the ported file because its only subject, `package_lineage`, is a `REMOVE` module |
| test_platform_entry.py | local_knowledge, service.knowledge | covered-by-S1 | named by the order as S1 territory (knowledge.platform / knowledge_coverage) |
| test_platform_mcp_trace.py | service.knowledge | covered-by-S1 | its only test calls knowledge.platform (knowledge_coverage) and imports test_platform_entry; it would also be excluded (legacy app kp_ops.service.app, knowledge_trace_middleware, fastapi) |
| test_platform_snapshot.py | platform_snapshot | ported: tests/test_platform_snapshot.py | 6 collected; ported under the A2 ruling; mechanical edit: `from test_operation_navigation import repo` -> `from ops_repo_fixture import repo` (A2 ruling) |
| test_portable_desk_memory.py | service.claude_episode_capture, service.claude_memory_hook, service.desk_binding, service.desk_identity, service.desk_memory_runtime, service.episodic_queue, service.episodic_summarizer | already-covered: tests/test_portable_desk_memory.py | identical after rename |
| test_portable_gateway_endpoint.py | service.agent_tooling, service.gateway_transport | ported: extensions/ops/tests/test_portable_gateway_endpoint.py | 8 collected |
| test_portable_tooling_wheel.py | scip_navigation, service.agent_tooling | ported: tests/test_portable_tooling_wheel.py | 3 collected; 2 excluded-test skip |
| test_published_navigation.py | scip_navigation, service.knowledge, service.published_navigation | ported: extensions/ops/tests/test_published_navigation.py | 7 collected; A2 ruling: ported unchanged (gets repo through test_scip_navigation) |
| test_python_scip_refresh.py | refresh_cli (OPS scripts/refresh_navigation.py) | ported: tests/test_python_scip_refresh.py | 8 collected; mechanical edit: OPS 'scripts/refresh_navigation.py' -> 'packages/tooling/src/kp_agent_tooling/refresh_cli.py' |
| test_rag_cp31_extraction.py | embeddings.embedders, embeddings.revision, embeddings.store | excluded | OPS-only kp_ops.embeddings.chunks and kp_ops.embeddings.pipeline |
| test_rag_cp3_embeddings.py | embeddings.embedders | excluded | OPS-only scripts/embed_chunks.py, re-exports from OPS-only embeddings.chunks and embeddings.pipeline, and a PostgreSQL migration |
| test_rationale_tooling.py | commit_rationale, service.agent_tooling | ported: extensions/ops/tests/test_rationale_tooling.py | 10 collected |
| test_reference_bounds_continuation.py | code_references.adapters, code_references.manifest, code_references.retrieval, service.knowledge, service.knowledge_context | ported: extensions/ops/tests/test_reference_bounds_continuation.py | 9 collected; 5 strict xfail (DIVERGENCES); 1 excluded-test skip |
| test_reference_live_fixes.py | code_references.diagnostics, code_references.manifest, code_references.recovery, code_references.retrieval, service.agent_tooling | excluded | the legacy gateway (knowledge_mcp, mcp_gateway, mcp_transport), fastapi, kp_core and OPS scripts/doc_code_reference_scenarios.py |
| test_reference_source_facts.py | code_references, code_references.population, code_references.source_facts, scip_navigation | excluded | borrows fixture from test_code_reference_population. That helper returns build_graph(kp_core MemoryAdapter), and the population under test writes into that OPS graph (A2 ruling: still excluded) |
| test_repository_coverage_acceptance.py | navigation_search_pages, repository_manifest | ported: tests/test_repository_coverage_acceptance.py | 6 collected |
| test_repository_coverage_adapter.py | service.agent_tooling | ported: tests/test_repository_coverage_adapter.py | 1 collected; mechanical edit: OPS 'scripts/agent_tooling_cli.py' -> 'packages/tooling/src/kp_agent_tooling/cli.py' |
| test_repository_manifest.py | repository_manifest | already-covered: tests/test_repository_manifest.py | identical after rename |
| test_review_ledger.py | opencode_answer, review_ledger | ported: extensions/ops/tests/test_review_ledger.py | 8 collected at S5; 1 excluded-test skip; fixture extensions/ops/tests/fixtures_opencode_trial66_sanitized.json copied. T1 deleted 3 tests whose subject, `opencode_answer`, is a `REMOVE` module: test_opencode_extracts_only_terminal_text_from_trial_shape, test_sanitized_trial66_timeline_yields_only_last_answer, test_newer_user_turn_rejects_stale_answer (5 remain) |
| test_scip_continuation.py | scip_navigation, service.knowledge | ported: extensions/ops/tests/test_scip_continuation.py | 5 collected; 1 excluded-test skip; 2 covered-by-S1 test skip; A2 ruling: ported unchanged (gets repo through test_scip_navigation) |
| test_scip_navigation.py | scip_navigation, service.knowledge | ported: extensions/ops/tests/test_scip_navigation.py | 11 collected; 2 excluded-test skip; ported under the A2 ruling; mechanical edit: `from test_operation_navigation import repo` -> `from ops_repo_fixture import repo` (A2 ruling) |
| test_semantic_index.py | embeddings.embedders, semantic_index, service.agent_tooling | already-covered: tests/test_semantic_index.py | identical after rename |
| test_serena_navigation.py | service.serena_navigation | ported: tests/test_serena_navigation.py | 34 collected; 14 strict xfail (DIVERGENCES); 1 excluded-test skip |
| test_server_identity.py | server_identity, service.agent_tooling | ported: tests/test_server_identity.py | 4 collected |
| test_session_continuation.py | embeddings.store | excluded | OPS-only dispatch/session services (dispatch_session_custody, session_context_epoch, session_continuation, session_vector_memory), the OPS graph and PostgreSQL |
| test_session_sources.py | service.episodic_memory, service.episodic_memory_tools, service.episodic_search, service.session_sources | already-covered: tests/test_session_sources.py | OPS tests identical; agent-tooling adds one test |
| test_session_vector_memory.py | embeddings.store | excluded | OPS-only kp_ops.service.session_vector_memory and session_context_epoch, and PostgreSQL |
| test_summary_contract.py | service.episodic_memory, service.memory_budget, service.summary_contract | already-covered: tests/test_summary_contract.py | identical after rename |
| test_summary_evidence.py | service.episodic_handoff, service.episodic_memory | already-covered: tests/test_summary_evidence.py | same assertions; OPS records/summary-* inputs relocated to tests/fixtures/summary-* (1 test) |
| test_summary_handoff_integration.py | service.episodic_memory, service.episodic_memory_tools | ported: tests/test_summary_handoff_integration.py | 2 collected |
| test_synthetic_evidence.py | observations | excluded | OPS-only kp_ops.synthetic_evidence |
| test_tool_delivery.py | tool_delivery | already-covered: tests/test_tool_delivery.py | identical after rename |
| test_topology_compiler.py | scip_navigation | excluded | OPS-only kp_ops.topology_compiler, test_topology_interfaces and test_topology_vocabulary |
| test_transition_evidence.py | behavior_model | excluded | OPS-only kp_ops.transition_evidence |
| test_trial_navigation_diagnostics.py | service.agent_tooling, service.repository_coverage, service.serena_navigation | ported: tests/test_trial_navigation_diagnostics.py | 4 collected |
| test_trial_response_capture.py | trial_response_capture | ported: extensions/ops/tests/test_trial_response_capture.py | 7 collected |
| test_typescript_context.py | typescript_context | ported: tests/test_typescript_context.py | 3 collected; 3 inherited OPS skip |
| test_validation_receipt_retention.py | service.agent_tooling, verification_finding | ported: extensions/ops/tests/test_validation_receipt_retention.py | 3 collected |
| test_vector_store_generalization.py | embeddings.store | ported: extensions/ops/tests/test_vector_store_generalization.py | 4 collected; 2 excluded-test skip |
| test_verification_adjacency.py | verification_adjacency, verification_packet | excluded | every test reads OPS-only records/lifecycle-30 and OPS scripts/lifecycle_workload_server.py (A4 ruling: product-scope question, not a test gap) |
| test_verification_correctness.py | verification_correctness, verification_packet | excluded | every test reads OPS-only records/lifecycle-30 (A4 ruling: product-scope question, not a test gap) |
| test_verification_cross_tool.py | evidence_references, observations, verification_finding | ported: extensions/ops/tests/test_verification_cross_tool.py | 4 collected |
| test_verification_finding.py | observations, verification_finding | ported: extensions/ops/tests/test_verification_finding.py | 24 collected |
| test_verification_handoff.py | verification_finding, verification_handoff | ported: extensions/ops/tests/test_verification_handoff.py | 2 collected |
| test_verification_packet.py | lifecycle_matrix, verification_packet | ported: extensions/ops/tests/test_verification_packet.py | 22 collected; 8 excluded-test skip |
| test_verification_plan.py | evidence_references, verification_plan | ported: extensions/ops/tests/test_verification_plan.py | 4 collected |
| test_worker_lifecycle_trial.py | lifecycle_matrix | ported: extensions/ops/tests/test_worker_lifecycle_trial.py | 3 collected; 2 inherited OPS skip |
| test_workspace_context.py | service.desk_identity, service.workspace_context | already-covered: tests/test_workspace_context.py | identical after rename |
| test_workspace_setup.py | service.agent_tooling, workspace_setup | ported: tests/test_workspace_setup.py | 15 collected; mechanical edit: OPS 'scripts/workspace_setup_cli.py' -> 'packages/tooling/src/kp_agent_tooling/setup_cli.py' |
| test_workspace_setup_server.py | workspace_setup_server | ported: tests/test_workspace_setup_server.py | 4 collected |

## Indirect scope (all excluded)

| OPS file | how it names an extracted module | status | reason |
| --- | --- | --- | --- |
| test_capability_map.py | capability_map (by name only) | excluded | runs OPS-only scripts/capability_map.py (it wraps kp_ops.capability_map and tool_discovery); imports no extracted module |
| test_card_embedding_at_the_mint_seam.py | kp_ops.embeddings re-export of embeddings.embedders, embeddings.store | excluded | imports the name through the OPS package __init__ (agent-tooling ships an empty one); also needs kp_core, the OPS graph, OPS-only embeddings.cards, write_commands and the legacy app |
| test_card_embeddings.py | kp_ops.embeddings re-export of embeddings.embedders, embeddings.store | excluded | imports the name through the OPS package __init__ (agent-tooling ships an empty one); also needs kp_core, the OPS graph and OPS-only embeddings.cards (board/card) |
| test_claude_tooling_preflight.py | agent_tooling_cli.py (fixture file name only) | excluded | tests OPS-only scripts/claude_tooling_preflight.py; agent_tooling_cli.py appears only as an empty fixture file name |
| test_composition_5a_identity_before_registration.py | kp_ops.embeddings re-export of embeddings.embedders, embeddings.store | excluded | imports the name through the OPS package __init__ (agent-tooling ships an empty one); also needs kp_core, the OPS graph, desk_memory* legacy, desk_run, binding_registry and mcp_transport |
| test_desk_entity_refs.py | kp_ops.embeddings re-export of embeddings.embedders | excluded | imports the name through the OPS package __init__ (agent-tooling ships an empty one); also needs kp_core, the OPS graph, desk_entity_refs, desk_run and binding_registry |
| test_desk_memory.py | kp_ops.embeddings re-export of embeddings.embedders, embeddings.store | excluded | imports the name through the OPS package __init__ (agent-tooling ships an empty one); also needs kp_core, the OPS graph and desk_memory* legacy (desk_memory, desk_memory_daemon, embeddings.desk_notes) |
| test_desk_memory_claims.py | kp_ops.embeddings re-export of embeddings.embedders, embeddings.store | excluded | imports the name through the OPS package __init__ (agent-tooling ships an empty one); also needs kp_core, the OPS graph and desk_memory* legacy (desk_memory_claims, desk_memory_promotion) |
| test_desk_memory_lifecycle.py | kp_ops.embeddings re-export of embeddings.embedders, embeddings.store | excluded | imports the name through the OPS package __init__ (agent-tooling ships an empty one); also needs kp_core, the OPS graph and desk_memory* legacy (desk_memory_lifecycle) |
| test_desk_memory_promotion.py | kp_ops.embeddings re-export of embeddings.embedders, embeddings.store | excluded | imports the name through the OPS package __init__ (agent-tooling ships an empty one); also needs kp_core, the OPS graph and desk_memory* legacy (desk_memory_promotion) |
| test_desk_run.py | kp_ops.embeddings re-export of embeddings.embedders, embeddings.store | excluded | imports the name through the OPS package __init__ (agent-tooling ships an empty one); also needs kp_core, the OPS graph, desk_run and embeddings.desk_bookends |
| test_desk_run_projection.py | kp_ops.embeddings re-export of embeddings.embedders, embeddings.store | excluded | imports the name through the OPS package __init__ (agent-tooling ships an empty one); also needs kp_core, the OPS graph, desk_run and binding_registry |
| test_desk_transcript_corpus.py | kp_ops.embeddings re-export of embeddings.embedders | excluded | imports the name through the OPS package __init__ (agent-tooling ships an empty one); also needs kp_core, the OPS graph, desk_run and desk_transcript_corpus |
| test_destination_assessment.py | agent_tooling_cli.py (fixture file name only) | excluded | tests OPS-only scripts/classify_agent_destination.py; agent_tooling_cli.py appears only as a fixture file name |
| test_doc_code_reference_profile.py | code_references (span names only) | excluded | imports OPS-only scripts/doc_code_reference_profile.py |
| test_document_retrieval_report.py | queries.document_retrieval_report (by file name only) | excluded | reaches the module only through OPS-only scripts/desk_memory_cli.py; imports test_desk_doc_corpus (kp_core) |
| test_feature_intake_card_evidence.py | kp_ops.embeddings re-export of embeddings.embedders, embeddings.store | excluded | imports the name through the OPS package __init__ (agent-tooling ships an empty one); also needs kp_core, the OPS graph, embeddings.cards, embeddings.retrieval and feature_intake_enrichment(_postgres) |
| test_find_similar_cards.py | kp_ops.embeddings re-export of embeddings.embedders, embeddings.store | excluded | imports the name through the OPS package __init__ (agent-tooling ships an empty one); also needs kp_core, the OPS graph, embeddings.cards and the OPS QueryRegistry |
| test_knowledge_navigation.py | service.knowledge_navigation (extracted, since retired) | excluded | kp_ops.service.knowledge_navigation was in setup.py SERVICE_MODULES but agent-tooling retired it (the commit "Retire legacy AST navigation"). It has no module under _impl, and the test imports it at module level |
| test_mint_card.py | kp_ops.embeddings re-export of embeddings.embedders, embeddings.store | excluded | imports the name through the OPS package __init__ (agent-tooling ships an empty one); also needs kp_core, the OPS graph, embeddings.cards and mutation |
| test_opencode_auto_capture_acceptance.py | kp_ops.embeddings re-export of embeddings.embedders, embeddings.store | excluded | imports the name through the OPS package __init__ (agent-tooling ships an empty one); also needs kp_core, the OPS graph and desk_memory* legacy (capture_jobs, claims, lifecycle) |
| test_rag_cp4_retrieval.py | kp_ops.embeddings re-export of embeddings.embedders, embeddings.store | excluded | imports the name through the OPS package __init__ (agent-tooling ships an empty one); also needs OPS-only embeddings.retrieval, PgVectorStore (PostgreSQL) and OPS scripts |
| test_rag_cp8_adapter.py | kp_ops.embeddings re-export of embeddings.revision | excluded | imports the name through the OPS package __init__ (agent-tooling ships an empty one); also needs kp_core, the OPS graph, kp_ops.adapter, kp_ops.benchmark and OPS scripts |
| test_tool_discovery.py | tool_discovery (by file name only) | excluded | runs OPS-only scripts/tool_registry.py (it wraps kp_ops.tool_discovery); imports no extracted module |

## DIVERGENCES

Each one is a strict xfail in the ported file. The OPS-baseline column is the same test run against OPS at the extraction commit source with this interpreter.

| file::test | items | failing assertion | OPS baseline | notes |
| --- | --- | --- | --- | --- |
| tests/test_memory_budget_ops_original.py::test_chunked_consolidation_preserves_original_coordinates | 1 | `result = store.consolidate_chunked_with(...)` raises `ValueError: consolidation packet framing exceeds budget` before `assert result['source_span_count'] == len(seen) > len(events)` | fails the same way against OPS at the extraction commit | pre-existing OPS red; tests/test_memory_budget.py raised context_length 1000 to 2400 |
| tests/test_memory_budget_ops_original.py::test_adjacent_events_share_packets_and_max_packets_preflights | 1 | `assert result['packet_count'] < 4` -> `assert 4 < 4` | fails the same way against OPS at the extraction commit | pre-existing OPS red; tests/test_memory_budget.py raised context_length 1500 to 2400 |
| tests/test_memory_budget_ops_original.py::test_cross_packet_citation_cannot_claim_unseen_source | 1 | `with pytest.raises(EpisodeConflict, match='proposal packet')` receives `ValueError: consolidation packet framing exceeds budget` | fails the same way against OPS at the extraction commit | pre-existing OPS red; tests/test_memory_budget.py raised context_length 1200 to 2400 |
| extensions/ops/tests/test_launch_claude_desk_memory_ops_original.py::test_missing_setup_keeps_a_diagnostic_mcp_connection | 1 | `assert [t['name'] for t in replies[1]['result']['tools']] == ['memory.connection_status']`; the list has 12 more items, starting with 'memory.search' | passes against OPS at the extraction commit | agent-tooling rewrote this assertion to all of episodic_memory_tools.TOOLS |
| extensions/ops/tests/test_launch_claude_desk_memory_ops_original.py::test_native_mcp_client_can_inspect_unselected_session | 1 | `assert [t.name for t in catalog.tools]==['memory.connection_status']` | passes against OPS at the extraction commit | agent-tooling rewrote this assertion to a superset check |
| extensions/ops/tests/test_reference_bounds_continuation.py::test_enabled_references_preserve_all_near_limit_retrieval_rows | 1 | `assert len(disabled["data"]["results"]) == 20` -> `assert 0 == 20`: retrieve excludes the synthetic hits (reason indexed_bytes_not_at_target, target_path_unavailable) | fails the same way against OPS at the extraction commit | pre-existing OPS red. A probe shows retrieve returning no rows for synthetic hits whose bytes are not at the target revision (reason indexed_bytes_not_at_target); that eligibility code is identical at OPS at the extraction commit |
| extensions/ops/tests/test_reference_bounds_continuation.py::test_enabled_references_preserve_baseline_selection_when_baseline_is_truncated | 1 | `assert 0 < len(paths(disabled)) < 50` -> `assert 0 < 0` | fails the same way against OPS at the extraction commit | pre-existing OPS red; same exclusion as the row above |
| extensions/ops/tests/test_reference_bounds_continuation.py::test_missing_reference_store_is_backend_unavailable | 1 | `report["data"]["results"][0]` -> `IndexError: list index out of range` | fails the same way against OPS at the extraction commit | pre-existing OPS red; no result row (not diagnosed further) |
| extensions/ops/tests/test_reference_bounds_continuation.py::test_nested_enabled_service_does_not_borrow_outer_generation | 1 | `assert observed[0]["by_repo"]["repo"]["reason"] == "backend_not_configured"` -> `IndexError: list index out of range` | fails the same way against OPS at the extraction commit | pre-existing OPS red; not diagnosed further |
| extensions/ops/tests/test_reference_bounds_continuation.py::test_context_bound_preserves_guidance_with_explicit_reference_omission | 1 | `any(row["code_reference_coverage"].get("metadata_omitted") for row in report["data"]["guidance"])` -> `KeyError: 'code_reference_coverage'` | fails the same way against OPS at the extraction commit | pre-existing OPS red; not diagnosed further |
| tests/test_serena_navigation.py::test_provider_contract | 2 | `monkeypatch.setattr('kp_agent_tooling._impl.service.knowledge_navigation.CodeNavigationProvider._run', ...)` or `import ...knowledge_navigation` -> `ModuleNotFoundError: No module named 'kp_agent_tooling._impl.service.knowledge_navigation'` | passes against OPS at the extraction commit | intentional: the legacy-navigation retirement commit retired knowledge_navigation; serena_navigation now uses navigation_process.NavigationProcessRunner.run |
| tests/test_serena_navigation.py::test_same_path_provider_config_mutation_refused | 1 | same as test_provider_contract | passes against OPS at the extraction commit | same as test_provider_contract |
| tests/test_serena_navigation.py::test_generated_provider_file_is_disclosed_and_refused | 1 | same as test_provider_contract | passes against OPS at the extraction commit | same as test_provider_contract |
| tests/test_serena_navigation.py::test_environment_identity_has_no_config_contents | 1 | same as test_provider_contract | passes against OPS at the extraction commit | same as test_provider_contract |
| tests/test_serena_navigation.py::test_selective_compact_lookup_retains_source_provenance | 1 | same as test_provider_contract | passes against OPS at the extraction commit | same as test_provider_contract |
| tests/test_serena_navigation.py::test_shortened_provider_result_is_explicitly_incomplete | 1 | same as test_provider_contract | passes against OPS at the extraction commit | same as test_provider_contract |
| tests/test_serena_navigation.py::test_reference_error_is_visible | 1 | same as test_provider_contract | passes against OPS at the extraction commit | same as test_provider_contract |
| tests/test_serena_navigation.py::test_subprocess_missing_diagnostics_are_explicit | 3 | same as test_provider_contract | passes against OPS at the extraction commit | same as test_provider_contract |
| tests/test_serena_navigation.py::test_real_subprocess_reports_exit_without_leaking_stderr | 1 | same as test_provider_contract | passes against OPS at the extraction commit | same as test_provider_contract |
| tests/test_serena_navigation.py::test_raised_match_limits_reach_worker | 2 | same as test_provider_contract | passes against OPS at the extraction commit | same as test_provider_contract |

## EXCLUDED TESTS (inside ported files)

| file::test | items | OPS-only dependency |
| --- | --- | --- |
| tests/test_desk_memory_serving_diagnostics.py::test_cli_reports_resident_or_cold_fallback_on_stderr_only | 1 | OPS-only scripts/desk_memory_cli.py and kp_ops.service.desk_memory_daemon (desk_memory* legacy) |
| tests/test_desk_memory_serving_diagnostics.py::test_resident_diagnostics_only_exposes_bounded_counters | 1 | OPS-only kp_ops.service.desk_memory_daemon (desk_memory* legacy) |
| extensions/ops/tests/test_doc_code_reference_delivery.py::test_real_committed_declarations_distinguish_exact_ambiguous_and_weaker | 1 | _resolution_system needs kp_core MemoryAdapter and the OPS graph kp_ops.persist.graph |
| extensions/ops/tests/test_doc_code_reference_delivery.py::test_symlink_is_never_followed_as_a_regular_file | 1 | same (_resolution_system) |
| extensions/ops/tests/test_doc_code_reference_delivery.py::test_incomplete_index_cannot_promote_single_observed_declaration | 1 | same (_resolution_system) |
| extensions/ops/tests/test_doc_code_reference_delivery.py::test_operation_binding_is_separate_from_exact_symbol_name | 1 | same (_resolution_system) |
| extensions/ops/tests/test_doc_code_reference_delivery.py::test_symbol_resolution_does_not_read_unrelated_declaration_blobs | 1 | same (_resolution_system) |
| extensions/ops/tests/test_doc_code_reference_delivery.py::test_more_than_twenty_exact_matches_report_total_and_bounded_targets | 1 | same (_resolution_system) |
| extensions/ops/tests/test_doc_code_reference_delivery.py::test_unsafe_pathlike_inputs_are_retained_as_non_dereferenced_diagnostics | 5 | same (_resolution_system) |
| extensions/ops/tests/test_doc_code_reference_delivery.py::test_submodule_path_is_never_dereferenced | 1 | same (_resolution_system) |
| extensions/ops/tests/test_knowledge_lifecycle.py::test_document_cli_lifecycle_changes_without_reindex | 2 | OPS test_document_retrieval_report over OPS-only scripts/desk_memory_cli.py (kp_core) |
| extensions/ops/tests/test_knowledge_lifecycle.py::test_mcp_uses_lifecycle_policy_after_catalog_change | 1 | the legacy gateway: fastapi, kp_ops.service.app, test_knowledge_mcp, test_mcp_gateway_compose |
| tests/test_navigation_incremental_refresh.py::test_wheel_refresh_uses_installed_policy_and_semantic_sources | 1 | builds the OPS-only portable_tooling/ extraction tree |
| extensions/ops/tests/test_observations.py::test_retained_baseline_and_candidate_receipts_have_distinct_provenance | 1 | OPS-only records/claude-review-baseline/register.py and its retained receipts |
| tests/test_portable_tooling_wheel.py::test_wheel_clean_install_and_packaged_assets | 1 | builds the OPS-only portable_tooling/ tree with pip wheel --no-build-isolation (the arm interpreter also lacks setuptools) |
| tests/test_portable_tooling_wheel.py::test_installed_memory_mcp_without_core_or_dispatch | 1 | same (OPS portable_tooling/) |
| extensions/ops/tests/test_reference_bounds_continuation.py::test_near_limit_diagnostic_totals_reconcile_after_public_service_trim | 1 | OPS-only scripts/doc_code_reference_scenarios.py (kp_core graph) |
| extensions/ops/tests/test_review_ledger.py::test_capture_publisher_keeps_transcript_separate | 1 | OPS-only scripts/opencode_trial_capture.py |
| tests/test_serena_navigation.py::test_cli_rejects_ambiguous_arguments_and_returns_failure | 1 | OPS-only scripts/serena_navigation.py |
| extensions/ops/tests/test_vector_store_generalization.py::test_pg_kind_filter_is_planner_visible_and_has_per_kind_partial_indexes | 1 | OPS-only PostgreSQL migration migrations/20260830_vector_store_generalize.sql |
| extensions/ops/tests/test_vector_store_generalization.py::test_one_table_one_model_space_refuses_mismatched_model_writes | 1 | same migration |
| extensions/ops/tests/test_verification_packet.py::test_retained_crossrepo_join | 1 | receipts fixture reads OPS-only records/lifecycle-30 and OPS scripts/lifecycle_workload_server.py |
| extensions/ops/tests/test_verification_packet.py::test_incompatible_evidence_is_rejected | 7 | same receipts fixture |
| tests/test_operation_navigation.py::test_projection_exposes_missing_evidence_and_validates_citations | 1 | OPS-only kp_ops.operation_navigation (project, validate_view); the module-level import is disabled with an S5 comment |
| extensions/ops/tests/test_scip_navigation.py::test_scip_cli_definitions_reads_catalog_and_partitioned_index | 1 | OPS-only scripts/scip_cli.py (not extracted) |
| extensions/ops/tests/test_scip_navigation.py::test_real_symbol_mcp_transport_and_tenant_refusal | 1 | the legacy gateway: fastapi, kp_ops.service.app, test_knowledge_mcp, test_mcp_gateway_compose |
| extensions/ops/tests/test_scip_continuation.py::test_member_transport_checks_anchor_tenant | 1 | the legacy gateway: fastapi, kp_ops.service.app, test_knowledge_mcp, test_mcp_gateway_compose |

## COVERED-BY-S1 TESTS (inside ported files)

Order S1 owns tests that need knowledge_coverage. These are not ported as live tests: they are kept verbatim with an unconditional `@pytest.mark.skip(reason="S5 covered-by-S1: …")`.
- A strict xfail would XPASS, and so fail, once S1 lands.
- A conditional skip would start asserting OPS behaviour against S1's implementation at the meet.
- Whether to un-skip them after the meet is the Coordinator's call.

| file::test | items | reason |
| --- | --- | --- |
| extensions/ops/tests/test_scip_continuation.py::test_member_continuation_and_coverage | 1 | calls service.execute('platform', ...), which imports knowledge_coverage (order S1). Here: ModuleNotFoundError at line 33, after its knowledge.symbol assertions pass |
| extensions/ops/tests/test_scip_continuation.py::test_corrupt_coverage_does_not_claim_verified | 1 | calls execute('platform', ...), which needs knowledge_coverage (order S1) |

**Meet resolution (Coordinator, 2026-09-30).** S1 merged in PR #27, so both tests above were un-skipped at the S5 meet. The skip markers were removed and the assertions left unchanged. Both now pass against S1's `knowledge_coverage`. That makes them an independent OPS-origin check of S1.

## INHERITED OPS SKIPS

These skip conditions are in the OPS originals unchanged. The test runs wherever its prerequisite exists.

| file | where | items | condition |
| --- | --- | --- | --- |
| extensions/ops/tests/test_code_reference_telemetry_otel.py | module (2 tests) | 1 | OPS importorskip('opentelemetry.sdk'); the optional telemetry extra is not installed in the arm interpreter |
| extensions/ops/tests/test_knowledge_telemetry.py | module (3 tests) | 1 | OPS importorskip('opentelemetry.sdk'); same |
| extensions/ops/tests/test_lifecycle_matrix.py | line 38 | 1 | OPS skip 'explicit isolated source checkouts required' |
| tests/test_typescript_context.py | lines 13, 33, 41 | 3 | OPS skip 'optional operator TypeScript runtime required' |
| extensions/ops/tests/test_worker_lifecycle_trial.py | lines 32, 42 | 2 | OPS skips 'controlled lifecycle execution not supplied' / 'real DB workload not supplied' (OPS records/lifecycle-30 absent) |

## ADAPTED EQUIVALENTS (already-covered, recorded rather than ported)

- `test_episodic_queue.py::test_operator_cli_initializes_enqueues_and_reads_status` and `test_claude_memory_hook.py::test_cli_precompact_fails_loudly_and_settings_preserve_spaced_paths`:
  - The OPS setup simulates the operator's external-volume mount. setup.py replaced that code in `queue_cli`/`claude_hook_cli` with a configured-state-root guard.
  - The OPS form fails here. The agent-tooling tests keep every assertion and supply a config inside the state root instead.
  - This is an intentional decoupling edit. It is counted as already-covered and not duplicated as an xfail.
- `test_launch_claude_desk_memory.py::test_operator_cli_rejected_selection_does_not_admit`: the OPS path `portable_tooling/kp_agent_tooling/desk_cli.py` is relocated to the installed `kp_agent_tooling.desk_cli`. Assertions are identical.
- `test_knowledge_service.py::test_reference_manifest_is_read_from_configured_revision` is not carried anywhere. Its KnowledgeService assertions run before a final OPS `scripts/knowledge_cli.py` subprocess, and that script is OPS-only. It is a coverage gap, not a port.

## AMBIGUITY (A2 and A4 ruled by the Coordinator; the others stand as the conservative reading)

- **A1, scope.**
  - The order scopes files that "import a module that now exists under `_impl`". P1 speaks of files that "name an extracted module".
  - Direct scope follows the first. The indirect table is added so that P1 holds under the second.
  - Synthesized package `__init__`s do not count. Taken literally, they would bring almost every OPS test into scope.
- **A2, "exists only in OPS". RULED by the Coordinator: narrow reading.**
  - Only the test's subject or its assertions needing OPS-only product code counts. Borrowing a self-contained test fixture does not.
  - Applied:
    - The OPS `repo` fixture (test_operation_navigation.py lines 7-14; git and tmp_path only) is copied verbatim to `tests/ops_repo_fixture.py`.
    - test_scip_navigation, test_scip_continuation, test_published_navigation and test_platform_snapshot are now ported. Two of them needed one import repointed; the other two get the fixture through test_scip_navigation.
    - test_operation_navigation is ported for its source_citations tests.
  - Re-checked under the ruling, still excluded because the borrowed helper builds the OPS-only system under test (kp_core MemoryAdapter and the OPS graph): test_doc_code_reference_acceptance, test_document_token_coverage and test_reference_source_facts.
  - test_claim_evidence stays excluded because its subject, claim_evidence, is OPS-only.
- **A3, test-level exclusion.**
  - A ported file whose OPS-only need is confined to some tests is ported whole. Those tests are skipped with an "S5 excluded" reason and listed above.
  - The alternative is excluding the whole file (file-level reading of the Exclusions rule).
- **A4, OPS data outside `tests/fixtures/`. RULED by the Coordinator: agreed.**
  - OPS `records/`, `migrations/` and scripts read as data stay OPS-only and are not copied.
  - verification_adjacency and verification_correctness have no restored tests, and verification_packet has 8 lifecycle-30 items excluded.
  - These modules hard-code the legacy product repositories' and OPS layouts (OPS records/lifecycle-30 receipts and OPS scripts/lifecycle_workload_server.py). Whether they belong in the product is a question for the Coordinator, not a test gap for this order to fill.
- **A5, equivalence.**
  - "Already-covered" is judged per test function. The agent-tooling test must keep every OPS assertion and the same inputs to the code under test.
  - A setup change forced by an intentional decoupling edit counts as equivalent (ADAPTED EQUIVALENTS). Changed inputs or rewritten assertions do not: those OPS tests are ported unchanged into `*_ops_original.py`.
- **A6, test_platform_mcp_trace.py.** It is recorded covered-by-S1 because its only test needs knowledge_coverage. It also depends on the OPS-only legacy app.

## Fixtures copied

- `extensions/ops/tests/fixtures_opencode_trial66_sanitized.json` is a byte-identical copy of the OPS file.
  - `test_review_ledger.py` reads it with `Path(__file__).with_name(...)`, so it sits beside the tests.
  - It contains only message structure, timestamps and the placeholders "sanitized text" and "sanitized reasoning". There are no credentials and no transcript content.
- `tests/ops_repo_fixture.py` holds the OPS `repo` fixture from test_operation_navigation.py lines 7-14 at the extraction commit, verbatim (A2 ruling). It is a test helper that uses only git and tmp_path; the file name keeps pytest from collecting it.
- No ported file reads anything from OPS `tests/fixtures/`.

## Mechanical edits beyond import statements

- `extensions/ops/tests/test_agent_tooling_stdio_text.py`: OPS 'scripts/agent_tooling_cli.py' -> 'packages/tooling/src/kp_agent_tooling/cli.py'
- `extensions/ops/tests/test_navigation_integration.py`: OPS 'scripts/agent_tooling_cli.py' -> 'packages/tooling/src/kp_agent_tooling/cli.py'
- `tests/test_navigation_pattern_errors.py`: OPS 'scripts/agent_tooling_cli.py' -> 'packages/tooling/src/kp_agent_tooling/cli.py'
- `tests/test_repository_coverage_adapter.py`: OPS 'scripts/agent_tooling_cli.py' -> 'packages/tooling/src/kp_agent_tooling/cli.py'
- `tests/test_workspace_setup.py`: OPS 'scripts/workspace_setup_cli.py' -> 'packages/tooling/src/kp_agent_tooling/setup_cli.py'
- `tests/test_navigation_refresh.py`: OPS 'scripts/refresh_navigation.py' -> 'packages/tooling/src/kp_agent_tooling/refresh_cli.py' (3 places)
- `tests/test_navigation_incremental_refresh.py`: OPS 'scripts/refresh_navigation.py' -> 'packages/tooling/src/kp_agent_tooling/refresh_cli.py'
- `tests/test_python_scip_refresh.py`: OPS 'scripts/refresh_navigation.py' -> 'packages/tooling/src/kp_agent_tooling/refresh_cli.py'
- `extensions/ops/tests/test_docker_deployment.py`: the `python -c` program's import statements `import kp_ops.service.…` -> `import kp_agent_tooling._impl.service.…`
- `extensions/ops/tests/test_scip_navigation.py`: `from test_operation_navigation import repo` -> `from ops_repo_fixture import repo` (A2 ruling)
- `tests/test_platform_snapshot.py`: `from test_operation_navigation import repo` -> `from ops_repo_fixture import repo` (A2 ruling)
- `tests/test_operation_navigation.py`: the inline repo fixture (OPS lines 7-14) is replaced by `from ops_repo_fixture import repo`, which holds it verbatim. The module-level `from kp_agent_tooling._impl.operation_navigation import project, validate_view` is commented out with an S5 note, because that module is OPS-only
