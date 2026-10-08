# Local Kanban source delta

Upstream: `https://github.com/cline/kanban.git` at
`abd4912c27ce6b7f18b5a8106c145fd838e90cc4` (2026-09-23 trial pin).
The fork was developed on a local trial branch. It is a local source checkout, not a
GitHub fork or upstream offer. Apache-2.0 `LICENSE` is retained, and this
repository's own changes are Apache-2.0 too (see `../NOTICE`). The complete list of
changed files is in the last section of this manifest.

`git log --reverse --format=%s abd4912c27ce6b7f18b5a8106c145fd838e90cc4..HEAD`
must produce exactly these subjects, in this order:

1. `K5: Select a physical runtime storage root`
2. `K1: Restrict trial launches to verified Claude and Codex arguments`
3. `K5: Keep debug reset inside selected storage root`
4. `K1: Reject unreviewed launch arguments and refresh adapter contract tests`
5. `K1: Disable native Cline launch in the production board`
6. `K5: Test reset preserves unrelated Cline data`
7. `K6: Build without telemetry or remote onboarding assets`
8. `K5: Refuse empty storage roots and guard trial startup`
9. `K6: Disable inherited OTEL and Featurebase egress`
10. `K6: Disable updater during guarded local trial startup`

| Delta | Layer and rationale | Files | Covering check | Upstream offer |
| --- | --- | --- | --- | --- |
| K5 physical root | 1: `KANBAN_STORAGE_ROOT` selects an existing, absolute, physical directory for Kanban state and worktrees. An absent configured root raises before fallback. The separate trial wrapper verifies that the trial volume is mounted. Portable default remains upstream behavior when unset. | `src/state/runtime-storage-root.ts`, `src/state/workspace-state.ts`, `src/config/runtime-config.ts`, `test/runtime/runtime-storage-root.test.ts` | storage-root tests | Not submitted |
| K1 launch posture | 2: The existing adapters inject bypass flags and native Cline has a separate launch path. A PATH shim can be bypassed, while an unchecked generic argument seam would admit unsafe flags. This trial directly limits launch to Claude, Codex and OpenCode, inserts Codex workspace sandbox and on-request approvals, and refuses other harnesses until policy checks exist. OpenCode (order O1) launches as its interactive form with `--prompt` under a generated `opencode.json` whose `permission` is `edit` allow, `bash` ask, `webfetch` ask, `external_directory` deny. Measured on the pinned binary, a lower config file can loosen a permission map from below whatever the board sets above it, so the launch reads no configuration a repository or user file feeds: it sets `OPENCODE_DISABLE_PROJECT_CONFIG` (the repository's own `opencode.json` and `.opencode` agents, commands and plugins are off), moves `XDG_CONFIG_HOME` to a board-owned directory (the user's global OpenCode config, providers included, is not read, and commands the agent runs see that directory too), clears the config variables that outrank the generated file, and refuses while `HOME/.opencode` carries configuration. OpenCode plan mode is refused: the policy's `edit` rule would open the plan agent. The plugin moves the card to review on OpenCode's `permission.asked` event; its `permission.ask` hook is never called at the pinned version. A task with a desk (order O2) gets its launch binding per launch: the desk memory server travels in `OPENCODE_CONFIG_CONTENT`, which may hold only that one server, and the capture hook command in the launch's own environment, never in the board-wide file. The plugin runs that command, detached, when the root session goes idle (Stop) and on compaction (PreCompact); the terminal's exit runs it for SessionEnd. Such a launch's global config directory is board-owned, empty and read-only: measured on the pinned binary, OpenCode otherwise installs a package into every writable config directory at every launch. Every OpenCode launch turns off sharing, the model-catalogue fetch and Claude Code's files, and clears provider credentials from its environment; the operator's OpenRouter key file reaches it only as a `{file:...}` reference. | `src/core/agent-catalog.ts`, `src/config/runtime-config.ts`, `src/terminal/agent-session-adapters.ts`, `src/cline-sdk/cline-session-runtime.ts`, `test/runtime/terminal/safe-launch.test.ts`, `test/runtime/terminal/o1-opencode-launch.test.ts`, `test/integration/o1/`, `test/runtime/terminal/o2-opencode-launch-binding.test.ts` | safe-launch argv tests; O1 and O2 adapter tests; the O1 A1 effective-policy check and the O2 F1/F2 run on the `opencode` image (they run when `AGENT_TOOLING_TEST_IMAGE_OPENCODE` names it) | Not submitted; generic policy seam candidate |
| K5 reset scope | 1: reset uses the configured Kanban state and worktree paths, so a local trial cannot delete the user's home Cline data. | `src/trpc/runtime-api.ts` | typecheck and storage-root test | Not submitted |
| K1 argument hardening | 2: no caller-supplied CLI arguments or alternate executable paths are admitted in the bounded trial. This blocks duplicate flags, aliases and config overrides before the adapter adds approved flags. Catalog base arguments are empty. Existing adapter and config tests now assert this contract. OpenCode gets the same refusal, and a caller's `OPENCODE_CONFIG` is only the model-resolution base: the launched process's `OPENCODE_CONFIG` is always the generated file. | `src/terminal/agent-session-adapters.ts`, `test/runtime/terminal/safe-launch.test.ts`, `test/runtime/terminal/agent-session-adapters.test.ts`, `test/runtime/config/runtime-config.test.ts` | 40 affected tests | Not submitted |
| K1 native Cline gate | 1: the production server passes `disableLaunch: true` into its Cline runtime; the runtime refuses before creating an SDK host. Tests can still exercise SDK behavior through their own explicitly constructed runtimes. | `src/server/runtime-server.ts`, `src/cline-sdk/cline-task-session-service.ts`, `src/cline-sdk/cline-session-runtime.ts`, `test/runtime/cline-sdk/cline-session-runtime.test.ts` | native Cline runtime suite | Not submitted |
| K5 reset assertion | 1: the runtime API test asserts configured state/worktree reset and preservation of unrelated `~/.cline/data`. | `test/runtime/trpc/runtime-api.test.ts` | runtime API suite | Not submitted |
| K6 no telemetry | 2: upstream has hardcoded Sentry DSNs and GitHub-hosted onboarding videos; a keys-only build leaves those paths. The trial disables PostHog and Sentry, omits remote onboarding media and sourcemap upload, and scans emitted assets for known hosts. | `src/telemetry/sentry-node.ts`, `web-ui/src/telemetry/posthog-config.ts`, `web-ui/src/telemetry/sentry.ts`, `web-ui/src/components/task-start-agent-onboarding-carousel.tsx`, `package.json`, `scripts/verify-no-telemetry-build.mjs`, this manifest | root/web typecheck, source build, emitted-asset scan | Not submitted; fork-only |
| K5 trial start guard | 0: a process-local wrapper checks the trial volume mount and physical root before startup, binds loopback, and clears inherited telemetry variables without changing global settings. The storage seam now rejects an explicitly empty value. A directory check alone does not prove the trial volume is mounted. | `scripts/start-ops-local-trial.sh`, `src/state/runtime-storage-root.ts`, `test/runtime/runtime-storage-root.test.ts` | storage-root tests and wrapper syntax check; mount failure not simulated | Not submitted |
| K6 inherited egress | 2: the UI no longer mounts Featurebase; the native Cline service has no telemetry adapters; the build replaces inherited OTEL values with disabled constants. The emitted-host gate includes Featurebase and a sentinel OTEL endpoint. | `web-ui/src/App.tsx`, `src/cline-sdk/cline-telemetry-service.ts`, `scripts/build.mjs`, `scripts/verify-no-telemetry-build.mjs`, `test/runtime/cline-sdk/cline-telemetry-service.test.ts`, this manifest | build with inherited OTEL sentinel, emitted-host scan, telemetry unit test | Not submitted; fork-only |
| K6 updater guard | 0: the loopback trial wrapper exports `KANBAN_NO_AUTO_UPDATE=1`, preventing Kanban's automatic update check or install path at startup. | `scripts/start-ops-local-trial.sh`, this manifest | wrapper syntax check; `src/update/update.ts` env gate inspected | Not submitted |

The trial storage root exists at
`<operator workspace>/kanban-trial-state`.
After building the checkout, the guarded loopback-only board start command is:

```sh
OPS_TRIAL_VOLUME='<mounted volume>' OPS_TRIAL_WORKSPACE='<operator workspace>' \
  '<operator workspace>/cline-kanban-source/scripts/start-ops-local-trial.sh'
```

Verification so far covers code paths, generated launch argv, typechecks, and
the source build's emitted strings. The build passed with inherited OTEL enabled
and a sentinel exporter URL; the emitted output contains none of the known hosts.
This does not prove comprehensive network silence. No Claude, Codex, or Cline
card was launched from this fork. OpenCode's effective approval policy is measured on its
pinned binary by the O1 A1 check. The other harnesses' effective policy, the legacy coordination bridge,
browser Resource Timing, and server/child-process network egress remain
unproven. A real trial must check those separately before this board replaces
dispatch or the studio.

## K7: Chat-originated observation projection pilot

A later ruling by the project owner makes the board a projection, with commands in chat.
`K7: Project explicit chat work observations without dispatch` adds the typed
`task observe` command, an immutable local receipt spool and the existing locked
board mutation/notification path. See `ops-chat-projection.md` for the live proof,
limitations and bounded next trial. This eleventh delta follows the ten above;
the original exactly-ten assertion applies only to the prepared fork baseline.

## K8: observational cards and guarded board mutations

`K8: Guard projected cards and preserve visual dependency inspection` adds the
projection policy, read-only card UI, server/worktree guards and isolated output
build option. Root/UI typechecks and focused tests cover it; see
`ops-chat-projection.md` for live evidence and adoption limits.

## Files changed relative to the fork point

Apache-2.0 section 4(b) change record. The list compares each file's Git blob with
the upstream tree at `abd4912c27ce6b7f18b5a8106c145fd838e90cc4`, recorded in
`upstream-tree-abd4912c.json` beside this manifest (read from the public GitHub API).
Paths are relative to `apps/kanban`. Every other file is byte-identical to upstream at
the fork point. Regenerate with `python apps/kanban/scripts/kanban_delta.py --write`.

**Modified upstream files (61).**

- `.gitignore`
- `package-lock.json`
- `package.json`
- `scripts/build.mjs`
- `src/cli.ts`
- `src/cline-sdk/cline-provider-service.ts`
- `src/cline-sdk/cline-session-runtime.ts`
- `src/cline-sdk/cline-task-session-service.ts`
- `src/cline-sdk/cline-telemetry-service.ts`
- `src/commands/hook-events/codex-hook-events.ts`
- `src/commands/hooks.ts`
- `src/commands/task.ts`
- `src/config/runtime-config.ts`
- `src/core/agent-catalog.ts`
- `src/core/api-contract.ts`
- `src/core/task-board-mutations.ts`
- `src/server/middleware.ts`
- `src/server/runtime-server.ts`
- `src/server/runtime-state-hub.ts`
- `src/state/workspace-state.ts`
- `src/telemetry/sentry-node.ts`
- `src/terminal/agent-session-adapters.ts`
- `src/terminal/codex-hook-config.ts`
- `src/terminal/session-manager.ts`
- `src/trpc/app-router.ts`
- `src/trpc/hooks-api.ts`
- `src/trpc/runtime-api.ts`
- `src/workspace/task-worktree.ts`
- `test/integration/task-worktree.integration.test.ts`
- `test/integration/workspace-state.integration.test.ts`
- `test/runtime/cline-sdk/cline-session-runtime.test.ts`
- `test/runtime/config/runtime-config.test.ts`
- `test/runtime/hooks-codex-parser.test.ts`
- `test/runtime/hooks-codex-watcher.test.ts`
- `test/runtime/terminal/agent-registry.test.ts`
- `test/runtime/terminal/agent-session-adapters.test.ts`
- `test/runtime/terminal/session-manager.test.ts`
- `test/runtime/trpc/hooks-api.test.ts`
- `test/runtime/trpc/runtime-api.test.ts`
- `vitest.config.ts`
- `web-ui/src/App.tsx`
- `web-ui/src/components/board-card.test.tsx`
- `web-ui/src/components/board-card.tsx`
- `web-ui/src/components/board-column.tsx`
- `web-ui/src/components/detail-panels/cline-agent-chat-panel.tsx`
- `web-ui/src/components/detail-panels/cline-chat-composer.tsx`
- `web-ui/src/components/runtime-settings-dialog.tsx`
- `web-ui/src/components/shared/cline-setup-section.tsx`
- `web-ui/src/components/task-create-dialog.tsx`
- `web-ui/src/components/task-inline-create-card.tsx`
- `web-ui/src/components/task-start-agent-onboarding-carousel.tsx`
- `web-ui/src/hooks/use-task-editor.ts`
- `web-ui/src/hooks/use-task-sessions.ts`
- `web-ui/src/runtime/runtime-config-query.ts`
- `web-ui/src/state/board-state.test.ts`
- `web-ui/src/state/board-state.ts`
- `web-ui/src/telemetry/posthog-config.ts`
- `web-ui/src/telemetry/sentry.ts`
- `web-ui/src/types/board.ts`
- `web-ui/vite.config.ts`
- `web-ui/vitest.config.ts`

**Added by this repository (115).**

- `.plan/docs/durable-inbox-live-trial.md`
- `.plan/docs/durable-inbox.md`
- `.plan/docs/standalone-inbox-publisher.md`
- `NOTICE`
- `docs/OPS-HANDOFF.md`
- `docs/adr-planning.md`
- `docs/harness-session-resolution.md`
- `docs/ops-chat-projection.md`
- `docs/ops-desk-temporal.md`
- `docs/ops-local-delta-manifest.md`
- `docs/upstream-tree-abd4912c.json`
- `scripts/kanban_delta.py`
- `scripts/project-desk-run-export.py`
- `scripts/start-ops-local-trial.sh`
- `scripts/test_desk_run_export.py`
- `scripts/verify-no-claude-agent-sdk-build.mjs`
- `scripts/verify-no-telemetry-build.mjs`
- `src/commands/hook-events/codex-native-binding.ts`
- `src/commands/inbox.ts`
- `src/core/adr-intake.ts`
- `src/core/desk-registry-contract.ts`
- `src/core/desk-temporal.ts`
- `src/core/durable-inbox.ts`
- `src/core/harness-session-resolution.ts`
- `src/core/projection-policy.ts`
- `src/core/work-observation.ts`
- `src/optional-providers/claude-agent-sdk-install.ts`
- `src/optional-providers/claude-agent-sdk-notice.ts`
- `src/optional-providers/claude-agent-sdk-pin.json`
- `src/optional-providers/claude-agent-sdk.ts`
- `src/optional-providers/claude-code-provider-registry.ts`
- `src/optional-providers/claude-code-provider-shim.ts`
- `src/server/desk-registry-bridge.ts`
- `src/server/task-lifecycle-observer.ts`
- `src/session-import/session-import-contract.ts`
- `src/session-import/session-import-runner.ts`
- `src/state/adr-evidence-store.ts`
- `src/state/adr-intake-store.ts`
- `src/state/adr-planning-store.ts`
- `src/state/desk-temporal-reader.ts`
- `src/state/durable-inbox-store.ts`
- `src/state/runtime-storage-root.ts`
- `src/state/task-lifecycle-store.ts`
- `src/state/work-observation-store.ts`
- `src/terminal/assistant-memory-launch.ts`
- `test/integration/d0f/d0f-f1-bundle-excludes-sdk.integration.test.ts`
- `test/integration/d0f/d0f-f2-board-without-sdk.integration.test.ts`
- `test/integration/d0f/d0f-f3-install-action.integration.test.ts`
- `test/integration/d0f/d0f-f5-provider-reaches-sdk.integration.test.ts`
- `test/integration/d0f/d0f-harness.ts`
- `test/integration/d0f/d0f-seams.ts`
- `test/integration/o1/fixtures/hook-recorder.mjs`
- `test/integration/o1/fixtures/run-in-image.mjs`
- `test/integration/o1/fixtures/stub-provider.mjs`
- `test/integration/o1/o1-a1-effective-policy.integration.test.ts`
- `test/runtime/adr-intake.test.ts`
- `test/runtime/adr-planning-store.test.ts`
- `test/runtime/cline-sdk/cline-telemetry-service.test.ts`
- `test/runtime/container-origin.test.ts`
- `test/runtime/desk-registry-bridge.test.ts`
- `test/runtime/desk-temporal.test.ts`
- `test/runtime/desks/desk-registry-t2-contract.test.ts`
- `test/runtime/durable-inbox-store.test.ts`
- `test/runtime/durable-inbox-transport.test.ts`
- `test/runtime/harness-session-resolution.test.ts`
- `test/runtime/hooks-codex-native-binding.test.ts`
- `test/runtime/launch/t3-launch-binding.test.ts`
- `test/runtime/optional-providers/claude-agent-sdk-install.test.ts`
- `test/runtime/runtime-storage-root.test.ts`
- `test/runtime/session-import-runner.test.ts`
- `test/runtime/task-lifecycle-hub.test.ts`
- `test/runtime/task-lifecycle.test.ts`
- `test/runtime/terminal/assistant-memory-launch.test.ts`
- `test/runtime/terminal/o1-opencode-image-runner.ts`
- `test/runtime/terminal/o1-opencode-launch-contract.test.ts`
- `test/runtime/terminal/o1-opencode-launch.test.ts`
- `test/runtime/terminal/o1-opencode-launched-process.test.ts`
- `test/runtime/terminal/o1-seams.ts`
- `test/runtime/terminal/o2-opencode-image-runner.ts`
- `test/runtime/terminal/o2-opencode-launch-binding.test.ts`
- `test/runtime/terminal/o2-opencode-launched-env.test.ts`
- `test/runtime/terminal/o2-test-image-runner.ts`
- `test/runtime/terminal/safe-launch.test.ts`
- `test/runtime/work-observation-store.test.ts`
- `test/runtime/work-observation.test.ts`
- `test/runtime/workspace-session-registry.test.ts`
- `web-ui/public/session-import-setup.html`
- `web-ui/src/components/__tests__/desk-dialogue-t2.test.tsx`
- `web-ui/src/components/__tests__/task-desk-selector-t3.test.tsx`
- `web-ui/src/components/adr-evidence-panel.test.tsx`
- `web-ui/src/components/adr-evidence-panel.tsx`
- `web-ui/src/components/adr-planning-chat.test.tsx`
- `web-ui/src/components/adr-planning-chat.tsx`
- `web-ui/src/components/adr-planning-model.ts`
- `web-ui/src/components/adr-planning-view.test.tsx`
- `web-ui/src/components/adr-planning-view.tsx`
- `web-ui/src/components/adr-slice-intake-form.test.tsx`
- `web-ui/src/components/adr-slice-intake-form.tsx`
- `web-ui/src/components/desk-bind-dialogue.tsx`
- `web-ui/src/components/desk-dialogue.test.tsx`
- `web-ui/src/components/desk-dialogue.tsx`
- `web-ui/src/components/desk-memory-view.tsx`
- `web-ui/src/components/desk-menu.test.tsx`
- `web-ui/src/components/desk-menu.tsx`
- `web-ui/src/components/desk-registry-fields.test.tsx`
- `web-ui/src/components/desk-role-dialogue.tsx`
- `web-ui/src/components/desk-session-dialogue.tsx`
- `web-ui/src/components/detail-panels/session-history-import-dialog.test.tsx`
- `web-ui/src/components/detail-panels/session-history-import-dialog.tsx`
- `web-ui/src/components/shared/claude-agent-sdk-install.test.tsx`
- `web-ui/src/components/shared/claude-agent-sdk-install.tsx`
- `web-ui/src/components/task-desk-picker.test.tsx`
- `web-ui/src/components/task-desk-picker.tsx`
- `web-ui/src/hooks/use-adr-planning.ts`
- `web-ui/src/hooks/use-desk-registry.ts`

**Removed upstream files (20).**

- `.plan/docs/ACP/ACP-docs.md`
- `.plan/docs/CLI References/claude-code-cli-reference.md`
- `.plan/docs/CLI References/codex-cli-reference.md`
- `.plan/docs/CLI References/gemini-cli-reference.md`
- `.plan/docs/CLI References/kiro-cli-reference.md`
- `.plan/docs/CLI References/opencode-cli-reference.md`
- `.plan/docs/Skills/agent-skills-protocol.md`
- `.plan/docs/Skills/claude-code-skills.md`
- `.plan/docs/blueprint-ui-docs.md`
- `.plan/docs/hooks-update/claude-code-hooks-docs.md`
- `.plan/docs/hooks-update/cline-cli-hooks-docs.md`
- `.plan/docs/hooks-update/gemini-cli-configuration.md`
- `.plan/docs/hooks-update/gemini-cli-hooks-docs.md`
- `.plan/docs/hooks-update/opencode-hooks-docs.md`
- `packages/desktop/build/bin/kanban`
- `packages/desktop/build/bin/kanban-dev`
- `packages/desktop/build/bin/kanban-dev.cmd`
- `packages/desktop/build/bin/kanban.cmd`
- `packages/desktop/build/entitlements.mac.plist`
- `packages/desktop/build/icon.icns`
