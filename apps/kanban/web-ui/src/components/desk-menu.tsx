import { useState } from "react";
import { DeskBindDialogue } from "@/components/desk-bind-dialogue";
import { DeskDialogue } from "@/components/desk-dialogue";
import { DeskMemoryView } from "@/components/desk-memory-view";
import { DeskRoleDialogue, type RoleInput } from "@/components/desk-role-dialogue";
import { DeskSessionDialogue } from "@/components/desk-session-dialogue";
import { Button } from "@/components/ui/button";
import { useDeskRegistry } from "@/hooks/use-desk-registry";
import { getRuntimeTrpcClient } from "@/runtime/trpc-client";
export function DeskMenu() {
	const registry = useDeskRegistry();
	const [selected, setSelected] = useState<string | null>(null);
	const [mode, setMode] = useState<"menu" | "create" | "edit" | "session" | "role" | "bind">("menu");
	const desk = registry.data?.desks.find((d) => d.desk_id === selected);
	if (registry.error)
		return (
			<section className="p-6">
				<h1 className="text-xl font-semibold">Desks</h1>
				<p role="alert" className="my-4 text-status-red">
					{registry.error}
				</p>
				<Button onClick={() => void registry.refresh()}>Retry</Button>
			</section>
		);
	if (!registry.data)
		return (
			<p className="p-6" role="status">
				Loading desks…
			</p>
		);
	// Registry metadata is optional: only a directory that says it is legacy hides the portable actions.
	const portable = registry.data.registry_mode !== "legacy";
	// Without a reported roster version, 0 is sent; the registry refuses a stale version, never overwrites.
	const rosterVersion = registry.data.roster_version ?? 0;
	const addRole = async (value: RoleInput) => {
		await getRuntimeTrpcClient(null).desks.saveRole.mutate(value);
		await registry.refresh();
	};
	if (mode === "create" || mode === "edit")
		return (
			<DeskDialogue
				key={mode + selected}
				existing={mode === "edit" ? desk : undefined}
				roles={registry.data.roles}
				rosterVersion={rosterVersion}
				onAddRole={portable ? addRole : undefined}
				onCancel={() => setMode("menu")}
				onSave={async (value) => {
					await registry.save(value);
					setSelected(value.desk_id);
					setMode("menu");
				}}
			/>
		);
	if (mode === "role" && portable)
		return (
			<DeskRoleDialogue
				roles={registry.data.roles}
				rosterVersion={rosterVersion}
				onCancel={() => setMode("menu")}
				onSave={async (value) => {
					await addRole(value);
					setMode("menu");
				}}
			/>
		);
	if (mode === "bind" && desk && portable)
		return (
			<DeskBindDialogue
				desk={desk}
				onCancel={() => setMode("menu")}
				onBind={async (value) => {
					await getRuntimeTrpcClient(null).desks.bind.mutate(value);
					await registry.refresh();
					setMode("menu");
				}}
			/>
		);
	if (mode === "session" && desk)
		return (
			<DeskSessionDialogue
				desk={desk}
				directory={registry.data}
				onCancel={() => setMode("menu")}
				onSave={async (value) => {
					await registry.annotate(value);
					setMode("menu");
				}}
			/>
		);
	const bound = desk ? registry.data.bindings?.filter((b) => b.desk_id === desk.desk_id) : undefined;
	return (
		<section className="mx-auto flex w-full max-w-5xl flex-col gap-5 overflow-y-auto p-6">
			<header className="flex items-center justify-between gap-4">
				<div>
					<h1 className="text-xl font-semibold">Desks</h1>
					<p className="text-sm text-text-secondary">
						Stable agents, shared memory, no dedicated repository required.
					</p>
				</div>
				<div className="flex gap-2">
					{portable ? <Button onClick={() => setMode("role")}>Add role</Button> : null}
					<Button
						variant="primary"
						onClick={() => {
							setSelected(null);
							setMode("create");
						}}
					>
						Create desk
					</Button>
				</div>
			</header>
			{!portable ? (
				<p role="status" className="text-sm text-text-secondary">
					Memory access for these desks is governed by the operator catalog, not by desk settings.
				</p>
			) : null}
			<div className="flex flex-wrap gap-2">
				{registry.data.desks.map((d) => (
					<Button
						key={d.desk_id}
						variant={selected === d.desk_id ? "primary" : "default"}
						onClick={() => setSelected(d.desk_id)}
					>
						{d.name}
					</Button>
				))}
			</div>
			{!registry.data.desks.length ? <p>Create your first desk to give an agent a name and purpose.</p> : null}
			{desk ? (
				<>
					<section className="rounded-lg bg-surface-1 p-4">
						<div className="flex items-center justify-between gap-4">
							<h2 className="font-semibold">{desk.name}</h2>
							<Button onClick={() => setMode("edit")}>Edit desk</Button>
						</div>
						<p className="my-3 whitespace-pre-wrap">{desk.description}</p>
						<p className="text-sm text-text-secondary">
							{registry.data.roles.find((r) => r.role_id === desk.role)?.label ?? desk.role} · Version{" "}
							{desk.version}
						</p>
						<p className="text-sm text-text-secondary">
							Repositories: {desk.repos.length ? desk.repos.join(", ") : "None"}
						</p>
						{portable ? (
							<p className="text-sm text-text-secondary">
								Capture {desk.capture ? "on" : "off"} · Memory proposals{" "}
								{desk.memory_write ? "allowed" : "not allowed"}
							</p>
						) : null}
						<div className="mt-3 flex items-center justify-between gap-3">
							<span>
								{registry.data.contexts.filter((c) => c.desk_id === desk.desk_id).length} linked sessions
							</span>
							<Button onClick={() => setMode("session")}>Link session context</Button>
						</div>
						{portable ? (
							<div className="mt-3 flex items-center justify-between gap-3">
								<span>{bound ? `${bound.length} bound sessions` : "Bound sessions not reported"}</span>
								<Button onClick={() => setMode("bind")}>Bind session</Button>
							</div>
						) : null}
					</section>
					<DeskMemoryView key={desk.desk_id} desk={desk} directory={registry.data} />
				</>
			) : null}
		</section>
	);
}
