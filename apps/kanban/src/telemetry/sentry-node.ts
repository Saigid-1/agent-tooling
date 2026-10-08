import * as Sentry from "@sentry/node";
import packageJson from "../../package.json" with { type: "json" };

// Modified for the local no-telemetry build: an inherited Sentry token cannot enable reporting.
const nodeSentryDsn = "";

const appVersion = typeof packageJson.version === "string" ? packageJson.version : "0.1.0";

let initialized = false;
const nodeSentryEnvironment =
	process.env.SENTRY_NODE_ENVIRONMENT?.trim() || process.env.NODE_ENV?.trim() || "development";

if (nodeSentryDsn) {
	Sentry.init({
		dsn: nodeSentryDsn,
		environment: nodeSentryEnvironment,
		release: `kanban@${appVersion}`,
		sendDefaultPii: false,
		initialScope: {
			tags: {
				app: "kanban",
				runtime_surface: "node",
			},
		},
	});
	initialized = true;
}

interface CaptureNodeExceptionOptions {
	area?: string;
}

export function captureNodeException(error: unknown, options?: CaptureNodeExceptionOptions): void {
	if (!initialized) {
		return;
	}

	Sentry.withScope((scope) => {
		if (options?.area) {
			scope.setTag("error_area", options.area);
		}
		Sentry.captureException(error);
	});
}

export async function flushNodeTelemetry(timeoutMs = 2_000): Promise<void> {
	if (!initialized) {
		return;
	}
	await Sentry.flush(timeoutMs);
}

export function isNodeSentryEnabled(): boolean {
	return initialized;
}
