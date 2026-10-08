"""The names D0b's tests assume, named once (order docs/work/orders/D0b-release-workflow.md).

The TEST arm wrote these blind to FEATURE. The meet reconciles each SEAM with FEATURE's names by
editing this file only, never an assertion. Every other value here is stated by the order and
changes only with the order; it is kept here so that no test restates it.

Seams (assumptions):
- `RELEASE_WORKFLOW`: the release workflow's file. FEATURE names it (write scope).
- `SOURCE_URL_ARG`: the one build argument every target's `org.opencontainers.image.source` label
  reads (R4: "The source URL becomes one build argument").
- `TAG_FORM`: the manifest-list tag form, `<target>-<version>` (R4's scheme, accepted by
  Verification); `<version>` is the git tag without its leading `v`.
- Job ids: none. The tests classify jobs by what their steps do (log in, push, tag, create a
  manifest list, an attestation or a release), never by id; tests/ci/d0b_workflow.py lists the
  forms it recognises.
- `LOCAL_BUILD_TESTS`: the tests R6 lets take a locally built image beside the pulled digests.
  Amendment 2: the set is DERIVED, and tests/ci/test_d0b_r6_local_build_set.py is the source; this
  list must equal what that test derives from the tree.
"""
from __future__ import annotations

# ----------------------------------------------------------------- seams

RELEASE_WORKFLOW = ".github/workflows/release.yml"
SOURCE_URL_ARG = "SOURCE_URL"
TAG_FORM = "{target}-{version}"


def manifest_tag(target: str, git_tag: str) -> str:
    """R4: `ghcr.io/saigid-1/agent-tooling:<target>-<version>`, `<version>` the git tag minus `v`."""
    assert git_tag.startswith("v"), git_tag
    return f"{REGISTRY_REPOSITORY}:{TAG_FORM.format(target=target, version=git_tag[1:])}"


# ----------------------------------------------------------------- stated by the order

# R1: the public repository's numeric id (the order's Open section, measured 2026-10-05).
PUBLIC_REPOSITORY_ID = 1404822873
# R4: the public home. ghcr requires a lowercase owner.
PUBLIC_OWNER = "saigid-1"
PUBLIC_SOURCE_URL = "https://github.com/Saigid-1/agent-tooling"
REGISTRY_REPOSITORY = "ghcr.io/saigid-1/agent-tooling"

# R2: the published targets; `opencode` is the optional one. `agents` and `acceptance` are never pushed.
PUBLISHED_TARGETS = ("runtime", "product", "ops", "opencode")
NEVER_PUSHED_TARGETS = ("agents", "acceptance")
# R4: the licences label of each published target.
LICENCES = {
    "runtime": "AGPL-3.0-only",
    "product": "AGPL-3.0-only",
    "ops": "AGPL-3.0-only",
    "opencode": "AGPL-3.0-only AND MIT",
}
# R7: the targets that carry the board bundle (`--expect-board`).
BOARD_TARGETS = ("product", "ops", "opencode")

# R3: one native runner per architecture.
AMD64_RUNNER = "ubuntu-latest"
ARM64_RUNNER = "ubuntu-24.04-arm"
RUNNERS = {AMD64_RUNNER: "linux/amd64", ARM64_RUNNER: "linux/arm64"}

# R6: the PR rehearsal's stand-in registry, a `registry:2` service container on each runner.
REHEARSAL_REGISTRY_IMAGE = "registry:2"
REHEARSAL_REGISTRY_HOSTS = ("localhost:5000", "127.0.0.1:5000")

# R1: the git tags a publish is simulated for (the rc and the full release).
RC_TAG = "v0.4.0-rc.1"
FINAL_TAG = "v0.4.0"

# R5: the key the arm runner's size step prints, and the reference excluded there by name.
ARM_SIZE_KEY = "aarch64/overlay2"
SIZE_VARIABLE = "AGENT_TOOLING_TEST_REFERENCE_BYTES"

# The D0b meet (ruled by Verification): images.yml runs tests/image on local tags, so it may ignore the
# pushed-digest module, and that module only. release.yml runs the suite whole.
IMAGES_WORKFLOW = ".github/workflows/images.yml"
PUSHED_DIGEST_ONLY_MODULE = "tests/image/test_d0b_r7_pushed_digests_image.py"

# R7: D0f's reusable check, by file (its function is `scan_image(image) -> Report`).
SDK_CHECK = "tests/image/agent_sdk_absence.py"

# The image-suite variables each target's image is named by (tests/image/image_harness.py, O3).
IMAGE_VARIABLES = {
    "product": "AGENT_TOOLING_TEST_IMAGE",
    "runtime": "AGENT_TOOLING_TEST_IMAGE_RUNTIME",
    "ops": "AGENT_TOOLING_TEST_IMAGE_OPS",
    "opencode": "AGENT_TOOLING_TEST_IMAGE_OPENCODE",
}
AGENTS_VARIABLE = "AGENT_TOOLING_TEST_IMAGE_AGENTS"
UNREVISIONED_VARIABLE = "AGENT_TOOLING_TEST_IMAGE_UNREVISIONED"
SOURCE_REVISION_VARIABLE = "AGENT_TOOLING_TEST_SOURCE_REVISION"

# R6 (F2), amendment 2: the tests that cannot take a pushed digest and run against an image built locally
# on the runner from the same commit. Node ids relative to the repository root; a trailing `::` names
# every test of the module. Derived; tests/ci/test_d0b_r6_local_build_set.py is the source.
LOCAL_BUILD_TESTS = {
    AGENTS_VARIABLE: (
        "tests/image/test_image_agents_and_credentials.py::test_agents_cli_answers_its_pinned_version[claude]",
        "tests/image/test_image_agents_and_credentials.py::test_agents_cli_answers_its_pinned_version[codex]",
        "tests/image/test_image_agents_and_credentials.py::test_target_contains_no_credential_files[agents]",
        "tests/image/test_image_agents_and_credentials.py::test_target_environment_has_no_credential_variables[agents]",
        "tests/image/test_image_identity_and_size.py::test_revision_label_equals_the_build_arg[agents]",
        "tests/image/test_image_product_roles.py::test_agents_carries_the_product_roles",
        "tests/image/variants/test_t6a_p1_variants.py::test_variant_cannot_import_the_extension[agents]",
        "tests/image/variants/test_t6a_p1_variants.py::test_variant_contains_no_extension_files[agents]",
        "tests/install/test_t7b_p1_desks_menu_image.py::",
        "tests/install/test_t7b_p2_desk_tasks_image.py::",
        "tests/install/test_t7b_p3_task_worktrees_image.py::",
        "tests/install/test_t7b_p4_assistant_memory_image.py::",
    ),
    UNREVISIONED_VARIABLE: (
        "tests/image/test_t7a_p4_image_identity.py::test_unlabelled_image_reports_unknown",
    ),
}

# Amendment 2: the two named negatives the derivation adds to what it derives from the tree.
NAMED_LOCAL_BUILD_TESTS = {
    AGENTS_VARIABLE: ("tests/image/test_image_identity_and_size.py::test_revision_label_equals_the_build_arg[agents]",),
    UNREVISIONED_VARIABLE: ("tests/image/test_t7a_p4_image_identity.py::test_unlabelled_image_reports_unknown",),
}

# The derivation's own control: the modules that take `agents` through t7b_harness `agents_world` without
# naming the variable, which a search for the variable's name misses (the miss amendment 2 records).
# The derivation must find every test of each.
DERIVATION_CONTROLS = (
    "tests/install/test_t7b_p1_desks_menu_image.py::",
    "tests/install/test_t7b_p3_task_worktrees_image.py::",
    "tests/install/test_t7b_p4_assistant_memory_image.py::",
)

# Amendment 1 (R8): the release stance, read by the release job into the notes body from ONE committed
# file, and the fourth line the release notes carry verbatim. Both stated by the amendment.
RELEASE_STANCE = "docs/RELEASE-STANCE.md"
CUTOVER_SCAN_LINE = ("The private-content scan in the cutover record read zero on every gate line before this tag "
                     "was pushed. That scan is the cutover gate; it does not run in CI.")
