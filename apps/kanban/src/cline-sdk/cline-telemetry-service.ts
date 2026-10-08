import { type BasicLogger, type ITelemetryService, TelemetryService } from "@clinebot/core";

let telemetrySingleton:
	| {
			telemetry: ITelemetryService;
		  }
	| undefined;

export function getCliTelemetryService(logger?: BasicLogger): ITelemetryService {
	if (!telemetrySingleton) {
		telemetrySingleton = {
			// No adapters or exporters can be attached in this local trial build.
			telemetry: new TelemetryService({ logger }),
		};
	}
	return telemetrySingleton.telemetry;
}

export async function disposeCliTelemetryService(): Promise<void> {
	if (!telemetrySingleton) {
		return;
	}
	const current = telemetrySingleton;
	telemetrySingleton = undefined;
	await current.telemetry.dispose();
}
