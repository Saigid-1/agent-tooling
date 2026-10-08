import type { RuntimeAppRouterOutputs } from "@runtime-trpc";
import { useCallback, useEffect, useRef, useState } from "react";
import { getRuntimeTrpcClient } from "@/runtime/trpc-client";
import type { RuntimeProjectSummary } from "@/runtime/types";
import { useInterval } from "@/utils/react-use";

export type PlanningResult = RuntimeAppRouterOutputs["planning"]["list"];
export type PlanningWorkspace = PlanningResult & { project: RuntimeProjectSummary };
export function useAdrPlanning(projects: RuntimeProjectSummary[]) {
	const [workspaces, setWorkspaces] = useState<PlanningWorkspace[]>([]);
	const [errors, setErrors] = useState<string[]>([]);
	const [loading, setLoading] = useState(true);
	const generation = useRef(0);
	const projectsRef = useRef(projects);
	projectsRef.current = projects;
	const projectKey = projects
		.map((project) => project.id)
		.sort()
		.join("|");
	const refresh = useCallback(async () => {
		const current = ++generation.current;
		const results = await Promise.all(
			projectsRef.current.map(async (project) => {
				try {
					return { data: { ...(await getRuntimeTrpcClient(project.id).planning.list.query()), project } };
				} catch (cause) {
					return { error: `${project.name}: ${cause instanceof Error ? cause.message : String(cause)}` };
				}
			}),
		);
		if (current !== generation.current) return;
		setWorkspaces(results.flatMap((result) => (result.data ? [result.data] : [])));
		setErrors(results.flatMap((result) => (result.error ? [result.error] : [])));
		setLoading(false);
	}, []);
	useEffect(() => {
		generation.current += 1;
		setLoading(true);
		void refresh();
		return () => {
			generation.current += 1;
		};
	}, [projectKey, refresh]);
	useInterval(() => void refresh(), 5000);
	return { workspaces, errors, loading, refresh };
}
