// A configured root must already exist on its physical volume. Kanban never
// creates a fallback under the user's home when the selected volume is absent.
import { lstatSync, realpathSync } from "node:fs";
import { isAbsolute, join, resolve } from "node:path";

export function getConfiguredStorageRoot(): string | null {
	const configured = process.env.KANBAN_STORAGE_ROOT;
	if (configured === undefined) {
		return null;
	}
	const selected = configured.trim();
	if (!selected) {
		throw new Error("KANBAN_STORAGE_ROOT cannot be empty.");
	}
	if (!isAbsolute(selected)) {
		throw new Error("KANBAN_STORAGE_ROOT must be an absolute physical directory.");
	}
	const root = resolve(selected);
	try {
		if (!lstatSync(root).isDirectory() || realpathSync(root) !== root) {
			throw new Error("not a physical directory");
		}
	} catch {
		throw new Error(`KANBAN_STORAGE_ROOT is missing or is not a physical directory: ${root}`);
	}
	return root;
}

export function getConfiguredRuntimeHomePath(): string | null {
	const root = getConfiguredStorageRoot();
	return root ? join(root, "kanban") : null;
}

export function getConfiguredWorktreesHomePath(): string | null {
	const root = getConfiguredStorageRoot();
	return root ? join(root, "worktrees") : null;
}
