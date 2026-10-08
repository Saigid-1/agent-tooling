import type { ReactElement } from "react";

import { NativeSelect } from "@/components/ui/native-select";
import { useDeskRegistry } from "@/hooks/use-desk-registry";

const NO_DESK = "";

/**
 * Task desk selector. The list comes from the configured desk registry; choosing a desk
 * binds the task's agent session to that desk's memory when the task starts.
 */
export function TaskDeskPicker({
	id,
	deskId,
	onDeskIdChange,
	disabled = false,
}: {
	id: string;
	deskId: string | undefined;
	onDeskIdChange: (value: string | undefined) => void;
	disabled?: boolean;
}): ReactElement {
	const registry = useDeskRegistry();
	const desks = registry.data?.desks ?? [];
	const known = deskId === undefined || desks.some((desk) => desk.desk_id === deskId);
	const loading = !registry.data && !registry.error;
	return (
		<div>
			<label htmlFor={id} className="text-[11px] text-text-secondary block mb-1">
				Desk
			</label>
			<NativeSelect
				id={id}
				size="sm"
				fill
				value={deskId ?? NO_DESK}
				disabled={disabled || loading || (Boolean(registry.error) && deskId === undefined)}
				onChange={(event) => {
					const value = event.currentTarget.value;
					onDeskIdChange(value === NO_DESK ? undefined : value);
				}}
			>
				<option value={NO_DESK}>{loading ? "Loading desks…" : "No desk"}</option>
				{desks.map((desk) => (
					<option key={desk.desk_id} value={desk.desk_id}>
						{desk.name} ({desk.role})
					</option>
				))}
				{!known && deskId !== undefined ? <option value={deskId}>Unknown desk ({deskId})</option> : null}
			</NativeSelect>
			{registry.error ? (
				<p role="status" className="text-[11px] text-text-tertiary mt-1 mb-0">
					Desks unavailable: {registry.error}
				</p>
			) : (
				<p className="text-[11px] text-text-tertiary mt-1 mb-0">
					With a desk, the agent session is bound to that desk's memory when the task starts.
				</p>
			)}
		</div>
	);
}
