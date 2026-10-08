import { useMemo, useState } from "react";
import { AdrEvidencePanel } from "@/components/adr-evidence-panel";
import { AdrPlanningChat } from "@/components/adr-planning-chat";
import { buildPlanningChatContext, groupPlanningAdrs } from "@/components/adr-planning-model";
import { AdrSliceIntakeForm } from "@/components/adr-slice-intake-form";
import { ClineMarkdownContent } from "@/components/detail-panels/cline-markdown-content";
import { Button } from "@/components/ui/button";
import { useAdrPlanning } from "@/hooks/use-adr-planning";
import { getRuntimeTrpcClient } from "@/runtime/trpc-client";
import type {
	RuntimeBoardData,
	RuntimeConfigResponse,
	RuntimeProjectSummary,
	RuntimeTaskChatMessage,
	RuntimeTaskSessionSummary,
} from "@/runtime/types";

export function AdrPlanningView({
	projects,
	workspaceId,
	board,
	sessions,
	config,
	messages,
	onOpenTrack,
}: {
	projects: RuntimeProjectSummary[];
	workspaceId: string;
	board: RuntimeBoardData;
	sessions: Record<string, RuntimeTaskSessionSummary>;
	config: RuntimeConfigResponse | null;
	messages: Record<string, RuntimeTaskChatMessage[]>;
	onOpenTrack: (workspaceId: string, taskId?: string) => void;
}) {
	const { workspaces, errors, loading, refresh } = useAdrPlanning(projects);
	const groups = useMemo(() => groupPlanningAdrs(workspaces), [workspaces]);
	const [selected, setSelected] = useState<string | null>(null);
	const [registerError, setRegisterError] = useState<string | null>(null);
	const [registering, setRegistering] = useState(false);
	const active = groups.find((group) => group.key === selected);
	const chatContext = active ? buildPlanningChatContext(active) : null;
	async function register(form: HTMLFormElement) {
		setRegistering(true);
		setRegisterError(null);
		const fields = new FormData(form);
		try {
			const record = await getRuntimeTrpcClient(workspaceId).planning.register.mutate({
				initiativeId: String(fields.get("initiative")),
				adrId: String(fields.get("adr")),
				path: String(fields.get("path")),
			});
			await refresh();
			setSelected(JSON.stringify([record.initiativeId, record.adrId]));
			form.reset();
		} catch (cause) {
			setRegisterError(cause instanceof Error ? cause.message : String(cause));
		} finally {
			setRegistering(false);
		}
	}
	return (
		<main className="flex-1 overflow-auto bg-surface-0 p-6 text-text-primary">
			<div className="mb-5 flex flex-wrap items-center justify-between gap-3">
				<div>
					<h1 className="text-xl font-semibold">ADR planning</h1>
					<p className="text-sm text-text-secondary">
						Initiatives, decisions, and execution tracks across your registered repositories.
					</p>
				</div>
				<Button onClick={() => void refresh()}>Refresh evidence</Button>
			</div>
			{errors.map((error) => (
				<p key={error} role="alert" className="mb-2 text-status-red">
					{error}
				</p>
			))}
			<details className="mb-5 rounded-lg border border-border p-4">
				<summary className="cursor-pointer font-medium">Add an ADR before creating slices</summary>
				<p className="my-2 text-sm text-text-secondary">
					Register a Markdown document from {projects.find((project) => project.id === workspaceId)?.name}. Its
					content snapshot is retained separately from the repository HEAD.
				</p>
				<form
					className="flex flex-wrap items-end gap-3"
					onSubmit={(event) => {
						event.preventDefault();
						void register(event.currentTarget);
					}}
				>
					{[
						{ name: "initiative", label: "Initiative ID" },
						{ name: "adr", label: "ADR ID" },
						{ name: "path", label: "Repository-relative Markdown path" },
					].map((field) => (
						<label className="text-sm" key={field.name}>
							{field.label}
							<input
								required
								name={field.name}
								className="mt-1 block rounded border border-border bg-surface-2 p-2"
							/>
						</label>
					))}
					<Button type="submit" disabled={registering}>
						{registering ? "Registering…" : "Register ADR snapshot"}
					</Button>
				</form>
				{registerError ? (
					<p role="alert" className="mt-2 text-status-red">
						{registerError}
					</p>
				) : null}
			</details>
			{loading ? (
				<p>Loading ADRs…</p>
			) : groups.length === 0 ? (
				<p className="text-text-secondary">
					No ADRs registered. Add a document above or reconcile a reviewed ADR intake to create execution slices.
				</p>
			) : null}
			{active ? (
				<div className="space-y-4">
					<Button onClick={() => setSelected(null)}>All ADRs</Button>
					<h2 className="text-lg font-semibold">
						{active.initiativeId} / {active.adrId}
					</h2>
					{active.documents.map(({ document, workspace }) => (
						<details
							key={`${workspace.project.id}:${document.sha256}`}
							className="rounded-lg border border-border p-4"
						>
							<summary className="cursor-pointer font-medium">
								{document.title} · {workspace.project.name} · {document.sha256.slice(0, 12)}
							</summary>
							<dl className="my-3 grid gap-1 text-sm">
								<dt>Declared status</dt>
								<dd>{document.declaredStatus ?? "Not declared"}</dd>
								<dt>Derived status</dt>
								<dd>Unverified; no inference from board position</dd>
								<dt>Source path</dt>
								<dd>{document.path}</dd>
								<dt>Repository HEAD at registration (snapshot may include uncommitted changes)</dt>
								<dd className="break-all">{document.sourceRevision}</dd>
								<dt>Exact content SHA256</dt>
								<dd className="break-all">{document.sha256}</dd>
							</dl>
							<ClineMarkdownContent content={document.content} />
						</details>
					))}
					<h3 className="font-semibold">Projects / execution tracks</h3>
					<AdrSliceIntakeForm
						projects={projects}
						initialWorkspaceId={workspaceId}
						onReconciled={refresh}
						onOpenTrack={onOpenTrack}
					/>
					{active.slices.length === 0 ? (
						<p className="text-sm text-text-secondary">
							No execution slices yet. Use the architectural conversation to refine scope and a reviewed intake
							to add native cards.
						</p>
					) : null}
					{active.slices.map(({ entry, workspace }) => {
						const nativeBoard = workspace.project.id === workspaceId ? board : workspace.state.board;
						const column = nativeBoard.columns.find((column) =>
							column.cards.some((card) => card.id === entry.taskId),
						);
						const card = column?.cards.find((card) => card.id === entry.taskId);
						const summary = (workspace.project.id === workspaceId ? sessions : workspace.state.sessions)[
							entry.taskId
						];
						return (
							<article
								key={`${entry.workspaceId}:${entry.taskId}`}
								className="space-y-3 rounded-lg border border-border bg-surface-1 p-4"
							>
								<div className="flex flex-wrap justify-between gap-2">
									<div>
										<h4 className="font-semibold">
											{entry.intake.slice_id}: {entry.intake.title}
										</h4>
										<p className="text-sm text-text-secondary">
											{entry.intake.owner.repository} · {entry.intake.owner.role}
										</p>
									</div>
									<Button
										onClick={() =>
											onOpenTrack(
												entry.workspaceId,
												card && column?.id !== "trash" ? entry.taskId : undefined,
											)
										}
									>
										Open native {!card || column?.id === "trash" ? "board" : "card"}
									</Button>
								</div>
								<p className="text-sm">
									Board: {column?.title ?? "Card missing"} · Session: {summary?.state ?? "Not started"}
									{workspace.project.id === workspaceId ? " · Live" : " · Refreshed every 5 seconds"}
								</p>
								<p className="break-all text-xs text-text-secondary">
									Workspace {entry.workspaceId} · {workspace.project.path} · Card {entry.taskId}
								</p>
								<p>{entry.intake.requester_outcome}</p>
								<p className="text-sm">
									Declared: {entry.intake.adr.declared_status} · Intake-derived:{" "}
									{entry.intake.adr.derived_status ?? "unknown"} · {entry.intake.adr.verification}
								</p>
								<p className="text-sm text-text-secondary">{entry.intake.adr.reason}</p>
								<p className="break-all text-xs">
									Source {entry.intake.adr.source.repository}:{entry.intake.adr.source.path}@
									{entry.intake.adr.source.revision ?? "external snapshot"}
									<br />
									Source SHA256 {entry.intake.adr.source.sha256}
									<br />
									Intake SHA256 {entry.intakeSha256}
								</p>
								<div>
									<h5 className="text-sm font-semibold">Unresolved questions</h5>
									{entry.intake.unresolved.length ? (
										<ul className="list-inside list-disc text-sm">
											{entry.intake.unresolved.map((question) => (
												<li key={question}>{question}</li>
											))}
										</ul>
									) : (
										<p className="text-sm text-text-secondary">None declared in this intake.</p>
									)}
								</div>
								{entry.intake.dependencies.length ? (
									<div>
										<h5 className="text-sm font-semibold">Prerequisites</h5>
										{entry.intake.dependencies.map((dependency) => (
											<p key={dependency.work_id} className="text-sm">
												{dependency.work_id} ({dependency.basis}): {dependency.reason}
											</p>
										))}
									</div>
								) : null}
								<details>
									<summary className="cursor-pointer text-sm">
										Complete reviewed intake / acceptance scenarios
									</summary>
									<pre className="mt-2 overflow-auto whitespace-pre-wrap break-words rounded bg-surface-0 p-3 text-xs">
										{JSON.stringify(entry.intake, null, 2)}
									</pre>
								</details>
								{card?.adrOrigin ? (
									<AdrEvidencePanel
										workspaceId={entry.workspaceId}
										taskId={entry.taskId}
										intakeSha256={entry.intakeSha256}
									/>
								) : (
									<p className="text-sm text-text-secondary">
										No native execution binding is available for this intake. Reconcile it into the target
										workspace before recording delivery evidence.
									</p>
								)}
							</article>
						);
					})}
					<AdrPlanningChat
						key={`${workspaceId}:${active.key}`}
						workspaceId={workspaceId}
						identity={active.key}
						config={config}
						sessions={sessions}
						messages={messages}
						contextNotice={chatContext?.notice ?? ""}
						prompt={chatContext?.prompt ?? ""}
					/>
				</div>
			) : (
				<div className="grid gap-3">
					{groups.map((group) => (
						<button
							type="button"
							key={group.key}
							onClick={() => setSelected(group.key)}
							className="rounded-lg border border-border bg-surface-1 p-4 text-left hover:bg-surface-2"
						>
							<span className="text-xs text-text-secondary">{group.initiativeId}</span>
							<h2 className="font-semibold">{group.adrId}</h2>
							<p className="text-sm text-text-secondary">
								{group.slices.length} execution slices ·{" "}
								{
									new Set([
										...group.documents.map((item) => item.workspace.project.id),
										...group.slices.map((item) => item.workspace.project.id),
									]).size
								}{" "}
								repositories
							</p>
						</button>
					))}
				</div>
			)}
		</main>
	);
}
