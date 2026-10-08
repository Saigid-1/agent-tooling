import { afterEach, describe, expect, it } from "vitest";
import { disposeCliTelemetryService, getCliTelemetryService } from "../../../src/cline-sdk/cline-telemetry-service";

const originalEnabled = process.env.OTEL_TELEMETRY_ENABLED;
const originalEndpoint = process.env.OTEL_EXPORTER_OTLP_ENDPOINT;

afterEach(async () => {
	await disposeCliTelemetryService();
	if (originalEnabled === undefined) delete process.env.OTEL_TELEMETRY_ENABLED;
	else process.env.OTEL_TELEMETRY_ENABLED = originalEnabled;
	if (originalEndpoint === undefined) delete process.env.OTEL_EXPORTER_OTLP_ENDPOINT;
	else process.env.OTEL_EXPORTER_OTLP_ENDPOINT = originalEndpoint;
});

describe("local Cline telemetry", () => {
	it("stays disabled even when OTEL exporter variables are inherited", () => {
		process.env.OTEL_TELEMETRY_ENABLED = "true";
		process.env.OTEL_EXPORTER_OTLP_ENDPOINT = "https://telemetry-sentinel.invalid";
		const telemetry = getCliTelemetryService();
		expect(telemetry.isEnabled()).toBe(false);
	});
});
