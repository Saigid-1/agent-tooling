import { describe, expect, it } from "vitest";
import { getRuntimeLaunchSupportedAgentCatalog } from "../../../src/core/agent-catalog";
import { prepareAgentLaunch } from "../../../src/terminal/agent-session-adapters";

const base = { taskId: "trial-card", cwd: process.cwd(), prompt: "", args: [], autonomousModeEnabled: true };

describe("trial launch posture", () => {
	it("catalog commands enter the adapter without custom arguments", () => {
		const entries = getRuntimeLaunchSupportedAgentCatalog();
		expect(entries.map((entry) => entry.id)).toEqual(["claude", "codex", "opencode"]);
		expect(entries.map((entry) => entry.baseArgs)).toEqual([[], [], []]);
	});

	it("uses Claude auto mode without bypass", async () => {
		const launch = await prepareAgentLaunch({ ...base, agentId: "claude", binary: "claude" });
		expect(launch.args).toContain("auto");
		expect(launch.args).not.toContain("--dangerously-skip-permissions");
	});

	it("uses the Codex workspace sandbox and on-request approvals", async () => {
		const launch = await prepareAgentLaunch({ ...base, agentId: "codex", binary: "codex" });
		expect(launch.args).toContain("--sandbox");
		expect(launch.args[launch.args.indexOf("--sandbox") + 1]).toBe("workspace-write");
		expect(launch.args[launch.args.indexOf("--ask-for-approval") + 1]).toBe("on-request");
		expect(launch.args).not.toContain("--dangerously-bypass-approvals-and-sandbox");
	});

	it("rejects all caller-supplied arguments, including duplicate and alternate policy flags", async () => {
		for (const args of [
			["--sandbox", "workspace-write", "--sandbox", "danger-full-access"],
			["--ask-for-approval", "on-request", "--ask-for-approval", "never"],
			["--yolo"],
			["-y"],
			["-c", "approval_policy=never"],
			["-capproval_policy=never"],
			["--config", "sandbox_mode=danger-full-access"],
			["--dangerously-bypass-hook-trust"],
		]) {
			await expect(prepareAgentLaunch({ ...base, agentId: "codex", args })).rejects.toThrow(/not reviewed/);
		}
		for (const args of [
			["--permission-mode", "auto", "--permission-mode", "bypassPermissions"],
			["--dangerously-skip-permissions"],
		]) {
			await expect(prepareAgentLaunch({ ...base, agentId: "claude", args })).rejects.toThrow(/not reviewed/);
		}
		await expect(prepareAgentLaunch({ ...base, agentId: "codex", binary: "/tmp/codex" })).rejects.toThrow(/catalog binary/);
	});

	it("refuses unsupported harnesses before an agent starts", async () => {
		await expect(prepareAgentLaunch({ ...base, agentId: "cline", binary: "cline" })).rejects.toThrow(/not ready/);
	});
});
