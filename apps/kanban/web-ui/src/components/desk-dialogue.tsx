import { useState } from "react";
import { DeskRoleDialogue, type RoleInput } from "@/components/desk-role-dialogue";
import { Button } from "@/components/ui/button";
import type { DeskDirectory, DeskInput, DeskProfile } from "@/hooks/use-desk-registry";

type Props = {
	existing?: DeskProfile;
	/** The configured roster from the registry; the dialogue never supplies its own roles. */
	roles: DeskDirectory["roles"];
	rosterVersion?: number;
	onAddRole?: (value: RoleInput) => Promise<void>;
	onSave: (value: DeskInput) => Promise<void>;
	onCancel: () => void;
};
const inputClass =
	"w-full rounded-md border border-border bg-surface-2 p-3 text-text-primary focus:border-border-focus";
const REPOS_MAX = 32;
const CONTEXT_DOC_BYTES = 16384;
const REVIEW = 3;
const ADD_ROLE = 7;
const lines = (text: string) => [
	...new Set(
		text
			.split("\n")
			.map((x) => x.trim())
			.filter(Boolean),
	),
];
const bytes = (text: string) => new TextEncoder().encode(text).length;

/**
 * Name → Description → Role → Review, one concern per screen. Repositories,
 * memory settings and the context document are optional screens reached from
 * the review, each returning to it.
 */
export function DeskDialogue({ existing, roles, rosterVersion, onAddRole, onSave, onCancel }: Props) {
	const [step, setStep] = useState(0);
	const [name, setName] = useState(existing?.name ?? "");
	const [description, setDescription] = useState(existing?.description ?? "");
	const [role, setRole] = useState(existing?.role ?? roles[0]?.role_id ?? "");
	const [repos, setRepos] = useState((existing?.repos ?? []).join("\n"));
	// A new desk captures and may propose memory by default; the memory screen switches either off.
	// An existing desk keeps its stored settings.
	const [capture, setCapture] = useState(existing?.capture ?? true);
	const [memoryWrite, setMemoryWrite] = useState(existing?.memory_write ?? true);
	const [contextDoc, setContextDoc] = useState(existing?.context_doc ?? "");
	const [deskId] = useState(existing?.desk_id ?? `desk:${crypto.randomUUID()}`);
	const [saving, setSaving] = useState(false);
	const [error, setError] = useState<string | null>(null);
	const titles = [
		"Name your agent",
		"Describe its purpose",
		"Choose its role",
		"Review your desk",
		"Choose its repositories",
		"Choose its memory settings",
		"Add a context document",
	];
	const repoList = lines(repos);
	const docBytes = bytes(contextDoc);
	const valid =
		step === 0
			? name.trim().length > 0
			: step === 1
				? description.trim().length > 0
				: step === 2
					? Boolean(role)
					: step === 4
						? repoList.length <= REPOS_MAX && repoList.every((r) => r.length <= 512)
						: step === 6
							? docBytes <= CONTEXT_DOC_BYTES
							: true;
	const roleLabel = roles.find((r) => r.role_id === role)?.label ?? role;
	const submit = async () => {
		setSaving(true);
		setError(null);
		try {
			await onSave({
				desk_id: deskId,
				name: name.trim(),
				description: description.trim(),
				role,
				repos: repoList,
				capture,
				memory_write: memoryWrite,
				context_doc: contextDoc.trim() ? contextDoc : null,
				expected_version: existing?.version ?? 0,
			});
		} catch (e) {
			setError(e instanceof Error ? e.message : String(e));
		} finally {
			setSaving(false);
		}
	};
	if (step === ADD_ROLE && onAddRole)
		return (
			<DeskRoleDialogue
				roles={roles}
				rosterVersion={rosterVersion ?? 0}
				onCancel={() => setStep(2)}
				onSave={async (value) => {
					await onAddRole(value);
					setRole(value.role_id);
					setStep(2);
				}}
			/>
		);
	const optional = step > REVIEW;
	return (
		<section className="mx-auto flex w-full max-w-xl flex-col gap-5 p-6" aria-label="Desk configuration">
			<p className="text-sm text-text-secondary">{optional ? "Optional setting" : `Step ${step + 1} of 4`}</p>
			<h1 className="text-xl font-semibold" aria-live="polite">
				{titles[step]}
			</h1>
			{step === 0 ? (
				<label className="flex flex-col gap-2">
					Agent name
					<input
						className={inputClass}
						maxLength={120}
						value={name}
						onChange={(e) => setName(e.target.value)}
						placeholder="Release helper"
					/>
				</label>
			) : null}
			{step === 1 ? (
				<label className="flex flex-col gap-2">
					Agent description
					<textarea
						className={inputClass}
						maxLength={4000}
						rows={7}
						value={description}
						onChange={(e) => setDescription(e.target.value)}
						placeholder="What should this agent help with?"
					/>
				</label>
			) : null}
			{step === 2 ? (
				<>
					<label className="flex flex-col gap-2">
						Role
						<select className={inputClass} value={role} onChange={(e) => setRole(e.target.value)}>
							{role && !roles.some((r) => r.role_id === role) ? (
								<option value={role} disabled>
									{role} (not in the configured roster)
								</option>
							) : null}
							{roles.map((r) => (
								<option key={r.role_id} value={r.role_id}>
									{r.label}
								</option>
							))}
						</select>
						<p className="text-sm text-text-secondary">{roles.find((r) => r.role_id === role)?.purpose}</p>
					</label>
					{onAddRole ? (
						<div>
							<Button onClick={() => setStep(ADD_ROLE)}>Add a role</Button>
						</div>
					) : null}
				</>
			) : null}
			{step === REVIEW ? (
				<div className="flex flex-col gap-3 rounded-lg bg-surface-1 p-4">
					<h2 className="font-semibold">{name}</h2>
					<p className="whitespace-pre-wrap">{description}</p>
					<p>{roleLabel}</p>
					<div className="flex items-center justify-between gap-3">
						<span>Repositories: {repoList.length ? repoList.join(", ") : "None"}</span>
						<Button size="sm" onClick={() => setStep(4)}>
							Change repositories
						</Button>
					</div>
					<div className="flex items-center justify-between gap-3">
						<span>
							Capture {capture ? "on" : "off"} · Memory proposals {memoryWrite ? "allowed" : "not allowed"}
						</span>
						<Button size="sm" onClick={() => setStep(5)}>
							Change memory
						</Button>
					</div>
					<div className="flex items-center justify-between gap-3">
						<span>Context document: {contextDoc.trim() ? `${docBytes} bytes` : "None"}</span>
						<Button size="sm" onClick={() => setStep(6)}>
							Change context
						</Button>
					</div>
					<p className="text-sm text-text-secondary">
						No repository is required. Sessions can be linked to this desk, and their records viewed by desk,
						repository, or workspace. Creating a profile does not launch an agent or admit a session.
					</p>
				</div>
			) : null}
			{step === 4 ? (
				<label className="flex flex-col gap-2">
					Repositories, one per line
					<textarea
						className={inputClass}
						rows={6}
						value={repos}
						onChange={(e) => setRepos(e.target.value)}
						placeholder={"repository-name\nanother-repository"}
					/>
					<span className="text-sm text-text-secondary">
						Optional, up to {REPOS_MAX}. Leave empty for work that is not repository-specific.
					</span>
				</label>
			) : null}
			{step === 5 ? (
				<fieldset className="flex flex-col gap-3">
					<legend className="sr-only">Memory settings</legend>
					<label className="flex items-center gap-2">
						<input type="checkbox" checked={capture} onChange={(e) => setCapture(e.target.checked)} />
						Capture sessions bound to this desk
					</label>
					<label className="flex items-center gap-2">
						<input type="checkbox" checked={memoryWrite} onChange={(e) => setMemoryWrite(e.target.checked)} />
						Allow bound sessions to propose memory
					</label>
					<p className="text-sm text-text-secondary">
						These settings apply only to sessions an operator binds to this desk.
					</p>
				</fieldset>
			) : null}
			{step === 6 ? (
				<label className="flex flex-col gap-2">
					Context document
					<textarea
						className={inputClass}
						rows={10}
						value={contextDoc}
						onChange={(e) => setContextDoc(e.target.value)}
						placeholder="Standing context a bound session receives with this desk"
					/>
					<span className="text-sm text-text-secondary" role={docBytes > CONTEXT_DOC_BYTES ? "alert" : undefined}>
						{docBytes} of {CONTEXT_DOC_BYTES} bytes
					</span>
				</label>
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
					{optional ? (
						<Button variant="primary" disabled={!valid} onClick={() => setStep(REVIEW)}>
							Done
						</Button>
					) : (
						<>
							{step > 0 ? (
								<Button disabled={saving} onClick={() => setStep(step - 1)}>
									Back
								</Button>
							) : null}
							{step < REVIEW ? (
								<Button variant="primary" disabled={!valid} onClick={() => setStep(step + 1)}>
									Next
								</Button>
							) : (
								<Button variant="primary" disabled={saving} onClick={() => void submit()}>
									{saving ? "Saving…" : existing ? "Save desk" : "Create desk"}
								</Button>
							)}
						</>
					)}
				</div>
			</div>
		</section>
	);
}
