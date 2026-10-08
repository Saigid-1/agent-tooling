import { createKanbanClineLogger } from "../cline-sdk/cline-runtime-logger";
import type { RuntimeTaskSessionSummary } from "../core/api-contract";
import { type LifecycleSource, lifecycleFingerprint, recordTaskLifecycle } from "../state/task-lifecycle-store";

type Item = { source: LifecycleSource; summary: RuntimeTaskSessionSummary; fingerprint: string };
type Track = {
	queue: Item[];
	active?: Item;
	tail?: Promise<void>;
	fingerprint?: string;
	gap: string | null;
	overflow?: RuntimeTaskSessionSummary;
	workspaceId: string;
};
/** Bounded per-task producer ordering; duplicate output updates coalesce without adding promise waiters. */
export function createTaskLifecycleObserver(
	persist: typeof recordTaskLifecycle = recordTaskLifecycle,
	deliver: (workspaceId: string, summary: RuntimeTaskSessionSummary) => void = () => {},
) {
	const logger = createKanbanClineLogger({ component: "task-lifecycle" });
	const tracks = new Map<string, Track>();
	let pending = 0;
	let closed = false;
	let activeWrites = 0;
	const writeWaiters: (() => void)[] = [];
	const acquireWrite = async () => {
		if (activeWrites < 8) {
			activeWrites++;
			return;
		}
		await new Promise<void>((resolve) => writeWaiters.push(resolve));
	};
	const releaseWrite = () => {
		const next = writeWaiters.shift();
		if (next) next();
		else activeWrites--;
	};
	const warn = (track: Track, summary: RuntimeTaskSessionSummary): RuntimeTaskSessionSummary =>
		track.gap
			? {
					...summary,
					warningMessage: [summary.warningMessage, `Lifecycle capture gap: ${track.gap}`]
						.filter(Boolean)
						.join("; "),
				}
			: summary;
	const fail = (track: Track, summary: RuntimeTaskSessionSummary, error: unknown) => {
		const message = (error instanceof Error ? error.message : "unknown capture error").slice(0, 1024);
		if (!track.gap) logger.error?.(`Lifecycle capture failed for task ${summary.taskId}: ${message}`);
		track.gap ??= message;
	};
	const run = async (track: Track) => {
		while (track.queue.length || track.overflow) {
			if (!track.queue.length && track.overflow) {
				const latest = track.overflow;
				track.overflow = undefined;
				track.fingerprint = undefined;
				deliver(track.workspaceId, warn(track, latest));
				continue;
			}
			const item = track.queue.shift();
			if (!item) break;
			track.active = item;
			try {
				await acquireWrite();
				let result: Awaited<ReturnType<typeof persist>>;
				try {
					result = await persist(track.workspaceId, item.source, item.summary, track.gap);
				} finally {
					releaseWrite();
				}
				track.gap ??= result.capture_gap;
				track.fingerprint = item.fingerprint;
			} catch (error) {
				fail(track, item.summary, error);
				track.fingerprint = undefined;
			}
			pending--;
			track.active = undefined;
			deliver(track.workspaceId, warn(track, item.summary));
		}
	};
	const schedule = (track: Track): void => {
		if (track.tail) return;
		track.tail = Promise.resolve()
			.then(() => run(track))
			.finally(() => {
				track.tail = undefined;
				if (track.queue.length || track.overflow) schedule(track);
			});
	};

	return {
		observe(workspaceId: string, source: LifecycleSource, summary: RuntimeTaskSessionSummary): void {
			if (closed) return;
			const key = JSON.stringify([workspaceId, summary.taskId]);
			let track = tracks.get(key);
			if (!track) {
				if (tracks.size >= 1024) {
					for (const [old, value] of tracks) {
						if (!value.tail) {
							tracks.delete(old);
							break;
						}
					}
				}
				if (tracks.size >= 1024) {
					const refused: Track = { workspaceId, queue: [], gap: "Lifecycle task capacity reached" };
					deliver(workspaceId, warn(refused, summary));
					return;
				}
				track = { workspaceId, queue: [], gap: null };
				tracks.set(key, track);
			}
			const snapshot = structuredClone(summary);
			let fingerprint: string;
			try {
				fingerprint = lifecycleFingerprint(source, snapshot);
			} catch (error) {
				fail(track, snapshot, error);
				track.overflow = snapshot;
				if (!track.tail) {
					deliver(workspaceId, warn(track, snapshot));
					track.overflow = undefined;
				}
				return;
			}
			if (track.overflow) {
				track.overflow = snapshot;
				return;
			}
			const latest = track.queue.at(-1) ?? track.active;
			if (latest?.fingerprint === fingerprint) {
				latest.summary = snapshot;
				return;
			}
			if (!track.tail && track.fingerprint === fingerprint) {
				deliver(workspaceId, warn(track, snapshot));
				return;
			}
			if (track.queue.length >= 256 || pending >= 4096) {
				fail(track, snapshot, new Error("Lifecycle capture queue at capacity"));
				if (!track.tail) {
					deliver(workspaceId, warn(track, snapshot));
					return;
				}
				track.overflow = snapshot;
				return;
			}
			track.queue.push({ source, summary: snapshot, fingerprint });
			pending++;
			schedule(track);
		},
		async drain(): Promise<void> {
			while ([...tracks.values()].some((t) => t.tail)) await Promise.all([...tracks.values()].map((t) => t.tail));
		},
		close(): void {
			closed = true;
		},
	};
}
