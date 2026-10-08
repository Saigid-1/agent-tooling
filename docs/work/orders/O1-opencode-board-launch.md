# O1: OpenCode launches from the board under a named approval policy

Status: frozen 2026-10-06 (the Coordinator's draft r2; Verification ruled it freezable with A1 and the P2 wording, both in). Amendment-1 (below) was added the same day, before the arms were dispatched, from the OpenCode read at the pinned version. Verification ruled it in.

**Base:** main after T12c. **Header:** ARM-HEADER. **The stopping rule applies.** A meet finding is a block or it is carried; the meet record is one paragraph; no commit sha, PR number or run id goes into a committed file. **Estimate:** small, a fraction of a T12a-sized slice. **Decided:** the Principal, 2026-10-06: OpenCode in 0.4.0 (with O2 and O3).

## Problem
Upstream's board already launches OpenCode:
- `apps/kanban/src/terminal/agent-session-adapters.ts` (`opencodeAdapter`, about :1413) launches the `opencode` binary;
- it writes a Kanban plugin whose hooks move the card (to_review, to_in_progress, activity);
- it resolves the model with `openrouter` first.

The fork's K1 launch posture (`docs/ops-local-delta-manifest.md`, "K1 launch posture") comments `"opencode"` out of `RUNTIME_LAUNCH_SUPPORTED_AGENT_IDS` (`src/core/agent-catalog.ts:76-77`): "Additional harnesses need a verified approval policy before launch". The adapter refuses any agent without one (`:1745`). Codex launches under `--sandbox workspace-write --ask-for-approval on-request` (`:1040-1041`).

## Properties
- **P1: the policy, named.** The `opencode.json` the adapter generates for a board launch carries exactly:
  - `"permission": {"edit": "allow", "bash": "ask", "webfetch": "ask", "external_directory": "deny"}`;
  - edits are confined to the task worktree, as Codex's workspace-write is;
  - every command and every web fetch needs the user's approval, which is the stricter reading of Codex's on-request.

  FEATURE confirms each key and value against OpenCode's documented configuration at the version O3 pins and records the version. If `external_directory` does not exist at that version, `edit` becomes `"ask"` and the order's falsifiers follow that.
  - **Where the approval appears:** the plugin's `permission.ask` hook only notifies the board (the card moves to review). The user answers in OpenCode's own terminal UI. That is fine for a terminal launch, and the demo knows where the approval shows.
- **A1: the EFFECTIVE policy, not the file (Verification).** OpenCode also reads a global config under the user's home (the paths `opencode-paths.ts` lists) and a PROJECT config in the working directory, which is the task worktree, so the repository being worked on.
  - **The property:** with a project `opencode.json` in the task worktree AND a global config in HOME that both set every permission key to `"allow"`, the launched OpenCode still asks for bash and webfetch and denies external directories.
  - **FEATURE measures OpenCode's config precedence** at the version O3 pins and records it. If the generated file cannot win by precedence, FEATURE uses what that version offers that does win (a CLI flag, an environment override, or refusing to launch while a loosening project config is present), and the meet record says which.
  - **This needs the real pinned binary,** so it is the one O1 test that runs on O3's image or a host install of the pinned version (not a stub). The meet says which.
- **P2: argument hardening, the same as Codex's (K1).** A launch with caller-supplied CLI arguments or an alternate executable path is refused before the adapter adds its own. The catalog's `baseArgs` stay empty.
  - **A caller's `OPENCODE_CONFIG`:** the adapter at main reads it as the base config for model resolution (`resolveOpenCodeBaseConfigPath`). For the launched process it is IGNORED: `OPENCODE_CONFIG` is always the generated file, so the caller's file can never become the policy. It stays readable only as the model-resolution base. The test asserts that the launched process's `OPENCODE_CONFIG` is the generated file whatever the caller set.
  - **If the pinned version merges `OPENCODE_CONFIG` with other sources,** A1's effective-policy test covers it.
- **P3: the gate opens for OpenCode only.**
  - `"opencode"` joins `RUNTIME_LAUNCH_SUPPORTED_AGENT_IDS`.
  - Every other harness without a verified policy (`gemini`, `droid`, `kiro`, …) is still refused.
  - **K1 stays decided:** O1 enables a HOST CLI terminal launch through the adapter path. The in-board Cline runtime's `disableLaunch: true` (runtime-server) is untouched.
- **P4: upstream reused.** The plugin hooks (card moves) and the OpenRouter-first model resolution are reused as they are. The fork's delta manifest records O1's change in the K1 rows.

## Falsifiers (mutants that must be RED)
- `"opencode"` removed from the launch list again;
- any one permission key dropped from the generated `opencode.json`, or set to `"allow"` where the order says `"ask"` or `"deny"`;
- a caller-supplied argument or an alternate executable accepted;
- the launched process's `OPENCODE_CONFIG` being the caller's file rather than the generated one;
- a loosening project or global config honoured: bash or webfetch allowed without asking, or an external directory written (A1, on the real binary);
- `gemini` (or any other policy-less harness) launched.

## Write scope
- `apps/kanban/src/core/agent-catalog.ts`;
- `apps/kanban/src/terminal/agent-session-adapters.ts` (the opencode adapter's generated config and its hardening);
- `apps/kanban/docs/ops-local-delta-manifest.md` (the K1 rows);
- Kanban tests under `apps/kanban/test/` (safe-launch and adapter);
- `kanban_delta.py --check` stays clean.

## Arms
FEATURE and TEST, blind. TEST writes the safe-launch and generated-config tests and the mutants above, using a stub `opencode` binary, as the Claude and Codex safe-launch tests do. The meet runs `npm` typecheck, vitest and build, and the mutation-RED limited to the falsifiers above.

## Amendment-1: what the pinned version does (2026-10-06)
The source of this amendment is the Coordinator's read of OpenCode at the version O3 pins (`opencode-ai@1.18.34`). Each fact below is cited there to a tag-pinned source line; none was executed. A1 on the real binary decides each one.

- **(a) A1's loosening configs gain three shapes.**
  - **How the rules merge.** Permission rules are evaluated last-match-wins in key order. Config sources are deep-merged, and a merge keeps an existing key at its EARLIER position.
    - So a lower-precedence file can place a `"*"` rule after a higher-precedence file's explicit keys.
    - The merge also replaces a string with an object (or the reverse), and recurses only when both values are objects.
  - **The fixtures.** A1's fixtures carry each of these, both as the project `opencode.json` and as the global config:
    - `{"*": "allow"}`;
    - `{"bash": "allow", "*": "allow"}`;
    - `{"bash": {"*": "allow"}}`, a pattern map against the policy's string `"ask"`.
- **(b) Ground for FEATURE's mechanism. These are facts to choose by, not a design.**
  - **Precedence, lowest to highest:**
    1. the global config, under `XDG_CONFIG_HOME/opencode`;
    2. `OPENCODE_CONFIG`;
    3. the project files;
    4. the `.opencode` directories and `OPENCODE_CONFIG_DIR`;
    5. `OPENCODE_CONFIG_CONTENT`;
    6. org, managed and MDM configs;
    7. `OPENCODE_PERMISSION`.

    So `OPENCODE_CONFIG` alone does not hold against a project file.
  - **Disabling project config.** `OPENCODE_DISABLE_PROJECT_CONFIG=1` turns off project files AND the repository's own `.opencode` directories, which hold its agents, commands and plugins. That is a product trade-off, and the meet record names it if FEATURE takes it.
  - **The global config** can be pointed at a board-owned directory through `XDG_CONFIG_HOME`.
  - **Measured at 1.18.34: a higher-precedence `"*"` rule does NOT hold the policy.** This line replaces the derivation the amendment first stated.
    - **Instruments:** `opencode debug config` and `opencode debug agent` (the merged ruleset in order), the interactive form under the board's terminal, and `opencode run` without `--auto`.
    - **Why it fails.** Each key keeps the position of the first source that names it. A `"*"` rule from `OPENCODE_CONFIG_CONTENT` or `OPENCODE_PERMISSION` is appended after the explicit keys and overrides them:
      - with `"*": "ask"`, `external_directory`'s deny and `edit`'s allow become ask;
      - with `"*": "allow"`, every key opens.
    - **Where it holds:** only against loosening files that carry their own `"*"`.
    - **Consequence:** project config cannot stay on.
  - **Session-level rules** (`POST /session {permission}`) hold, but a TUI launch cannot set them.
- **(c) Where an approval is noticed.** At this version the plugin's `permission.ask` hook is declared but never triggered. A runtime ask publishes the `permission.asked` EVENT, which the plugin's `event` hook receives. Properties:
  - **The board learns of an ask from the `permission.asked` event.** P1's "the card moves to review" holds through that event.
  - **The board launches OpenCode's interactive form** (the TUI with `--prompt`), never `opencode run`. Without `--auto`, `run` rejects every ask automatically. P2's fixed argv already guarantees this; the launch test asserts it.
- **Falsifiers added (mutants that must be RED):**
  - any of (a)'s three shapes honoured, on the real binary;
  - an ask that does not move the card to review, on the real binary;
  - a launch argv that uses `run`.
