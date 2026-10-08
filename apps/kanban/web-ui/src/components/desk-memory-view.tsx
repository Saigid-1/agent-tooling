import type { RuntimeAppRouterOutputs } from "@runtime-trpc";
import { useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import type { DeskDirectory, DeskProfile } from "@/hooks/use-desk-registry";
import { getRuntimeTrpcClient } from "@/runtime/trpc-client";
export function DeskMemoryView({ desk, directory }: { desk: DeskProfile; directory: DeskDirectory }) {
	const [kind, setKind] = useState<"agent" | "repo" | "global">("agent");
	const [repo, setRepo] = useState("");
	const [query, setQuery] = useState("");
	const [result, setResult] = useState<RuntimeAppRouterOutputs["desks"]["search"] | null>(null);
	const [error, setError] = useState<string | null>(null);
	const [busy, setBusy] = useState(false);
	const request = useRef(0);
	const repos = [...new Set(directory.contexts.flatMap((c) => c.repos))].sort();
	const change = (next: typeof kind) => {
		request.current++;
		setKind(next);
		setResult(null);
		setError(null);
		setBusy(false);
	};
	const search = async () => {
		const version = ++request.current;
		setBusy(true);
		setError(null);
		setResult(null);
		try {
			const data = await getRuntimeTrpcClient(null).desks.search.query({
				query,
				limit: 5,
				view: { kind, ...(kind === "global" ? {} : { value: kind === "agent" ? desk.desk_id : repo }) },
			});
			if (version === request.current) setResult(data);
		} catch (e) {
			if (version === request.current) setError(e instanceof Error ? e.message : String(e));
		} finally {
			if (version === request.current) setBusy(false);
		}
	};
	return (
		<section className="flex flex-col gap-3 rounded-lg border border-border p-4" aria-label="Memory views">
			<h2 className="font-semibold">Explore memory</h2>
			<div className="flex gap-2">
				{(
					[
						["agent", "This desk"],
						["repo", "Repository"],
						["global", "Global"],
					] as const
				).map(([value, label]) => (
					<Button key={value} variant={kind === value ? "primary" : "ghost"} onClick={() => change(value)}>
						{label}
					</Button>
				))}
			</div>
			<p className="text-sm text-text-secondary">
				Global means this shared workspace only. Views retain citations and do not grant access or establish truth.
			</p>
			{kind === "repo" ? (
				<label>
					Repository
					<select
						className="ml-2 rounded-md bg-surface-2 p-2"
						value={repo}
						onChange={(e) => {
							request.current++;
							setRepo(e.target.value);
							setResult(null);
							setBusy(false);
						}}
					>
						<option value="">Select repository</option>
						{repos.map((r) => (
							<option key={r}>{r}</option>
						))}
					</select>
				</label>
			) : null}
			<form
				onSubmit={(e) => {
					e.preventDefault();
					void search();
				}}
				className="flex gap-2"
			>
				<input
					aria-label="Search memory"
					className="min-w-0 flex-1 rounded-md border border-border bg-surface-2 p-2"
					maxLength={256}
					value={query}
					onChange={(e) => setQuery(e.target.value)}
					placeholder="Search exact words or a phrase"
				/>
				<Button type="submit" variant="primary" disabled={busy || !query.trim() || (kind === "repo" && !repo)}>
					{busy ? "Searching…" : "Search"}
				</Button>
			</form>
			{error ? (
				<p role="alert" className="text-status-red">
					{error}
				</p>
			) : null}
			{result ? (
				<>
					<p role="status" className="text-sm text-text-secondary">
						{result.covered_episodes} of {result.total_episodes} source records indexed · {result.index_status}
					</p>
					{result.results.length ? (
						result.results.map((row, index) => (
							<article
								key={`${row.episode_id}:${row.event_id}:${index}`}
								className="rounded-md bg-surface-1 p-3"
							>
								<blockquote className="whitespace-pre-wrap">{row.quote}</blockquote>
								<details className="mt-2 text-xs text-text-secondary">
									<summary>Source citation</summary>
									<p className="break-all">
										{row.episode_id} · {row.event_id} · characters {row.start}–{row.end}
									</p>
								</details>
							</article>
						))
					) : (
						<p>No matching indexed evidence. Absence is not established.</p>
					)}
				</>
			) : null}
		</section>
	);
}
