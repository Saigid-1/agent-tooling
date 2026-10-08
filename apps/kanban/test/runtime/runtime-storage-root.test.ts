import { afterEach, describe, expect, it } from "vitest";
import {
	getConfiguredRuntimeHomePath,
	getConfiguredStorageRoot,
	getConfiguredWorktreesHomePath,
} from "../../src/state/runtime-storage-root";

const originalRoot = process.env.KANBAN_STORAGE_ROOT;

afterEach(() => {
	if (originalRoot === undefined) delete process.env.KANBAN_STORAGE_ROOT;
	else process.env.KANBAN_STORAGE_ROOT = originalRoot;
});

describe("physical runtime storage root", () => {
	it("uses an existing physical root for state and worktrees", () => {
		process.env.KANBAN_STORAGE_ROOT = process.cwd();
		expect(getConfiguredStorageRoot()).toBe(process.cwd());
		expect(getConfiguredRuntimeHomePath()).toBe(`${process.cwd()}/kanban`);
		expect(getConfiguredWorktreesHomePath()).toBe(`${process.cwd()}/worktrees`);
	});

	it("refuses a missing root instead of creating a home fallback", () => {
		process.env.KANBAN_STORAGE_ROOT = `${process.cwd()}/missing-volume-for-kanban-test`;
		expect(() => getConfiguredRuntimeHomePath()).toThrow(/missing or is not a physical directory/);
	});

	it("refuses a relative root", () => {
		process.env.KANBAN_STORAGE_ROOT = "relative-root";
		expect(() => getConfiguredStorageRoot()).toThrow(/absolute physical directory/);
	});

	it("refuses an explicitly empty root", () => {
		process.env.KANBAN_STORAGE_ROOT = "  ";
		expect(() => getConfiguredStorageRoot()).toThrow(/cannot be empty/);
	});
});
