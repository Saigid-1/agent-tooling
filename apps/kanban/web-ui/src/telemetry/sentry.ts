import * as Sentry from "@sentry/react";

// Modified for the local no-telemetry build.
const sentryDsn = "";
const sentryEnvironment = import.meta.env.MODE;

let initialized = false;

export function initializeSentry(): void {
	if (!sentryDsn || initialized) {
		return;
	}

	Sentry.init({
		dsn: sentryDsn,
		environment: sentryEnvironment,
		release: `kanban@${__APP_VERSION__}`,
		sendDefaultPii: false,
		initialScope: {
			tags: {
				app: "kanban",
				runtime_surface: "web",
			},
		},
	});

	initialized = true;
}

export function isSentryEnabled(): boolean {
	return initialized;
}
