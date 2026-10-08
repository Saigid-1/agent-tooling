import { createHash } from "node:crypto";
import { mkdirSync, mkdtempSync, readFileSync, symlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import type { RuntimeTaskSessionSummary } from "../../src/core/api-contract";
import { createTaskLifecycleObserver } from "../../src/server/task-lifecycle-observer";
import { readTaskLifecycle, recordTaskLifecycle } from "../../src/state/task-lifecycle-store";

let root: string;
const summary = (state: RuntimeTaskSessionSummary["state"], updatedAt = 1): RuntimeTaskSessionSummary => ({
	taskId: "task-1",
	state,
	agentId: "cline",
	workspacePath: "/fixture",
	pid: null,
	startedAt: 1,
	updatedAt,
	lastOutputAt: null,
	reviewReason: null,
	exitCode: null,
	lastHookAt: null,
	latestHookActivity: null,
});
beforeEach(() => {
	root = mkdtempSync(join(tmpdir(), "lifecycle-"));
	vi.stubEnv("KANBAN_STORAGE_ROOT", root);
});
afterEach(() => {
	vi.unstubAllEnvs();
	vi.restoreAllMocks();
});
it("retains rapid transitions before batching, excludes output-only changes, and survives observer restart", async () => {
	const observer = createTaskLifecycleObserver();
	await Promise.all([
		observer.observe("w", "cline", summary("running", 1)),
		observer.observe("w", "cline", summary("running", 2)),
		observer.observe("w", "cline", summary("awaiting_review", 3)),
		observer.observe("w", "cline", summary("running", 4)),
	]);
	await observer.drain();
	const events = await readTaskLifecycle("w", "task-1");
	expect(events.records.map((r) => r.state.state)).toEqual(["running", "awaiting_review", "running"]);
	const fresh = createTaskLifecycleObserver();
	fresh.observe("w", "cline", summary("running", 10));
	await fresh.drain();
	expect((await readTaskLifecycle("w", "task-1")).records).toHaveLength(3);
	expect((await readTaskLifecycle("other", "task-1")).records).toEqual([]);
	expect(events.records[0]).not.toHaveProperty("latestHookActivity");
});
it("separates attempts and checkpoints without inventing a native session or desk", async () => {
	await recordTaskLifecycle("w", "terminal", summary("running"));
	await recordTaskLifecycle("w", "terminal", { ...summary("running"), startedAt: 2 });
	await recordTaskLifecycle("w", "terminal", {
		...summary("running"),
		startedAt: 2,
		latestTurnCheckpoint: { turn: 1, ref: "test", commit: "a".repeat(40), createdAt: 3 },
	});
	const result = await readTaskLifecycle("w", "task-1");
	expect(result.records).toHaveLength(3);
	expect(result.records[0].state).not.toHaveProperty("desk_id");
	expect(result.evidence_boundary).toContain("not authenticated");
});
it("fails visibly, retries and persists a sticky gap before later UI coalescing", async () => {
	let calls = 0;
	const delivered: RuntimeTaskSessionSummary[] = [];
	const observer = createTaskLifecycleObserver(
		async (w, source, state, gap) => {
			if (++calls === 1) throw new Error("disk unavailable");
			return recordTaskLifecycle(w, source, state, gap);
		},
		(_w, s) => delivered.push(s),
	);
	observer.observe("w", "cline", summary("running"));
	await observer.drain();
	observer.observe("w", "cline", summary("awaiting_review", 2));
	await observer.drain();
	expect(delivered.every((s) => s.warningMessage?.includes("capture gap"))).toBe(true);
	expect((await readTaskLifecycle("w", "task-1")).records[0].capture_gap).toContain("disk unavailable");
	expect(calls).toBe(2);
});
it("orders mixed producers, coalesces duplicate waiters and delivers overflow last", async () => {
	vi.spyOn(console, "error").mockImplementation(() => {});
	let release: (() => void) | undefined;
	const gate = new Promise<void>((resolve) => {
		release = resolve;
	});
	const delivered: number[] = [];
	const observed: string[] = [];
	const observer = createTaskLifecycleObserver(
		async (_w, source, _s, gap) => {
			await gate;
			observed.push(source);
			return { replay: false, sequence: 1, capture_gap: gap ?? null };
		},
		(_w, s) => delivered.push(s.updatedAt),
	);
	observer.observe("w", "cline", summary("running", 1));
	for (let i = 2; i < 2000; i++) observer.observe("w", "cline", summary("running", i));
	observer.observe("w", "terminal", summary("awaiting_review", 2000));
	release?.();
	await observer.drain();
	expect(observed).toEqual(["cline", "terminal"]);
	expect(delivered).toEqual([1999, 2000]);
	const slow = createTaskLifecycleObserver(
		async () => ({ replay: false, sequence: 1, capture_gap: null }),
		(_w, s) => delivered.push(s.updatedAt),
	);
	for (let i = 0; i < 300; i++) slow.observe("over", "cline", summary(i % 2 ? "running" : "awaiting_review", i));
	slow.close();
	slow.observe("over", "cline", summary("running", 999));
	await slow.drain();
	expect(delivered.at(-1)).toBe(299);
	expect(delivered).not.toContain(999);
});
it("refuses tampering and symlink storage", async () => {
	await recordTaskLifecycle("w", "cline", summary("running"));
	const hash = (s: string) => createHash("sha256").update(JSON.stringify(s)).digest("hex");
	const path = join(root, "task-lifecycle", hash("w"), `${hash("task-1")}.json`);
	const data = JSON.parse(readFileSync(path, "utf8"));
	data.records[0].state.state = "awaiting_review";
	writeFileSync(path, JSON.stringify(data));
	await expect(readTaskLifecycle("w", "task-1")).rejects.toThrow(/integrity/);
	const next = mkdtempSync(join(tmpdir(), "lifecycle-link-"));
	mkdirSync(join(next, "target"));
	symlinkSync(join(next, "target"), join(next, "task-lifecycle"));
	vi.stubEnv("KANBAN_STORAGE_ROOT", next);
	await expect(recordTaskLifecycle("w", "cline", summary("running"))).rejects.toThrow(/physical/);
});
it("recovers an idle task rejected while other tasks saturate the global queue", async () => {
	vi.spyOn(console, "error").mockImplementation(() => {});
	let release: (() => void) | undefined;
	const gate = new Promise<void>((resolve) => {
		release = resolve;
	});
	const seen: string[] = [];
	const deliveries: RuntimeTaskSessionSummary[] = [];
	const observer = createTaskLifecycleObserver(
		async (_w, _source, state, gap) => {
			await gate;
			seen.push(state.taskId);
			return { replay: false, sequence: 1, capture_gap: gap ?? null };
		},
		(_w, state) => deliveries.push(state),
	);
	for (let task = 0; task < 16; task++)
		for (let event = 0; event < 256; event++)
			observer.observe("w", "cline", {
				...summary(event % 2 ? "running" : "awaiting_review", event),
				taskId: `fill-${task}`,
			});
	observer.observe("w", "cline", { ...summary("running"), taskId: "new" });
	expect(deliveries.at(-1)?.warningMessage).toContain("capacity");
	release?.();
	await observer.drain();
	observer.observe("w", "cline", { ...summary("running", 2), taskId: "new" });
	await observer.drain();
	expect(seen.at(-1)).toBe("new");
	expect(deliveries.at(-1)?.warningMessage).toContain("capacity");
});

it("sizes journal read buffers to actual bytes rather than maximum capacity", async () => {
	await recordTaskLifecycle("w", "terminal", summary("running"));
	const allocate = vi.spyOn(Buffer, "alloc");
	await readTaskLifecycle("w", "task-1");
	expect(allocate.mock.calls.length).toBeGreaterThan(0);
	expect(allocate.mock.calls.every(([size]) => size < 8192)).toBe(true);
});

it("caps concurrent journal writes across independent tasks", async () => {
	let active = 0;
	let maximum = 0;
	const observer = createTaskLifecycleObserver(async () => {
		active++;
		maximum = Math.max(maximum, active);
		await new Promise<void>((resolve) => setTimeout(resolve, 1));
		active--;
		return { replay: false, sequence: 1, capture_gap: null };
	});
	for (let i = 0; i < 50; i++) observer.observe("w", "cline", { ...summary("running"), taskId: `task-${i}` });
	await observer.drain();
	expect(maximum).toBe(8);
	expect(active).toBe(0);
});
