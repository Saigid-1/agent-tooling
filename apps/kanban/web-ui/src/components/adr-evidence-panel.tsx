import type { RuntimeAppRouterInputs, RuntimeAppRouterOutputs } from "@runtime-trpc";
import { useCallback, useEffect, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import { getRuntimeTrpcClient } from "@/runtime/trpc-client";
import { useInterval } from "@/utils/react-use";

type EvidenceInput = RuntimeAppRouterInputs["planning"]["recordEvidence"]["evidence"];
type EvidenceRecords = RuntimeAppRouterOutputs["planning"]["evidence"];
export function AdrEvidencePanel({
	workspaceId,
	taskId,
	intakeSha256,
}: {
	workspaceId: string;
	taskId: string;
	intakeSha256: string;
}) {
	const [lifecycle, setLifecycle] = useState<RuntimeAppRouterOutputs["planning"]["lifecycle"] | null>(null);
	const [records, setRecords] = useState<EvidenceRecords>([]);
	const [readError, setReadError] = useState<string | null>(null);
	const [mutationError, setMutationError] = useState<string | null>(null);
	const refreshGeneration = useRef(0);
	const [pending, setPending] = useState(false);
	const [kind, setKind] = useState<EvidenceInput["kind"]>("green");
	const request = useRef<{ payload: string; eventId: string } | null>(null);
	const [rejected, setRejected] = useState(false);
	const refresh = useCallback(async () => {
		const generation = ++refreshGeneration.current;
		try {
			const client = getRuntimeTrpcClient(workspaceId);
			const [evidence, observations] = await Promise.all([
				client.planning.evidence.query({ taskId }),
				client.planning.lifecycle.query({ taskId }),
			]);
			if (generation !== refreshGeneration.current) return;
			setRecords(evidence);
			setLifecycle(observations);
			setReadError(null);
		} catch (cause) {
			if (generation === refreshGeneration.current)
				setReadError(cause instanceof Error ? cause.message : String(cause));
		}
	}, [workspaceId, taskId]);
	useEffect(() => {
		void refresh();
		return () => {
			refreshGeneration.current += 1;
		};
	}, [refresh]);
	useInterval(() => void refresh(), 5000);
	async function record(form: HTMLFormElement) {
		const fields = new FormData(form);
		setPending(true);
		setMutationError(null);
		try {
			const draft = {
				intakeSha256,
				kind,
				requirementId: String(fields.get("requirementId")),
				rationale: String(fields.get("rationale")),
				outcome:
					kind === "red"
						? ("failed" as const)
						: kind === "acceptance"
							? rejected
								? ("rejected" as const)
								: ("accepted" as const)
							: kind === "amendment_proposal"
								? ("proposed" as const)
								: ("passed" as const),
				evidence: [{ path: String(fields.get("path")), sha256: String(fields.get("sha256")) }],
				...(kind === "amendment_proposal" ? { amendment: String(fields.get("amendment")) } : {}),
				actor: { kind: "local_operator_assertion" as const, id: "local-planning-ui" },
			};
			const payload = JSON.stringify(draft);
			if (request.current?.payload !== payload) request.current = { payload, eventId: crypto.randomUUID() };
			await getRuntimeTrpcClient(workspaceId).planning.recordEvidence.mutate({
				taskId,
				evidence: { ...draft, eventId: request.current.eventId },
			});
			request.current = null;
			form.reset();
			await refresh();
		} catch (cause) {
			setMutationError(cause instanceof Error ? cause.message : String(cause));
		} finally {
			setPending(false);
		}
	}
	return (
		<details className="rounded-md border border-border p-3">
			<summary className="cursor-pointer font-medium">Requirement evidence · {records.length} records</summary>
			<p className="my-2 text-sm text-text-secondary">
				Acceptance and amendment proposals are attributed local assertions, not authenticated human approvals.
				Requirement IDs follow intake acceptance scenarios: acceptance:1, acceptance:2, and so on. Board position
				never proves delivery.
			</p>

			{readError ? (
				<p role="alert" className="text-status-red">
					Evidence refresh failed: {readError}
				</p>
			) : null}
			{mutationError ? (
				<p role="alert" className="text-status-red">
					{mutationError}
				</p>
			) : null}
			{lifecycle ? (
				<details className="my-3">
					<summary className="cursor-pointer text-sm">
						Native lifecycle observations · {lifecycle.records.length}
					</summary>
					<p className="my-2 text-xs text-text-secondary">{lifecycle.evidence_boundary}</p>
					{lifecycle.records.length === 0 ? (
						<p className="text-sm">No captured transitions.</p>
					) : (
						<ol className="space-y-1">
							{lifecycle.records.map((record) => (
								<li key={record.sha256} className="text-xs">
									{record.sequence}. {record.observed_at} · {record.source} · {record.state.state}
									{record.capture_gap ? (
										<strong className="block text-status-orange">Capture gap: {record.capture_gap}</strong>
									) : null}
								</li>
							))}
						</ol>
					)}
				</details>
			) : null}
			{records.length === 0 ? (
				<p className="text-sm text-text-secondary">No requirement evidence recorded.</p>
			) : (
				<ul className="space-y-2">
					{records.map((record) => (
						<li key={record.id} className="rounded bg-surface-2 p-2 text-sm">
							<strong>
								{record.input.requirementId}: {record.input.kind} · {record.input.outcome}
							</strong>
							<p>{record.input.rationale}</p>
							<p className="break-all text-xs">
								Attribution: {record.input.actor.kind} · {record.input.actor.id}
							</p>
							<details className="text-xs">
								<summary className="cursor-pointer">Captured execution binding</summary>
								<p className="break-all">Repository: {record.execution.repository}</p>
								<p className="break-all">Base revision: {record.execution.baseRef}</p>
								<p className="break-all">Execution intake SHA256: {record.execution.intakeSha256}</p>
							</details>
							<p className="break-all text-xs text-text-secondary">
								{record.recordedAt} · intake {record.input.intakeSha256}
								{record.input.intakeSha256 !== intakeSha256 ? " (previous revision)" : ""}
							</p>
							{record.input.evidence.map((ref) => (
								<p key={ref.path} className="break-all text-xs">
									{ref.path} · SHA256 {ref.sha256}
								</p>
							))}
							{record.input.amendment ? (
								<pre className="whitespace-pre-wrap">{record.input.amendment}</pre>
							) : null}
						</li>
					))}
				</ul>
			)}
			<form
				className="mt-3 grid gap-2"
				onSubmit={(event) => {
					event.preventDefault();
					void record(event.currentTarget);
				}}
			>
				<label className="text-sm">
					Evidence kind
					<select
						className="ml-2 rounded bg-surface-2 p-2"
						value={kind}
						onChange={(event) => setKind(event.target.value as EvidenceInput["kind"])}
					>
						{["red", "green", "implementation", "merge", "deployment", "acceptance", "amendment_proposal"].map(
							(value) => (
								<option key={value}>{value}</option>
							),
						)}
					</select>
				</label>
				{kind === "acceptance" ? (
					<label className="text-sm">
						<input type="checkbox" checked={rejected} onChange={(event) => setRejected(event.target.checked)} />{" "}
						Record rejected acceptance
					</label>
				) : null}
				{[
					{ name: "requirementId", label: "Requirement ID" },
					{ name: "path", label: "Physical evidence file path" },
					{ name: "sha256", label: "Evidence SHA256" },
					{ name: "rationale", label: "Rationale" },
				].map((field) => (
					<label key={field.name} className="text-sm">
						{field.label}
						<input
							required
							name={field.name}
							className="mt-1 block w-full rounded border border-border bg-surface-2 p-2"
						/>
					</label>
				))}
				{kind === "amendment_proposal" ? (
					<label className="text-sm">
						Proposed ADR amendment
						<textarea required name="amendment" className="mt-1 block w-full rounded bg-surface-2 p-2" />
					</label>
				) : null}
				<Button type="submit" disabled={pending}>
					{pending ? "Recording…" : "Record evidence"}
				</Button>
			</form>
		</details>
	);
}
