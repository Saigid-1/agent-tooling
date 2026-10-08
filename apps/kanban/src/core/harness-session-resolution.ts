import type { RuntimeAgentId, RuntimeNativeSessionBinding, RuntimeTaskSessionSummary } from "./api-contract";

export interface HarnessRegistrySnapshot {
	workspaceId: string;
	repoPath: string;
	sessions: Record<string, RuntimeTaskSessionSummary>;
	evidence: { path: string; sha256: string | null; modifiedAt: string | null; observedAt: string; present: boolean };
}

export type HarnessSessionQuery =
	| { kind: "kanban_task"; workspaceId: string; taskId: string; agentId: RuntimeAgentId }
	| { kind: "native_session"; workspaceId: string; nativeId: string; agentId: RuntimeAgentId }
	| { kind: "host_session"; workspaceId: string; hostId: string };

/** A saved Kanban task key is a route identity; it is not a native harness session identity. */
export function resolveHarnessSession(snapshots: HarnessRegistrySnapshot[], query: HarnessSessionQuery) {
	const inspected = snapshots.filter((snapshot) => snapshot.workspaceId === query.workspaceId);
	const evidence = inspected.map((snapshot) => snapshot.evidence);
	if (query.kind === "host_session") {
		return {
			status: "unresolved" as const,
			query,
			reason:
				"A host session ID is distinct from a provider-native ID; this registry has no verified host-to-task mapping.",
			evidence,
		};
	}
	const matches: Array<{
		snapshot: HarnessRegistrySnapshot;
		session: RuntimeTaskSessionSummary;
		binding: RuntimeNativeSessionBinding | null;
	}> = [];
	for (const snapshot of inspected) {
		for (const session of Object.values(snapshot.sessions)) {
			if (query.kind === "kanban_task") {
				if (session.taskId === query.taskId && session.agentId === query.agentId) {
					matches.push({ snapshot, session, binding: null });
				}
			} else {
				for (const binding of session.nativeSessionBindings ?? []) {
					if (binding.providerId === query.agentId && binding.nativeId === query.nativeId) {
						matches.push({ snapshot, session, binding });
					}
				}
			}
		}
	}
	if (matches.length !== 1) {
		return {
			status: matches.length ? ("ambiguous" as const) : ("unresolved" as const),
			query,
			reason: matches.length
				? "More than one exact registry candidate exists for this identity."
				: inspected.some((snapshot) => !snapshot.evidence.present)
					? "The workspace is registered, but its saved session registry file is missing."
					: query.kind === "native_session"
						? "No exact provider-native session binding occurs in the inspected Kanban registry."
						: "No exact workspace, task and provider identity occurs in the inspected registry.",
			evidence,
		};
	}
	const { snapshot, session, binding } = matches[0];
	return {
		status: "resolved" as const,
		query,
		route: {
			kind: "kanban_task" as const,
			registryPath: snapshot.evidence.path,
			workspaceId: snapshot.workspaceId,
			taskId: session.taskId,
			runtimeEndpoint: null,
		},
		provider: binding?.providerId ?? session.agentId,
		currentProvider: session.agentId,
		nativeBinding: binding,
		knownNativeBindings: session.nativeSessionBindings ?? [],
		capabilities: {
			saved_summary_inspection: true,
			terminal_input:
				query.kind === "native_session"
					? ("not_attributed_to_native_session" as const)
					: ("supported_by_kanban_runtime_if_active" as const),
			structured_message_acknowledgement: false,
			provider_resume: "not_assessed" as const,
		},
		observation: {
			state: session.state,
			stateSource: "persisted_kanban_summary" as const,
			stateUpdatedAt: new Date(session.updatedAt).toISOString(),
			observedAt: snapshot.evidence.observedAt,
			pid: session.pid,
			reachability: "not_checked" as const,
			deliveryAssurance: "none" as const,
		},
		evidence: snapshot.evidence,
	};
}
