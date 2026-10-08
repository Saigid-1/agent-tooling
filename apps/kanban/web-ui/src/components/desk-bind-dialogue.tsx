import type { RuntimeAppRouterInputs } from "@runtime-trpc";
import { useState } from "react";
import { Button } from "@/components/ui/button";
import type { DeskProfile } from "@/hooks/use-desk-registry";

export type SessionBindingInput = RuntimeAppRouterInputs["desks"]["bind"];
type Props = {
	desk: DeskProfile;
	onBind: (value: SessionBindingInput) => Promise<void>;
	onCancel: () => void;
};
const field = "w-full rounded-md border border-border bg-surface-2 p-3 text-text-primary focus:border-border-focus";
const SESSION_ID = /^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$/;
const bounded = (value: string, max: number) => value.trim().length > 0 && value.trim().length <= max;

/** Operator binding: admits exactly one native session to this desk's memory. Never a model tool. */
export function DeskBindDialogue({ desk, onBind, onCancel }: Props) {
	const [step, setStep] = useState(0);
	const [session, setSession] = useState("");
	const [parent, setParent] = useState("");
	const [harness, setHarness] = useState("");
	const [provider, setProvider] = useState("");
	const [model, setModel] = useState("");
	const [workspace, setWorkspace] = useState("");
	const [saving, setSaving] = useState(false);
	const [error, setError] = useState<string | null>(null);
	const titles = ["Identify the session", "Choose its target", "Confirm its workspace", "Review the binding"];
	const valid =
		step === 0
			? SESSION_ID.test(session.trim()) &&
				(!parent.trim() || (SESSION_ID.test(parent.trim()) && parent.trim() !== session.trim()))
			: step === 1
				? bounded(harness, 256) && bounded(provider, 256) && bounded(model, 256)
				: step === 2
					? bounded(workspace, 1024)
					: true;
	const submit = async () => {
		setSaving(true);
		setError(null);
		try {
			await onBind({
				harness: harness.trim(),
				provider: provider.trim(),
				model: model.trim(),
				native_session_id: session.trim(),
				desk_id: desk.desk_id,
				source: "operator",
				workspace: workspace.trim(),
				parent_session_id: parent.trim() || null,
			});
		} catch (e) {
			setError(e instanceof Error ? e.message : String(e));
		} finally {
			setSaving(false);
		}
	};
	return (
		<section className="mx-auto flex w-full max-w-xl flex-col gap-5 p-6" aria-label="Session binding">
			<p className="text-sm text-text-secondary">
				{desk.name} · Step {step + 1} of 4
			</p>
			<h1 className="text-xl font-semibold" aria-live="polite">
				{titles[step]}
			</h1>
			{step === 0 ? (
				<>
					<label className="flex flex-col gap-2">
						Native session ID
						<input
							className={field}
							maxLength={128}
							value={session}
							onChange={(e) => setSession(e.target.value)}
							placeholder="The harness's own session ID"
						/>
					</label>
					<label className="flex flex-col gap-2">
						Parent session ID (optional)
						<input className={field} maxLength={128} value={parent} onChange={(e) => setParent(e.target.value)} />
					</label>
					<p className="text-sm text-text-secondary">
						Use letters, digits, hyphens or underscores, as the harness reports them.
					</p>
				</>
			) : null}
			{step === 1 ? (
				<>
					<label className="flex flex-col gap-2">
						Harness
						<input
							className={field}
							maxLength={256}
							value={harness}
							onChange={(e) => setHarness(e.target.value)}
							placeholder="claude-code"
						/>
					</label>
					<label className="flex flex-col gap-2">
						Provider
						<input
							className={field}
							maxLength={256}
							value={provider}
							onChange={(e) => setProvider(e.target.value)}
						/>
					</label>
					<label className="flex flex-col gap-2">
						Model
						<input className={field} maxLength={256} value={model} onChange={(e) => setModel(e.target.value)} />
					</label>
				</>
			) : null}
			{step === 2 ? (
				<label className="flex flex-col gap-2">
					Workspace
					<input
						className={field}
						maxLength={1024}
						value={workspace}
						onChange={(e) => setWorkspace(e.target.value)}
						placeholder="/path/to/workspace"
					/>
				</label>
			) : null}
			{step === 3 ? (
				<div className="flex flex-col gap-2 rounded-lg bg-surface-1 p-4">
					<p>Desk: {desk.name}</p>
					<p>Session: {session.trim()}</p>
					<p>Parent session: {parent.trim() || "None"}</p>
					<p>
						Target: {harness.trim()} · {provider.trim()} · {model.trim()}
					</p>
					<p>Workspace: {workspace.trim()}</p>
					<p className="mt-2 text-sm text-text-secondary">
						Binding admits exactly this session to this desk's memory. A session stays bound to one desk and
						target; binding it elsewhere is refused.
					</p>
				</div>
			) : null}
			{error ? (
				<p role="alert" className="text-status-red">
					{error}
				</p>
			) : null}
			<div className="flex justify-between gap-3">
				<Button disabled={saving} onClick={onCancel}>
					Cancel
				</Button>
				<div className="flex gap-2">
					{step > 0 ? (
						<Button disabled={saving} onClick={() => setStep(step - 1)}>
							Back
						</Button>
					) : null}
					{step < 3 ? (
						<Button variant="primary" disabled={!valid} onClick={() => setStep(step + 1)}>
							Next
						</Button>
					) : (
						<Button variant="primary" disabled={saving} onClick={() => void submit()}>
							{saving ? "Binding…" : "Bind session"}
						</Button>
					)}
				</div>
			</div>
		</section>
	);
}
