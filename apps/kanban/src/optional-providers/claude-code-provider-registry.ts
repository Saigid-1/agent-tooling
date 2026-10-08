// Holds the Claude Code provider module once the board has loaded it from the
// user's own install under the board's state directory. Nothing in this file
// imports the provider package or the Claude Agent SDK: the board does not ship
// either one, and loads them only from the location the install action wrote.

export const CLAUDE_CODE_PROVIDER_ID = "claude-code";
export const CLAUDE_AGENT_SDK_INSTALL_PROCEDURE = "runtime.installClaudeAgentSdk";
export const CLAUDE_AGENT_SDK_INSTALL_ACTION_LABEL = "Install Claude Agent SDK";

/** The part of an AI SDK language model the board reads in its probe. */
export interface ClaudeCodeLanguageModelLike {
	readonly specificationVersion?: unknown;
	readonly provider?: unknown;
	readonly modelId?: unknown;
}

/** The provider function ai-sdk-provider-claude-code's createClaudeCode() returns. */
export type ClaudeCodeProviderLike = ((modelId: string, settings?: unknown) => ClaudeCodeLanguageModelLike) & {
	languageModel?: (modelId: string, settings?: unknown) => ClaudeCodeLanguageModelLike;
	specificationVersion?: unknown;
};

/** The export of ai-sdk-provider-claude-code that the bundled Cline SDK imports. */
export interface ClaudeCodeProviderModule {
	createClaudeCode: (options?: unknown) => ClaudeCodeProviderLike;
}

let loadedProviderModule: ClaudeCodeProviderModule | null = null;

export function setLoadedClaudeCodeProviderModule(module: ClaudeCodeProviderModule | null): void {
	loadedProviderModule = module;
}

export function getLoadedClaudeCodeProviderModule(): ClaudeCodeProviderModule | null {
	return loadedProviderModule;
}

export class ClaudeAgentSdkNotInstalledError extends Error {
	readonly code = "CLAUDE_AGENT_SDK_NOT_INSTALLED";

	constructor() {
		super(
			`The Claude Code provider is not installed. This board does not include the Claude Agent SDK; ` +
				`use "${CLAUDE_AGENT_SDK_INSTALL_ACTION_LABEL}" in the provider settings ` +
				`(board action ${CLAUDE_AGENT_SDK_INSTALL_PROCEDURE}) to obtain it under Anthropic's terms.`,
		);
		this.name = "ClaudeAgentSdkNotInstalledError";
	}
}
