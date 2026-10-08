// The notice the board shows, and the user acknowledges, before it fetches the
// Claude Agent SDK. The text is pinned here and by its SHA-256 below: an edit to
// the text without a new digest makes the install action refuse (and its test
// fail). The install request must carry the digest of exactly this text.

import { createHash } from "node:crypto";

export const CLAUDE_AGENT_SDK_NOTICE_TITLE = "Claude Agent SDK: an optional component you obtain yourself";

export const CLAUDE_AGENT_SDK_NOTICE_TEXT = [
	"This board does not include the Claude Agent SDK (the npm package @anthropic-ai/claude-agent-sdk). The Claude Code provider needs it. Install it only if you want to use that provider; every other provider and every board function works without it.",
	"",
	"If you choose Install, the board downloads @anthropic-ai/claude-agent-sdk and its provider adapter, ai-sdk-provider-claude-code, from the npm registry, at the versions this board's lockfile pins unless you choose another SDK version, and installs them in this board's state directory. Nothing is downloaded until you acknowledge this notice.",
	"",
	"- You obtain the Claude Agent SDK from its publisher, Anthropic, under Anthropic's terms. The package declares its licence as \"SEE LICENSE IN README.md\". Read those terms before you install it. They, and not this board's licence, govern your use of the SDK and of any Anthropic service it reaches.",
	"- The authors and distributors of this board do not license the Claude Agent SDK to you, and they give no warranty of any kind for it or for your use of it. It is not part of this board, and this board's licence does not cover it.",
	"- You are responsible for complying with Anthropic's terms, including any account, subscription or usage terms that apply to you.",
	"",
	"By acknowledging this notice you confirm that you have read it, and that you obtain the Claude Agent SDK yourself, under Anthropic's terms.",
].join("\n");

/** SHA-256 (hex) of CLAUDE_AGENT_SDK_NOTICE_TEXT as UTF-8. */
export const CLAUDE_AGENT_SDK_NOTICE_SHA256 = "34cfeae3da76b89ce8f96dc061e59329cc02dd0042ab513fdbcd960c10386c1f";

export function computeClaudeAgentSdkNoticeSha256(text: string = CLAUDE_AGENT_SDK_NOTICE_TEXT): string {
	return createHash("sha256").update(text, "utf8").digest("hex");
}

/** True when the shipped text still matches its pinned digest. */
export function isClaudeAgentSdkNoticePinned(): boolean {
	return computeClaudeAgentSdkNoticeSha256() === CLAUDE_AGENT_SDK_NOTICE_SHA256;
}
