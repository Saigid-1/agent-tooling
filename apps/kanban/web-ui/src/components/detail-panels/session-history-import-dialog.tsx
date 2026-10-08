import { History } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import { Dialog, DialogBody, DialogFooter, DialogHeader } from "@/components/ui/dialog";
import { Spinner } from "@/components/ui/spinner";
import { getRuntimeTrpcClient } from "@/runtime/trpc-client";
import type { RuntimeTaskSessionSummary } from "@/runtime/types";
import type { SessionImportAction, SessionImportResult, SessionImportSetup } from "../../../../src/session-import/session-import-contract";

type ImportResult = SessionImportResult;
type SetupResult = SessionImportSetup;
type ImportMode = "full" | "current-turn-and-forward";

const FOLLOW_STATUS_INTERVAL_MS = 3_000;

function jobStorageKey(workspaceId: string | null, taskId: string, nativeSessionId: string, sourceFile: string): string {
	return `kanban.session-import.job.${workspaceId ?? "none"}.${taskId}.${nativeSessionId}.${sourceFile}`;
}

function pendingOwnerKey(jobId: string): string { return `kanban.session-import.owner.${jobId}`; }

function sessionIdInPath(path: string): string | null {
	return path.match(/(?:^|\/)rollout-[^/]*-([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.jsonl$/i)?.[1] ?? null;
}

type PendingOwner = { jobId: string; selectedDeskId: string; assertedBy: string };

function hasCapturedEvidence(result: ImportResult): boolean {
	return Number(result.counts?.imported ?? 0) + Number(result.counts?.already_present ?? 0) > 0;
}

function jobErrors(errors: ImportResult["errors"]): Array<{ code: string; message: string; offset: number | null }> {
	return (errors ?? []).flatMap((item) => {
		if (!item || typeof item !== "object") return [];
		const entry = item as Record<string, unknown>;
		return [{ code: typeof entry.code === "string" ? entry.code : "import_error",
			message: typeof entry.message === "string" ? entry.message : "Inspect this job before retrying.",
			offset: typeof entry.at_offset === "number" ? entry.at_offset : null }];
	});
}

function formatCounts(counts: Record<string, unknown> | undefined): string {
	if (!counts) return "No counts reported";
	const entries = Object.entries(counts).filter((entry): entry is [string, number] => typeof entry[1] === "number");
	return entries.length ? entries.map(([key, value]) => `${key.replaceAll("_", " ")}: ${value}`).join(" · ") : "No counts reported";
}

export function SessionHistoryImportDialog({
	open,
	onOpenChange,
	taskId,
	workspaceId,
	summary,
}: {
	open: boolean;
	onOpenChange: (open: boolean) => void;
	taskId: string;
	workspaceId: string | null;
	summary: RuntimeTaskSessionSummary | null;
}): React.ReactElement {
	const binding = useMemo(
		() => summary?.nativeSessionBindings?.find((item) => item.providerId === "codex") ?? null,
		[summary?.nativeSessionBindings],
	);
	const [setup, setSetup] = useState<SetupResult | null>(null);
	const [sourceFile, setSourceFile] = useState("");
	const [nativeSessionId, setNativeSessionId] = useState("");
	const [mode, setMode] = useState<ImportMode>("full");
	const [desks, setDesks] = useState<NonNullable<ImportResult["desks"]>>([]);
	const [selectedDeskId, setSelectedDeskId] = useState("");
	const [preview, setPreview] = useState<ImportResult | null>(null);
	const [job, setJob] = useState<ImportResult | null>(null);
	const [consent, setConsent] = useState(false);
	const [ownerConsent, setOwnerConsent] = useState(false);
	const [assertedBy, setAssertedBy] = useState("");
	const [claimId, setClaimId] = useState<string | null>(null);
	const [ownershipError, setOwnershipError] = useState<string | null>(null);
	const [pendingOwner, setPendingOwner] = useState<PendingOwner | null>(null);
	const [ownerAttemptFailed, setOwnerAttemptFailed] = useState(false);
	const ownerClaimInFlight = useRef(false);
	const [busy, setBusy] = useState(false);
	const [error, setError] = useState<string | null>(null);
	const identityRevision = useRef(0);
	const client = getRuntimeTrpcClient(null);
	const runImport = async (action: SessionImportAction): Promise<ImportResult> =>
		await client.sessionImport.run.mutate(action) as ImportResult;

	useEffect(() => {
		if (!open) return;
		let cancelled = false;
		void client.sessionImport.setup.query().then(async (nextSetup) => {
			if (cancelled) return;
			setSetup(nextSetup);
			if (!nextSetup.configured) return;
			const result = await runImport({ action: "desks" });
			if (cancelled) return;
			if (result.status === "error") throw new Error(result.message ?? "Could not list registered desks.");
			setDesks(result.desks ?? []);
		}).catch(() => {
			if (!cancelled) setError("Could not load the host setup or registered desks.");
		});
		return () => { cancelled = true; };
	}, [client, open]);

	useEffect(() => {
		identityRevision.current += 1;
		setSourceFile(binding?.sourcePath ?? "");
		setNativeSessionId(binding?.nativeId ?? "");
		setSelectedDeskId("");
		setPreview(null);
		setJob(null);
		setConsent(false);
		setOwnerConsent(false);
		setClaimId(null);
		setPendingOwner(null);
		setOwnerAttemptFailed(false);
		setOwnershipError(null);
		setError(null);
	}, [binding?.nativeId, binding?.sourcePath, taskId, workspaceId]);

	useEffect(() => {
		if (!open) return;
		if (!sourceFile || !nativeSessionId) return;
		let cancelled = false;
		const savedJobId = sessionStorage.getItem(jobStorageKey(workspaceId, taskId, nativeSessionId, sourceFile));
		if (!savedJobId) return;
		void runImport({ action: "status", jobId: savedJobId }).then((result) => {
			if (cancelled) return;
			setJob(result);
			const saved = sessionStorage.getItem(pendingOwnerKey(savedJobId));
			if (saved) {
				try {
					const pending = JSON.parse(saved) as PendingOwner;
					if (pending.jobId === savedJobId && pending.selectedDeskId === result.attribution?.selected_desk_id) setPendingOwner(pending);
				} catch { sessionStorage.removeItem(pendingOwnerKey(savedJobId)); }
			}
		}).catch(() => { if (!cancelled) setError("Could not refresh the saved import job."); });
		return () => { cancelled = true; };
	}, [client, nativeSessionId, open, sourceFile, taskId, workspaceId]);

	useEffect(() => {
		if (!open || !job?.job_id || ["complete", "paused", "error"].includes(job.phase ?? "") || job.status === "error") return;
		const jobId = job.job_id;
		const timer = window.setInterval(() => {
			void runImport({ action: "status", jobId }).then(setJob).catch(() => setError("Could not refresh import progress."));
		}, FOLLOW_STATUS_INTERVAL_MS);
		return () => window.clearInterval(timer);
	}, [client, job?.job_id, job?.phase, open]);

	const resetPreview = () => {
		identityRevision.current += 1;
		setPreview(null);
		setJob(null);
		setConsent(false);
		setOwnerConsent(false);
		setClaimId(null);
		setPendingOwner(null);
		setOwnerAttemptFailed(false);
		setOwnershipError(null);
		setError(null);
	};

	const runPreview = async () => {
		const revision = identityRevision.current;
		setBusy(true);
		setError(null);
		try {
			const result = await runImport({
				action: "preview", sourceFile: sourceFile.trim(), nativeSessionId: nativeSessionId.trim(), selectedDeskId, mode,
			});
			if (revision !== identityRevision.current) return;
			if (result.status === "error") {
				setError(result.message ?? result.code ?? "The import preview failed.");
				setPreview(null);
			} else {
				setPreview(result);
			}
		} catch (caught) {
			if (revision === identityRevision.current) setError(caught instanceof Error ? caught.message : "The import preview failed.");
		} finally {
			setBusy(false);
		}
	};

	const applyPreview = async () => {
		if (!preview?.plan_token || !consent || !ownerConsent || !assertedBy.trim()) return;
		const revision = identityRevision.current;
		setBusy(true);
		setError(null);
		try {
			const result = await runImport({ action: "apply", planToken: preview.plan_token, consent: true });
			if (revision !== identityRevision.current) return;
			if (result.status === "error") {
				setError(result.message ?? result.code ?? "The import did not start.");
				return;
			}
			setJob(result);
			if (result.job_id) {
				sessionStorage.setItem(jobStorageKey(workspaceId, taskId, nativeSessionId, sourceFile), result.job_id);
				const pending = { jobId: result.job_id, selectedDeskId, assertedBy: assertedBy.trim() };
				sessionStorage.setItem(pendingOwnerKey(result.job_id), JSON.stringify(pending));
				setPendingOwner(pending);
				setOwnerAttemptFailed(false);
				const start = await runImport({ action: "follow", jobId: result.job_id });
				if (revision === identityRevision.current) setJob({ ...result, ...start });
			}
			setPreview(null);
		} catch (caught) {
			if (revision === identityRevision.current) setError(caught instanceof Error ? caught.message : "The import did not start.");
		} finally {
			setBusy(false);
		}
	};

	const stopFollowing = async () => {
		if (!job?.job_id) return;
		setBusy(true);
		setError(null);
		try {
			setJob(await runImport({ action: "stop", jobId: job.job_id }));
		} catch (caught) {
			setError(caught instanceof Error ? caught.message : "Could not stop future capture.");
		} finally {
			setBusy(false);
		}
	};

	const retryWorker = async () => {
		if (!job?.job_id) return;
		setBusy(true);
		setError(null);
		try {
			const start = await runImport({ action: "follow", jobId: job.job_id });
			setJob({ ...job, ...start });
		} catch (caught) {
			setError(caught instanceof Error ? caught.message : "Could not restart the import worker.");
		} finally {
			setBusy(false);
		}
	};

	const resumePausedJob = async () => {
		if (!job?.job_id) return;
		setBusy(true);
		setError(null);
		try {
			const resumed = await runImport({ action: "resume", jobId: job.job_id });
			if (resumed.status === "error") {
				setError(resumed.message ?? resumed.code ?? "Could not resume this job.");
				return;
			}
			const start = await runImport({ action: "follow", jobId: job.job_id });
			setJob({ ...resumed, ...start });
		} catch (caught) {
			setError(caught instanceof Error ? caught.message : "Could not resume this job.");
		} finally {
			setBusy(false);
		}
	};

	useEffect(() => {
		if (!open || !job?.job_id || !pendingOwner || ownerAttemptFailed || pendingOwner.jobId !== job.job_id ||
			job.attribution?.status === "asserted" || !hasCapturedEvidence(job) || ownerClaimInFlight.current) return;
		const revision = identityRevision.current;
		ownerClaimInFlight.current = true;
		void runImport({ action: "assert-owner", jobId: pendingOwner.jobId,
			selectedDeskId: pendingOwner.selectedDeskId, assertedBy: pendingOwner.assertedBy }).then((result) => {
			if (revision !== identityRevision.current) return;
			if (result.status === "error" || !result.claim_id) {
				setOwnershipError(result.message ?? result.code ?? "The desk ownership assertion was not recorded.");
				setOwnerAttemptFailed(true);
			} else {
				setClaimId(result.claim_id);
				setOwnershipError(null);
				setPendingOwner(null);
				sessionStorage.removeItem(pendingOwnerKey(pendingOwner.jobId));
			}
		}).catch((caught) => {
			if (revision === identityRevision.current) {
				setOwnershipError(caught instanceof Error ? caught.message : "The desk ownership assertion was not recorded.");
				setOwnerAttemptFailed(true);
			}
		}).finally(() => { ownerClaimInFlight.current = false; });
	}, [client, job, open, pendingOwner, ownerAttemptFailed]);

	const selectedDesk = preview?.attribution?.selected_desk_id ?? job?.attribution?.selected_desk_id;
	const coverage = preview?.coverage ?? job?.coverage;
	const currentTurnAnchor = preview?.current_turn_anchor;
	const recordedClaimId = claimId ?? (typeof job?.attribution?.claim_id === "string" ? job.attribution.claim_id : null);
	const jobMode = job?.coverage?.mode ?? mode;

	return (
		<Dialog open={open} onOpenChange={onOpenChange} contentClassName="max-w-xl">
			<DialogHeader title="Import session history" icon={<History size={16} />} />
			<DialogBody className="space-y-4 text-sm text-text-secondary">
				<p>Bring an exact Codex session into the registered desk selected by this Kanban host. Importing history does not grant a desk role or admit a session.</p>
				{!setup?.configured ? (
					<div className="rounded-md border border-status-orange/40 bg-status-orange/10 p-3 text-status-orange">
						<p>{setup?.message ?? "Checking host setup..."}</p>
						<a href="/session-import-setup.html" target="_blank" rel="noreferrer" className="underline">Host setup guide</a>
					</div>
				) : (
					<div className="rounded-md border border-border bg-surface-2 p-3 text-xs">
						<p>The host import service is ready. Preview verifies the exact session and selected desk.</p>
						<details className="mt-1"><summary>Host setup details</summary><p>Registered desk config: {setup.configPath}</p></details>
					</div>
				)}
				<label className="block space-y-1">
					<span className="text-text-primary">Source runtime</span>
					<select disabled className="w-full rounded-md border border-border bg-surface-2 p-2 text-text-primary"><option>Codex</option></select>
				</label>
				<label className="block space-y-1">
					<span className="text-text-primary">Registered desk</span>
					<select value={selectedDeskId} onChange={(event) => { setSelectedDeskId(event.target.value); resetPreview(); }} className="w-full rounded-md border border-border bg-surface-2 p-2 text-text-primary">
						<option value="">Choose a reviewed desk</option>
						{desks.map((desk) => <option key={desk.binding_key} value={desk.binding_key}>{desk.desk_label} · {desk.role} · {desk.repo_key}</option>)}
					</select>
				</label>
				<label className="block space-y-1">
					<span className="text-text-primary">Exact session file on this host</span>
					<input value={sourceFile} onChange={(event) => { const value = event.target.value; setSourceFile(value); if (!nativeSessionId.trim()) setNativeSessionId(sessionIdInPath(value) ?? ""); resetPreview(); }} placeholder="/absolute/path/to/rollout-session.jsonl" className="w-full rounded-md border border-border bg-surface-2 p-2 text-text-primary" />
				</label>
				<label className="block space-y-1">
					<span className="text-text-primary">Native session ID</span>
					<input value={nativeSessionId} onChange={(event) => { setNativeSessionId(event.target.value); resetPreview(); }} placeholder="Verified from session metadata" className="w-full rounded-md border border-border bg-surface-2 p-2 text-text-primary" />
				</label>
				<p className="text-xs">{binding ? "Source path and session ID came from this task’s observed native binding. Review them before preview." : "No native Codex binding is recorded for this chat. Select the exact file. A standard rollout filename can fill the session ID; preview verifies it against native metadata."}</p>
				<fieldset className="space-y-2">
					<legend className="mb-1 text-text-primary">Capture scope</legend>
					<label className="flex gap-2"><input type="radio" checked={mode === "full"} onChange={() => { setMode("full"); resetPreview(); }} />Full session</label>
					<label className="flex gap-2"><input type="radio" checked={mode === "current-turn-and-forward"} onChange={() => { setMode("current-turn-and-forward"); resetPreview(); }} />Current turn onward, including future turns</label>
				</fieldset>
				{preview ? (
					<div className="space-y-2 rounded-md border border-border-bright bg-surface-2 p-3">
						<p className="font-semibold text-text-primary">Preview</p>
						<p>Selected desk: {typeof selectedDesk === "string" ? (desks.find((desk) => desk.binding_key === selectedDesk)?.desk_label ?? selectedDesk) : "Unavailable"}</p>
						<p>Reviewed snapshot: bytes {coverage?.start_offset ?? 0}–{coverage?.reviewed_complete_end ?? "unknown"} of {coverage?.observed_size ?? "unknown"}. First bounded batch reaches byte {preview.next_batch_offset ?? "unknown"}.</p>
						{(coverage?.reviewed_partial_trailing_bytes ?? 0) > 0 ? <p>{coverage?.reviewed_partial_trailing_bytes} incomplete trailing bytes are excluded until a new reviewed snapshot.</p> : null}
						{(coverage?.pre_anchor_excluded_bytes ?? 0) > 0 ? <p>{coverage?.pre_anchor_excluded_bytes} bytes before the selected turn are excluded.</p> : null}
						{coverage?.compaction_policy ? <p>{coverage.compaction_policy}</p> : null}
						<p>First bounded batch preview: {formatCounts(preview.counts)}. These are not whole-session totals.</p>
						{currentTurnAnchor ? <p>Current turn begins at verified row offset {currentTurnAnchor.user_row_offset}; future turns will be followed.</p> : null}
						<label className="flex items-start gap-2 text-text-primary"><input type="checkbox" checked={consent} onChange={(event) => setConsent(event.target.checked)} />I approve importing this exact session into the displayed registered desk.</label>
						<label className="flex items-start gap-2 text-text-primary"><input type="checkbox" checked={ownerConsent} onChange={(event) => setOwnerConsent(event.target.checked)} />I assert that this session belongs to the selected desk. This records an owner claim; it does not admit an agent.</label>
						<label className="block space-y-1"><span>Your name or operator ID</span><input value={assertedBy} onChange={(event) => setAssertedBy(event.target.value)} className="w-full rounded-md border border-border bg-surface-1 p-2 text-text-primary" /></label>
					</div>
				) : null}
				{job ? <div className="space-y-1 rounded-md border border-border bg-surface-2 p-3">
					<p>Import status: {job.phase ?? job.status}.</p>
					<details className="text-xs"><summary>Job reference</summary><p className="break-all">{job.job_id}</p></details>
					<p>{job.worker_active ? (jobMode === "full" ? "Import worker is active." : "Future capture worker is active.") : job.phase === "following" ? "Waiting for capture worker heartbeat." : null}</p>
					{job.worker_active === false && !["complete", "paused", "error"].includes(job.phase ?? "") ? <p className="text-status-orange">No active worker is reported for this unfinished job.</p> : null}
					<p>Captured through byte {job.coverage?.next_offset ?? "unknown"} of {job.coverage?.observed_size ?? "unknown"}; reviewed complete end {job.coverage?.reviewed_complete_end ?? "unknown"}. {job.coverage?.complete_at_snapshot ? "Reviewed snapshot complete." : "Capture continues."}</p>
					{(job.coverage?.partial_trailing_bytes ?? 0) > 0 ? <p>{job.coverage?.partial_trailing_bytes} trailing bytes are waiting for a complete native row.</p> : null}
					<p>{formatCounts(job.counts)}</p>
					{job.status === "error" || job.phase === "error" ? <p role="alert" className="text-status-red">{job.message ?? job.code ?? "The import stopped with an error."}</p> : null}
					{jobErrors(job.errors).map((entry, index) => <p key={`${entry.code}-${index}`} role="alert" className="text-status-red">{entry.code}: {entry.message}{entry.offset !== null ? ` (byte ${entry.offset})` : ""}</p>)}
					{(job.index_pending_episodes ?? 0) > 0 ? <p className="text-status-orange">{job.index_pending_episodes} captured episode(s) await indexing. Retry the worker to repair this job.</p> : null}
					<p>Desk owner claim: {recordedClaimId ? "recorded" : pendingOwner ? "awaiting first captured visible event" : "not yet recorded"}.</p>
					{recordedClaimId ? <details className="text-xs"><summary>Owner claim reference</summary><p className="break-all">{recordedClaimId}</p></details> : null}
						{pendingOwner && ["complete", "error", "paused"].includes(job.phase ?? "") && !hasCapturedEvidence(job) ? <p role="alert" className="text-status-orange">No visible source event was captured, so desk ownership cannot be asserted for this job.</p> : null}
						{ownershipError ? <p role="alert" className="text-status-red">{ownershipError}</p> : null}
					</div> : null}
				{error ? <p role="alert" className="text-status-red">{error}</p> : null}
			</DialogBody>
			<DialogFooter>
				{job && !["complete", "paused", "error"].includes(job.phase ?? "") ? <Button variant="default" disabled={busy} onClick={() => void stopFollowing()}>{jobMode === "full" ? "Stop import" : "Stop future capture"}</Button> : null}
				{job && job.worker_active === false && !["complete", "paused", "error"].includes(job.phase ?? "") ? <Button variant="default" disabled={busy} onClick={() => void retryWorker()}>Retry worker</Button> : null}
				{job?.phase === "paused" ? <Button variant="default" disabled={busy} onClick={() => void resumePausedJob()}>{jobMode === "full" ? "Resume import" : "Resume capture"}</Button> : null}
				{job && pendingOwner && ownerAttemptFailed && hasCapturedEvidence(job) ? <Button variant="default" disabled={busy} onClick={() => { setOwnershipError(null); setOwnerAttemptFailed(false); }}>Retry owner claim</Button> : null}
				<Button variant="default" disabled={!setup?.configured || busy || !sourceFile.trim() || !nativeSessionId.trim() || !selectedDeskId} onClick={() => void runPreview()}>{busy ? <Spinner size={14} /> : "Preview"}</Button>
				<Button variant="primary" disabled={!preview?.plan_token || !consent || !ownerConsent || !assertedBy.trim() || busy} onClick={() => void applyPreview()}>Apply import</Button>
			</DialogFooter>
		</Dialog>
	);
}
