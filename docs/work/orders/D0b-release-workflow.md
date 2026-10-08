# D0b — The release workflow: four published targets (`opencode` optional), both architectures, the public home

ADR: [AT-0004](../../adr/AT-0004-portable-kanban-suite.md); the release decisions are in the Principal's release document (AT-0005/AT-0006). Header: [ARM-HEADER](ARM-HEADER.md). Dispatch: FEATURE + TEST.

Status: frozen 2026-10-07 (the Coordinator's draft r5; Verification's reads r1 → r5, ruled freezable at r5).
- r2 added Verification's first read: the org-name miss (R4a) and the arm64 reference rule (R5).
- r3 added Verification's second read, F1–F6 and four holds, and the arm-runner probe.
- r4 re-bases on main after O2 (freeze condition (c)):
  - R2 publishes O3's `opencode` target as a fourth, optional target (the Principal's O1–O3 decision);
  - R4 gives it its own tag and a licences label naming both licences;
  - R7 scans it;
  - the 12 private-org sites are re-measured at the new base;
  - the F2 list is re-measured, with the OpenCode-variable tests taking the pushed `opencode` digest;
  - every private commit, PR and run reference is replaced by the step it names.
- r5 takes in Verification's read of r4:
  - R4a's scrub names the six PRIVATE-REF sites still in the tree and the one decided exception (census file names);
  - the scan's exception is narrowed to those names;
  - the record pass lists every non-counted class;
  - the gate reads four VIOLATIONS lines plus the PRIVATE-REF detail row;
  - the write scope's lines are restated at main after O2.
- **Verification read the Principal's 2026-10-06 words as deciding `opencode`'s publication** as an optional, separately tagged target.

Base: main after O2. That is D0a-3, T12b, D0f, T12c, O3, O1, S1a and O2, all merged. D0f shipped first because `product`, `ops` and `opencode` carry the board bundle. K1's arms run beside D0b's from the same base (the Principal's word, 2026-10-07); the write scopes are disjoint.

This order carries D0a-1's H8 (condition 3) and the audit's two decided rows: no image containing Claude Code or Codex is published, and the claude-agent-sdk is never bundled.

**The Principal's decisions** (2026-10-04, direct in the Coordinator's session, verbatim):
- "the ghcr package will be public, but on another account."
- "v0.4.0 it is, fresh public history makes more sense."
- "publish without claude code ... we don't need to publish claude/codex images at all as we cannot provide nor authoritatively ensure license compliance in doing so".
- The release candidate is hosted on the public repo: "agreed with A, the public repo."
- The public repo is `https://github.com/Saigid-1/agent-tooling` (relayed verbatim by Verification).
- He approved refreshing the private scan with the org-name class, and the arm-runner probe ("Yes, refresh it", "Yes, open the probe", AskUserQuestion).
- **OpenCode in 0.4.0** (2026-10-06, verbatim): "Let's include it since it's minimal effort at this point. We can include the opencode image, but it should be an optional install in case the user doesn't need it."
  - O3 built the `opencode` target and left publication to this order: "If the release workflow (D0b) publishes it, it is a separate, named tag."
  - O3's licence decision ("Use upstream MIT (Recommended)") ships the upstream MIT LICENSE beside the binary.

**Problem (measured at main after D0a-3, 2026-10-04; the private-org sites re-measured at main after O2, 2026-10-07).**
- `.github/workflows/images.yml` builds and tests on amd64 only, and never pushes. Its `pull_request.paths` (`images.yml:13–20`) do not include `.github/workflows/**`. The repo has 0 releases, and there is no agent-tooling ghcr package.
- Every Dockerfile target's `org.opencontainers.image.source` is the private org repo's URL. It is hard-coded in one `LABEL` per target: 6 at this base. O3's `opencode` target added the sixth (Verification's scan at the O3 meet: ORG 11 → 12). No target carries `org.opencontainers.image.licenses`. Line numbers move with every new target, so this order names the sites by pattern, never by a list.
- **12 lines in 5 tracked files name the private org** (re-measured at main after O2; file and line only):
  - those 6 labels;
  - `docs/DOCKER.md:92`, `:93` and `:113`, the image references in the first-run examples;
  - `tests/install/s3_harness.py:47` and `tests/install/test_p3_refusals.py:47`, registry-shaped fixtures;
  - `docs/work/orders/ARM-HEADER.md:13`.

  Verification measured 11 independently at D0a-3, and 12 at the O3 meet.
  - **This is a D0a-1 audit coverage miss** (Verification, 2026-10-04). The slug `<org>/agent-tooling` is the private repository's name: class REPO, whose default is scrub. The three `ghcr.io` lines are also PRIVATE-REF, a private package location.
  - No audit row lists any of these lines. The private scan's name list was built from the audit's rows, so the scan reported 0. D0a-3 applied the audit as ordered, and D0a-3 is not reopened.
- **The arm64 size reference.** One arm64 reference already exists: `tests/image/image_harness.py:48`, `DEFAULT_REFERENCE_BYTES = 2_330_000_000`, measured on arm64 with the containerd store (`images.yml:64`), on a Mac. What is missing is a reference for the **arm runner's** store.
  - `images.yml`'s size step has only `x86_64/overlay2` and `x86_64/overlayfs`. Any other key fails with "no measured size reference".
- **The arm runner (a probe PR, 2026-10-05T01:29Z, closed unmerged, branch deleted).** `ubuntu-24.04-arm` IS available in the private org repo. The probe's run printed:
  - `uname -m`: `aarch64`;
  - Docker 28.0.4, linux/arm64;
  - the size step's key `docker info --format '{{.Architecture}}/{{.Driver}}'`: **`aarch64/overlay2`** (backing filesystem extfs).
- The recording Mac is arm64.

## Properties
- **R1. Trigger and the push guard.**
  - **Triggers:**
    - a tag `v*` pushed;
    - `pull_request`, when the release workflow's own file or what goes into an image changes (its `paths` include the workflow file itself; F6);
    - `workflow_dispatch`, which declares no input that reaches any publish condition.
  - **Every publishing job,** meaning anything that logs in, pushes, tags, creates a manifest list or attestation, or creates a release, carries its guard at **job level**:
    - `github.event_name == 'push'`;
    - `startsWith(github.ref, 'refs/tags/v')`;
    - `github.repository_id == <the public repo's numeric id>`.
  - **The repository id is numeric,** so it does not depend on case and survives a rename. The Principal reads it once (`gh api repos/Saigid-1/agent-tooling --jq .id`), and FEATURE writes it in the workflow as a literal. A slug comparison is not used (F3).
  - Every publishing job `needs` every build and test job, transitively, so a test failure blocks the publish. No job the publish depends on has `continue-on-error`.
  - On `pull_request` and `workflow_dispatch`, and in the private org repo under any event, the build and test jobs run and no publishing job runs. That is how this order is verified before any tag exists.
  - A tag containing `-rc.` publishes a release marked **pre-release**. `v0.4.0` publishes a full release.
- **R2. Targets: `runtime`, `product`, `ops` and `opencode` only.** `agents` and `acceptance` are never pushed, under any tag or event. They stay in-tree local-build targets.
  - **`opencode` is the optional install** (the Principal's 2026-10-06 decision). It is O3's target: FROM `product-base`, it adds the pinned OpenCode CLI and its upstream MIT LICENSE, and carries no claude, codex or agent SDK.
  - It is published as its own named tag (R4). It is never selected by `plan` unless the operator asks for it, and DOCKER.md presents it as optional.
  - The three default targets are unchanged.
- **R3. Both architectures, native, one path each.** amd64 builds on `ubuntu-latest`, and arm64 on `ubuntu-24.04-arm` (measured available, above).
  - The frozen workflow has exactly one arm64 path, the native runner. A runner label is static, so there is no QEMU branch at run time (F4).
  - `docker buildx imagetools create` merges each target's two per-arch digests into one manifest list.
- **R4. Tags and labels.**
  - **Registry:** `ghcr.io/saigid-1/agent-tooling`. ghcr requires a lowercase owner.
  - **The tag scheme (proposed here, for Verification's review before the freeze):** `ghcr.io/saigid-1/agent-tooling:<target>-<version>`.
    - `<version>` is the git tag without its leading `v`, e.g. `product-0.4.0-rc.1` and `product-0.4.0`. The optional target is tagged the same way, as `opencode-0.4.0-rc.1` and `opencode-0.4.0`.
    - The target comes first, so a pre-release suffix never reads as a target.
    - There is no `latest` and no floating tag. The per-arch digests are never tagged; only the manifest lists are.
    - DOCKER.md and D0c pull by digest.
  - **Every published target carries:**
    - `org.opencontainers.image.revision` = the commit the tag points at in the public repo;
    - `org.opencontainers.image.source` = `https://github.com/Saigid-1/agent-tooling`;
    - `org.opencontainers.image.licenses` = `AGPL-3.0-only` for `runtime`, `product` and `ops`, and `AGPL-3.0-only AND MIT` for `opencode`. MIT is the bundled OpenCode CLI's licence (O3's decision; its LICENSE is at `/usr/local/share/doc/opencode/LICENSE`).
  - **The source URL becomes one build argument,** with the public URL as its default, so no target hard-codes a repository. Every target's `LABEL` reads it, whatever targets exist at D0b's base, including `opencode`. The private-org lines (12 at this base) are repointed to the public home:
    - the labels, to the public GitHub URL;
    - the three DOCKER.md lines and the two test fixtures, to `ghcr.io/saigid-1/agent-tooling`;
    - ARM-HEADER's line, to the public repo.
  - **The repointed fixtures** (`s3_harness.py:47`, `test_p3_refusals.py:47` at main after O2) still never reach the network. T9b `image_absent` is unchanged, and the installer never pulls (Verification hold).
  - **The build-identity file** (`/usr/local/share/agent-tooling-build-identity.json`) carries the same revision as the label (T7a P4 unchanged).
  - **SBOM and provenance attestations,** if `docker/build-push-action` supports them on both runners. FEATURE states which.
- **R4a. The audit and the private scan close the miss (Verification, 2026-10-04).** Both are required before any public push.
  - **The audit.** `docs/work/PUBLIC-READINESS-AUDIT.md` gains one row per private-org line at D0b's base (12 at main after O2): class REPO, and the three `ghcr.io` lines also PRIVATE-REF. The `LABEL` lines may share one row naming the pattern, "every target's `org.opencontainers.image.source` in deploy/Dockerfile". Each decision reads "Replace (D0b)". As before, the rows cite file:line and class only, never the value. This is FEATURE's.
  - **The private scan: DONE** (Coordinator, 2026-10-04, with the Principal's go).
    - `org_names.txt` was added to the record, and `scan.py` gained a class "ORG private owner (any file)". It counts in every file, whatever its audit row, because the public tree must never name the private org.
    - Its total is a third line, `VIOLATIONS (private org, any file): N`.
    - At main after D0a-3 it found exactly the 11 lines then present (11 findings, 11 lines, 5 files); at main after O2, the 12 above (Verification's scan: 0/0/12/0). The other VIOLATIONS lines stay 0.
    - The record is refreshed with new `SHA256SUMS`; the D0a-3 verdict's record is kept unchanged beside it. Both stay private: they are named by their hashes in the private record, never in the public tree.
  - **The gate.** Verification re-runs the refreshed scan from the record: 12 at D0b's base, and 0 on D0b's head. All four VIOLATIONS lines at 0 (the fourth is added below) is the cutover gate before `v0.4.0-rc.1`.
  - **PRIVATE-REF in every file (Verification, 2026-10-06, found by reading the scan's `--detail`).**
    - **The hole.** The scan COUNTS the PRIVATE-REF class only in files with an audit row. At main after the first scrub it reports, but does not count:
      - 26 private commit shas in 13 files added since the audit: the T12b tests' docstrings ("RED at <sha>"), `tests/fixtures/t12b/manifest.json`'s `generated_at_revision`, `tests/image/agent_sdk_absence.py`, and the D0f harness and seams;
      - 6 in add-keep-row files (old T10 and boundary fixtures that cite a commit).

      In the public history they dangle exactly like the 55 the first scrub removed.
    - **The record.** `scan.py` gains PRIVATE-REF counted in every file, as the org class already is, on a fourth line: `VIOLATIONS (private ref, any file): N`. The record is refreshed (new `SHA256SUMS`), and the current one is kept unchanged beside it.
      - **The exception is narrowed (r5, Verification).** Today the fourth line excepts "census file names and recorded keep decisions". It excepts ONLY the census file names. A class default in a file added after the audit is not a recorded decision.
      - **The same pass lists every non-counted class with findings at D0b's head,** each with the audit rule that keeps it, so the gate's zeros are read beside what they do not count. At main after O2 (Verification's measurement):
        - SESSION native UUID shape, non-row: 4;
        - DESK instance AAA, add-keep-row: 5;
        - DESK instance ops desk/repo context: add-keep-row 55 (the test ledger, four test files) and keep-row 8 (README 5, Dockerfile 1, an inventory 2);
        - HASH binding derived: 22.
    - **R4a is not yet true of the tree (r5, Verification's measurement at main after O2).**
      - **The six PRIVATE-REF sites.** The detail row "PRIVATE-REF commit of this repository" reads add-keep-row 6, census file name 8, everything else 0. The fourth line reads 0 only because of the exception above. The six are real commits of this repository, in files with no audit row:
        - `tests/boundary/fixtures/pre_move_tools_list.json` (`merge_base`, `merge_base_tree`);
        - `tests/boundary/t1_contract.py` (`ORDER_MERGE_BASE`);
        - `tests/fixtures/t10-read-path-projection/generate.py` (`BASE` and its docstring);
        - `goldens.json` (`meta.base_sha`);
        - `tests/test_t10_p5_identical_results.py`, which asserts that sha.
      - **The eight census-file-name sites** are references to `T11a-census-<sha>.md` and `T12-census-<sha>.md`, whose names carry a short private commit. They are kept by the audit row the Principal accepted at D0a-3 ("The file name keeps its census label"). That is the ONE decided exception to the rule below, named here so a reader of the rule does not take it for an oversight.
    - **The scrub at D0b.** Remove every PRIVATE-REF site outside that exception, the six above included:
      - test docstrings name the step, never the commit;
      - a fixture or golden that records a base (the boundary fixture, the T10 generator and its goldens) records the generator's own content hash instead of a commit, or drops the field. The two tests that read it (`t1_contract.py`, `test_t10_p5_identical_results.py`) are adapted to read that instead, and keep their property.
    - **The gate at D0b's head:**
      - all four VIOLATIONS lines read 0;
      - the "PRIVATE-REF commit of this repository" detail row reads 0 in every column except "census file name".
    - **The rule from here:** no commit sha, PR number or CI run id of this repository in any committed doc, test, comment or fixture, except the census file names above.
- **R5. The size contract on the arm runner.**
  - P5 runs on both runners. The arm runner's reference is MEASURED on that runner, never guessed. It is the retired `full` image (its last commit before T7a removed the target), built on `ubuntu-24.04-arm`, measured with P5's own instrument (Engine API `/images/json` `Size`).
  - It is recorded under the key the runner prints, **`aarch64/overlay2`**, beside the amd64 values, with its date. FEATURE obtains it from a PR run of the workflow.
  - **`DEFAULT_REFERENCE_BYTES` (2.33 GB, a Mac's containerd store) is excluded by name** for the runner, even under a store of the same family (F5).
  - **A reference must exist before a run can be called a check** (Verification). The runner is available, so the reference is recorded from a PR run **before D0b merges**, and the rc's arm64 P5 is a comparison.
    - Should that fail to happen, the rc's arm64 size falsifier is "reference recorded, not checked", and `v0.4.0` must come from a run whose arm64 comparison passed.
    - The Principal's choice about a first rc without the check then applies. As measured, it does not arise.
- **R6. Tests against what was pushed, in this order (F1):**
  1. **Per-arch push by digest only.** Each runner pushes its four targets with `push-by-digest`, with no tag.
  2. **R7, then R6, on those digests.**
     - Each runner pulls its own architecture's digests by `@sha256:`.
     - It runs the image contract (`tests/image`), the installer's empty-root contract (`tests/install`), the host adapter (`tests/host`) and `deploy/ci/check_ops_variant.py` against them.
  3. **Only after both architectures pass:** the manifest lists, their tags (R4), the attestations and the release page.
  4. **A failed digest** is never tagged, never in a manifest list, and never on the release page.
     - It remains pullable by digest for anyone who has the digest. ghcr keeps it as an untagged version, and no R9 permission deletes it. The rc record states this.
     - Removing it is the Principal's act in the package settings.
  - **The PR rehearsal of R6 (F1).** On `pull_request`, steps 1–2 run against a `registry:2` service container on each runner (`localhost:5000`): push by digest, pull by digest, and the same test jobs. At rc.1 the only thing that changes is the registry host.
  - **Tests that cannot take a pushed digest (F2; named at main after D0a-3, re-measured at main after O2 for r4).** `agents` is never pushed (R2), and `tests/image/image_harness.py:75` `required()` fails, never skips, on an unset variable. So these tests run against an **`agents` image built locally on the runner from the same commit**, never deselected. The same holds for the T7a P4 negative case:
    - `tests/image/test_image_agents_and_credentials.py`:
      - `test_agents_cli_answers_its_pinned_version` (claude, codex);
      - `test_target_contains_no_credential_files[agents]`;
      - `test_target_environment_has_no_credential_variables[agents]`;
    - `tests/image/test_image_identity_and_size.py`: `test_revision_label_equals_the_build_arg[agents]`;
    - `tests/image/test_image_product_roles.py`: `test_agents_carries_the_product_roles`;
    - `tests/image/variants/test_t6a_p1_variants.py`:
      - `test_variant_cannot_import_the_extension[agents]`;
      - `test_variant_contains_no_extension_files[agents]`;
    - `tests/install/test_t7b_p2_desk_tasks_image.py`: all five tests. The module's fixtures install the `agents` image (`t7b_harness.py:18, 52`);
    - `tests/image/test_t7a_p4_image_identity.py`: the unrevisioned negative case, built locally without `SOURCE_REVISION`.
    - `tests/install/test_d0f_f4_sdk_install_runtime_image.py` (f4a–f4d), added by D0f: these take the pushed `product` digest, but need npm's cache filled on the runner (the D0f step above). Without it they error at setup (measured in the first CI run after D0f's meet).

    The local `agents` build is FROM the same commit's Dockerfile. It is never pushed and never scanned by R7.

    **Re-measured at main after O2 (r4).** `pytest --collect-only -m image tests/image tests/install tests/host` collects 254 tests. The `agents`-bound tests are exactly the list above, unchanged.
  - **The OpenCode-variable tests** (`AGENT_TOOLING_TEST_IMAGE_OPENCODE`) take the PUSHED `opencode` digest, because R2 now publishes it:
    - O1's `tests/image/test_o1_opencode_launch_image.py` (32);
    - O2's `tests/image/test_o2_f1_strangers_run_image.py`, `test_o2_f2_per_launch_isolation_image.py` and `test_o2_opencode_harness_image.py` (13);
    - O3's `tests/image/test_o3_*.py` (19). Their `default_image` cases take the pushed `product`, `runtime` and `ops` digests.
    - O1's vitest integration (`apps/kanban/test/integration/o1`) takes the same digest.
  - **Two conditions for running them on a runner:**
    - **The runners are bundled from the tree.** O1's and O2's image runners are bundled with `apps/kanban`'s esbuild at test time, so these jobs need `apps/kanban`'s `node_modules`. That is the same D0f npm step, which must not be dropped from any job that runs them.
    - **O2's runs need the container options its harness passes:** `--network none`, DNS recorders on 127.0.0.1, and `--sysctl net.ipv4.ip_unprivileged_port_start=0`. FEATURE states that both runners accept them; a runner that refuses fails the job, never skips it.
  - **The recording Mac.** The arm64 `product` digest is also smoke-tested there by the Coordinator at the rc: pull by digest, `tooling-container health` and `verify`. The result goes in the rc record, not in CI.
- **R7. No CLI and no SDK in any pushed image.** At R6 step 2, each per-arch digest of each pushed target passes two checks:
  - **D0f's reusable per-image falsifier** (Verification hold: named by file and function; confirmed at the freeze once D0f has merged): `tests/image/agent_sdk_absence.py`, its function `scan_image(image) -> Report`, and its CLI `python tests/image/agent_sdk_absence.py [--expect-board] <image|repo@sha256:digest>`.
    - **Exit codes:** 0 clean, 1 carries the SDK, 2 cannot state a result. Any non-zero blocks the publish.
    - **What it reads:** every layer of the image (`docker save`, including files a later layer deletes, gzip streams and zip members). It looks for SDK package paths, source-map `sources`, and five marker strings from `sdk.mjs` 0.2.128.
    - **Controls:** the build-identity file, and for product, ops and opencode (`--expect-board`), `/app/dist/cli.js` and its map. `opencode` carries the board bundle from `product-base`; O2's F1 precondition already runs the check on it with `--expect-board`.
    - **The bundle-level check** is D0f FEATURE's `apps/kanban/scripts/verify-no-claude-agent-sdk-build.mjs [dir]`, which already ends `npm run build`. It runs inside every product and ops image build.
  - A file-system listing: no `@anthropic-ai/claude-code`, no `@openai/codex`, and no `claude` or `codex` executable on `PATH`.
    - For `opencode`, the listing also requires `opencode` on `PATH` answering O3's pin with `--network none`, and its upstream LICENSE at `/usr/local/share/doc/opencode/LICENSE`.
    - O3's image tests hold both. Taken on the pushed digest, they make the check non-vacuous for that target.

  A failing digest blocks step 3 for its target, and R6's step 4 rule applies to it.
- **R8. The release page.** The release lists every digest, per target and per architecture, plus each manifest list, with the pull command by digest. It links NOTICE and the source at the tag (the AGPL source offer).
- **R9. Permissions.**
  - Build and test jobs: `contents: read`.
  - The push job: `packages: write`, plus `id-token: write` and `attestations: write` only if R4's attestations are used.
  - The release job: `contents: write`.
  - No other secret: `GITHUB_TOKEN` only, and no personal token.
  - Package visibility is the Principal's account setting, never the workflow's.
- **R10. DOCKER.md's first run.** It states the pull by digest (`docker pull ghcr.io/saigid-1/agent-tooling@sha256:<digest>`, the digest from the release page) before `plan`, and `--image` takes that same reference. The installer still never pulls (T9b `image_absent`, unchanged).

## Falsifiers
- **A reachable publish.** A publishing job reachable from `pull_request` or `workflow_dispatch`, from a branch push, or from any repository id but the public repo's. TEST checks each from the parsed workflow:
  - the guard is at job level on every publishing job;
  - every publishing job `needs` every build and test job;
  - no `continue-on-error` on a job a publish depends on;
  - no `workflow_dispatch` input reaches a publish condition;
  - the slug is not used as the guard.
- The release workflow's file missing from its own `pull_request.paths`.
- `agents` or `acceptance` pushed under any tag or event.
- A pushed target missing an arm64 or an amd64 manifest. An arm64 image built anywhere but `ubuntu-24.04-arm`.
- **Labels and tags.**
  - A revision label that is not the public tag's commit; a source label that is not the public URL; no licences label; a build-identity revision differing from the label.
  - A tag outside R4's scheme, a `latest` or floating tag, or a tagged per-arch digest.
  - **The tag scheme (ACCEPTED by Verification, 2026-10-04), with its two falsifiers:**
    - a manifest-list tag whose `<version>` does not equal the pushed git tag minus `v`;
    - a tag on the release page resolving to a digest other than the manifest list the page lists.
- **The private org.**
  - Any tracked file naming it, checked by a positive public pattern in the H6 class style: every `ghcr.io/<owner>/agent-tooling` and `github.com/<owner>/agent-tooling` in the tree has the owner `saigid-1`, case-insensitive. The test never names the private org.
  - The refreshed private scan's third line not 0 on the pushed commit.
- **The size check.**
  - P5 on the arm runner using a reference not measured on that runner, or using `DEFAULT_REFERENCE_BYTES`.
  - P5 skipped on the arm runner.
  - D0b merged with no `aarch64/overlay2` reference recorded.
- **Order and testing.**
  - A manifest list, tag or release created before both architectures' R6 and R7 pass.
  - Tests run against a local build where R6 says the pushed digest, outside the named list.
  - The arm64 digest not tested on the arm runner.
  - The PR leg not rehearsing push and pull by digest against `registry:2`.
- Any pushed per-arch digest containing claude-agent-sdk, claude-code or codex in any file (R7).
- A release page without every per-arch digest and manifest list.
- A token permission beyond R9, or any secret other than `GITHUB_TOKEN`.
- DOCKER.md's first run without the pull by digest before `plan`.
- A repointed fixture that reaches the network.
- Any `LABEL` in `deploy/Dockerfile` whose `org.opencontainers.image.source` is a literal URL rather than the build argument. This catches a target added after D0b, by pattern.

## Write scope
- **FEATURE:**
  - the new release workflow (FEATURE names the file), and `images.yml` only where the build is shared with it. FEATURE says what moves, and the statement names **the D0f step that fills npm's cache** (`actions/setup-node` with Node 22, then `npm --prefix apps/kanban ci --ignore-scripts`, added at D0f's meet).
    - **Why it matters:** the F4 image tests' stand-in registry serves the published SDK from that cache. The step fetches the SDK into the RUNNER's cache: use, not redistribution, inside the registry-dependency class D0f named.
    - **Its first measured instance:** the first CI run after D0f's meet, where all four F4 tests errored at setup on an empty cache.
    - **Who inherits it:** the release workflow's test jobs, R6's `registry:2` rehearsal on the PR, and the public repo's CI. Each must carry the step, or its F4 tests error the same way.
  - `deploy/Dockerfile`: the source build argument, the licences label (with `opencode`'s two licences), and every target's label (6 at this base);
  - `docs/DOCKER.md`: the first-run pull (with `opencode` presented as the optional pull) and the three image-reference lines (92, 93, 113 at main after O2: every `ghcr.io/<owner>/agent-tooling` reference in the file);
  - `docs/work/PUBLIC-READINESS-AUDIT.md`: the 12 R4a rows only;
  - `tests/install/s3_harness.py:47` and `tests/install/test_p3_refusals.py:47` (at main after O2): the registry owner only;
  - the R4a scrub: `tests/boundary/fixtures/pre_move_tools_list.json`, `tests/boundary/t1_contract.py`, `tests/fixtures/t10-read-path-projection/generate.py` and `goldens.json`, `tests/test_t10_p5_identical_results.py`, and any further PRIVATE-REF site the refreshed scan names at D0b's base, outside the census file names;
  - `docs/work/orders/ARM-HEADER.md:13`: the repository name only;
  - `images.yml`'s size step (or the shared step): the `aarch64/overlay2` reference.
- **TEST:**
  - new tests under `tests/` for R1, R2, R4, R8 and R9 from the parsed workflow and the Dockerfile, and for R4's public-home pattern over `git ls-files`;
  - an image-marked test for R7 that takes a digest reference and calls D0f's reusable check;
  - for R6, the harness change that lets the named tests take a locally built `agents` image beside the pulled digests.
- **Not in scope:**
  - the README (D0a-2);
  - pushing to `Saigid-1`, which is the Principal's act at the cutover;
  - the package's visibility;
  - D0c's timed first run.

## How it is verified before the cutover
- **The PR.** The workflow runs on the PR in the private org repo, on both runners.
  - The build, test and `registry:2` rehearsal jobs run.
  - The publish jobs are unreachable by R1's guard, and TEST proves that from the parsed workflow.
  - The `aarch64/overlay2` reference is recorded from this run.
- **The rc.**
  1. The first real push is `v0.4.0-rc.1` in the public repo. It happens after the cutover gate:
     - the private scan reports 0 on all four VIOLATIONS lines, and its "PRIVATE-REF commit of this repository" detail row is 0 in every column except "census file name" (R4a);
     - H6 is green;
     - the target repo is empty.
  2. The first push creates the ghcr package **private**. The Principal sets it to public, a step on the cutover checklist between the rc push and the stranger's pull (Verification hold).
  3. Then the Coordinator and Verification check R4, R6, R7 and R8 at the published artifacts with a stranger's pull: no login and no credential.

## Amendment 1 (the Principal, 2026-10-07): the release stance in the release notes from day 0

**The Principal's decision** (given directly in Verification's session at about 16:05Z, relayed verbatim): "Let's include those lines in the readme and the protect description/charter so it exists from day 0 in the release notes." ("protect" reads as "project".)

**R8 gains the stance.**
- **One source.** The release page's body carries the project's release stance from ONE committed file, `docs/RELEASE-STANCE.md`. The release job reads the file, never a copy pasted into the workflow.
- **The file holds exactly these three claims** (wording may be tightened at the meet; the three claims must survive):
  1. **What blocks a release.** A finding at a verification meet blocks the release only if it is a leak, a redistribution, data loss or a silent failure. Everything else is carried, in the open.
  2. **Every carried item is an issue.** Each names where it is in the code and what done looks like. The open count on day one is deliberate.
  3. **Every property has a falsifier.** Each shipped property names the change that must turn its test red; a test that cannot go red is not evidence.
- **A fourth line, in the release notes only, worded as a procedural fact, never as a CI measurement** (Verification's condition 1): "The private-content scan in the cutover record read zero on every gate line before this tag was pushed. That scan is the cutover gate; it does not run in CI."
- **The cutover checklist** (the Principal's acts) carries the scan step BEFORE the tag push, with its output recorded, and Verification reruns it on the public digest afterwards (condition 2). Without that step ahead of the tag, the fourth line could become false with nothing to catch it.
- **Never in the file:** no private reference, no count of private findings, no private repository or org name.

**Write scope:** FEATURE adds `docs/RELEASE-STANCE.md` and the release job's read; TEST adds the three tests/docs falsifiers. The new file passes `pytest tests/docs` (H6 and the hygiene checks).

**Falsifiers:**
- **Under tests/docs:**
  - `docs/RELEASE-STANCE.md` exists and holds the three claims;
  - release.yml's release job reads that file into the notes body;
  - the notes template carries the fourth line.
  - Mutant: inline the text in the workflow, or drop a claim.
- **The README copy is D0a-2's.** D0a-2's README and project description carry the same three claims, and a tests/docs test holds the README's text equal to `docs/RELEASE-STANCE.md` (the two-copies lesson of backlog 44).

**Not in this order:**
- The README and the charter text (D0a-2).
- The GitHub repository "About" description, an account setting and the Principal's act at the cutover. The cutover checklist names the sentence.

## Amendment 2 (Verification, 10-07): the F2 local-build set is derived, not hand-listed

**Measured at the D0b meet.** Four t7b modules take the `agents` image through `t7b_harness.agents_world`, but R6's F2 list named only `test_t7b_p2_desk_tasks_image.py`. The three unlisted modules carry 5 tests:
- `tests/install/test_t7b_p1_desks_menu_image.py` (2);
- `tests/install/test_t7b_p3_task_worktrees_image.py` (1);
- `tests/install/test_t7b_p4_assistant_memory_image.py` (2).

With the harness giving the local `agents` build only to listed tests, they FAIL in pushed-digest mode ("AGENT_TOOLING_TEST_IMAGE_AGENTS is unset").

The r4 sentence "the agents-bound tests are exactly the list above, unchanged" was false. It was re-measured by searching for the variable name, not by fixture use, and the freeze read accepted it without re-deriving it.

**R6's F2 set is DERIVED; the test is the source.**
- **The local-build set is:**
  - every image-marked test whose module imports `t7b_harness.agents_world`, or reads `AGENT_TOOLING_TEST_IMAGE_AGENTS` (directly, or through `t6a_variants.VARIABLE['agents']`);
  - plus the two named negatives: the T7a P4 unrevisioned case, and `test_image_identity_and_size`'s `agents` case.
- **A tests/docs or tests/ci test derives that set from the tree** (import and variable usage) and asserts it EQUALS the harness's `LOCAL_BUILD_TESTS`.
  - Mutant: add a module that takes `agents_world` without listing it. The test goes RED.
- **R6's prose keeps the enumeration for the reader,** marked "derived; the test is the source". At main after O2 that is the r4 list plus the three modules above.
- **FEATURE's workflow is unaffected:** it builds `agents` locally for the whole suite run, and the harness narrows which tests see it.

## Open (for the freeze)
- **The public repo's numeric id (R1): `1404822873`** (`Saigid-1/agent-tooling`, the Principal's choice, 2026-10-05: "Use this one https://github.com/Saigid-1/agent-tooling.git").
  - **Measured 2026-10-05 from the public API:** created 2026-10-04T20:44:57Z, public, empty (0 refs, size 0), default branch `main`.
  - **The name matches this order,** so nothing else changes. (A second empty repo, `Saigid-1/agent-tooling-`, also exists; this order does not use it.)
  - **Push access.** The Principal added this machine's account (`FortyLoveKicker`) as a collaborator; measured 2026-10-05: `push: true`, still empty. The push itself waits for the cutover gate and his go.
- **The tag scheme (R4): ACCEPTED** by Verification, 2026-10-04, with the two falsifiers above. r4 extends it to `opencode-<version>` without changing its form; it is for Verification's read.
- **Verification's freeze conditions (2026-10-04),** each with its state at r4:
  - (a) D0f has merged and R7 names its check by file and function: **met**. R7 names `tests/image/agent_sdk_absence.py` `scan_image(image) -> Report` and its CLI.
  - (b) the repository id is in the order as a literal: **met** (R1, above).
  - (c) the F2 list is re-measured at the freeze base: **met for main after O2** (R6, above). The freeze base is main after O2, once O2 has merged.
- **For Verification's read at r4:**
  - `opencode` as the fourth published target (R2);
  - its tag (R4);
  - its licences label `AGPL-3.0-only AND MIT` (R4);
  - its R7 scan with `--expect-board`;
  - the OpenCode-variable tests and their two runner conditions (R6).
