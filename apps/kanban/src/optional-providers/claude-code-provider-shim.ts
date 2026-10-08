// The board's build resolves the package specifier "ai-sdk-provider-claude-code"
// to this module (scripts/build.mjs, claudeAgentSdkExternalPlugin). Neither that
// package nor the Claude Agent SDK it imports is bundled into the board. The
// bundled Cline SDK calls createClaudeCode() only when the Claude Code provider is
// used; it then receives the provider the user installed, or a clear
// "not installed" error that affects only that provider.

import {
	ClaudeAgentSdkNotInstalledError,
	type ClaudeCodeProviderLike,
	getLoadedClaudeCodeProviderModule,
} from "./claude-code-provider-registry";

export function createClaudeCode(options?: unknown): ClaudeCodeProviderLike {
	const providerModule = getLoadedClaudeCodeProviderModule();
	if (!providerModule) {
		throw new ClaudeAgentSdkNotInstalledError();
	}
	return providerModule.createClaudeCode(options);
}
