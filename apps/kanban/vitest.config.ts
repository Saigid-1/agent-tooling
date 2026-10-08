import { defineConfig } from "vitest/config";

process.env.NODE_ENV = "production";

// Integration files start real runtime servers and CLI subprocesses with fixed
// startup deadlines; runtime-state-stream and task-command-exit miss them when
// they share the CPU with the parallel unit files. They run as their own project,
// after every other file has finished, one file at a time. They are never skipped.
const INTEGRATION_FILES = "test/integration/**/*.test.ts";

export default defineConfig({
	test: {
		globals: true,
		environment: "node",
		// `packages/**` excluded: those workspaces have their own vitest
		// configs and runtime shapes (e.g. Electron) and are run explicitly by
		// CI. New workspaces under `packages/` MUST get matching install/test
		// steps in .github/workflows/test.yml or they fall out of CI coverage.
		exclude: [
			"apps/**",
			"packages/**",
			"web-ui/**",
			"third_party/**",
			"**/node_modules/**",
			"**/dist/**",
			".worktrees/**",
		],
		testTimeout: 15_000,
		projects: [
			{
				extends: true,
				test: {
					name: "unit",
					exclude: [INTEGRATION_FILES],
					sequence: { groupOrder: 0 },
				},
			},
			{
				extends: true,
				test: {
					name: "integration",
					include: [INTEGRATION_FILES],
					fileParallelism: false,
					sequence: { groupOrder: 1 },
				},
			},
		],
	},
});
