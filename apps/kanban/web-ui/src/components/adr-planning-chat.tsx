import { createHomeAgentSessionId } from "@runtime-home-agent-session";
import { useState } from "react";
import { ClineAgentChatPanel } from "@/components/detail-panels/cline-agent-chat-panel";
import { Button } from "@/components/ui/button";
import { createIdleTaskSession } from "@/hooks/app-utils";
import { useClineChatRuntimeActions } from "@/hooks/use-cline-chat-runtime-actions";
import { getRuntimeTrpcClient } from "@/runtime/trpc-client";
import type { RuntimeConfigResponse, RuntimeTaskChatMessage, RuntimeTaskSessionSummary } from "@/runtime/types";

export function AdrPlanningChat({
	workspaceId,
	identity,
	prompt,
	contextNotice,
	config,
	messages,
	sessions,
}: {
	workspaceId: string;
	identity: string;
	prompt: string;
	contextNotice?: string;
	config: RuntimeConfigResponse | null;
	messages: Record<string, RuntimeTaskChatMessage[]>;
	sessions: Record<string, RuntimeTaskSessionSummary>;
}) {
	// This is a native workspace chat identity, not a desk admission or execution card.
	const taskId = `${createHomeAgentSessionId(workspaceId, "cline")}:adr:${encodeURIComponent(identity)}`;
	const [summary, setSummary] = useState<RuntimeTaskSessionSummary | null>(null);
	const [started, setStarted] = useState(false);
	const [sentContext, setSentContext] = useState<string | null>(null);
	const [pending, setPending] = useState(false);
	const [error, setError] = useState<string | null>(null);
	const actions = useClineChatRuntimeActions({ currentProjectId: workspaceId, onSessionSummary: setSummary });
	async function start() {
		setPending(true);
		setError(null);
		try {
			const history = await getRuntimeTrpcClient(workspaceId).runtime.getTaskChatMessages.query({ taskId });
			if (!history.ok && history.error !== "Task chat session is not available.")
				throw new Error(history.error ?? "Could not load planning chat history");
			if (history.messages.length === 0) {
				// The native home-chat send path resumes persisted history or creates a session in plan mode.
				const sent = await actions.sendTaskChatMessage(taskId, prompt, { mode: "plan" });
				if (!sent.ok) throw new Error(sent.message ?? "Could not send ADR context");
				setSentContext(prompt);
			} else {
				setSummary(sessions[taskId] ?? { ...createIdleTaskSession(taskId), mode: "plan" });
			}
			setStarted(true);
		} catch (cause) {
			setError(cause instanceof Error ? cause.message : String(cause));
		} finally {
			setPending(false);
		}
	}
	async function refreshContext() {
		setPending(true);
		setError(null);
		try {
			const sent = await actions.sendTaskChatMessage(taskId, prompt, { mode: "plan" });
			if (!sent.ok) throw new Error(sent.message ?? "Could not refresh ADR context");
			setSentContext(prompt);
		} catch (cause) {
			setError(cause instanceof Error ? cause.message : String(cause));
		} finally {
			setPending(false);
		}
	}
	const activeSummary = sessions[taskId] ?? summary;
	return (
		<section className="flex min-h-0 flex-col gap-3 rounded-lg border border-border p-4">
			<h3 className="font-semibold">Architectural conversation</h3>
			<p className="text-sm text-text-secondary">
				Discuss this ADR in native Cline plan mode in the selected repository. A new conversation sends the prepared
				ADR context to your configured Cline provider. Resuming preserves previously sent context.
			</p>
			{contextNotice ? <p className="text-xs text-text-secondary">{contextNotice}</p> : null}
			{started ? (
				<div className="space-y-2">
					<p role="status" className="text-sm text-text-secondary">
						{sentContext === prompt
							? "The current planning context has been sent to this conversation."
							: "The current planning context has not been sent during this visit. Existing messages may describe earlier revisions."}
					</p>
					<Button
						disabled={pending || !config || activeSummary?.state === "running"}
						onClick={() => void refreshContext()}
					>
						{pending ? "Sending context…" : "Send refreshed planning context"}
					</Button>
				</div>
			) : null}
			{error ? (
				<p role="alert" className="text-status-red">
					{error}
				</p>
			) : null}
			{!started || !activeSummary ? (
				<Button disabled={pending || !config} onClick={() => void start()}>
					{pending ? "Starting…" : "Start / resume architectural chat"}
				</Button>
			) : (
				<div className="flex h-[520px] min-h-0">
					<ClineAgentChatPanel
						taskId={taskId}
						workspaceId={workspaceId}
						summary={activeSummary}
						runtimeConfig={config}
						defaultMode="plan"
						showComposerModeToggle={false}
						onSendMessage={(id, text, options) =>
							actions.sendTaskChatMessage(id, text, { ...options, mode: "plan" })
						}
						onCancelTurn={actions.cancelTaskChatTurn}
						onLoadMessages={actions.loadTaskChatMessages}
						incomingMessages={messages[taskId] ?? null}
						composerPlaceholder="Explore requirements, slices, evidence, and unresolved decisions"
					/>
				</div>
			)}
		</section>
	);
}
