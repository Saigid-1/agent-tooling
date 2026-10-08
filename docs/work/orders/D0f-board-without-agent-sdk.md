# D0f — The board without bundled claude-agent-sdk: an external SDK behind a user-initiated install action

ADR: [AT-0004](../../adr/AT-0004-portable-kanban-suite.md). Header: [ARM-HEADER](ARM-HEADER.md). Dispatch: FEATURE + TEST.

Status: frozen 2026-10-04 (DEMO-0 draft r5, Coordinator). Verification ruled it freezable with its additions (2026-10-04). Base: main at the merge of D0a-1's pull request (D0a-1 merged). It ships before D0b, because both `product` and `ops` carry the board bundle.

**The Principal's decision** (2026-10-04, direct in the Coordinator's session, verbatim): "cline is apache - https://github.com/cline/kanban/blob/main/LICENSE - users must obtain the sdk themselves, but we can bundle the clickable install script to pull it in and provide notice that we do not warrant or license their usage of the package, etc."

**Problem (measured at the D0a-1 meet by the Coordinator and Verification).**
- In the built product image and the live ops image, the board bundle `/app/dist/cli.js` (16.6 MB) contains `@anthropic-ai/claude-agent-sdk`: `cli.js.map` lists `../node_modules/@anthropic-ai/claude-agent-sdk/sdk.mjs`. It is reached through `ai-sdk-provider-claude-code` and `@clinebot/llms`.
- `claude-agent-sdk` 0.2.128 declares `SEE LICENSE IN README.md`, which is Anthropic's terms. Publishing the image would redistribute it.

## Properties
- **F1. External, never bundled.**
  - The Kanban build marks `@anthropic-ai/claude-agent-sdk` external, along with `ai-sdk-provider-claude-code` if it carries the SDK. No published-target image contains its code or sources: not in `cli.js`, not in `cli.js.map`, and not in `node_modules`.
  - `@clinebot/*` stays bundled. That is the Principal's reading, and NOTICE records the measurement beside it (D0a-3).
- **F2. The board works without the SDK.**
  - The board starts and serves, and every non-Claude provider and every board function works, with the SDK absent.
  - The Claude provider shows as "not installed", with the install action offered. It does not crash or block anything else.
- **F3. The install action is clickable and user-initiated.**
  - It is a board action (FEATURE names it), or a documented command the board offers.
  - On the user's action, it installs the SDK, and the provider package if external, at the user's choice of version: by default, the version the lockfile pins.
  - **The notice is shown AND acknowledged before anything is fetched:** we neither warrant nor license the user's use of the package; the user obtains it under Anthropic's terms. Its text is pinned in the repo.
  - No install happens without that acknowledged action, whether at start, at upgrade, or from any automatic path.
- **F4. The runtime constraints (Verification).**
  - Every role image is read-only with capabilities dropped, so the SDK installs under `/state`, the one writable mount (FEATURE names the path), and the board resolves it from there.
  - The board role needs outbound network for the action; `capture` has none and never runs it.
  - The action does not restart the board: T7a's stays-up contract, no RestartCount change.
  - The installed SDK survives a board restart, because it lives on `/state`.
- **F5. The provider reaches the SDK (Verification).** After the install action, the Claude provider is registered, and it reaches the SDK's entry point. A test-credential-free probe proves the import and the provider's construction. The live model call is proved by the Principal's own dry run, never by an arm.

## Falsifiers
- A published-target image (`runtime`, `product`, `ops`) containing claude-agent-sdk code or sources, in any file. This is a per-image file-system check, extended into bundle contents and source maps: the `sdk.mjs` source path, or a distinctive SDK symbol FEATURE names.
- The board not starting or serving, or any non-Claude function failing, when the SDK is absent.
- The Claude provider not registered, or not reaching the SDK entry point, after the action.
- An install with no user action, without the notice shown and acknowledged, or writing outside `/state`.
- The board restarting because of the action.
- `capture` attempting the install, or needing network.

## Write scope
- **FEATURE:**
  - `apps/kanban`: the build configuration (externals), the Claude provider's loading path, the install action and its notice;
  - the packaged `compose.yaml`, only if the board role needs a network or mount setting it lacks (named);
  - `docs/HARNESSES.md`, the Claude entry's install pointer (D0a-2 owns the rest of the page);
  - NOTICE's line saying claude-agent-sdk is not redistributed, in coordination with D0a-3. The meet reconciles it if both touch it.
- **TEST:**
  - new tests: Kanban vitest for F2, F3 and F5, and image-marked tests for F1 and F4;
  - the per-image file-system falsifier as a reusable check that D0b's publish job also runs.

## Amendment 1 (meet rulings, Coordinator with Verification, 2026-10-05)
- **The seams, as built.** TEST wrote blind; the meet reconciled its assumed names with FEATURE's in one place each (`apps/kanban/test/integration/d0f/d0f-seams.ts`, `tests/install/d0f_harness.py`), without weakening any assertion.
  - **The notice:** `CLAUDE_AGENT_SDK_NOTICE_TEXT` in `apps/kanban/src/optional-providers/claude-agent-sdk-notice.ts`, SHA-256 `34cfeae3…`.
  - **Status:** `runtime.getClaudeAgentSdkStatus`. `installed` is read strictly from `state`, so `installing` and `unavailable` are neither true nor false.
  - **Install:** `runtime.installClaudeAgentSdk({acknowledgedNoticeSha256, sdkVersion})`. The acknowledgement IS the digest of the shown notice.
  - **Install path:** `<runtime home>/optional-packages/claude-agent-sdk`, which is `/state/kanban/kanban/optional-packages/claude-agent-sdk` in the image.
  - **The package list:** `claude-agent-sdk-pin.json`, key `install`.
- **F3c (Coordinator ruling, Verification concurred).** "Installs at the user's choice of version", with F4's /state, means exactly that version on disk after a successful install.
  - **The defect:** a superseded install (~283 MB) stayed until the next start.
  - **The fix:** one function, `removeSupersededClaudeAgentSdkInstalls(root, current)`, called at start and after a successful install. It removes every non-current entry of `installs/`, staging leftovers included, and never follows a symlinked `installs/` or entry.
  - **Tests:**
    - the running board reports the chosen version loaded with no restart;
    - a failed install (npm exit 1, or an integrity mismatch) removes nothing but its own staging directory, and the previous version stays usable.
- **The board package cannot be published (Verification's finding at the meet).**
  - **The finding:** `apps/kanban/package.json` lacked `"private": true` and kept upstream's `publishConfig` (access public, provenance). Its `dependencies` reach the SDK through `@clinebot/llms` → `ai-sdk-provider-claude-code`, so `npm publish` would have redistributed it.
  - **The fix:** it is now private, with no `publishConfig`. `tests/docs/test_kanban_packages_unpublishable.py` holds every tracked apps/kanban package.json to that, red-first. The audit carries the row (3P-TERMS, "Replace (D0f)").
- **Readings (Verification ruled; no change needed):**
  - **The build-stage fetch is use, not redistribution.**
    - The SDK 0.2.128 and the adapter 3.4.4 are production entries in `apps/kanban/package-lock.json`, reached through `@clinebot/llms` 0.0.38. The eight platform packages are optional.
    - The image's `kanban-build` stage runs a full `npm ci` (esbuild needs it), so the SDK is fetched into that throwaway stage on the builder. The final stages copy only `package.json`, `node_modules/node-pty` and `dist`.
    - No restructure. An `overrides` stub would break the install action's lockfile-integrity check, and moving `@clinebot/llms` to devDependencies changes nothing fetched.
    - **The gates:** the build's bundle scan (`scripts/verify-no-claude-agent-sdk-build.mjs`, the end of `npm run build`), and the F1 checker (`tests/image/agent_sdk_absence.py`) on every pushed digest (D0b R7). Both are red-first tested.
  - **The tests use Anthropic's real SDK at its pin** (accepted).
    - FEATURE verifies each pinned package against the lockfile's integrity, so the test registries (vitest and the F4 image) serve SDK 0.2.128 and adapter 3.4.4 as published, byte for byte, from npm's cache. The SDK is installed into scratch /state and imported by the test board.
    - There is no model call (a closed `ANTHROPIC_BASE_URL` and proxies; an internal network in F4) and no platform binary. "Never serve Anthropic packages" narrows to "never the platform packages", the ones the licence question was about.
    - **Dependency class (a flake to recognise, not a block):** these tests depend on npm serving those two packages at those versions. A registry outage or an unpublish turns them red for a reason outside the tree. Use in tests is use; NOTICE's statement stands.
    - **Its first measured instance (the `images` job's first run after Amendment 1).** The `images` job's four F4 image tests errored at setup: npm's cache on the runner lacked the published tarballs the action fetches at the lockfile's versions. The runner never installs the board on the host, only inside Docker, so its cache was empty.
      - **The fix (images.yml):** `actions/setup-node` (Node 22), then `npm --prefix apps/kanban ci --ignore-scripts`, before the image steps. It fetches the SDK into the runner's npm cache, which is use, within this class.
      - **Who inherits it:** D0b's release workflow, its R6 rehearsal and the public repo's CI. D0b's order names the step.
  - **NOTICE.** D0a-3's section-1 statement and FEATURE's image-section line ("not redistributed in any published image… installs them only at the user's acknowledged request") agree, and both stay.
- **K1, noted for the Principal.** The fork disables native Cline launch (`disableLaunch: true`), so the `claude-code` provider is unreachable in the production board even after the install. FEATURE therefore also placed the install section in Settings → General.
  - D0f's purpose (no SDK in any published image) is unaffected.
  - Enabling the provider is a product decision. Verification put it to the Principal, and it bears on what D0e's second video can show.
