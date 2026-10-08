import { afterEach, expect, it, vi } from "vitest";
import { invokeDeskRegistry, runRegistryCommand } from "../../src/server/desk-registry-bridge";

afterEach(() => vi.unstubAllEnvs());
it("requires an operator command and passes JSON without a shell", async () => {
	vi.stubEnv("KANBAN_DESK_REGISTRY_COMMAND", "");
	await expect(invokeDeskRegistry("list")).rejects.toThrow("not configured");
	const result = await runRegistryCommand(
		process.execPath,
		["-e", "process.stdin.on('data',d=>process.stdout.write(JSON.stringify({value:JSON.parse(d.toString())})))"],
		{ name: "$(no shell)" },
	);
	expect(result).toEqual({ value: { name: "$(no shell)" } });
});
it("refuses oversized requests and invalid child responses", async () => {
	await expect(runRegistryCommand(process.execPath, [], "x".repeat(70000))).rejects.toThrow("64 KiB");
	await expect(runRegistryCommand(process.execPath, ["-e", "process.stdout.write('not-json')"])).rejects.toThrow(
		"invalid response",
	);
});
