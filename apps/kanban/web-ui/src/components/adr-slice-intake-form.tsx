import { useState } from "react";
import { Button } from "@/components/ui/button";
import { getRuntimeTrpcClient } from "@/runtime/trpc-client";
import type { RuntimeProjectSummary } from "@/runtime/types";
export function AdrSliceIntakeForm({
	projects,
	initialWorkspaceId,
	onReconciled,
	onOpenTrack,
}: {
	projects: RuntimeProjectSummary[];
	initialWorkspaceId: string;
	onReconciled: () => Promise<void>;
	onOpenTrack: (workspaceId: string, taskId?: string) => void;
}) {
	const [error, setError] = useState<string | null>(null);
	const [pending, setPending] = useState(false);
	const [created, setCreated] = useState<{ workspaceId: string; taskId: string } | null>(null);
	async function reconcile(form: HTMLFormElement) {
		const fields = new FormData(form);
		const workspaceId = String(fields.get("workspace"));
		setPending(true);
		setError(null);
		setCreated(null);
		try {
			const supersedes = String(fields.get("supersedes")).trim();
			const result = await getRuntimeTrpcClient(workspaceId).planning.reconcile.mutate({
				path: String(fields.get("path")).trim(),
				baseRef: String(fields.get("baseRef")).trim(),
				...(supersedes ? { supersedes } : {}),
			});
			setCreated(result);
			await onReconciled();
		} catch (cause) {
			setError(cause instanceof Error ? cause.message : String(cause));
		} finally {
			setPending(false);
		}
	}
	return (
		<details className="rounded-lg border border-border p-4">
			<summary className="cursor-pointer font-medium">Add a reviewed execution slice</summary>
			<p className="my-2 text-sm text-text-secondary">
				Import an approved ADR intake JSON into an explicit target repository. This creates or reconciles a native
				backlog card; use its Start control when ready to execute.
			</p>
			<form
				className="grid gap-3"
				onSubmit={(event) => {
					event.preventDefault();
					void reconcile(event.currentTarget);
				}}
			>
				<label className="text-sm">
					Execution repository
					<select
						name="workspace"
						defaultValue={initialWorkspaceId}
						className="mt-1 block w-full rounded bg-surface-2 p-2"
					>
						{projects.map((project) => (
							<option key={project.id} value={project.id}>
								{project.name} — {project.path}
							</option>
						))}
					</select>
				</label>
				{[
					{ name: "path", label: "Physical reviewed intake JSON path", required: true },
					{
						name: "baseRef",
						label: "Target execution commit (40-character SHA; independent of ADR source)",
						required: true,
					},
					{ name: "supersedes", label: "Previous intake SHA256 (only for an explicit revision)", required: false },
				].map((field) => (
					<label key={field.name} className="text-sm">
						{field.label}
						<input
							required={field.required}
							name={field.name}
							className="mt-1 block w-full rounded border border-border bg-surface-2 p-2"
						/>
					</label>
				))}
				<Button type="submit" disabled={pending}>
					{pending ? "Reconciling…" : "Create / reconcile native backlog card"}
				</Button>
			</form>
			{error ? (
				<p role="alert" className="mt-2 text-status-red">
					{error}
				</p>
			) : null}
			{created ? (
				<div className="mt-3">
					<p className="break-all text-sm">
						Native card {created.taskId} in workspace {created.workspaceId}
					</p>
					<Button onClick={() => onOpenTrack(created.workspaceId, created.taskId)}>Open native card</Button>
				</div>
			) : null}
		</details>
	);
}
