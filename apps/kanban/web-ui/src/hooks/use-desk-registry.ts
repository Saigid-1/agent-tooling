import type { RuntimeAppRouterInputs, RuntimeAppRouterOutputs } from "@runtime-trpc";
import { useCallback, useEffect, useRef, useState } from "react";
import { getRuntimeTrpcClient } from "@/runtime/trpc-client";

export type DeskDirectory = RuntimeAppRouterOutputs["desks"]["list"];
export type DeskProfile = DeskDirectory["desks"][number];
export type DeskInput = RuntimeAppRouterInputs["desks"]["save"];
export type SessionContextInput = RuntimeAppRouterInputs["desks"]["annotate"];
export function useDeskRegistry() {
	const [data, setData] = useState<DeskDirectory | null>(null);
	const [error, setError] = useState<string | null>(null);
	const [busy, setBusy] = useState(false);
	const generation = useRef(0);
	const refresh = useCallback(async () => {
		const current = ++generation.current;
		setBusy(true);
		try {
			const next = await getRuntimeTrpcClient(null).desks.list.query();
			if (current === generation.current) {
				setData(next);
				setError(null);
			}
		} catch (e) {
			if (current === generation.current) setError(e instanceof Error ? e.message : String(e));
		} finally {
			if (current === generation.current) setBusy(false);
		}
	}, []);
	useEffect(() => {
		void refresh();
		return () => {
			generation.current++;
		};
	}, [refresh]);
	const save = async (input: DeskInput) => {
		await getRuntimeTrpcClient(null).desks.save.mutate(input);
		await refresh();
	};
	const annotate = async (input: SessionContextInput) => {
		await getRuntimeTrpcClient(null).desks.annotate.mutate(input);
		await refresh();
	};
	return { data, error, busy, refresh, save, annotate };
}
