import { useState } from "react";
import { Button } from "@/components/ui/button";
import type { DeskDirectory, DeskProfile, SessionContextInput } from "@/hooks/use-desk-registry";

const field = "w-full rounded-md border border-border bg-surface-2 p-3 text-text-primary";
const values = (text: string) => [
	...new Set(
		text
			.split("\n")
			.map((x) => x.trim())
			.filter(Boolean),
	),
];
export function DeskSessionDialogue({
	desk,
	directory,
	onSave,
	onCancel,
}: {
	desk: DeskProfile;
	directory: DeskDirectory;
	onSave: (value: SessionContextInput) => Promise<void>;
	onCancel: () => void;
}) {
	const [step, setStep] = useState(0);
	const [session, setSession] = useState("");
	const [repos, setRepos] = useState("");
	const [adrs, setAdrs] = useState("");
	const [cards, setCards] = useState("");
	const [provider, setProvider] = useState("");
	const [model, setModel] = useState("");
	const [account, setAccount] = useState("");
	const [saving, setSaving] = useState(false);
	const [error, setError] = useState<string | null>(null);
	const current = directory.contexts.find((c) => c.source_session_id === session);
	const select = (id: string) => {
		setSession(id);
		const c = directory.contexts.find((x) => x.source_session_id === id);
		setRepos(c?.repos.join("\n") ?? "");
		setAdrs(c?.adrs.join("\n") ?? "");
		setCards(c?.cards.join("\n") ?? "");
		setProvider(c?.provider ?? "");
		setModel(c?.model ?? "");
		setAccount(c?.account_ref ?? "");
	};
	const submit = async () => {
		setSaving(true);
		setError(null);
		try {
			await onSave({
				source_session_id: session,
				desk_id: desk.desk_id,
				repos: values(repos),
				adrs: values(adrs),
				cards: values(cards),
				provider: provider.trim() || null,
				model: model.trim() || null,
				account_ref: account.trim() || null,
				expected_version: current?.version ?? 0,
				source_ref: "operator:desk-menu",
			});
		} catch (e) {
			setError(e instanceof Error ? e.message : String(e));
		} finally {
			setSaving(false);
		}
	};
	const titles = [
		"Choose a captured session",
		"Choose its repositories",
		"Link its work",
		"Record its provenance",
		"Review session context",
	];
	return (
		<section className="mx-auto flex w-full max-w-xl flex-col gap-5 p-6" aria-label="Session context">
			<p className="text-sm text-text-secondary">
				{desk.name} · Step {step + 1} of 5
			</p>
			<h1 className="text-xl font-semibold">{titles[step]}</h1>
			{step === 0 ? (
				<>
					<label>
						Captured session
						<select className={field} value={session} onChange={(e) => select(e.target.value)}>
							<option value="">Select a session</option>
							{directory.sessions.map((s) => (
								<option key={s.source_session_id} value={s.source_session_id}>
									{s.runtime} · {s.native_id}
								</option>
							))}
						</select>
					</label>
					{directory.session_limit_reached ? (
						<p role="status">
							The session list is bounded to 500 entries. Ask the operator for a targeted registration if this
							session is not listed.
						</p>
					) : null}
					<p className="text-sm text-text-secondary">
						This links retained evidence to a desk profile. It does not admit or launch a session.
					</p>
				</>
			) : null}
			{step === 1 ? (
				<label>
					Repositories, one per line
					<textarea
						className={field}
						rows={5}
						value={repos}
						onChange={(e) => setRepos(e.target.value)}
						placeholder={"example-app\nexample-lib"}
					/>
					<p className="text-sm text-text-secondary">Leave empty for work that is not repository-specific.</p>
				</label>
			) : null}
			{step === 2 ? (
				<>
					<label>
						ADR references, one per line
						<textarea className={field} rows={3} value={adrs} onChange={(e) => setAdrs(e.target.value)} />
					</label>
					<label>
						Card references, one per line
						<textarea className={field} rows={3} value={cards} onChange={(e) => setCards(e.target.value)} />
					</label>
					<p className="text-sm text-text-secondary">
						Use repository-qualified IDs or URLs so links remain unambiguous.
					</p>
				</>
			) : null}
			{step === 3 ? (
				<>
					<label>
						Provider
						<input
							className={field}
							value={provider}
							onChange={(e) => setProvider(e.target.value)}
							maxLength={512}
						/>
					</label>
					<label>
						Model
						<input className={field} value={model} onChange={(e) => setModel(e.target.value)} maxLength={512} />
					</label>
					<label>
						Account reference
						<input
							className={field}
							value={account}
							onChange={(e) => setAccount(e.target.value)}
							maxLength={512}
						/>
					</label>
					<p className="text-sm text-text-secondary">
						Optional provenance, not credentials. Leave unknown values blank; these do not change the desk’s
						identity or access.
					</p>
				</>
			) : null}
			{step === 4 ? (
				<div className="rounded-md bg-surface-1 p-4">
					<p>Desk: {desk.name}</p>
					<p>Repositories: {values(repos).join(", ") || "None"}</p>
					<p>
						ADR links: {values(adrs).length} · Card links: {values(cards).length}
					</p>
					<p>
						Provider: {provider || "Unknown"} · Model: {model || "Unknown"}
					</p>
					<p className="mt-3 text-sm text-text-secondary">
						The prior context remains in history. This changes the current views, not the transcript.
					</p>
				</div>
			) : null}
			{error ? (
				<p role="alert" className="text-status-red">
					{error}
				</p>
			) : null}
			<div className="flex justify-between">
				<Button disabled={saving} onClick={onCancel}>
					Cancel
				</Button>
				<div className="flex gap-2">
					{step > 0 ? (
						<Button disabled={saving} onClick={() => setStep(step - 1)}>
							Back
						</Button>
					) : null}
					{step < 4 ? (
						<Button variant="primary" disabled={!session} onClick={() => setStep(step + 1)}>
							Next
						</Button>
					) : (
						<Button variant="primary" disabled={saving} onClick={() => void submit()}>
							{saving ? "Saving…" : "Save context"}
						</Button>
					)}
				</div>
			</div>
		</section>
	);
}
