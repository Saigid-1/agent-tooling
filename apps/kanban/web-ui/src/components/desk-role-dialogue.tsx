import type { RuntimeAppRouterInputs } from "@runtime-trpc";
import { useState } from "react";
import { Button } from "@/components/ui/button";
import type { DeskDirectory } from "@/hooks/use-desk-registry";

export type RoleInput = RuntimeAppRouterInputs["desks"]["saveRole"];
type Props = {
	roles: DeskDirectory["roles"];
	rosterVersion: number;
	onSave: (value: RoleInput) => Promise<void>;
	onCancel: () => void;
};
const field = "w-full rounded-md border border-border bg-surface-2 p-3 text-text-primary focus:border-border-focus";
const slug = (label: string) =>
	label
		.trim()
		.toLowerCase()
		.replace(/[^a-z0-9]+/g, "-")
		.replace(/^-+|-+$/g, "")
		.slice(0, 128);

/** Adds one role to the operator's configured roster. Roles are templates; they grant nothing. */
export function DeskRoleDialogue({ roles, rosterVersion, onSave, onCancel }: Props) {
	const [label, setLabel] = useState("");
	const [roleId, setRoleId] = useState("");
	const [edited, setEdited] = useState(false);
	const [purpose, setPurpose] = useState("");
	const [saving, setSaving] = useState(false);
	const [error, setError] = useState<string | null>(null);
	const id = edited ? roleId : slug(label);
	const duplicate = roles.some((r) => r.role_id === id);
	const valid =
		label.trim().length > 0 &&
		label.length <= 128 &&
		id.length > 0 &&
		id.length <= 128 &&
		id.trim() === id &&
		!duplicate &&
		purpose.length <= 1000;
	const submit = async () => {
		setSaving(true);
		setError(null);
		try {
			await onSave({ role_id: id, label: label.trim(), purpose: purpose.trim(), expected_version: rosterVersion });
		} catch (e) {
			setError(e instanceof Error ? e.message : String(e));
		} finally {
			setSaving(false);
		}
	};
	return (
		<section className="mx-auto flex w-full max-w-xl flex-col gap-5 p-6" aria-label="Add a role">
			<h1 className="text-xl font-semibold">Add a role</h1>
			<label className="flex flex-col gap-2">
				Role name
				<input
					className={field}
					maxLength={128}
					value={label}
					onChange={(e) => setLabel(e.target.value)}
					placeholder="Release helper"
				/>
			</label>
			<label className="flex flex-col gap-2">
				Role ID
				<input
					className={field}
					maxLength={128}
					value={id}
					onChange={(e) => {
						setEdited(true);
						setRoleId(e.target.value);
					}}
				/>
			</label>
			{duplicate ? (
				<p role="status" className="text-sm text-text-secondary">
					This role ID is already in the roster.
				</p>
			) : null}
			<label className="flex flex-col gap-2">
				Purpose
				<textarea
					className={field}
					maxLength={1000}
					rows={4}
					value={purpose}
					onChange={(e) => setPurpose(e.target.value)}
					placeholder="What desks with this role are for"
				/>
			</label>
			<p className="text-sm text-text-secondary">
				A role is a template in this registry's roster. It does not grant memory access or admit a session.
			</p>
			{error ? (
				<p role="alert" className="text-status-red">
					{error}
				</p>
			) : null}
			<div className="flex justify-between gap-3">
				<Button disabled={saving} onClick={onCancel}>
					Cancel
				</Button>
				<Button variant="primary" disabled={saving || !valid} onClick={() => void submit()}>
					{saving ? "Saving…" : "Add role"}
				</Button>
			</div>
		</section>
	);
}
