import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ClaudeAgentSdkInstallSection } from "@/components/shared/claude-agent-sdk-install";
import type { RuntimeClaudeAgentSdkStatus } from "@/runtime/types";

const NOTICE_SHA256 = "a".repeat(64);

function status(overrides: Partial<RuntimeClaudeAgentSdkStatus> = {}): RuntimeClaudeAgentSdkStatus {
	return {
		providerId: "claude-code",
		sdkPackage: "@anthropic-ai/claude-agent-sdk",
		providerPackage: "ai-sdk-provider-claude-code",
		state: "not_installed",
		installRoot: "/state/kanban/kanban/optional-packages/claude-agent-sdk",
		pinned: { sdkVersion: "0.2.128", providerVersion: "3.4.4" },
		installed: null,
		provider: {
			registered: false,
			sdkEntryReached: false,
			sdkEntry: null,
			sdkVersion: null,
			modelConstructed: false,
			modelSpecificationVersion: null,
			error: null,
		},
		notice: {
			title: "Claude Agent SDK notice",
			text: "You obtain it under Anthropic's terms.",
			sha256: NOTICE_SHA256,
		},
		installAction: {
			procedure: "runtime.installClaudeAgentSdk",
			label: "Install Claude Agent SDK",
			requiresAcknowledgedNoticeSha256: true,
		},
		lastInstallError: null,
		...overrides,
	};
}

const queries = vi.hoisted(() => ({
	fetchClaudeAgentSdkStatus: vi.fn(),
	installClaudeAgentSdk: vi.fn(),
}));

vi.mock("@/runtime/runtime-config-query", () => queries);

function byTestId<T extends Element>(id: string): T | null {
	return document.body.querySelector(`[data-testid="${id}"]`) as T | null;
}

describe("ClaudeAgentSdkInstallSection", () => {
	let container: HTMLDivElement;
	let root: Root;

	beforeEach(() => {
		(globalThis as typeof globalThis & { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
		queries.fetchClaudeAgentSdkStatus.mockReset().mockResolvedValue(status());
		queries.installClaudeAgentSdk.mockReset().mockResolvedValue({
			ok: true,
			code: "installed",
			error: null,
			status: status({ state: "installed" }),
		});
		container = document.createElement("div");
		document.body.appendChild(container);
		root = createRoot(container);
	});

	afterEach(() => {
		act(() => root.unmount());
		container.remove();
		document.body.innerHTML = "";
	});

	it("shows the provider as not installed and sends nothing until the notice is acknowledged", async () => {
		await act(async () => {
			root.render(<ClaudeAgentSdkInstallSection workspaceId={null} open />);
		});
		expect(byTestId("claude-agent-sdk-state")?.getAttribute("data-state")).toBe("not_installed");
		expect(byTestId("claude-agent-sdk-state")?.textContent).toBe("Not installed");

		await act(async () => {
			byTestId<HTMLButtonElement>("claude-agent-sdk-install")?.click();
		});
		expect(byTestId("claude-agent-sdk-notice")?.textContent).toBe("You obtain it under Anthropic's terms.");
		const confirm = byTestId<HTMLButtonElement>("claude-agent-sdk-confirm-install");
		expect(confirm?.disabled).toBe(true);
		await act(async () => {
			confirm?.click();
		});
		expect(queries.installClaudeAgentSdk).not.toHaveBeenCalled();

		await act(async () => {
			byTestId<HTMLButtonElement>("claude-agent-sdk-acknowledge")?.click();
		});
		expect(byTestId<HTMLButtonElement>("claude-agent-sdk-confirm-install")?.disabled).toBe(false);
		await act(async () => {
			byTestId<HTMLButtonElement>("claude-agent-sdk-confirm-install")?.click();
		});
		expect(queries.installClaudeAgentSdk).toHaveBeenCalledWith(null, {
			acknowledgedNoticeSha256: NOTICE_SHA256,
			sdkVersion: null,
		});
		expect(byTestId("claude-agent-sdk-state")?.getAttribute("data-state")).toBe("installed");
	});

	it("renders nothing, and blocks nothing, when the runtime cannot answer", async () => {
		queries.fetchClaudeAgentSdkStatus.mockReset().mockRejectedValue(new Error("offline"));
		await act(async () => {
			root.render(<ClaudeAgentSdkInstallSection workspaceId={null} open />);
		});
		expect(byTestId("claude-agent-sdk-section")).toBeNull();
	});
});
