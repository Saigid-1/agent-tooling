# O3: an optional image target that carries OpenCode

Status: frozen 2026-10-06 (the Coordinator's draft r2; Verification ruled it freezable with A1 and the Q3 wording, both in).

**Base:** main after T12c and the O1 freeze. **Header:** ARM-HEADER. **The stopping rule applies** (block or carry; a one-paragraph meet record; no commit sha, PR number or run id in a committed file). **Estimate:** small.
**Decided:** the Principal, 2026-10-06: "We can include the opencode image, but it should be an optional install in case the user doesn't need it", and "Optional image target (Recommended)".
**Order of work:** O3 comes FIRST. It carries the pinned real binary that O1's A1 (effective policy) and O2's tests run against.

## Problem (census O3, §7–8)
- **The `agents` target** (`deploy/Dockerfile:150-175`) is `FROM product-base`. It sets `ARG CLAUDE_CODE_VERSION`/`CODEX_VERSION`, runs `npm ci` of `deploy/image/agents/package.json`, checks each installed version, symlinks the CLIs into `/usr/local/bin`, sets `DISABLE_AUTOUPDATER=1` and labels the versions.
- **The installer selects no target;** `--image` is a digest. `--board-agents` changes only the board's mounts and profiles (`runtime_install.py:547-555`, `:588-589`, `:871-879`).
- **OpenCode appears in the repo only as** the `opencode` binary and `github.com/sst/opencode` (`apps/kanban/src/core/agent-catalog.ts:38-44`), plus the MIT `@opencode-ai/sdk` as a board dependency. The CLI's npm package name and licence are recorded nowhere.

## Properties
- **Q1: an optional target.** A new Dockerfile target (FEATURE names it, e.g. `opencode`) adds the OpenCode CLI:
  - the agents-target pattern: a pinned `ARG OPENCODE_VERSION`, `npm ci` of a committed package.json and lockfile, a version check, a symlink, no auto-update, and a version label;
  - FROM `product-base`, so it carries no claude or codex: OpenCode is MIT, unlike them, so it may be published.
- **Q2: never selected unless asked.** The default images (`runtime`, `product`, `ops`) and the default `plan` are unchanged; the existing image tests stay green.
  - **A1 (Verification), the pin that does not exist today:** the census found no default-plan golden. TEST adds one. A plan with the default components, and one with the live-like set (tooling, refresh, capture, board, indexer), render no `opencode` service or profile and the same `COMPOSE_PROFILES` as today. The same pin serves S1's "never selected unless asked".
  - The new image is used only when the operator passes its digest. DOCKER.md says how, beside the agents image.
  - If the release workflow (D0b) publishes it, it is a separate, named tag.
- **Q3: the licence recorded first.** Before the image is built, the CLI's npm package name and licence go into NOTICE and the public-readiness audit (one row).
  - The licence comes from BOTH the npm package's metadata at the pinned version AND the LICENSE file at the upstream repository's tag for that version. The @clinebot packages taught us npm metadata can be absent.
  - If either is missing, or they disagree, or the licence is not MIT or Apache-2.0: STOP. That is the Principal's decision, and the image is not built.
- **Q4: the pin is the binary O1 and O2 test against.** The pinned version is recorded where O1's A1 test and O2 read it (an image label, plus one constant the tests import).

## Falsifiers (mutants RED)
- the default plan, the live-like plan, or a default image carries `opencode` (A1's pin);
- the new image lacks `opencode` at the pinned version (the version check removed);
- the image carries `claude`, `codex` or the Claude Agent SDK (D0f's `agent_sdk_absence.py` and the agents-CLI image test, extended to `opencode` as an expected-present binary in this target only);
- NOTICE or the audit lacks the CLI's licence row, or names a licence the npm metadata and the upstream LICENSE do not both state.

## Write scope
- `deploy/Dockerfile` (the new target);
- `deploy/image/opencode/package.json` + lockfile;
- NOTICE;
- `docs/work/PUBLIC-READINESS-AUDIT.md` (one row);
- DOCKER.md ("Images");
- `tests/image/` (the new target's test);
- `.github/workflows/images.yml` (build and test the target).

## Arms
FEATURE and TEST, blind. The meet builds the target locally, one image at a time, under the host memory watcher. CI builds it on amd64.
