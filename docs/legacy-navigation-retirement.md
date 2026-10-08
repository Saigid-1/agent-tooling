# Legacy AST navigation retirement

The independent package no longer carries `_impl.service.knowledge_navigation` or
launches `kp_ops.code_navigation`. Its scan/trace/index cache belongs to the legacy
source repository. The exact source is preserved in that repository's graveyard, where an
archive commit records its hash and restoration boundaries. The original legacy source
also remains at `kp_ops/service/knowledge_navigation.py` there.

PortableKnowledgeProvider already rejected legacy AST configuration. Direct internal
KnowledgeService construction now rejects it too, with an explicit retired-provider
error. Configure `serena` or `published_scip`; published SCIP is unchanged. Internal
imports of CodeNavigationProvider are intentionally removed, with no compatibility
alias to an executable legacy provider.

Serena discovery, overview and inspect used the old class only for bounded worker
execution. They now use NavigationProcessRunner in navigation_process. Output limits,
deadlines, descendant termination, scrubbed environment and suppressed stderr remain
unchanged. NavigationSubprocessError lives beside that runner, preserving returncode
and explicit unavailable diagnostics; errors never establish symbol absence.

This is source retirement, not a live service shutdown. No private index/cache records
were deleted or migrated. Rollback is a revert of the removal commit, subject to a new
dependency review before enabling the retired configuration.
