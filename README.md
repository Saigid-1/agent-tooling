# Agent Tooling

Agent Tooling gives coding agents desk-scoped memory, transcript capture and code navigation over MCP and a CLI, with a Kanban board as the control surface.

A desk is data in the memory store: a name, a role, its repositories, and whether its sessions are captured and may write memory. A session reaches a desk's memory only through a binding, made when the board or the host adapter launches it, or by the operator; neither a model nor a hook payload can choose a desk or admit itself. Memory and navigation need neither the board nor a particular agent. The design is [ADR AT-0004](docs/adr/AT-0004-portable-kanban-suite.md); desks and bindings are in [DESK-MENU.md](docs/DESK-MENU.md) and [LAUNCH-BINDING.md](docs/LAUNCH-BINDING.md).

## Release stance

1. **What blocks a release.** A finding at a verification meet blocks the release only if it is a leak, a redistribution, data loss or a silent failure. Everything else is carried, in the open.
2. **Every carried item is an issue.** Each names where it is in the code and what done looks like. The open count on day one is deliberate.
3. **Every property has a falsifier.** Each shipped property names the change that must turn its test red; a test that cannot go red is not evidence.

## Components

- `packages/tooling` (`kp-agent-tooling`): the portable core. Code navigation (search, SCIP, Serena, semantic and local code lookup), desk memory and its indexer, the desk registry, launch binding and harness profiles, the host adapter, transcript capture, session import, refresh, the model gateway, the optional summarizer, setup and the installer. It imports nothing from the OPS extension, and its default tool lists contain core tools only.
- `extensions/ops` (`kp-agent-tooling-ops`): the OPS extension, a separate distribution that depends on the core, never the reverse. It plugs into the navigation server through the `kp_agent_tooling.tools` entry-point group. See [its README](extensions/ops/README.md).
- `apps/kanban`: the board, a fork of Cline Kanban. It adds the **Desks** menu over the desk registry, desk task launch binding, the **Import history** dialog, the sidebar Workspace Assistant, a durable filesystem inbox and board observation.
- `deploy`: the multi-target `deploy/Dockerfile`, the Compose manifest (`deploy/compose.yaml`, a symlink to the copy packaged with the core), example operator configs, the pinned agent CLIs of the optional image targets (`deploy/image/agents`, `deploy/image/opencode`) and the telemetry configuration.
- `scripts`: operator helpers for the core (model profile, Kanban import trial).

## Images

Each release publishes four targets of `deploy/Dockerfile` to `ghcr.io/saigid-1/agent-tooling`, for linux/amd64 and linux/arm64:

- `product`: navigation, memory, capture, refresh and the board. It is the image an installation expects.
- `runtime`: the slim Python package (the memory and navigation CLI), without Node, Serena or the board.
- `ops`: `product` plus the OPS extension.
- `opencode`, optional: `product` plus the OpenCode CLI, for board tasks that run OpenCode. Pull it only if you need it.

The [release page](https://github.com/Saigid-1/agent-tooling/releases) lists, for each target, its manifest list (tagged `<target>-<version>`) and each architecture's digest, with the pull by digest. The `agents` target (`product` plus the Claude Code and Codex CLIs) is never published; build it from a checkout if you want board tasks to run those CLIs in the board's container.

No published image contains Claude Code, Codex or the Claude Agent SDK; Claude Code and Codex are installed by the user on their own host.

## First run

[DOCKER.md](docs/DOCKER.md#first-run-from-an-empty-root) is the whole first run: from an empty directory to every role healthy, one desk, one bound session, capture running, the model gateway ready and a search that answers. It pulls the `product` image by digest, renders a private runtime root (the plan writes nothing, the apply needs the reviewed plan's hash, and a receipt records every file), starts the roles and configures them. For a host install without Docker, see [FRESH-WORKSPACE.md](docs/FRESH-WORKSPACE.md) and [MEMORY.md](docs/MEMORY.md).

## Harnesses and model providers

[HARNESSES.md](docs/HARNESSES.md) covers Claude Code, Codex, OpenCode with OpenRouter, the model gateway (OpenRouter and a local model behind an OpenAI-compatible endpoint), and the board's in-board agent with the optional Claude Agent SDK. For each it says how you install it, whether any published image contains it, how this tooling binds to it, and whether it is Built.

## Licence

Agent Tooling is licensed under the GNU Affero General Public License, version 3 only (`AGPL-3.0-only`; see [LICENSE](LICENSE)). `apps/kanban`, the Cline Kanban fork, keeps its Apache-2.0 licence and upstream notices ([apps/kanban/LICENSE](apps/kanban/LICENSE), [apps/kanban/NOTICE](apps/kanban/NOTICE)). The third-party components the repository contains or builds into its images keep their own licences, listed in [NOTICE](NOTICE).

## Contributing

Read [CONTRIBUTING.md](CONTRIBUTING.md): open an issue first for anything larger than a small fix, keep each pull request to one change with its tests, and sign off every commit under the Developer Certificate of Origin (`git commit -s`). There is no contributor licence agreement. CONTRIBUTING.md also lists the commands CI runs and the fixtures that cannot be regenerated from the public history. Private state, credentials, transcripts and model caches stay outside Git.
