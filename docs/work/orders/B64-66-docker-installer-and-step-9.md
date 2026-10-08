# B64–66: DOCKER.md's installer paths and step 9's hooks

Header: [ARM-HEADER](ARM-HEADER.md). Dispatch: three arms at once, one per item (B64, B65, B66). Each arm is SINGLE: it writes its section of `docs/DOCKER.md` and the falsifier test for it.

Status: frozen 2026-10-08 (the Coordinator's draft r3; Verification's reads r1 → r2 → r3).
- r3 drops a literal e-mail address from the commit rule; the docs hygiene test refuses one in a tracked file.
- r2 takes in Verification's read, five additions:
  1. B66's "no event outside the profile" scans the union of all packaged profiles' events;
  2. B66's region keeps the `memory.search` block, "a search that answers" and the topic-scope sentence;
  3. a hint for B65's mounts, and its project name carries the prefix;
  4. B64's test defines what an offer of a published package is;
  5. the arms' checks name the tests that read DOCKER.md and what each requires in each region.

Base: main after B57's merge, plus this order's freeze merge.

**The Principal's decisions this order carries:**
- **Before rc.1** (2026-10-08, direct in the Coordinator's session, answering whether to fix or carry backlog 64, 65 and 66): "Fix as one doc slice".
- **Three arms at once** (2026-10-08, relayed by Verification): "fix all 3 - delegate all 3 to opus 5.5 agents simultaneously".

## Problem (measured, 2026-10-08)
- **64.** "Getting the installer" offers `pipx install ./kp_agent_tooling-<version>-py3-none-any.whl   # or the released kp-agent-tooling package`.
  - No `kp-agent-tooling` package exists on PyPI (404).
  - The release workflow publishes container images only, with no wheel.
  - The section gives no command that builds the wheel it installs.
- **65.** "From the product image, with no Python on the host" still opens with "Not yet verifiable from this tree: it needs the `product` image planned in AT-0004 T6a".
  - The `product` image is built and published by the release workflow, so the stated reason is stale.
  - No one has run the path: `plan` and `apply` inside the product image, then `prepare` on the host.
  - The paragraph also claims "The rendered root is identical to a host-side install because every path is identical". Nothing measures this.
- **66.** Step 9 says "The launch is bound to the desk, so the capture role ingests the turn under the desk", but not how the turn gets there.
  - **Verification's camera dry run (stub `claude`, no model call):** before any hook fired, workspace capture had filed the turn at topic scope only, and the desk-scope search answered nothing. Once Claude's `Stop` and `SessionEnd` payloads were sent, the spool was applied and the documented search answered.
  - **In the Principal's own off-camera turn,** the hooks fired, and the desk-scope search answered 4 results.

**Why a test file under `tests/install/` matters (B65).**
- A pull request that touches only `docs/**` runs neither `images.yml` nor the release workflow's rehearsals.
- The image tests that read DOCKER.md, for example T7a P2's `Docs.require`, then first run at the tag. A broken pattern would stop rc.1's publish.
- B65's image-marked test puts `tests/install/**` in the change, so this PR runs both rehearsals and `images.yml` against the edited document.
- The general gap is carried as backlog 67, not fixed here.

## Common to the three arms
- **Write scope.**
  - Each arm edits only its own region of `docs/DOCKER.md`, named below, and adds only its own new test file(s).
  - No product code, workflow, other document or existing test.
- **Before removing or rewording a sentence,** find every test that reads DOCKER.md (`git grep -l DOCKER.md -- tests`; 27 files at the base). Name them, and confirm that no test requires the text being removed. Report the statements each one requires inside the arm's region. In particular:
  - T7a P2's `Docs.require` statements: at the base, all of them lie outside the three regions.
  - D0a-2 F7 (`tests/docs/test_d0a2_readme_harnesses.py`): B66's region holds what it requires.
  - The capture-boundary test (`tests/docs/test_t7a_p5_capture_boundary.py`) reads every capture-titled paragraph of DOCKER.md. B64 and B65 add no capture-titled paragraph.
- **RED first:** each new test fails at base, with the command, exit code and summary line recorded.
- **Runs:**
  - `pytest tests/docs` at base and at head;
  - the default suite once, at head;
  - B65 also runs its image test (see B65).
- **No private references** in any committed file: no commit sha, PR number, CI run id, private org or host path (`/Users/`, `/Volumes/`, `/home/`). Docs hygiene (`pytest tests/docs`) must be green.
- **Commit:** one commit per arm, ending with the Claude attribution trailer (`Co-Authored-By`) that every merged slice carries. Do not push.

## B64: the installer section offers only what 0.4.0 has
**Region:**
- from the `### Getting the installer` heading up to, but not including, the paragraph that starts `**From the product image, with no Python on the host.**`;
- plus the Install prerequisites bullet `` `kp-agent-install`, from one of the install paths below. `` if the arm needs it.

**Properties:**
- **P1.** The section says that 0.4.0 publishes container images only, with no wheel and no package index release. It offers no package or wheel that is not published: no PyPI, no "released … package", no release-page wheel. It names the ways to get the installer:
  - a source checkout (`pip install -e packages/tooling`);
  - a wheel the reader builds from `packages/tooling`, with the exact command;
  - the product image, pointing to that paragraph without editing it.
- **P2. The wheel path is measured.**
  - At the arm's head, build the wheel with the documented command.
  - Install it as documented into a fresh virtual environment under the arm's TMPDIR.
  - Run `kp-agent-install plan --help`. Record the exit code.
  - The section's "Verified with…" sentence states what was run.
- **P3. The test** `tests/docs/test_b64_installer_paths.py` reads the section and fails when:
  - it offers a published installer package or wheel while the release workflow uploads no wheel (read from `.github/workflows/release.yml`); or
  - it names a source directory that does not exist in the tree.

  **What counts as an offer of a published package or wheel:**
  - an install line naming `kp-agent-tooling` or `kp_agent_tooling` without a local path (`./` or `packages/`);
  - the words "PyPI", "released … package" or "release page" beside "wheel" or "package".

  A local-path install, for example `pip install -e packages/tooling` or `pipx install ./kp_agent_tooling-<version>-py3-none-any.whl` after the documented build, is not an offer.

**Falsifiers (the meet's mutants):**
- Fa: `# or the released kp-agent-tooling package` re-inserted.
- Fb: a `pip install kp-agent-tooling` line added.
- Fc: `packages/tooling` renamed in the doc to a directory that does not exist.

Each makes P3's test RED.

## B65: the product-image installer path, run once and stated as measured
**Region:** from the paragraph that starts `**From the product image, with no Python on the host.**` up to, but not including, `### Plan and apply`.

**Properties:**
- **P1. Run the documented path once against a locally built `product` image,** each step through the host guard, recording each command, its summary and its exit code:
  1. `plan` in the container;
  2. `apply` in the container with `--expected-plan-sha256`;
  3. `kp-agent-install prepare` on the host;
  4. `kp-agent-install verify` on the host.
- **P2. The paragraph states what was measured,** replacing "Not yet verifiable … T6a".
  - If the command as written fails, make the smallest doc change that makes it work, for example a mount, `--home` or a transcript root, and state it.
    - **A hint, not a demand (Verification):** `plan` requires a root that does not exist yet, while a bind mount's source must exist (Docker Desktop would otherwise create it owned by root). The smallest change may be to mount the root's PARENT and let `plan`/`apply` create the root inside it, plus the transcript roots, which step 1 says must exist.
  - If no doc-only change makes it work, the paragraph says that the path is not supported in 0.4.0, with the measured reason. Report it under AMBIGUITY; the meet rules block or carry.
- **P3. The "identical root" sentence is measured.** For the same arguments, the container's `plan_sha256` equals the host-side `plan_sha256`. Otherwise the sentence is corrected to what holds.
- **P4. The image-marked test** `tests/install/test_b65_product_image_installer_image.py`, with a helper module of the arm's own if needed:
  - reads the documented command from DOCKER.md's block, substituting the variables, so the document cannot drift from what runs;
  - runs it with the image `AGENT_TOOLING_TEST_IMAGE` (the `product` image, as `images.yml` sets it);
  - asserts plan and apply exit 0, plan identity with the host-side plan, and the host's `verify` reports `verified`;
  - removes every volume and container it creates, with the `b65-` prefix. Its Compose project name itself starts with `b65-`, so the volume `prepare` creates (`<project>_memory`) carries the prefix.

  It runs with `--network none`, and its cost is measured and reported. It joins the tag run's `tests/install -m image`, so it must not be timing-dependent.

**Falsifiers (the meet's mutants):**
- Fa: the doc's command loses the runtime root's bind mount.
- Fb: the root mounted at a different target path.
- Fc: the stale "Not yet verifiable" sentence restored.

Each makes P4's test RED. Fc may be caught by a static check inside the same module.

## B66: step 9 names the hooks that bring the turn to the desk
**Region:** from `**9. Search.**` up to, but not including, `### Board tasks with in-container agents`.

The region keeps three things that other tests or step 9's own reading need:
- the `memory.search` command block;
- the words "a search that answers" in the prose after it (D0a-2 F7 requires both);
- the sentence that only a `"scope": "topic"` search reaches what workspace capture records.

**Properties:**
- **P1.** Step 9 says how the turn reaches the desk:
  - the Claude launch installs its hooks through `--settings`, for the events of its harness profile;
  - the turn is captured under the desk when its `Stop` hook fires at the end of the turn, and at exit by `SessionEnd`;
  - workspace capture alone files the turn at topic scope (already stated; kept);
  - a harness or wrapper that never fires the hooks leaves nothing at desk scope.
- **P2. The test** `tests/docs/test_b66_step9_hooks.py`:
  - derives the Claude profile's hook events from the packaged `harness-profiles.json`, through the installed package, not a copied list;
  - asserts that step 9 names `Stop` and `SessionEnd`;
  - asserts that step 9 names no hook event outside that profile. The universe it scans is the union of the hook events across ALL packaged profiles, so `UserPromptSubmit` (the Codex profile's) is detectable as outside Claude's.

**Falsifiers (the meet's mutants):**
- Fa: the doc names `UserPromptSubmit` (Codex's event, not Claude's).
- Fb: the `Stop` sentence removed.
- Fc: the profile read without `SessionEnd`, a copy with the event dropped and passed to the test's reader.

Each makes P2's test RED.

## The meet (the Coordinator)
- **Merge the three arm branches into one meet branch.**
  - A conflict at a region boundary is resolved by keeping both texts, and is reported.
  - Run `pytest tests/docs tests/ci`, the default suite, B65's image test, and T7a P2's documented-steps test, under the host guard.
  - Apply the mutation-RED for the named falsifiers only.
  - Run the private scan r4.
- **The PR's CI runs both rehearsals and `images.yml`** (B65's file is under `tests/install/`). The run of record is the arm64 rehearsal, with the full `tests/install -m image` against the edited document.

## Open (for the freeze)
- None.
- **DCO** (the Principal, 2026-10-08, direct in Verification's session: "confirmed, sign off goes in the public commit"): the DCO is a contributor's certification to the public repository. The private history is not published, and the first public commit is the Principal's, with his `Signed-off-by:` for the whole tree. The arms' commits carry no sign-off.
