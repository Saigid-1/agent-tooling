import type { PostHogConfig } from "posthog-js";

// Modified for the local no-telemetry build, including inherited build environment keys.
export const posthogApiKey = null;
export const posthogHost = "";

export const posthogOptions: Partial<PostHogConfig> = {
	api_host: posthogHost,
	defaults: "2026-01-30",
	autocapture: false,
	capture_pageview: true,
	capture_pageleave: true,
	disable_session_recording: true,
	capture_exceptions: false,
	person_profiles: "identified_only",
	disable_surveys: true,
	disable_surveys_automatic_display: true,
	disable_web_experiments: true,
};

export function isTelemetryEnabled(): boolean {
	return false;
}
