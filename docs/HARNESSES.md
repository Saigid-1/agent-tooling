# Harnesses and model providers

A harness is the agent CLI that runs a session; a model provider answers the model
gateway's capability tools. Each entry below says how you install it, whether any
published image contains it, how this tooling binds to it, and its status.
**Built** means the implementation and its tests are merged in this tree; nothing
here is claimed for 0.4.0 without it.

Launch binding and the harness profiles are in [LAUNCH-BINDING.md](LAUNCH-BINDING.md),
host launches in [HOST-ADAPTER.md](HOST-ADAPTER.md), the published images in
[DOCKER.md](DOCKER.md#images).

## Claude Code

**Install.** You install Claude Code yourself, on your own host, from Anthropic
([quickstart](https://docs.anthropic.com/en/docs/claude-code/quickstart)). No
published image contains it: its licence is Anthropic's own terms, not an
OSI-approved licence, and this project does not redistribute it ([NOTICE](../NOTICE)).
The `agents` image target, which you build yourself from a checkout and which is
never published, installs Claude Code 2.1.280, the version
`deploy/image/agents/package-lock.json` pins, for board tasks that run in the
board's container ([DOCKER.md](DOCKER.md#board-tasks-with-in-container-agents-agents-image)).

**Binding.** The harness profile `claude` launches `claude` with a session id minted
at launch (`--session-id`), so the session is bound to its desk before its first
turn; the desk memory MCP server through `--mcp-config`; and its `Stop`,
`PreCompact` and `SessionEnd` hooks through `--settings`. Capture reads the
session's transcript under `~/.claude/projects`. On your host,
`kp-agent-host --config <adapter config> launch claude --desk <desk id>` runs it
bound, with source `host`; in the `agents` image, a board task with a desk runs it
bound, with source `board`.

**Status.** Built: the harness profile and launch binding (T3) and the host
adapter (T4).

## Codex

**Install.** You install the Codex CLI yourself, on your own host
([openai/codex](https://github.com/openai/codex)). No published image contains it.
Its npm licence is Apache-2.0, but the release publishes no image containing
Claude Code or Codex: the project cannot ensure licence compliance for those
CLIs ([the public-readiness audit](work/PUBLIC-READINESS-AUDIT.md)). Only the
`agents` target, built locally, installs Codex 0.159.2, the version
`deploy/image/agents/package-lock.json` pins.

**Binding.** The harness profile `codex` is bound on the session's first hook,
which carries its session id. The desk memory MCP server (`mcp_servers`) and the
`UserPromptSubmit` and `Stop` hooks travel as `-c` configuration overrides.
Capture reads the session's rollout under `~/.codex/sessions`. On your host,
`kp-agent-host --config <adapter config> launch codex --desk <desk id>` runs it
bound, with source `host`; in the `agents` image, a board task with a desk does,
with source `board`.

**Status.** Built: the harness profile and launch binding (T3) and the host
adapter (T4). The host adapter's known limitations are listed in
[HOST-ADAPTER.md](HOST-ADAPTER.md#known-limitations).

## OpenCode (with OpenRouter)

**Install.** OpenCode ships in the optional `opencode` image: `product` plus the
OpenCode CLI 1.18.34, the version `deploy/image/opencode/package-lock.json` and the
Dockerfile's `OPENCODE_VERSION` pin. The OpenCode CLI is MIT; the image carries its
upstream LICENSE. Pull it only if board tasks should run OpenCode, and install its
digest as the image of your runtime ([DOCKER.md](DOCKER.md#images)): the
`opencode` binary exists only there. There is nothing to install on your host,
because OpenCode runs only as a board task.

**Binding.** OpenCode is board-launched only. A desk task's launch is bound
through the harness profile `opencode`: the desk memory MCP server travels in
`OPENCODE_CONFIG_CONTENT` and the launch's hook command in
`KP_AGENT_LAUNCH_HOOK_COMMAND`, which the board's OpenCode plugin runs for each
hook event (`Stop`, `PreCompact`, `SessionEnd`); the session is bound on its first
hook. Capture reads the bound session through OpenCode's own export
(`opencode export`, parser `opencode-export`), and only a root session of the
launch workspace. Every board launch of OpenCode runs
under the board's approval policy: edits inside the task worktree are allowed,
every shell command and web fetch waits for your answer in OpenCode's terminal, and
other directories are denied. The worktree's own OpenCode configuration is not read.

- **A host launch of OpenCode is refused.** OpenCode reports its turn end only
  through the board's plugin, so `kp-agent-launch prepare` refuses an `opencode`
  launch with source `host` before it writes anything. `kp-agent-host launch
  opencode` is refused too.
- **Only a bound board launch is captured.** Workspace capture reads Claude and
  Codex transcript roots, and OpenCode writes no transcript, so an OpenCode
  session outside a bound board launch is never captured.
- **The operator's OpenRouter key file.** OpenCode tasks use OpenRouter with the
  model gateway's key file, `$root/config/models/openrouter.key`
  ([DOCKER.md](DOCKER.md#first-run-from-an-empty-root), step 7). The board role sees
  it read-only and names it to OpenCode by reference
  (`KANBAN_OPENCODE_OPENROUTER_KEY_FILE`); the key is never written into the
  board's files, a launch receipt or the launched process's environment, and
  provider credentials in the board's own environment are cleared for the launched
  process.
- **An unbound launch reaches the npm registry.** An OpenCode task launched without
  a desk is unbound, and OpenCode then runs its own background npm install of
  `@opencode-ai/plugin`, so the board role reaches the npm registry. A bound launch,
  a task with a desk, installs nothing: its configuration directory is the board's,
  empty and read-only.

**Status.** Built: the optional `opencode` image (O3), the board launch under the
approval policy (O1), and launch binding with export capture (O2).

## The model gateway: OpenRouter and a local model

**Install.** The gateway (`kp-agent-models`) is part of the core package, so every
published image contains it; you supply each provider and its key file. For
OpenRouter, that is your OpenRouter key. For a local open-weight model, you run its
server yourself (Ollama or similar) with an OpenAI-compatible API; no image built
from this repository contains a model server.

**Binding.** The gateway's configuration (`agent-tooling.model-gateway.v1`) routes
each capability (`image.generate`, `audio.transcribe`, `audio.speak`,
`text.complete`, or one you name) to a provider and a model, and each route becomes
one `model.<capability>` MCP tool ([MODEL-GATEWAY.md](MODEL-GATEWAY.md)). The
provider kinds are `openrouter` and `openai-compatible`. A local model is an
`openai-compatible` provider whose `base_url` is the server's OpenAI-compatible
API. Plain `http` is accepted for a loopback host only, unless the provider sets
`allow_plain_http: true`; in the Docker runtime the gateway runs in the `tooling`
container, whose loopback is the container's own. Every provider names an
`api_key_file`, read only to build the request's Authorization header.

- **A local model is a gateway route, not a desk-task harness, in 0.4.0.** Desk
  tasks run Claude Code, Codex or OpenCode; no harness profile names a gateway
  provider.
- **The summarizer is opt-in.** The summarizer role (S1a) starts only when you
  plan the runtime with the `summarizer` component; its Compose service is in the
  `summarizer` profile. It completes through a gateway chat route, and before it
  sends anything it reads the routed model's context length and parameters from the
  provider's `/models` listing, in the shape OpenRouter returns. A provider whose
  listing lacks them leaves the summarizer `not_configured`, and it sends nothing.
  No local model server's listing has been measured against it.

**Status.** Built: the gateway with both provider kinds (M1, tested against a stub
provider of each kind) and the optional summarizer (S1a). A route to a local model
is configuration of the `openai-compatible` kind; no local model server has been
measured in this tree.

## The in-board Cline agent and the optional Claude Agent SDK

The board, a fork of Cline Kanban, carries Cline's own in-board agent and its chat
panel. **Its launch is gated in 0.4.0:** the board builds its Cline session service
with launch disabled, and a start is refused with "Cline agent launch is not ready
under a verified approval policy." Desk tasks launch through the registry's harness
profiles instead: Claude Code and Codex (on your host, or in the `agents` image) and
OpenCode (in the `opencode` image).

**Install.** The in-board agent is part of the board. Its Claude Code provider needs
the Claude Agent SDK, which you install yourself through the board (below); no
published image contains the SDK, because it is under Anthropic's terms.

**Status.** Built, with the in-board agent's launch gated in 0.4.0, so it is not
a desk-task harness in this release. The Claude Agent SDK's install action:
Built (D0f).

### The Claude Agent SDK

The board's Claude Code provider runs on the Claude Agent SDK
(`@anthropic-ai/claude-agent-sdk`). No image built from this repository
contains it, and the board works without it: the provider reads "Not
installed" and every other provider and board function is unaffected.

To install it, open the board's **Settings**, find **Optional components** under
**General**, and choose **Install Claude Agent SDK…**. The board shows a notice
first: you obtain the SDK from Anthropic under Anthropic's terms, and this
project neither licenses nor warrants your use of it. The **Install** button
stays disabled until you acknowledge that notice; nothing is downloaded before.

The board then installs the SDK and its provider adapter
(`ai-sdk-provider-claude-code`) from the npm registry, at the versions
`apps/kanban/package-lock.json` pins unless you enter another SDK version, under
its state directory: `/state/kanban/kanban/optional-packages/claude-agent-sdk`
in the container image. The board role needs outbound network for this one
action; it does not restart, and the install survives a restart because it
lives on `/state`.

The action is the board's tRPC mutation `runtime.installClaudeAgentSdk`, which
installs nothing unless its `acknowledgedNoticeSha256` is the SHA-256 of the
notice text; `runtime.getClaudeAgentSdkStatus` returns that text, its digest and
the provider's state. The notice text is pinned in
`apps/kanban/src/optional-providers/claude-agent-sdk-notice.ts`.
