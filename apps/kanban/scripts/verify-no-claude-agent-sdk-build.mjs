// The Claude Agent SDK is never redistributed with the board (scripts/build.mjs keeps
// it and its provider adapter external; users install them through the board's install
// action). Reject emitted assets, source maps included, that carry the SDK's or the
// adapter's code or sources: their package paths, the SDK's entry module, or the
// environment variable the SDK itself sets (a symbol only its code contains).
import { readdirSync, readFileSync } from "node:fs";
import { join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const markers = [
	"claude-agent-sdk/sdk.mjs",
	"node_modules/@anthropic-ai/claude-agent-sdk",
	"node_modules/ai-sdk-provider-claude-code",
	"CLAUDE_AGENT_SDK_VERSION",
];

let scanned = 0;

function scan(directory) {
	for (const entry of readdirSync(directory, { withFileTypes: true })) {
		const path = join(directory, entry.name);
		if (entry.isDirectory()) {
			scan(path);
			continue;
		}
		scanned += 1;
		const content = readFileSync(path, "latin1");
		for (const marker of markers) {
			if (content.includes(marker)) {
				throw new Error(`Claude Agent SDK content (${marker}) in ${path}; it must never be bundled.`);
			}
		}
	}
}

// Default: this app's dist/. A directory argument scans another build output.
const target = process.argv[2] ? resolve(process.argv[2]) : fileURLToPath(new URL("../dist/", import.meta.url));
scan(target);
console.log(`No Claude Agent SDK code or sources in ${scanned} emitted files under ${target}.`);
