"""D0b, the release workflow, read statically (docs/work/orders/D0b-release-workflow.md).

R1 trigger and push guard, R2 targets, R3 architectures, R4 tags and build labels, R5 the arm runner's
size reference, R6 tests against what was pushed, R8 the release page's sources, R9 permissions.
Every test reads the release workflow (its file is a seam: tests/d0b_seams.py) through
tests/ci/d0b_workflow.py, which evaluates it for SCENARIOS (an event, a ref, a repository id) and
lists the forms it recognises. Nothing is run. What these tests cannot see (the published release
page, the registry's manifest lists, the runner's own capabilities) is checked at the rc with a
stranger's pull, as the order's "How it is verified" states.
"""
from __future__ import annotations

import re

import pytest

import d0b_seams as seams
import d0b_workflow as w
from image.image_harness import DEFAULT_REFERENCE_BYTES
from t6a_workflows import filter_matches, pull_request_runs_for, require_workflows, triggers

GUARD_CONTEXT = re.compile(r"\bgithub\s*\.\s*(repository(?!_id)\b|repository_owner\b|event\s*\.\s*repository\b)", re.I)
INPUTS = re.compile(r"\binputs\s*\.|\bgithub\s*\.\s*event\s*\.\s*inputs\b", re.I)


def _publishing(workflow) -> dict[str, list[str]]:
    jobs = w.publishing_jobs(workflow)
    assert jobs, w.redact("the release workflow has no publishing job (nothing logs in, pushes to a registry, tags, "
                          "creates a manifest list or the release); jobs read for a v-tag push:\n"
                          + w.describe(w.views(workflow, w.TAG_RC)))
    return jobs


def _pre_publish(workflow, publishing) -> list[str]:
    """Build and test jobs: not publishing, and not depending on a publishing job."""
    return [j for j in workflow.jobs if j not in publishing and not (w.closure(workflow, j) & set(publishing))]


def _all_views(workflow, scenarios=w.ALL_SCENARIOS):
    return [v for s in scenarios for v in w.views(workflow, s)]


# ======================================================================= R1


def test_r1_triggers_are_a_v_tag_push_pull_requests_and_manual_dispatch():
    """GREEN-IF the release workflow exists and parses, and its triggers are exactly `push`, `pull_request` and
    `workflow_dispatch`, where `push` runs for the tags v0.4.0 and v0.4.0-rc.1 and for no branch (a `tags` filter
    covering `v*`, no `branches`)."""
    workflow = w.require_release()
    on = triggers(workflow)
    assert set(on) == {"push", "pull_request", "workflow_dispatch"}, f"triggers are {sorted(on)}"
    push = on["push"]
    assert "tags" in push and "branches" not in push and "branches-ignore" not in push, \
        f"push trigger {push!r}: R1 publishes from a pushed `v*` tag and runs for no branch push"
    for tag in (seams.RC_TAG, seams.FINAL_TAG):
        assert filter_matches(push["tags"], tag), f"a push of tag {tag} does not run the workflow: tags {push['tags']!r}"
        assert not filter_matches(push.get("tags-ignore") or [], tag), f"tags-ignore excludes {tag}"


def test_r1_pull_request_paths_include_the_workflow_itself_and_the_image_inputs():
    """GREEN-IF a pull request that changes only the release workflow's own file runs it (F6), and so does one that
    changes only a file under deploy/, packages/, extensions/ or apps/kanban/ (what goes into an image)."""
    workflow = w.require_release()
    probes = [seams.RELEASE_WORKFLOW, "deploy/Dockerfile", "packages/tooling/d0b-probe.py",
              "extensions/ops/d0b-probe.py", "apps/kanban/src/d0b-probe.ts"]
    missing = [p for p in probes if not pull_request_runs_for(workflow, p)]
    assert not missing, (f"a pull request touching only these paths does not run {seams.RELEASE_WORKFLOW}: {missing}; "
                         f"pull_request trigger: {triggers(workflow).get('pull_request')!r}")


def test_r1_no_publishing_job_is_reachable_outside_a_v_tag_push_in_the_public_repository():
    """GREEN-IF the workflow has publishing jobs, among them one that pushes to the public registry and one that
    creates the release, and every publishing job's OWN job-level `if` (with GitHub's implicit `success() &&`)
    evaluates to definitely false for: a pull request; a manual dispatch on main and on tag v0.4.0; a push of branch
    main and of a branch named v0.4.0; a push of tag v0.4.0 in a repository whose id is not 1404822873; a push of a
    tag not starting with `v`; and a push of v0.4.0 whose needed job failed. It is not definitely false (true, or
    depending only on values the reader cannot know) for a push of v0.4.0-rc.1 and of v0.4.0 in the public
    repository."""
    workflow = w.require_release()
    publishing = _publishing(workflow)
    acts = {a.kind for v in _all_views(workflow, w.PUBLISHING_SCENARIOS) for a in v.acts}
    pushes = [a for v in _all_views(workflow, w.PUBLISHING_SCENARIOS) for a in w.remote_pushes(v)]
    assert pushes, "positive control: on a v-tag push nothing is pushed to the public registry"
    assert "release" in acts, "positive control: on a v-tag push nothing creates the release"
    problems = []
    for job_id in publishing:
        for scenario in w.FORBIDDEN:
            if scenario.needs_failed and not w.needs_of(workflow.jobs[job_id]):
                continue  # no needed job to fail; the needs test covers a publish that needs nothing
            for view in w.views(workflow, scenario, job_id):
                if view.job_if is not False:
                    problems.append(f"{view.label}: reachable on {scenario.name} (job-level if "
                                    f"{workflow.jobs[job_id].get('if')!r} -> {view.job_if})")
        for scenario in w.PUBLISHING_SCENARIOS:
            for view in w.views(workflow, scenario, job_id):
                if view.job_if is False:
                    problems.append(f"{view.label}: never runs on {scenario.name}, so nothing is published")
    assert not problems, w.redact("publishing jobs (" + ", ".join(f"{j}: {r}" for j, r in publishing.items()) + "):\n"
                                  + "\n".join(problems))


def test_r1_the_guard_compares_the_numeric_repository_id_never_a_slug():
    """GREEN-IF every publishing job's job-level `if` compares `github.repository_id` with the literal 1404822873 and
    names neither `github.repository` (the slug), `github.repository_owner` nor `github.event.repository` (F3)."""
    workflow = w.require_release()
    problems = []
    for job_id in _publishing(workflow):
        text = w.condition_text(workflow.jobs[job_id].get("if"))
        if GUARD_CONTEXT.search(text):
            problems.append(f"{job_id}: the guard reads a slug: {text!r}")
        if not re.search(r"github\s*\.\s*repository_id\s*==\s*'?" + str(seams.PUBLIC_REPOSITORY_ID) + r"\b'?", text) and \
                not re.search(r"'?" + str(seams.PUBLIC_REPOSITORY_ID) + r"'?\s*==\s*github\s*\.\s*repository_id", text):
            problems.append(f"{job_id}: the guard does not compare github.repository_id with "
                            f"{seams.PUBLIC_REPOSITORY_ID}: {text!r}")
    assert not problems, w.redact("\n".join(problems))


def test_r1_no_dispatch_input_reaches_a_publish_condition():
    """GREEN-IF no publishing job's `if`, and no `if` of a job a publishing job needs (transitively), reads
    `inputs.*` or `github.event.inputs.*`, and no `workflow_dispatch` input is named in those conditions."""
    workflow = w.require_release()
    publishing = _publishing(workflow)
    declared = (triggers(workflow).get("workflow_dispatch") or {}).get("inputs") or {}
    problems = []
    for job_id in sorted({*publishing, *(j for p in publishing for j in w.closure(workflow, p))}):
        text = str((workflow.jobs.get(job_id) or {}).get("if") or "")
        if INPUTS.search(text):
            problems.append(f"{job_id}: its if reads a dispatch input: {text!r}")
        named = [name for name in declared if re.search(rf"\b{re.escape(str(name))}\b", text)]
        if named:
            problems.append(f"{job_id}: its if names the dispatch input(s) {named}: {text!r}")
    assert not problems, w.redact("\n".join(problems))


def test_r1_every_publishing_job_needs_every_build_and_test_job():
    """GREEN-IF the workflow has at least one build-and-test job (not publishing, not depending on a publishing
    job); every publishing job needs every such job, transitively; and every publishing job that creates a manifest
    list, a tag, an attestation or the release needs, transitively, every non-publishing job of the workflow."""
    workflow = w.require_release()
    publishing = _publishing(workflow)
    builders = _pre_publish(workflow, publishing)
    assert builders, f"no build-and-test job ahead of the publishing jobs {sorted(publishing)}"
    non_publishing = [j for j in workflow.jobs if j not in publishing]
    problems = []
    for job_id in publishing:
        missing = [b for b in builders if b not in w.closure(workflow, job_id)]
        if missing:
            problems.append(f"{job_id} does not need {missing}")
    for job_id in w.step3_jobs(workflow):
        missing = [b for b in non_publishing if b not in w.closure(workflow, job_id)]
        if missing:
            problems.append(f"{job_id} (manifest list, tag, attestation or release) does not need {missing}")
    assert not problems, w.redact("a test failure would not block the publish:\n" + "\n".join(problems))


def test_r1_no_continue_on_error_where_a_publish_depends_on_it():
    """GREEN-IF no job that a publishing job needs (transitively), no job of a reusable workflow such a job calls,
    and no step of either, sets `continue-on-error` to anything but false."""
    workflow = w.require_release()
    publishing = _publishing(workflow)
    upstream = sorted({j for p in publishing for j in w.closure(workflow, p)})
    problems = []
    for job_id in upstream:
        for job in w.job_dicts(workflow, job_id):
            if str(job.get("continue-on-error", False)).strip().lower() not in ("false",):
                problems.append(f"{job_id}: continue-on-error {job.get('continue-on-error')!r}")
            for index, step in enumerate(job.get("steps") or []):
                if isinstance(step, dict) and str(step.get("continue-on-error", False)).strip().lower() not in ("false",):
                    problems.append(f"{job_id} step {index} ({step.get('name') or step.get('uses') or 'run'}): "
                                    f"continue-on-error {step.get('continue-on-error')!r}")
    assert not problems, w.redact("\n".join(problems))


def test_r1_build_and_test_jobs_run_on_pull_requests_dispatch_and_in_any_repository():
    """GREEN-IF the workflow has at least one build-and-test job, and every such job's job-level `if` is not
    definitely false on a pull request, on a manual dispatch, and on a push of tag v0.4.0 in a repository whose id is
    not the public repo's (the private org repo, under any event, runs the build and test jobs)."""
    workflow = w.require_release()
    publishing = _publishing(workflow)
    builders = _pre_publish(workflow, publishing)
    assert builders, f"no build-and-test job ahead of the publishing jobs {sorted(publishing)}"
    problems = []
    for job_id in builders:
        for scenario in (w.PULL_REQUEST, w.DISPATCH_MAIN, w.TAG_PRIVATE):
            for view in w.views(workflow, scenario, job_id):
                if view.job_if is False:
                    problems.append(f"{view.label} does not run on {scenario.name}")
    assert not problems, w.redact("\n".join(problems))


def test_r1_an_rc_tag_publishes_a_prerelease_and_the_final_tag_a_full_release():
    """GREEN-IF the step that creates the release marks it pre-release exactly for a `-rc.` tag: a release action's
    `prerelease` input evaluates to true for v0.4.0-rc.1 and to false for v0.4.0; or a `gh release create` is given
    `--prerelease` only under a condition on `-rc.` (its script names both, and the command line for v0.4.0 does
    not carry `--prerelease` itself)."""
    workflow = w.require_release()
    found = []
    for scenario, expected in ((w.TAG_RC, True), (w.TAG_FINAL, False)):
        for view in w.views(workflow, scenario):
            for act in view.of("release"):
                if "with" in act.detail:
                    value = w._truthy_input(act.detail["with"].get("prerelease"))
                    found.append((scenario.name, value is expected, f"{view.label}: prerelease={value}"))
                else:
                    words = act.detail["command"]
                    if words[:3] != ["gh", "release", "create"]:
                        continue
                    script = "\n".join(text for step, text in view.scripts() if step == act.step)
                    conditional = "--prerelease" in script and "-rc." in script
                    on_line = "--prerelease" in words
                    ok = conditional and not (on_line and not expected)
                    found.append((scenario.name, ok, f"{view.label}: gh release create; --prerelease under an "
                                                     f"-rc. condition: {conditional}; on the v0.4.0 line: {on_line}"))
    assert {name for name, _, _ in found} == {w.TAG_RC.name, w.TAG_FINAL.name}, \
        f"no release-creating step found for both tags: {found}"
    bad = [detail for _, ok, detail in found if not ok]
    assert not bad, w.redact("\n".join(bad))


# ======================================================================= R2 and R3


def _push_targets(view) -> list[tuple[str | None, list[str]]]:
    return [(a.detail.get("target"), a.detail["destinations"]) for a in view.of("push")]


def test_r2_only_runtime_product_ops_and_opencode_are_pushed():
    """GREEN-IF in no scenario does any push (to any registry, the rehearsal's included) carry the agents or
    acceptance target, or a target the reader cannot name; and on a v-tag push each of runtime, product, ops and
    opencode is pushed to the public registry."""
    workflow = w.require_release()
    problems = []
    for view in _all_views(workflow):
        for target, destinations in _push_targets(view):
            if target in seams.NEVER_PUSHED_TARGETS:
                problems.append(f"{view.label} on {view.scenario.name}: pushes {target} to {destinations}")
            elif target not in seams.PUBLISHED_TARGETS:
                problems.append(f"{view.label} on {view.scenario.name}: pushes an image whose target the reader "
                                f"cannot name ({target!r}) to {destinations}")
    for scenario in w.PUBLISHING_SCENARIOS:
        pushed = {a.detail.get("target") for v in w.views(workflow, scenario) for a in w.remote_pushes(v)}
        missing = [t for t in seams.PUBLISHED_TARGETS if t not in pushed]
        if missing:
            problems.append(f"{scenario.name}: {missing} never pushed to the public registry (pushed: {sorted(map(str, pushed))})")
    assert not problems, w.redact("\n".join(problems))


def test_r3_every_published_target_is_pushed_from_both_native_runners():
    """GREEN-IF on a push of v0.4.0-rc.1 and of v0.4.0 each published target is pushed to the public registry from a
    job on ubuntu-latest and from a job on ubuntu-24.04-arm, and every build in the workflow that names a platform
    names only its own runner's (linux/amd64 on ubuntu-latest, linux/arm64 on ubuntu-24.04-arm)."""
    workflow = w.require_release()
    problems = []
    for scenario in w.PUBLISHING_SCENARIOS:
        seen = {(a.detail.get("target"), v.runner) for v in w.views(workflow, scenario) for a in w.remote_pushes(v)}
        for target in seams.PUBLISHED_TARGETS:
            for runner in seams.RUNNERS:
                if (target, runner) not in seen:
                    problems.append(f"{scenario.name}: {target} is not pushed from {runner}")
    for view in _all_views(workflow):
        for act in view.of("build"):
            platforms = act.detail.get("platforms") or []
            native = seams.RUNNERS.get(view.runner)
            if platforms and (native is None or platforms != [native]):
                problems.append(f"{view.label}: builds {act.detail.get('target')} for {platforms} (native: {native})")
    assert not problems, w.redact("\n".join(problems))


def test_r3_no_emulation_and_every_runner_is_a_static_native_label():
    """GREEN-IF no step uses docker/setup-qemu-action or tonistiigi/binfmt and no script line (comments aside)
    installs qemu or binfmt; every job's runs-on resolves, per matrix combination, to a literal label; and every job
    that builds from deploy/Dockerfile runs on ubuntu-latest or ubuntu-24.04-arm."""
    workflow = w.require_release()
    problems = []
    for view in _all_views(workflow, (w.PULL_REQUEST, w.TAG_RC)):
        for act in view.of("emulation"):
            problems.append(f"{view.label}: uses {act.detail['uses']}")
        for _step, text in view.scripts():
            for line in w.logical_lines(text):
                if re.search(r"qemu|binfmt", line, re.I):
                    problems.append(f"{view.label}: {line.strip()[:160]}")
        if w.UNRESOLVED in view.runner or "${{" in view.runner:
            problems.append(f"{view.job_id}: runs-on is not a static label ({view.runner})")
        if view.of("build") and view.runner not in seams.RUNNERS:
            problems.append(f"{view.label}: builds images on {view.runner}, not on {sorted(seams.RUNNERS)}")
    assert not problems, w.redact("\n".join(sorted(set(problems))))


# ======================================================================= R4


def _created_tags(workflow, scenario) -> tuple[set[str], list[str]]:
    tags, sources = set(), []
    for view in w.views(workflow, scenario):
        for act in view.of("tag"):
            for tag in act.detail["tags"]:
                if not w.is_local_registry(tag):
                    tags.add(tag)
                    sources.append(f"{view.label}: {act.detail['via']} -> {tag}")
        for act in view.of("push"):
            for tag in act.detail.get("tags") or []:
                name, version, digest = w.split_ref(tag)
                if version and not digest and not w.is_local_registry(tag):
                    tags.add(tag)
                    sources.append(f"{view.label}: {act.detail.get('via')} pushes the tag {tag}")
    return tags, sources


def test_r4_manifest_list_tags_are_target_dash_version_and_nothing_else():
    """GREEN-IF the tags the workflow creates in a registry other than the rehearsal's (manifest-list tags, tagged
    pushes, metadata-action tags) resolve, for a push of v0.4.0-rc.1, to exactly
    `ghcr.io/saigid-1/agent-tooling:<target>-0.4.0-rc.1` for runtime, product, ops and opencode, and for v0.4.0 to
    exactly `...:<target>-0.4.0`: no `latest`, no floating tag, no other target, and `<version>` the git tag minus
    its leading `v`."""
    workflow = w.require_release()
    problems = []
    for scenario, tag in ((w.TAG_RC, seams.RC_TAG), (w.TAG_FINAL, seams.FINAL_TAG)):
        created, sources = _created_tags(workflow, scenario)
        expected = {seams.manifest_tag(t, tag) for t in seams.PUBLISHED_TARGETS}
        if created != expected:
            problems.append(f"{scenario.name}:\n  unexpected: {sorted(created - expected)}\n  missing: "
                            f"{sorted(expected - created)}\n  seen:\n    " + "\n    ".join(sources))
    assert not problems, w.redact("\n".join(problems))


def test_r4_per_arch_digests_are_pushed_by_digest_and_never_tagged():
    """GREEN-IF on a v-tag push every push to the public registry is by digest (`push-by-digest=true`) and carries
    no tag (only a bare repository name), so that only manifest lists are ever tagged; and at least one such push
    exists."""
    workflow = w.require_release()
    pushes, problems = 0, []
    for view in _all_views(workflow, w.PUBLISHING_SCENARIOS):
        for act in w.remote_pushes(view):
            pushes += 1
            tagged = [t for t in act.detail.get("tags") or [] if w.split_ref(t)[1]]
            if not act.detail.get("by_digest"):
                problems.append(f"{view.label}: pushes {act.detail.get('target')} to {act.detail['destinations']} "
                                f"not by digest ({act.detail.get('via')})")
            if tagged:
                problems.append(f"{view.label}: tags the per-arch image {act.detail.get('target')}: {tagged}")
    assert pushes, "positive control: nothing is pushed to the public registry on a v-tag push"
    assert not problems, w.redact("\n".join(problems))


LABELS = ("org.opencontainers.image.source", "org.opencontainers.image.revision", "org.opencontainers.image.licenses")


def test_r4_published_builds_carry_the_tag_commit_and_never_override_the_labels():
    """GREEN-IF every build of a published target in the workflow (any scenario) passes the build argument
    SOURCE_REVISION as the workflow's own commit (`github.sha`, `$GITHUB_SHA` or `git rev-parse HEAD`), every
    actions/checkout checks out that commit (no other `ref`), and no build sets a source, revision or licences
    label or annotation (or metadata-action labels) to anything but the public URL, that commit and the target's
    licences."""
    workflow = w.require_release()
    problems, builds = [], 0
    for view in _all_views(workflow):
        for act in view.of("checkout"):
            ref = act.detail.get("ref") or ""
            if ref and ref not in (w.SHA_VALUE, view.scenario.ref, view.scenario.ref_name):
                problems.append(f"{view.label}: checks out {ref!r}")
        for act in view.of("build"):
            target = act.detail.get("target")
            if target not in seams.PUBLISHED_TARGETS or act.detail.get("push") is False:
                continue  # a local test build (the unrevisioned product of T7a P4) is never published
            builds += 1
            revision = act.detail["build_args"].get("SOURCE_REVISION")
            if revision != w.SHA_VALUE:
                problems.append(f"{view.label} on {view.scenario.name}: {target} built with SOURCE_REVISION={revision!r}")
            expected = {LABELS[0]: seams.PUBLIC_SOURCE_URL, LABELS[1]: w.SHA_VALUE, LABELS[2]: seams.LICENCES[target]}
            for key, value in act.detail.get("labels", {}).items():
                if key in expected and value != expected[key]:
                    problems.append(f"{view.label}: {target} label {key}={value!r}")
            arg = act.detail["build_args"].get(seams.SOURCE_URL_ARG)
            if arg is not None and arg != seams.PUBLIC_SOURCE_URL:
                problems.append(f"{view.label}: {target} built with {seams.SOURCE_URL_ARG}={arg!r}")
    assert builds, "positive control: the workflow pushes no build of a published target"
    assert not problems, w.redact("\n".join(sorted(set(problems))))


# ======================================================================= R5


def _size_cases() -> list[tuple[str, dict]]:
    found = []
    for workflow in require_workflows():
        for scenario in (w.PULL_REQUEST, w.TAG_RC):
            for view in w.views(workflow, scenario):
                for act in view.of("size-reference"):
                    found.append((f"{workflow.name}:{view.label}", act.detail["cases"]))
    return found


def test_r5_the_arm_runner_has_its_own_measured_size_reference():
    """GREEN-IF a size-reference step (one that writes AGENT_TOOLING_TEST_REFERENCE_BYTES) maps `aarch64/overlay2`
    to an integer byte count that is neither DEFAULT_REFERENCE_BYTES (2330000000, a Mac's containerd store) nor an
    amd64 reference; and every job of the release workflow that runs tests/image on ubuntu-24.04-arm runs such a
    step, with that key, before it."""
    cases = _size_cases()
    arm = {where: c[seams.ARM_SIZE_KEY] for where, c in cases if seams.ARM_SIZE_KEY in c}
    assert arm, f"no size-reference step maps {seams.ARM_SIZE_KEY}: {cases or 'no size-reference step'}"
    amd = {v for _, c in cases for k, v in c.items() if k.startswith("x86_64/")}
    bad = {where: v for where, v in arm.items() if v == DEFAULT_REFERENCE_BYTES or v in amd}
    assert not bad, f"{seams.ARM_SIZE_KEY} reuses a reference not measured on the arm runner: {bad}"
    workflow = w.require_release()
    problems = []
    for view in _all_views(workflow, (w.PULL_REQUEST, w.TAG_RC)):
        if view.runner != seams.ARM64_RUNNER:
            continue
        for act in w.view_runs_suite(view, "tests/image"):
            before = [a for a in view.of("size-reference") if a.step < act.step and seams.ARM_SIZE_KEY in a.detail["cases"]]
            if not before:
                problems.append(f"{view.label} on {view.scenario.name}: tests/image runs without the "
                                f"{seams.ARM_SIZE_KEY} size reference before it")
    assert not problems, w.redact("\n".join(problems))


# ======================================================================= R6


def _digest_env(act, scenario) -> dict[str, str]:
    env = act.detail["env"]
    return {t: env.get(seams.IMAGE_VARIABLES[t], "") for t in seams.PUBLISHED_TARGETS}


def test_r6_pull_requests_rehearse_push_and_pull_by_digest_against_registry_2_on_both_runners():
    """GREEN-IF on a pull request, for each runner (ubuntu-latest and ubuntu-24.04-arm), a job runs that has a
    `registry:2` service container, pushes runtime, product, ops and opencode by digest to localhost:5000, pulls
    each back by digest from localhost:5000, and runs tests/image, tests/install and tests/host with `-m image` with
    those pulled digests as the images."""
    workflow = w.require_release()
    found, notes = set(), []
    for view in w.views(workflow, w.PULL_REQUEST):
        if view.job_if is False:
            continue
        services = view.job.get("services") or {}
        registry = any(str((s or {}).get("image", "")).split("@")[0] in (seams.REHEARSAL_REGISTRY_IMAGE,) or
                       str((s or {}).get("image", "")).startswith(seams.REHEARSAL_REGISTRY_IMAGE + ".")
                       for s in services.values() if isinstance(s, dict))
        local_pushes = {a.detail.get("target") for a in view.of("push") if a.detail.get("by_digest") and
                        all(w.is_local_registry(d) for d in a.detail["destinations"])}
        pulled = {a.detail["ref"] for a in view.of("pull") if w.is_local_registry(a.detail["ref"]) and
                  w.is_digest_ref(a.detail["ref"])}
        suites = {s: w.view_runs_suite(view, s) for s in ("tests/image", "tests/install", "tests/host")}
        wired = all(any(all(w.is_local_registry(r) and r in pulled for r in _digest_env(act, w.PULL_REQUEST).values())
                        for act in acts) for acts in suites.values())
        ok = registry and set(seams.PUBLISHED_TARGETS) <= local_pushes and wired
        notes.append(f"{view.label}: registry:2 service {registry}; pushed by digest to localhost {sorted(map(str, local_pushes))}; "
                     f"pulled by digest {len(pulled)}; suites on the pulled digests {wired}")
        if ok:
            found.add(view.runner)
    missing = [r for r in seams.RUNNERS if r not in found]
    assert not missing, w.redact(f"no pull-request rehearsal on {missing}:\n" + "\n".join(notes))


def _suite_views(workflow, scenarios=(w.PULL_REQUEST, w.TAG_RC)):
    return [v for v in _all_views(workflow, scenarios) if v.job_if is not False and w.image_suite_runs(v)]


def test_r6_image_suites_take_pulled_digests_and_only_the_named_local_builds():
    """GREEN-IF the workflow runs image suites, and in every job that does (pull request and v-tag push), each
    image-suite run names the runtime, product, ops and opencode images by digest references
    (`<registry>/<name>@<digest>`) that the job pulled by digest before it; names AGENT_TOOLING_TEST_IMAGE_AGENTS as
    a local build of the agents target in that job, never pushed and never a registry digest; and names
    AGENT_TOOLING_TEST_IMAGE_UNREVISIONED as a local build of product without SOURCE_REVISION."""
    workflow = w.require_release()
    views = _suite_views(workflow)
    assert views, "the release workflow runs no image suite (pytest tests/image|tests/install|tests/host -m image)"
    problems = []
    for view in views:
        pulled = {a.detail["ref"]: a.step for a in view.of("pull")}
        local = {}
        for act in view.of("build"):
            for tag in act.detail.get("tags") or []:
                local.setdefault(tag, []).append(act)
        for run in w.image_suite_runs(view):
            env = run.detail["env"]
            where = f"{view.label} on {view.scenario.name}, step {run.step}"
            for target, ref in _digest_env(run, view.scenario).items():
                if not w.is_digest_ref(ref):
                    problems.append(f"{where}: {seams.IMAGE_VARIABLES[target]}={ref!r} is not a digest reference")
                elif pulled.get(ref, run.step + 1) > run.step:
                    problems.append(f"{where}: {seams.IMAGE_VARIABLES[target]}={ref!r} is not pulled before the suite")
            agents = env.get(seams.AGENTS_VARIABLE, "")
            builds = [a for a in local.get(agents, []) if a.detail.get("target") == "agents" and a.step < run.step]
            if not agents or w.is_digest_ref(agents) or not builds or any(a.detail.get("push") for a in builds):
                problems.append(f"{where}: {seams.AGENTS_VARIABLE}={agents!r} is not a local, unpushed agents build "
                                "of this job")
            unrevisioned = env.get(seams.UNREVISIONED_VARIABLE, "")
            builds = [a for a in local.get(unrevisioned, []) if a.detail.get("target") == "product" and
                      "SOURCE_REVISION" not in a.detail["build_args"] and a.step < run.step]
            if not unrevisioned or not builds or any(a.detail.get("push") for a in builds):
                problems.append(f"{where}: {seams.UNREVISIONED_VARIABLE}={unrevisioned!r} is not a local product build "
                                "without SOURCE_REVISION")
    assert not problems, w.redact("\n".join(sorted(set(problems))))


def test_r6_every_suite_runs_whole_with_the_d0f_npm_step_on_both_runners():
    """GREEN-IF, on a pull request and on a v-tag push, for each runner a job runs tests/image, tests/install and
    tests/host with `-m image` exactly (no `-k`, `--deselect`, `--ignore`, `--ignore-glob` or node-id filter) and
    `deploy/ci/check_ops_variant.py`, and every job that runs an image suite first runs actions/setup-node with
    Node 22 and `npm --prefix apps/kanban ci --ignore-scripts` (the D0f step that fills npm's cache)."""
    workflow = w.require_release()
    problems = []
    for scenario in (w.PULL_REQUEST, w.TAG_RC):
        covered = set()
        for view in _suite_views(workflow, (scenario,)):
            first = min(a.step for a in w.image_suite_runs(view))
            node = [a for a in view.of("setup-node") if a.step < first and
                    re.match(r"^22(\.|$|\.x)", a.detail["node-version"].strip())]
            npm = [a for a in view.of("npm-ci") if a.step < first and a.detail["directory"] == "apps/kanban" and
                   "--ignore-scripts" in a.detail["args"]]
            if not (node and npm):
                problems.append(f"{view.label} on {scenario.name}: image suites run without the D0f npm step before "
                                f"them (setup-node 22: {bool(node)}, npm --prefix apps/kanban ci --ignore-scripts: "
                                f"{bool(npm)})")
            whole = {}
            for act in w.image_suite_runs(view):
                filtered = [o for o in act.detail["options"] if o in ("-k", "--deselect", "--ignore", "--ignore-glob")]
                if filtered or act.detail["node_filters"] or act.detail["markexpr"].strip() != "image":
                    problems.append(f"{view.label} on {scenario.name}: a suite run deselects tests: -m "
                                    f"{act.detail['markexpr']!r} options {filtered} filters {act.detail['node_filters']}")
                    continue
                for target in act.detail["targets"]:
                    whole[target] = True
            if all(whole.get(s) for s in ("tests/image", "tests/install", "tests/host")) and view.of("ops-check"):
                covered.add(view.runner)
        for runner in seams.RUNNERS:
            if runner not in covered:
                problems.append(f"{scenario.name}: no job on {runner} runs tests/image, tests/install and tests/host "
                                "whole with -m image and check_ops_variant.py")
    assert not problems, w.redact("\n".join(sorted(set(problems))))


def test_r6_manifest_lists_tags_and_release_wait_for_both_architectures_tests_on_the_pushed_digests():
    """GREEN-IF the workflow has jobs that create a manifest list, a tag, an attestation or the release, and each
    of them needs (transitively), for each runner, a job that on a v-tag push pulls runtime, product, ops and
    opencode from ghcr.io/saigid-1/agent-tooling by digest and runs tests/image (where D0f's R7 check runs) and
    tests/install with `-m image` on them."""
    workflow = w.require_release()
    step3 = w.step3_jobs(workflow)
    assert step3, "no job creates a manifest list, a tag, an attestation or the release"
    problems = []
    for job_id in step3:
        upstream = w.closure(workflow, job_id)
        for runner in seams.RUNNERS:
            ok = False
            for view in w.views(workflow, w.TAG_RC):
                if view.job_id not in upstream or view.runner != runner:
                    continue
                pulled = {a.detail["ref"] for a in view.of("pull")}
                for suite in ("tests/image", "tests/install"):
                    if not any(all(r.lower().startswith(seams.REGISTRY_REPOSITORY + "@") and r in pulled
                                   for r in _digest_env(a, w.TAG_RC).values()) for a in w.view_runs_suite(view, suite)):
                        break
                else:
                    ok = True
            if not ok:
                problems.append(f"{job_id} does not wait for the pushed digests' tests on {runner}")
    assert not problems, w.redact("\n".join(problems) + "\njobs on a v-tag push:\n" + w.describe(w.views(workflow, w.TAG_RC)))


# ======================================================================= R8


def _release_text(workflow) -> str:
    texts = []
    for view in w.views(workflow, w.TAG_RC):
        if view.of("release"):
            texts.extend(text for _step, _kind, text in view.texts)
            for _step, text in view.scripts():
                for path in re.findall(r"(?:^|\s)((?:deploy|scripts|tests|\.github)/[\w./-]+\.(?:py|sh|md|j2|tmpl))", text):
                    file = w.REPO_ROOT / path
                    if file.is_file():
                        texts.append(file.read_text())
    return "\n".join(texts)


def test_r8_the_release_notes_list_every_digest_and_manifest_list_and_link_notice_and_the_source():
    """GREEN-IF the job that creates the release (with the in-tree scripts its steps run) names runtime, product,
    ops and opencode, both architectures (amd64 and arm64), the manifest list, a `docker pull <image>@` by digest,
    NOTICE, and a link to the source at the tag (`/tree/<tag>` or `/archive/`, the AGPL source offer)."""
    workflow = w.require_release()
    text = _release_text(workflow)
    assert text, "no job creates the release"
    checks = {
        **{f"target {t}": re.search(rf"\b{t}\b", text) for t in seams.PUBLISHED_TARGETS},
        "amd64": re.search(r"amd64|ubuntu-latest", text),
        "arm64": re.search(r"arm64|aarch64|ubuntu-24\.04-arm", text),
        "manifest list": re.search(r"manifest", text, re.I),
        "pull by digest": re.search(r"docker pull \S*@", text),
        "NOTICE": re.search(r"\bNOTICE\b", text),
        "source at the tag": re.search(r"/(?:tree|archive)/\S*(?:ref_name|GITHUB_REF|v0\.4\.0|TAG|tag)", text, re.I),
    }
    missing = [name for name, found in checks.items() if not found]
    assert not missing, f"the release notes' sources lack: {missing}"


# ======================================================================= R9


def test_r9_each_job_holds_only_the_permissions_r9_names():
    """GREEN-IF the workflow sets `permissions` explicitly (at the top, or on every job); none is `read-all` or
    `write-all`; every job may hold `contents: read`; `packages: write` only in a job that pushes to the public
    registry, creates a manifest list or a tag; `contents: write` only in the job that creates the release;
    `id-token: write` and `attestations: write` only in a job that creates an attestation; and nothing else."""
    workflow = w.require_release()
    problems = []
    for job_id, job in workflow.jobs.items():
        permissions = w.effective_permissions(workflow, job)
        if permissions is None:
            problems.append(f"{job_id}: no permissions set (the repository default applies)")
            continue
        if isinstance(permissions, str):
            problems.append(f"{job_id}: permissions {permissions!r}")
            continue
        kinds = {a.kind for v in _all_views(workflow) if v.job_id == job_id for a in v.acts}
        remote = any(w.remote_pushes(v) for v in _all_views(workflow) if v.job_id == job_id)
        allowed = {("contents", "read"), ("contents", "none")}
        if remote or kinds & {"manifest", "tag"}:
            allowed.add(("packages", "write"))
        if "release" in kinds:
            allowed.add(("contents", "write"))
        if "attest" in kinds:
            allowed |= {("id-token", "write"), ("attestations", "write")}
        extra = sorted(f"{k}: {v}" for k, v in permissions.items()
                       if (str(k), str(v).lower()) not in allowed and str(v).lower() != "none")
        if extra:
            problems.append(f"{job_id}: {extra} beyond R9 (acts: {sorted(kinds)})")
    assert not problems, w.redact("\n".join(problems))


SECRET = re.compile(r"\bsecrets\s*(?:\.\s*([A-Za-z_][A-Za-z0-9_]*)|\[)")
SECRET_NAME = re.compile(r"token|password|passwd|secret|_key$|^key$", re.I)
GITHUB_TOKEN_VALUE = re.compile(r"\s*\$\{\{\s*(?:secrets\s*\.\s*GITHUB_TOKEN|github\s*\.\s*token)\s*\}\}\s*")


def test_r9_no_secret_but_github_token():
    """GREEN-IF the workflow reads no secret other than `secrets.GITHUB_TOKEN` (or `github.token`), passes no
    `secrets: inherit` or `secrets:` mapping, and gives no step input or environment variable whose name says token,
    password or key any other value."""
    workflow = w.require_release()
    problems = []
    for path, key, text in w_walk(workflow.data):
        if key == "secrets":
            problems.append(f"{path}: passes secrets")
        if text is None:
            continue
        for match in SECRET.finditer(text):
            if match.group(1) != "GITHUB_TOKEN":
                problems.append(f"{path}: reads {match.group(0)!r}")
        parent, _, name = path.rpartition(".")
        if (parent.endswith(".with") or parent.endswith("env")) and SECRET_NAME.search(name) and \
                not GITHUB_TOKEN_VALUE.fullmatch(text):
            problems.append(f"{path}: {name} is {text.strip()[:80]!r}, not the GITHUB_TOKEN")
    assert not problems, w.redact("\n".join(problems))


def w_walk(value, path: str = ""):
    if isinstance(value, dict):
        for key, item in value.items():
            here = f"{path}.{key}" if path else str(key)
            yield here, str(key), None
            yield from w_walk(item, here)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from w_walk(item, f"{path}[{index}]")
    elif isinstance(value, str):
        yield path, None, value
