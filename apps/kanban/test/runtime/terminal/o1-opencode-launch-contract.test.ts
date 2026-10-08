// Order O1: OpenCode launches from the board under a named approval policy
// (docs/work/orders/O1-opencode-board-launch.md, with amendment-1). Contract tests of the launch the
// adapter prepares; the seams they assume are in ./o1-seams.ts. The launched process itself (its
// environment and argv, through the board's session manager and a stub `opencode`) is tested in
// ./o1-opencode-launched-process.test.ts; the effective policy on the real pinned binary in
// tests/image/test_o1_opencode_launch_image.py.

import { readFileSync } from "node:fs";

import { afterEach, describe, expect, it } from "vitest";

import { getRuntimeLaunchSupportedAgentCatalog, isRuntimeAgentLaunchSupported } from "../../../src/core/agent-catalog";
import { prepareAgentLaunch } from "../../../src/terminal/agent-session-adapters";
import {
	O1_BOARD_LAUNCH,
	O1_LAUNCH_SUPPORTED,
	O1_POLICY,
	O1_POLICYLESS,
	type O1TempHome,
	o1TempHome,
} from "./o1-seams";

let temp: O1TempHome | null = null;

function freshHome(): O1TempHome {
	temp = o1TempHome();
	return temp;
}

afterEach(() => {
	temp?.restore();
	temp = null;
});

function boardLaunch(overrides: Partial<Parameters<typeof prepareAgentLaunch>[0]> = {}) {
	const { cwd } = temp ?? freshHome();
	return prepareAgentLaunch({ taskId: "o1-card", cwd, ...O1_BOARD_LAUNCH, ...overrides });
}

describe("O1: OpenCode launches from the board under a named approval policy", () => {
	// Falsifier: "opencode" removed from the launch list again.
	it("P3/P2: the launch list holds OpenCode beside Claude and Codex, with no catalog arguments", () => {
		const entries = getRuntimeLaunchSupportedAgentCatalog();
		expect([...entries.map((entry) => entry.id)].sort()).toEqual([...O1_LAUNCH_SUPPORTED].sort());
		expect(isRuntimeAgentLaunchSupported("opencode")).toBe(true);
		const opencode = entries.find((entry) => entry.id === "opencode");
		expect(opencode?.binary).toBe("opencode");
		expect(opencode?.baseArgs).toEqual([]);
	});

	// Falsifier: any one permission key dropped from the generated opencode.json, or set to "allow"
	// where the order says "ask" or "deny".
	it("P1: the generated opencode.json carries exactly the named policy", async () => {
		freshHome();
		const launch = await boardLaunch();
		const generated = launch.env.OPENCODE_CONFIG;
		expect(generated, "the launch names no generated config in OPENCODE_CONFIG (seam O1-S1)").toBeTruthy();
		const config = JSON.parse(readFileSync(generated as string, "utf8")) as { permission?: unknown };
		expect(config.permission).toEqual(O1_POLICY);
	});

	// Falsifier: a caller-supplied argument or an alternate executable accepted.
	it("P2: caller arguments and an alternate executable are refused before the adapter adds its own", async () => {
		freshHome();
		// Positive control: the plain board launch proceeds, so a refusal below is the hardening's,
		// not a gate that refuses OpenCode outright.
		await expect(boardLaunch()).resolves.toBeDefined();
		for (const args of [
			["run"],
			["run", "--auto", "do it"],
			["--auto"],
			["--prompt", "do it"],
			["--agent", "build"],
			["--model", "openrouter/some/model"],
			["-m", "anthropic/some-model"],
			["--continue"],
			["--session", "ses_caller"],
			["--port", "4096"],
			["--pure"],
			["serve"],
			["."],
		]) {
			await expect(
				boardLaunch({ args }),
				`caller arguments ${JSON.stringify(args)} were accepted`,
			).rejects.toThrow();
		}
		for (const binary of ["/tmp/opencode", "/usr/local/bin/opencode", "./opencode", "opencode-ai", "sh"]) {
			await expect(boardLaunch({ binary }), `alternate executable ${binary} was accepted`).rejects.toThrow();
		}
	});

	// Falsifier: gemini (or any other policy-less harness) launched.
	it("P3: the gate opens for OpenCode only; gemini and every other policy-less harness is refused", async () => {
		freshHome();
		await expect(boardLaunch()).resolves.toBeDefined();
		for (const agentId of O1_POLICYLESS) {
			expect(isRuntimeAgentLaunchSupported(agentId), `${agentId} is in the launch list`).toBe(false);
			await expect(
				boardLaunch({ agentId, binary: undefined }),
				`${agentId} was prepared for launch without a verified policy`,
			).rejects.toThrow();
		}
	});

	// Amendment-1 falsifier: a launch argv that uses `run`.
	it("amendment-1 (c): the launch is OpenCode's interactive form with --prompt, never `opencode run`", async () => {
		freshHome();
		for (const overrides of [{}, { startInPlanMode: true }, { resumeFromTrash: true }]) {
			let launch: Awaited<ReturnType<typeof boardLaunch>>;
			try {
				launch = await boardLaunch(overrides);
			} catch (error) {
				// Meet reconciliation: a refused plan-mode launch starts nothing, so it cannot be `opencode run`.
				// The order does not name plan mode; the implementation refuses it rather than let the policy's
				// edit "allow" override the plan agent's own edit rules.
				if ("startInPlanMode" in overrides) {
					continue;
				}
				throw error;
			}
			expect(launch.binary ?? "opencode").toBe("opencode");
			expect(launch.args, `argv ${JSON.stringify(launch.args)} uses run`).not.toContain("run");
			if (!overrides.resumeFromTrash) {
				const promptIndex = launch.args.indexOf("--prompt");
				expect(promptIndex, `argv ${JSON.stringify(launch.args)} has no --prompt`).toBeGreaterThanOrEqual(0);
				expect(launch.args[promptIndex + 1]).toContain(O1_BOARD_LAUNCH.prompt);
			}
		}
	});
});
