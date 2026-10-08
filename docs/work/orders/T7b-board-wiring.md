# T7b — Board wiring in Docker: desks, launch binding, agent workspaces

ADR: [AT-0004](../../adr/AT-0004-portable-kanban-suite.md). Header: [ARM-HEADER](ARM-HEADER.md). Dispatch: FEATURE + TEST.

## Problem

The board is the product's primary interface (AT-0004: Kanban replaces the legacy board, dispatch and intake). In the Docker deployment, the `board` role starts and answers on loopback, as the T7 rehearsal and T7a showed, but its desk features are not wired.

- **Desk menu.** The image sets `KANBAN_DESK_REGISTRY_COMMAND='["/usr/local/bin/kp-agent-desk-registry"]'` with no `--config`. The compose `board` role adds nothing, so the Desks menu cannot reach a registry.
- **Desk launches.** `KANBAN_LAUNCH_BINDING_COMMAND` is unset in the image and in compose. A desk task in the board therefore never starts, which is fail-closed by design. LAUNCH-BINDING.md says "the deployment slice sets the image default".
- **Registry path.** Since T4 the runtime has one fixed registry operator path, `/config/launch/registry.json`. The rendered host adapter config already uses it.
- **Workspaces for in-container agents.** In the opt-in `agents` image, a board task runs the Claude/Codex CLI inside the board container. The installer gives the board no repository mount at all, so Kanban cannot create a task worktree.
- **Assistant memory.** The assistant memory command is wired only by hand (`--binding`, `KANBAN_ASSISTANT_MEMORY_WORKSPACE`), and the documentation does not state the container form.

## Properties and falsifiers

- **P1: the Desks menu works in Docker.** The `board` role's desk registry command is `kp-agent-desk-registry --config /config/launch/registry.json`, run in the board container.
  - With that registry configured (DOCKER.md/HOST-ADAPTER.md steps), the Desks menu lists, saves a desk and binds a session through the board's own HTTP interface, writing the same registry the `tooling` and `capture` roles read.
  - Without it, the menu reports that the registry is not configured, and the board keeps serving. `kp-agent-install verify` readiness names `config/launch/registry.json` for `board`.

  Falsifier: a menu action that cannot reach a configured registry; a board that fails or exits for an absent registry; or a write the other roles cannot read.
- **P2: desk tasks bind and capture in the container.** The board role sets `KANBAN_LAUNCH_BINDING_COMMAND` to `["/usr/local/bin/kp-agent-launch", "--config", "/config/launch/registry.json"]`.
  - With the registry configured, a desk task started from the board in an `agents`-mode container binds with `source: "board"` (Claude at launch, Codex on its first hook).
  - Its Stop is captured under the desk, and `memory.search` from the bound session finds that turn.
  - With the registry absent, a desk task does not start and the reason is shown. A task without a desk launches exactly as before.

  Falsifier: a desk task launching unbound; a capture outside the desk; or a desk-less task changed.
- **P3: agent workspaces are writable only where needed.** A new plan input, `--board-agents`, documented for use with the `agents` image, mounts each planned repository read-write at its host path into the `board` role only. Kanban can then create and remove task worktrees there.
  - Without the flag, the board gets no repository mount, as today.
  - Every other role keeps its current mounts. The isolation test (`tests/install/test_p4_rendered_isolation.py`) stays unmodified and green.
  - `--board-agents` without the `board` component is refused at `plan`.

  Falsifier: a read-write repository mount in any role but `board`; a board task worktree that cannot be created with the flag; or a repository mount without it.
- **P4: the assistant has documented memory wiring.** DOCKER.md states the container form of the assistant memory command, its binding file under `$root/config/board/` and the workspace variable. With them set, the sidebar assistant's memory calls reach the registry desk named in the binding; without them, its memory reports unconfigured.
  Falsifier: a documented step that does not work verbatim, or assistant memory reaching another desk.
- **P5: documentation.** DOCKER.md's board section, LAUNCH-BINDING.md (container example) and HOST-ADAPTER.md (one registry for host and board launches) describe P1–P4 as the code behaves.
  Falsifier: a statement the code contradicts.

## Write scope

- **FEATURE:**
  - `assets/deploy/compose.yaml` (board role environment, and the board repository mounts under `--board-agents`);
  - `_impl/runtime_install.py` and `install_cli.py` (`--board-agents`, plus board readiness for `config/launch/registry.json`);
  - `deploy/Dockerfile`, final stages only, if image defaults change;
  - `deploy/image/kanban` and `deploy/image/container.py`, only if the board's start or health needs it;
  - `apps/kanban/src/server/desk-registry-bridge.ts` and `apps/kanban/src/terminal/agent-session-adapters.ts`, only for an unconfigured-registry message;
  - docs: `DOCKER.md`, `LAUNCH-BINDING.md`, `HOST-ADAPTER.md`, `DESK-MENU.md`.

  Every existing test stays unmodified and green.
- **TEST:**
  - new files under `tests/install/` (P3, and P1 readiness);
  - image-marked tests under `tests/install/` or `tests/image/` for P1, P2 and P4 in real containers. They use the `agents` image (`AGENT_TOOLING_TEST_IMAGE_AGENTS`) with fake `claude`/`codex` executables placed ahead on PATH. Never call a real provider.
  - Kanban tests under `apps/kanban/test/`, new files only, if a board message changes;
  - `tests/docs/` (P5).

## Acceptance at the meet

The Coordinator:
- builds `agents` from the merged tree;
- installs into an empty root with `--board-agents`;
- creates a desk from the board's Desks menu;
- starts one desk task with the real Claude Code CLI, a short prompt, against a fixture repository;
- shows the binding, the capture and a desk-scope `memory.search` hit.
