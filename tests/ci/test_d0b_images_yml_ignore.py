"""D0b meet (ruled by Verification): images.yml may ignore the pushed-digest module, and nothing else.

images.yml runs tests/image against images it builds and names by local tags. The R7 pushed-digest tests
(tests/image/test_d0b_r7_pushed_digests_image.py) require digest references, so they fail there by design,
and the meet lets images.yml's tests/image run ignore that one module. release.yml keeps the suite whole
(test_r6_every_suite_runs_whole_with_the_d0f_npm_step_on_both_runners). This test keeps the exception from
widening without a red.

images.yml is read through tests/ci/d0b_workflow.py, on a pull request and on a push to main. A run's
arguments are its own plus PYTEST_ADDOPTS as the step sees it. Anything that narrows what runs counts:
`--ignore` (other than that one module), `--ignore-glob`, `--deselect`, `-k`, a `-m` other than exactly
`image`, a node id, a target that is a part of tests/image instead of all of it, `--co`/`--collect-only`,
`--setup-only`/`--setup-plan`, `--lf`/`--last-failed`, `--sw`/`--stepwise`, and the options that change what
is collected or loaded: `-p` (`-p no:...` included), `-c`, `-o`/`--override-ini`, `--rootdir`,
`--confcutdir`, `--pyargs`.
"""
from __future__ import annotations

import posixpath
import shlex

import d0b_seams as seams
import d0b_workflow as w

SUITE = "tests/image"
NARROWING_FLAGS = {"--co", "--collect-only", "--setup-only", "--setup-plan", "--lf", "--last-failed", "--sw",
                   "--stepwise", "--sw-skip", "--stepwise-skip", "--pyargs"}
NARROWING_VALUES = {"--ignore-glob", "--deselect", "-k", "-p", "-c", "-o", "--override-ini", "--rootdir", "--confcutdir"}
VALUE_OPTIONS = w._PYTEST_VALUE_OPTIONS | NARROWING_VALUES | {"--ignore", "-m"}


def _path(value: str, cwd: str) -> str:
    return posixpath.normpath(posixpath.join(cwd or ".", value.strip()))


def narrowing(args: list[str], cwd: str) -> list[str]:
    """What in a tests/image run's arguments keeps a test from running, beyond the one allowed ignore."""
    found, targets, j = [], [], 0
    while j < len(args):
        arg = args[j]
        name, eq, inline = arg.partition("=")
        value = inline if eq else (args[j + 1] if j + 1 < len(args) else "")
        step = 1 if eq else 2
        if name == "--ignore":
            if _path(value, cwd) != seams.PUSHED_DIGEST_ONLY_MODULE:
                found.append(f"--ignore {value}")
        elif name == "-m":
            if value.strip() != "image":
                found.append(f"-m {value!r}")
        elif name in NARROWING_VALUES:
            found.append(f"{name} {value}")
        elif name in NARROWING_FLAGS:
            found.append(name)
            step = 1
        elif arg.startswith(("-k", "-p", "-c", "-o", "-m")) and not arg.startswith("--") and len(arg) > 2:
            if arg.startswith("-m"):
                if arg[2:].strip() != "image":
                    found.append(f"-m {arg[2:]!r}")
            else:
                found.append(arg)
            step = 1
        elif arg.startswith("-"):
            step = 2 if (name in VALUE_OPTIONS and not eq) else 1
        else:
            if "::" in arg:
                found.append(f"node id {arg}")
            targets.append(_path(arg.split("::", 1)[0], cwd))
            step = 1
        j += step
    partial = [t for t in targets if t.startswith(SUITE + "/")]
    if partial and SUITE not in targets:
        found.append(f"part of {SUITE} only: {partial}")
    return found


def test_images_yml_runs_tests_image_whole_except_the_pushed_digest_module():
    """GREEN-IF images.yml runs `pytest tests/image -m image` at least once (on a pull request or a push to main),
    and every pytest run in it whose targets include tests/image, with PYTEST_ADDOPTS added to its arguments,
    ignores, deselects or filters NOTHING except `--ignore` of tests/image/test_d0b_r7_pushed_digests_image.py
    (that one ignore, or none): no other `--ignore`, no `--ignore-glob`, `--deselect`, `-k`, node id, partial
    target, `-m` other than `image`, `--co`/`--collect-only`, `--setup-only`/`--setup-plan`, `--lf`, `--sw`, `-p`,
    `-c`, `-o`, `--rootdir`, `--confcutdir` or `--pyargs`."""
    workflow = w._load(w.REPO_ROOT / seams.IMAGES_WORKFLOW)
    assert not workflow.error, f"{seams.IMAGES_WORKFLOW} does not parse: {workflow.error}"
    runs, problems = 0, []
    for scenario in (w.PULL_REQUEST, w.BRANCH_PUSH):
        for view in w.views(workflow, scenario):
            if view.job_if is False:
                continue
            for act in view.of("pytest"):
                if not any(t == SUITE or t.startswith(SUITE + "/") for t in act.detail["targets"]):
                    continue
                runs += 1
                addopts = act.detail["env"].get("PYTEST_ADDOPTS", "")
                args = list(act.detail["args"]) + (shlex.split(addopts) if addopts else [])
                for item in narrowing(args, act.detail.get("cwd", ".")):
                    problems.append(f"{view.label} on {scenario.name}, step {act.step}: {item}")
    assert runs, f"{seams.IMAGES_WORKFLOW} runs no pytest over {SUITE}, so the exception guards nothing"
    assert not problems, (f"{seams.IMAGES_WORKFLOW} narrows its {SUITE} run beyond --ignore "
                          f"{seams.PUSHED_DIGEST_ONLY_MODULE}:\n" + "\n".join(sorted(set(problems))))
