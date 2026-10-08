// Modified for the local trial: reject emitted assets with known telemetry and remote onboarding hosts.
import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

const forbidden = [
	"data.cline.bot",
	"ingest.us.sentry.io",
	"github.com/user-attachments/assets",
	"do.featurebase.app",
	"telemetry-sentinel.invalid",
];

function scan(directory) {
	for (const entry of readdirSync(directory, { withFileTypes: true })) {
		const path = join(directory, entry.name);
		if (entry.isDirectory()) scan(path);
		else {
			const content = readFileSync(path, "utf8");
			for (const host of forbidden) {
				if (content.includes(host)) throw new Error(`Forbidden remote host ${host} in ${path}`);
			}
		}
	}
}

scan(fileURLToPath(new URL("../dist/", import.meta.url)));
console.log("No known telemetry or remote onboarding hosts in emitted assets.");
