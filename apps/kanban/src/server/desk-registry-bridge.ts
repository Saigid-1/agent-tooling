import { spawn } from "node:child_process";
import { existsSync } from "node:fs";
import { basename, isAbsolute } from "node:path";
import { z } from "zod";
import { type DeskRegistryAction, deskRegistryRequestSchemas } from "../core/desk-registry-contract";

const NOT_CONFIGURED = "Desk registry is not configured on this host.";
const REGISTRY_EXECUTABLE = "kp-agent-desk-registry";

/**
 * True when the command runs the registry executable directly (not through e.g. `docker exec`)
 * and names no `--config`, or names one that is not written yet. The board in the container is
 * configured with the runtime's fixed registry path before the operator writes it; checking on
 * every request lets the Desks menu work once the file exists, without a board restart.
 */
function registryConfigUnwritten(command: string[]): boolean {
	if (basename(command[0] ?? "") !== REGISTRY_EXECUTABLE) return false;
	const index = command.indexOf("--config");
	const inline = command.find((item) => item.startsWith("--config="));
	const config = index >= 0 ? command[index + 1] : inline?.slice("--config=".length);
	return !config || (isAbsolute(config) && !existsSync(config));
}

/** Validate a request against the contract; fields it does not define never reach the registry. */
export function deskRegistryRequest(action: DeskRegistryAction, input?: unknown): unknown {
	if (!Object.hasOwn(deskRegistryRequestSchemas, action)) throw new Error("Unknown desk registry action.");
	const parsed = deskRegistryRequestSchemas[action].safeParse(input);
	if (!parsed.success)
		throw new Error("Desk registry refused this request: it has fields or values the desk contract does not define.");
	return parsed.data;
}

/** Fixed operator command; the browser cannot choose executable, configuration or tenant. */
export async function invokeDeskRegistry(action: DeskRegistryAction, input?: unknown): Promise<unknown> {
	const request = deskRegistryRequest(action, input);
	const configured = process.env.KANBAN_DESK_REGISTRY_COMMAND;
	if (!configured) throw new Error(NOT_CONFIGURED);
	const command = z.array(z.string().min(1).max(4096)).min(1).max(20).parse(JSON.parse(configured));
	const executable = command[0];
	if (!executable || !isAbsolute(executable))
		throw new Error("Desk registry requires an absolute operator executable.");
	if (registryConfigUnwritten(command))
		throw new Error(`${NOT_CONFIGURED} Write the registry operator configuration it names, then reload.`);
	return runRegistryCommand(executable, [...command.slice(1), action], request);
}

export function runRegistryCommand(executable: string, args: string[], input?: unknown): Promise<unknown> {
	const body = input === undefined ? "" : JSON.stringify(input);
	if (Buffer.byteLength(body) > 65536) return Promise.reject(new Error("Desk request exceeds 64 KiB."));
	return new Promise((resolve, reject) => {
		const child = spawn(executable, args, { stdio: ["pipe", "pipe", "pipe"] });
		let bytes = 0;
		const output: Buffer[] = [];
		let completed = false;
		const finish = (error?: Error, value?: unknown) => {
			if (completed) return;
			completed = true;
			clearTimeout(timer);
			if (error) reject(error);
			else resolve(value);
		};
		const timer = setTimeout(() => {
			child.kill();
			finish(new Error("Desk registry timed out; refresh before retrying a save."));
		}, 20000);
		child.on("error", () => finish(new Error("Desk registry process could not start.")));
		child.stdin.on("error", () => finish(new Error("Desk registry input unavailable.")));
		child.stdout.on("data", (chunk: Buffer) => {
			bytes += chunk.length;
			if (bytes > 2 * 1024 * 1024) {
				child.kill();
				finish(new Error("Desk registry response exceeds its bound."));
			} else output.push(chunk);
		});
		child.stderr.on("data", () => {}); // Never relay source paths or credentials from a child.
		child.on("close", (code) => {
			if (code !== 0) {
				finish(new Error("Desk registry refused this request. Reload and check the fields and current version."));
				return;
			}
			try {
				finish(undefined, JSON.parse(Buffer.concat(output).toString("utf8")));
			} catch {
				finish(new Error("Desk registry returned an invalid response."));
			}
		});
		child.stdin.end(body);
	});
}
