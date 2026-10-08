# D0a-2: the README, the harnesses page and the release stance a stranger reads first

Header: [ARM-HEADER](ARM-HEADER.md). Dispatch: FEATURE + TEST.

Status: frozen 2026-10-07 (the Coordinator's draft r3; Verification's reads r1 → r3, ruled freeze-ready at r3).
- r3 takes in Verification's freeze-read preparation at D0b's head:
  - F1's patterns name the symbol each tree fact is read from; host capture is read from the capture code (Verification's nit at r3);
  - P4 and F7 place the search in the first run's OWN steps, before the "Board tasks with in-container agents" subsection, whose prose already mentions `memory.search`, so a section-wide grep would be GREEN at base.
- r2 took in Verification's first read, notes 1–6:
  - the charter is one copy fewer;
  - P5 adds the T10 goldens;
  - P1 states the R7 fact;
  - F8 (version numbers equal the tree's pins);
  - F1's list adds the summarizer and OpenCode host capture;
  - P4 is scoped to the first run's end, with a guard on D0b's pull text.

Base: main after D0b. DOCKER.md's first run (the pull by digest, the optional `opencode` pull) and `docs/RELEASE-STANCE.md` are D0b's, and this order links them, never restates them.

**The Principal's decisions this order carries:**
- **K1, Option 1** (2026-10-06, relayed by Verification): 0.4.0 ships with the in-board Cline agent's launch gated; desk tasks launch through the registry's host harness profiles. HARNESSES.md states it.
- **OpenCode in 0.4.0** (2026-10-06): "We can include the opencode image, but it should be an optional install in case the user doesn't need it." O1, O2 and O3 are merged, and D0b publishes the `opencode` target as an optional tag.
- **The release stance** (2026-10-07, relayed by Verification): "Let's include those lines in the readme and the protect description/charter so it exists from day 0 in the release notes." D0b put the three claims in `docs/RELEASE-STANCE.md` and the release notes; this order puts them in the README and the project's charter.

## Problem (measured at main after D0b's meet head, 2026-10-07)
**`README.md` (37 lines) is stale.** It says:
- the repository is private and has no public licence grant, although LICENSE is AGPL-3.0-only (D0a-1);
- "That Dockerfile is not in this tree yet", although `deploy/Dockerfile` has six targets;
- the OPS retirement is pending.

It names no published image, no harness and no release stance.

**`docs/HARNESSES.md` (28 lines) covers only the Claude Agent SDK's optional install.** It has no Claude Code or Codex host harness, no OpenCode, no OpenRouter, and no local model.

**What is Built, measured from the tree:**
- **Claude Code and Codex:** host harness profiles (T3) and the host adapter (T4). The user installs each CLI on their own host. Neither is in any published image (the D0a-1 audit, D0f, D0b R7).
- **OpenCode:** the optional `opencode` image (O3, pinned 1.18.34, upstream MIT LICENSE shipped). It runs as a board-launched desk task under an approval policy (O1), with a per-launch binding, capture through `opencode export` and turn-end hooks (O2).
  - A host launch of `opencode` is refused (O2).
  - The board names the operator's OpenRouter key FILE to OpenCode by reference (`KANBAN_OPENCODE_OPENROUTER_KEY_FILE`); the key is never written.
  - Measured at O2: an UNBOUND OpenCode launch (no desk) still runs OpenCode's own background npm install of `@opencode-ai/plugin`, as before O2. Bound launches install nothing. Backlog 50; Verification ruled that it must be NAMED where a stranger reads.
- **The model gateway** (M1, `kp-agent-models`): provider kinds `openrouter` and `openai-compatible`. A local open-weight model behind an OpenAI-compatible endpoint (Ollama or similar) is routable as a gateway route for the capability tools and the optional summarizer. It is NOT a desk-task harness in 0.4.0.
- **The in-board Cline agent:** launch gated in 0.4.0 (K1, Option 1). The Claude Agent SDK install is optional (D0f; HARNESSES.md today).

## Properties
- **P1. The README is rewritten to the final tree.** It covers:
  - what the suite is;
  - its components;
  - the published image targets (`runtime`, `product`, `ops`, and the optional `opencode`) with where to pull them (the release page);
  - first run, as a LINK to DOCKER.md, never restating a `kp-agent-install` or `docker compose` command;
  - harnesses, as a link to HARNESSES.md;
  - the licence (AGPL-3.0-only; Apache-2.0 for `apps/kanban`; NOTICE for third parties);
  - how to contribute (CONTRIBUTING.md, DCO).

  It names no private org, repository, desk, session or commit.
  - **The R7 fact, as a README sentence** (Verification): "No published image contains Claude Code, Codex or the Claude Agent SDK; Claude Code and Codex are installed by the user on their own host." The release page says it too. The README is where a stranger decides whether to pull.
- **P2. The release stance, two copies, one source.**
  - The README carries a "Release stance" section equal to `docs/RELEASE-STANCE.md`'s three claims. Equal means the same text after normalising Markdown emphasis and line breaks; a test holds it (the two-copies lesson, backlog 44).
  - **The project's charter is the README's opening plus that section,** one copy fewer (Verification). There is no separate `docs/CHARTER.md`: it would be a third copy with no reader.
  - **The GitHub "About" sentence is the README's first sentence.** It is named on the cutover checklist and nowhere else, and set by the Principal at the cutover.
- **P3. HARNESSES.md, "Harnesses and model providers".** One entry each for:
  - Claude Code;
  - Codex;
  - OpenCode (with OpenRouter);
  - the model gateway (OpenRouter, and an OpenAI-compatible local model);
  - the in-board Cline agent and the optional Claude Agent SDK (today's section, kept).

  **Each entry gives:**
  - how the user installs it themselves (or that it ships in the optional image), and whether any published image contains it, with the licence reason where it does not;
  - how this tooling binds to it;
  - its status, **Built or not, never "supported" without Built**.

  **It states, in its own words:**
  - OpenCode is board-launched only, and a host launch is refused;
  - OpenCode needs the optional `opencode` image and the operator's OpenRouter key file;
  - **an unbound OpenCode launch reaches the npm registry** for OpenCode's own plugin install; bound launches do not (backlog 50, Verification's ruling);
  - a local model is a gateway route, not a desk-task harness, in 0.4.0;
  - the in-board agent's launch is gated in 0.4.0.
- **P4. The first run ends at a search that answers** (Rehearsal 1, F3). DOCKER.md's first run gains one documented `memory.search` call as the bound session: a command block the stranger runs, followed by text that calls its result "a search that answers", plus one sentence: a captured session must be assigned to a desk before its turns are searchable.
  - **Where:** at the end of the first run's OWN steps, after the host-launch paragraph and before the "Board tasks with in-container agents (`agents` image)" subsection. That subsection sits inside the "First run from an empty root" section, and its prose already says the session's MCP server answers `memory.search` (measured at D0b's head). That prose is not a search call and is not the first run's end.
  - P4 edits ONLY that step: the search call and its sentences. D0b's step 0 text (the pull by digest before `plan`, and the optional `opencode` pull) is untouched, as is the board-tasks subsection; D0b owns the rest of the file.
- **P5. The contributor page states what cannot be regenerated publicly.** CONTRIBUTING.md names both frozen fixtures, each in the same sentence shape (what it is, why it cannot be regenerated from the public history, what its test compares):
  - the T1 pre-move capture (`tests/boundary/fixtures/pre_move_tools_list.json`): its test compares tool names and input schemas only (the D0b meet note);
  - the T10 goldens (`tests/fixtures/t10-read-path-projection/`): `generate.py`'s docstring says so at D0b's head (Verification). Their test compares the read path's results against the golden and records the generator's and the base product's content hashes.

## Falsifiers (each with a mutant that must be RED)
- **F1.** A README or HARNESSES sentence names a harness, provider or slice as supported or available when the tree does not have it Built.
  - **The closed list of claim patterns,** each tied to the tree fact that makes it true or false. The test derives each fact from the named symbol, never from a copied value:
    - a local model runs desk tasks (false: the gateway's provider kinds are `openrouter` (`model_gateway.py`) and `openai-compatible` (`model_gateway_providers.py`'s provider table), and no desk-task harness profile names either);
    - OpenCode runs on the host (false: `test_a_host_launch_of_opencode_is_refused_before_anything_is_written` in `tests/launch/test_o2_r2_opencode_prepare.py`, prepare with `source=host` refused);
    - OpenCode captures host sessions (false, read from the capture code, not the launch test: `harness_profiles.py`'s `opencode` profile captures in mode `export` with parser `opencode-export` (`EXPORT_PARSERS`); `opencode_export_capture.py` reads only a BOUND session that is a root session of the launch workspace; and the host sweep, `workspace_capture.py`, names no export mode and no OpenCode. Backlog 31 is the carry);
    - the summarizer runs by default (false: the packaged `compose.yaml` asset's summarizer service carries `profiles: [summarizer]`, opt-in, S1a);
    - any published image contains Claude Code, Codex or the Claude Agent SDK (false: D0b's R7 step and D0f's install action);
    - the in-board agent launches tasks in 0.4.0 (false: K1, Option 1).
  - Mutant: insert any one of these as an affirmative sentence.
- **F2.** The README's stance section differs from `docs/RELEASE-STANCE.md` (normalised).
  - Mutant: reword or drop a claim in one copy only.
- **F3.** A README first-run section restates a `kp-agent-install` or `docker compose` command.
- **F4.** Any link, image reference or label points at a non-public owner. Covered by D0b's public-home test over `git ls-files`; this order adds no second copy of it.
- **F5.** A HARNESSES entry lacks an install pointer or a status.
- **F6.** HARNESSES.md does not name the unbound OpenCode launch's registry egress.
- **F7.** DOCKER.md's first run has no documented `memory.search` CALL (a command block the stranger runs) whose result the text calls "a search that answers", within the first run's own steps: from the "First run from an empty root" heading to the "Board tasks with in-container agents" subsection heading.
  - **RED at base,** by design: the section's only `memory.search` today is prose inside the board-tasks subsection, outside that range and not a command block. A section-wide grep would be GREEN at base and is not this falsifier.
  - Mutant: move the search step into the board-tasks subsection. RED.
  - Its guard, GREEN at base: D0b's pull-by-digest text before `plan` is still there.
- **F8. Versions are a two-copies risk** (Verification). Every version number in README.md or HARNESSES.md equals the tree's pin:
  - OpenCode: from `deploy/image/opencode`'s lock, or the Dockerfile's `OPENCODE_VERSION` ARG;
  - Claude Code and Codex: from `deploy/image/agents`' lock.
  - Mutant: bump the doc's number. Without this test, the first pin bump would leave a stale doc with no red.

## Write scope
- **FEATURE:**
  - `README.md`;
  - `docs/HARNESSES.md`;
  - `CONTRIBUTING.md` (P5 only);
  - `docs/DOCKER.md` (P4 only: the first run's search step and its sentence).
- **TEST:** new tests under `tests/docs` for F1–F3 and F5–F8. The F1 test reads a closed list of claim patterns, and each pattern names the tree fact that makes it true or false.
- **Not in scope:**
  - `docs/RELEASE-STANCE.md` (D0b's; the README copies it);
  - the release workflow;
  - the GitHub "About" sentence (the Principal's);
  - D0c's timed run.

## Open (for the freeze)
- None. The charter question is settled by note 1.
