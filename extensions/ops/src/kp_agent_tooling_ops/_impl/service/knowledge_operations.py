"""Authoritative declarations for the public knowledge operation surface.

The declaration is deliberately literal so a revision-pinned reader can inspect it
without importing or executing code from the target checkout.
"""

KNOWLEDGE_OPERATION_DECLARATIONS = (
    {
        "operation": "capabilities",
        "description": "List the bounded capability IDs and map paths declared for one repository. Catalog inventory only.",
        "handler": "kp_agent_tooling_ops._impl.service.knowledge.KnowledgeService.execute_for_tenant",
        "dispatch_method": "execute_for_tenant",
        "binding": "nameable",
    },
    {"operation": "reference_recovery",
     "description": "Recover bounded resolved code references from an exact serving generation, target revision set, and document coordinate without semantic search.",
     "handler": "kp_agent_tooling_ops._impl.service.knowledge.KnowledgeService.execute_for_tenant",
     "dispatch_method": "execute_for_tenant", "binding": "nameable"},
    {"operation": "reference_diagnostics",
     "description": "Recover bounded document-reference diagnostics from an exact serving generation and document coordinate. Generation mismatch requires a fresh parent read.",
     "handler": "kp_agent_tooling_ops._impl.service.knowledge.KnowledgeService.execute_for_tenant",
     "dispatch_method": "execute_for_tenant", "binding": "nameable"},
    {
        "operation": "symbol",
        "description": "Resolve revision-pinned SCIP occurrences and definition candidates inside a configured platform. Static evidence only.",
        "handler": "kp_agent_tooling_ops._impl.service.knowledge.KnowledgeService.execute_for_tenant",
        "dispatch_method": "execute_for_tenant",
        "binding": "nameable",
    },
    {
        "operation": "platform",
        "description": "Inspect configured multi-repository source identity and readiness gaps. Does not establish build or runtime identity.",
        "handler": "kp_agent_tooling_ops._impl.service.knowledge.KnowledgeService.execute_for_tenant",
        "dispatch_method": "execute_for_tenant",
        "binding": "nameable",
    },
    {
        "operation": "context",
        "description": "Start here: compose maintained guidance, reference freshness and static code navigation for an exact target commit. Gaps and runtime evidence limits remain explicit.",
        "handler": "kp_agent_tooling_ops._impl.service.knowledge.KnowledgeService.execute_for_tenant",
        "dispatch_method": "execute_for_tenant",
        "binding": "nameable",
    },
    {
        "operation": "discover",
        "description": "Find lexical operator entry-point candidates in configured committed source. Not semantic absence proof.",
        "handler": "kp_agent_tooling_ops._impl.service.knowledge.KnowledgeService.execute",
        "dispatch_method": "execute",
        "binding": "nameable",
    },
    {
        "operation": "retrieve",
        "description": "Retrieve maintained guidance only when indexed blob bytes match the requested commit; default uses the configured default branch. Historical candidates require explicit opt-in and are separate.",
        "handler": "kp_agent_tooling_ops._impl.service.knowledge.KnowledgeService.execute_for_tenant",
        "dispatch_method": "execute_for_tenant",
        "binding": "nameable",
    },
    {
        "operation": "check_references",
        "description": "Resolve a named capability map; changed references need review and do not prove architectural violations.",
        "handler": "kp_agent_tooling_ops._impl.service.knowledge.KnowledgeService.execute",
        "dispatch_method": "execute",
        "binding": "nameable",
    },
)
