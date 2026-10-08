"""Static reader of the D0b release workflow (docs/work/orders/D0b-release-workflow.md).

Nothing here runs a workflow, a container or a network call. The workflow file is parsed with PyYAML
(through tests/ci/t6a_workflows.py) and then read the way GitHub would run it, for one SCENARIO at a
time: an event, a ref and a repository id. Three things make that possible:

- An evaluator for GitHub expressions (`${{ }}` and `if:`), with three-valued logic. A value the
  reader cannot know (`inputs.*`, `vars.*`, a step output it did not compute, `github.repository`)
  is UNKNOWN; `&&`, `||` and `!` propagate it. A publish condition that is UNKNOWN in a forbidden
  scenario counts as reachable. Equality is GitHub's: case-insensitive strings, other types
  compared as numbers. A job or step `if` without a status function is `success() && (...)`.
- A small interpreter for `run:` scripts: continuations, comments, heredocs (skipped),
  `;`/`&&`/`||`/`|` lists, `for ... in ...; do ... done`, `cd`, assignments and `export`,
  `$X`/`${X}`/`${X#p}`/`${X##p}`/`${X%p}`/`${X%%p}`/`${X:-d}`/`${X:N}`/`${X,,}`/`${X^^}`,
  `$(git rev-parse HEAD)`, `$(echo ...)`, `$(echo ... | sed 's/^v//')`, and `echo "K=V" >>
  "$GITHUB_ENV"` / `"$GITHUB_OUTPUT"`. Anything else it cannot evaluate becomes an UNRESOLVED part
  (`<<?...>>`) of the word, never a guess.
- A model of the actions a release workflow uses: `docker/build-push-action`, `docker/login-action`
  (or any action whose name contains "login"), `docker/metadata-action` (tags, flavor, labels),
  `docker/setup-qemu-action`, `actions/checkout`, `actions/setup-node`, the attestation actions and
  the release actions.

From each step it records ACTS: login, build, push, pull, tag, manifest, attest, release,
registry-write, emulation, pytest, npm-ci, setup-node, sdk-scan, ops-check, size-reference. The
recognised commands: `docker [buildx|image] build` (`--target -f -t --push --load -o --platform
--build-arg --label`), `docker [image] push|pull|tag`, `docker login`, `docker buildx imagetools
create`, `docker manifest create|push`, `crane|gcrane|regctl|skopeo|oras` writes, `cosign
sign|attest`, `gh release create|upload|edit`, `gh api ... releases`, `curl` writes to a GitHub API,
`python tests/image/agent_sdk_absence.py`, `pytest`/`python -m pytest`, `npm ... ci`,
`python deploy/ci/check_ops_variant.py`.

Not recognised (a test that needs them reads nothing there): `docker buildx bake`, remote
(`owner/repo/...@ref`) reusable workflows, and actions other than the ones named above.

A job that calls a local reusable workflow (`uses: ./.github/workflows/...`) is read as the jobs it
calls, with its `with:` as their `inputs` and its own `if` joined to theirs; a step that uses a local
composite action (`uses: ./path` with `runs.using: composite`) is read as that action's steps. A step
whose condition is definitely false in a scenario is not read for that scenario (GitHub never runs it);
a step whose condition is true or UNKNOWN is.

A job is PUBLISHING when, in any scenario, it holds a write permission, or a step that can run logs in,
pushes to a registry other than the rehearsal registry on localhost, tags, creates a manifest list, an
attestation or a release, or writes to a registry by another tool.
"""
from __future__ import annotations

import itertools
import json
import posixpath
import re
import shlex
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import pytest

import d0b_seams as seams
from t6a_workflows import REPO_ROOT, Workflow, _load, triggers  # noqa: F401

RELEASE_PATH = REPO_ROOT / seams.RELEASE_WORKFLOW
DOCKERFILE = "deploy/Dockerfile"

SHA_VALUE = "@github.sha@"            # what github.sha / GITHUB_SHA / git rev-parse HEAD resolve to
TOKEN_VALUE = "@github.token@"        # secrets.GITHUB_TOKEN and github.token
UNRESOLVED = "<<?"                    # the start of a part the reader could not evaluate


def marker(text: str) -> str:
    """An UNRESOLVED part: one shell word (no whitespace), so word splitting never cuts it."""
    return UNRESOLVED + re.sub(r"\s+", "_", str(text)) + ">>"


# ======================================================================= expressions


class Unknown:
    """A value the reader cannot know."""

    __slots__ = ("why",)

    def __init__(self, why: str = ""):
        self.why = why

    def __repr__(self) -> str:
        return f"<unknown {self.why}>"


def is_unknown(value) -> bool:
    return isinstance(value, Unknown)


class Context(dict):
    """A context object. A missing member is null when the object is closed, else UNKNOWN."""

    def __init__(self, data=None, *, closed: bool = False, name: str = ""):
        super().__init__(data or {})
        self.closed = closed
        self.name = name

    def member(self, key: str):
        if key in self:
            return self[key]
        for k in self:
            if isinstance(k, str) and k.lower() == key.lower():
                return self[k]
        return None if self.closed else Unknown(f"{self.name}.{key}")


_TOKEN = re.compile(r"""\s*(?:
    (?P<num>(?:0x[0-9a-fA-F]+|\d+(?:\.\d+)?(?:[eE][+-]?\d+)?))
  | (?P<str>'(?:[^']|'')*')
  | (?P<op>==|!=|<=|>=|&&|\|\||[<>!()\[\],.*])
  | (?P<name>[A-Za-z_][A-Za-z0-9_-]*)
)""", re.X)


def _tokenize(text: str) -> list[tuple[str, str]]:
    tokens, pos = [], 0
    while pos < len(text):
        if text[pos:].strip() == "":
            break
        match = _TOKEN.match(text, pos)
        if not match or match.end() == pos:
            raise ValueError(f"cannot read the expression {text!r} at {pos}")
        kind = match.lastgroup
        tokens.append((kind, match.group(kind)))
        pos = match.end()
    return tokens


def truth(value):
    """GitHub truthiness: True, False, or None when the value is UNKNOWN."""
    if is_unknown(value):
        return None
    if value is None or value is False:
        return False
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value == value and value != 0
    if isinstance(value, str):
        return value != ""
    return True


def _number(value) -> float:
    if value is None:
        return 0.0
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return 0.0
        try:
            return float(int(text, 16)) if text.lower().startswith("0x") else float(text)
        except ValueError:
            return float("nan")
    return float("nan")


def _kind(value) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    return "object"


def equals(a, b):
    if is_unknown(a) or is_unknown(b):
        return Unknown("comparison")
    ka, kb = _kind(a), _kind(b)
    if ka == kb:
        if ka == "string":
            return a.lower() == b.lower()
        if ka == "object":
            return a is b
        return a == b
    return _number(a) == _number(b)


def _compare(a, b, op: str):
    if is_unknown(a) or is_unknown(b):
        return Unknown("comparison")
    if isinstance(a, str) and isinstance(b, str):
        a, b = a.lower(), b.lower()
    else:
        a, b = _number(a), _number(b)
    return {"<": a < b, "<=": a <= b, ">": a > b, ">=": a >= b}[op]


def to_text(value) -> str:
    if is_unknown(value):
        return marker(value.why)
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, (dict, list)):
        return json.dumps(value)
    return str(value)


class _Parser:
    def __init__(self, text: str, ctx: dict):
        self.tokens = _tokenize(text)
        self.i = 0
        self.ctx = ctx

    def peek(self):
        return self.tokens[self.i] if self.i < len(self.tokens) else (None, None)

    def take(self, value=None):
        token = self.peek()
        if value is not None and token[1] != value:
            raise ValueError(f"expected {value!r}, got {token[1]!r}")
        self.i += 1
        return token

    def parse(self):
        value = self.or_()
        if self.i != len(self.tokens):
            raise ValueError(f"unexpected {self.peek()[1]!r}")
        return value

    def or_(self):
        value = self.and_()
        while self.peek()[1] == "||":
            self.take()
            right = self.and_()
            t = truth(value)
            if t is True:
                continue
            value = right if t is False else Unknown("||")
        return value

    def and_(self):
        value = self.cmp()
        while self.peek()[1] == "&&":
            self.take()
            right = self.cmp()
            t = truth(value)
            if t is False:
                continue
            if t is True:
                value = right
            else:
                value = False if truth(right) is False else Unknown("&&")
        return value

    def cmp(self):
        value = self.unary()
        while self.peek()[1] in ("==", "!=", "<", "<=", ">", ">="):
            op = self.take()[1]
            right = self.unary()
            if op == "==":
                value = equals(value, right)
            elif op == "!=":
                result = equals(value, right)
                value = result if is_unknown(result) else not result
            else:
                value = _compare(value, right, op)
        return value

    def unary(self):
        if self.peek()[1] == "!":
            self.take()
            t = truth(self.unary())
            return Unknown("!") if t is None else not t
        return self.postfix(self.primary())

    def postfix(self, value):
        while True:
            token = self.peek()
            if token[1] == ".":
                self.take()
                kind, name = self.take()
                if name == "*":
                    value = Unknown("filter")
                    continue
                value = self._member(value, name)
            elif token[1] == "[":
                self.take()
                if self.peek()[1] == "*":
                    self.take()
                    self.take("]")
                    value = Unknown("filter")
                    continue
                key = self.or_()
                self.take("]")
                value = Unknown("index") if is_unknown(key) else self._member(value, to_text(key) if not
                                                                               isinstance(key, (int, float)) else key)
            else:
                return value

    @staticmethod
    def _member(value, key):
        if is_unknown(value):
            return Unknown(f"{value.why}.{key}")
        if isinstance(value, Context):
            return value.member(str(key))
        if isinstance(value, dict):
            return value.get(key)
        if isinstance(value, list) and isinstance(key, (int, float)):
            index = int(key)
            return value[index] if 0 <= index < len(value) else None
        return None

    def primary(self):
        kind, text = self.take()
        if kind == "num":
            return float(int(text, 16)) if text.lower().startswith("0x") else float(text)
        if kind == "str":
            return text[1:-1].replace("''", "'")
        if text == "(":
            value = self.or_()
            self.take(")")
            return value
        if kind == "name":
            lowered = text.lower()
            if lowered == "true":
                return True
            if lowered == "false":
                return False
            if lowered == "null":
                return None
            if self.peek()[1] == "(":
                self.take("(")
                args = []
                while self.peek()[1] != ")":
                    args.append(self.or_())
                    if self.peek()[1] == ",":
                        self.take()
                self.take(")")
                return self.call(lowered, args)
            context = self.ctx.get(lowered)
            return context if context is not None else Unknown(text)
        raise ValueError(f"unexpected {text!r}")

    def call(self, name: str, args: list):
        status = self.ctx.get("__status__", {})
        if name in ("success", "failure", "always", "cancelled"):
            return status.get(name, Unknown(name))
        if any(is_unknown(a) for a in args):
            return Unknown(f"{name}()")
        if name in ("startswith", "endswith"):
            a, b = (to_text(args[0]).lower(), to_text(args[1]).lower())
            return a.startswith(b) if name == "startswith" else a.endswith(b)
        if name == "contains":
            haystack, needle = args
            if isinstance(haystack, list):
                return any(equals(item, needle) is True for item in haystack)
            return to_text(needle).lower() in to_text(haystack).lower()
        if name == "format":
            fmt = to_text(args[0])
            return re.sub(r"\{(\d+)\}", lambda m: to_text(args[1 + int(m.group(1))]), fmt)
        if name == "join":
            items = args[0] if isinstance(args[0], list) else [args[0]]
            return (to_text(args[1]) if len(args) > 1 else ",").join(to_text(i) for i in items)
        if name == "tojson":
            return json.dumps(args[0])
        if name == "fromjson":
            try:
                return json.loads(to_text(args[0]))
            except ValueError:
                return Unknown("fromJSON")
        return Unknown(f"{name}()")


def evaluate(text: str, ctx: dict):
    """Evaluate one expression; a text the reader cannot parse is UNKNOWN."""
    try:
        return _Parser(text, ctx).parse()
    except (ValueError, IndexError, TypeError) as error:
        return Unknown(f"unparsed {text!r}: {error}")


_INTERPOLATION = re.compile(r"\$\{\{(.*?)\}\}", re.S)
_STATUS_FUNCTION = re.compile(r"\b(?:success|failure|always|cancelled)\s*\(", re.I)


def interpolate(value, ctx: dict):
    """`${{ }}` replaced by its value, recursively through lists and mappings."""
    if isinstance(value, str):
        return _INTERPOLATION.sub(lambda m: to_text(evaluate(m.group(1).strip(), ctx)), value)
    if isinstance(value, list):
        return [interpolate(v, ctx) for v in value]
    if isinstance(value, dict):
        return {k: interpolate(v, ctx) for k, v in value.items()}
    return value


def condition_text(condition) -> str:
    """An `if:` value as one expression, with GitHub's implicit `success() &&`."""
    if condition is None:
        text = ""
    elif isinstance(condition, bool):
        text = "true" if condition else "false"
    else:
        text = str(condition).strip()
    whole = re.fullmatch(r"\$\{\{(.*)\}\}", text, re.S)
    if whole and "${{" not in whole.group(1):
        text = whole.group(1).strip()
    text = _INTERPOLATION.sub(lambda m: f"({m.group(1).strip()})", text)
    if not text:
        return "success()"
    return text if _STATUS_FUNCTION.search(text) else f"success() && ({text})"


def condition(value, ctx: dict):
    """True, False, or None (UNKNOWN) for an `if:` value."""
    return truth(evaluate(condition_text(value), ctx))


# ======================================================================= scenarios


@dataclass(frozen=True)
class Scenario:
    name: str
    event: str
    ref: str
    repository_id: int = seams.PUBLIC_REPOSITORY_ID
    needs_failed: bool = False

    @property
    def ref_name(self) -> str:
        for prefix in ("refs/tags/", "refs/heads/", "refs/pull/"):
            if self.ref.startswith(prefix):
                return self.ref[len(prefix):]
        return self.ref

    @property
    def ref_type(self) -> str:
        return "tag" if self.ref.startswith("refs/tags/") else "branch"

    def github(self) -> Context:
        return Context({
            "event_name": self.event, "ref": self.ref, "ref_name": self.ref_name, "ref_type": self.ref_type,
            "repository_id": str(self.repository_id), "sha": SHA_VALUE, "token": TOKEN_VALUE,
            "server_url": "https://github.com", "api_url": "https://api.github.com",
            "workspace": "@workspace@", "action_path": "@action_path@",
            "head_ref": "", "base_ref": "",
            "event": Context(name="github.event"),
        }, name="github")

    def default_env(self, runner: str | None) -> dict:
        return {
            "GITHUB_EVENT_NAME": self.event, "GITHUB_REF": self.ref, "GITHUB_REF_NAME": self.ref_name,
            "GITHUB_REF_TYPE": self.ref_type, "GITHUB_REPOSITORY_ID": str(self.repository_id),
            "GITHUB_SHA": SHA_VALUE, "GITHUB_SERVER_URL": "https://github.com",
            "GITHUB_WORKSPACE": "@workspace@", "RUNNER_TEMP": "@runner.temp@",
            "RUNNER_ARCH": {seams.ARM64_RUNNER: "ARM64", seams.AMD64_RUNNER: "X64"}.get(runner or "", "@runner.arch@"),
            "GITHUB_ENV": "@GITHUB_ENV@", "GITHUB_OUTPUT": "@GITHUB_OUTPUT@",
            "GITHUB_STEP_SUMMARY": "@GITHUB_STEP_SUMMARY@", "GITHUB_PATH": "@GITHUB_PATH@",
        }


def _tag(name: str, *, repository_id: int = seams.PUBLIC_REPOSITORY_ID, needs_failed=False) -> Scenario:
    return Scenario(f"push of tag {name}" + (" (private repository id)" if repository_id != seams.PUBLIC_REPOSITORY_ID
                                             else "") + (" with a needed job failed" if needs_failed else ""),
                    "push", f"refs/tags/{name}", repository_id, needs_failed)


PULL_REQUEST = Scenario("pull_request", "pull_request", "refs/pull/7/merge")
DISPATCH_MAIN = Scenario("workflow_dispatch on main", "workflow_dispatch", "refs/heads/main")
DISPATCH_TAG = Scenario(f"workflow_dispatch on tag {seams.FINAL_TAG}", "workflow_dispatch",
                        f"refs/tags/{seams.FINAL_TAG}")
BRANCH_PUSH = Scenario("push of branch main", "push", "refs/heads/main")
BRANCH_V_PUSH = Scenario("push of a branch named v0.4.0", "push", "refs/heads/v0.4.0")
TAG_PRIVATE = _tag(seams.FINAL_TAG, repository_id=1)
TAG_NOT_V = _tag("release-0.4.0")
TAG_RC = _tag(seams.RC_TAG)
TAG_FINAL = _tag(seams.FINAL_TAG)
TAG_FINAL_NEEDS_FAILED = _tag(seams.FINAL_TAG, needs_failed=True)

FORBIDDEN = (PULL_REQUEST, DISPATCH_MAIN, DISPATCH_TAG, BRANCH_PUSH, BRANCH_V_PUSH, TAG_PRIVATE, TAG_NOT_V,
             TAG_FINAL_NEEDS_FAILED)
PUBLISHING_SCENARIOS = (TAG_RC, TAG_FINAL)
ALL_SCENARIOS = (*FORBIDDEN, *PUBLISHING_SCENARIOS)


# ======================================================================= loading


def require_release() -> Workflow:
    """The release workflow, parsed; FAILS when it is missing or does not parse."""
    if not RELEASE_PATH.is_file():
        pytest.fail(f"the release workflow {seams.RELEASE_WORKFLOW} does not exist (R1; the file name is a seam in "
                    "tests/d0b_seams.py)", pytrace=False)
    workflow = _load(RELEASE_PATH)
    if workflow.error:
        pytest.fail(f"{seams.RELEASE_WORKFLOW} does not parse: {workflow.error}", pytrace=False)
    if not workflow.jobs:
        pytest.fail(f"{seams.RELEASE_WORKFLOW} defines no jobs", pytrace=False)
    return workflow


def matrix_combinations(job: dict) -> list[dict]:
    """GitHub's expansion: the product of the axes, minus `exclude`, then `include` (an include that matches
    no combination adds one; a matrix of `include` only is exactly its includes)."""
    strategy = job.get("strategy") if isinstance(job.get("strategy"), dict) else {}
    matrix = strategy.get("matrix")
    if not isinstance(matrix, dict):
        return [{}]
    axes = {k: v for k, v in matrix.items() if k not in ("include", "exclude") and isinstance(v, list)}
    combos = [dict(zip(axes, values)) for values in itertools.product(*axes.values())] if axes else []
    for excluded in matrix.get("exclude") or []:
        if isinstance(excluded, dict):
            combos = [c for c in combos if not all(c.get(k) == v for k, v in excluded.items())]
    for included in matrix.get("include") or []:
        if not isinstance(included, dict):
            continue
        matched = False
        for combo in combos:
            if axes and all(combo.get(k) == v for k, v in included.items() if k in axes):
                combo.update({k: v for k, v in included.items() if k not in axes})
                matched = True
        if not matched:
            combos.append(dict(included))
    return combos or [{}]


def needs_of(job: dict) -> list[str]:
    needs = job.get("needs") if isinstance(job, dict) else None
    if isinstance(needs, str):
        return [needs]
    return [str(n) for n in needs] if isinstance(needs, list) else []


def closure(workflow: Workflow, job_id: str) -> set[str]:
    """Every job `job_id` needs, transitively."""
    seen, stack = set(), list(needs_of(workflow.jobs.get(job_id) or {}))
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        stack.extend(needs_of(workflow.jobs.get(current) or {}))
    return seen


# ======================================================================= shell


_SUBSTITUTION = re.compile(r"__D0BSUBST(\d+)__")
_PARAMETER = re.compile(
    r"\$\{(?P<name>[A-Za-z_][A-Za-z0-9_]*)(?:(?P<op>##|#|%%|%|:-|:=|-|,,|\^\^|:)(?P<arg>[^}]*))?\}"
    r"|\$(?P<bare>[A-Za-z_][A-Za-z0-9_]*)")
_KEYWORDS = {"do", "then", "else", "elif", "fi", "{", "}", "!", "time", "if", "while", "until", "esac", "in"}
_SEPARATORS = {"&&", "||", ";", "|", "&", "|&", ";;", "(", ")"}
_HEREDOC = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")
_REDIRECT = re.compile(r"^(?:\d*|&)(>>|>|<|>&|<&)(.*)$")


def _glob_regex(pattern: str) -> str:
    out, i = "", 0
    while i < len(pattern):
        char = pattern[i]
        if char == "*":
            out += ".*"
        elif char == "?":
            out += "."
        elif char == "[" and "]" in pattern[i + 1:]:
            end = pattern.index("]", i + 1)
            out += pattern[i:end + 1]
            i = end
        else:
            out += re.escape(char)
        i += 1
    return out


def _remove(value: str, pattern: str, op: str) -> str:
    if UNRESOLVED in value:
        return value
    regex = re.compile(_glob_regex(pattern) + r"\Z", re.S)
    lengths = range(len(value) + 1)
    if op in ("#", "##"):
        order = lengths if op == "#" else reversed(lengths)
        for n in order:
            if regex.match(value[:n]):
                return value[n:]
    else:
        order = reversed(lengths) if op == "%" else lengths
        for n in order:
            if regex.match(value[n:]):
                return value[:n]
    return value


def expand(word: str, variables: dict, substitutions: dict) -> str:
    """One shell word, expanded: parameters and the recognised command substitutions."""
    def parameter(match: re.Match) -> str:
        name = match.group("name") or match.group("bare")
        value = variables.get(name)
        op, arg = match.group("op"), match.group("arg")
        if value is None:
            if op in (":-", "-", ":="):
                return expand(arg or "", variables, substitutions)
            return marker(f"${name}")
        if op in ("#", "##", "%", "%%"):
            return _remove(value, expand(arg or "", variables, substitutions), op)
        if op == ",,":
            return value if UNRESOLVED in value else value.lower()
        if op == "^^":
            return value if UNRESOLVED in value else value.upper()
        if op == ":" and arg is not None:
            parts = arg.split(":")
            try:
                start = int(parts[0])
                return value[start:start + int(parts[1])] if len(parts) > 1 else value[start:]
            except ValueError:
                return marker(f"${{{name}:{arg}}}")
        return value

    text = _PARAMETER.sub(parameter, word)
    return _SUBSTITUTION.sub(lambda m: _substitution(substitutions[int(m.group(1))], variables, substitutions), text)


def _substitution(inner: str, variables: dict, substitutions: dict) -> str:
    inner = inner.strip()
    if re.fullmatch(r"git(?:\s+-C\s+\S+)?\s+rev-parse\s+(?:--verify\s+)?HEAD(?:\^\{commit\})?", inner):
        return SHA_VALUE
    sed = re.fullmatch(r"echo\s+(.+?)\s*\|\s*sed\s+(?:-e\s+)?['\"]?s/\^v//['\"]?", inner)
    if sed:
        value = " ".join(expand(w, variables, substitutions) for w in _split_words(sed.group(1)))
        return value[1:] if value.startswith("v") else value
    if re.match(r"jq\b", inner) and ".tags" in inner:
        # docker/metadata-action's JSON, as its documented `imagetools create` recipe reads it
        source = re.search(r"\$\{?DOCKER_METADATA_OUTPUT_JSON\}?", inner)
        text = variables.get("DOCKER_METADATA_OUTPUT_JSON") if source else None
        try:
            tags = json.loads(text)["tags"] if text else None
        except (ValueError, KeyError, TypeError):
            tags = None
        if isinstance(tags, list):
            return " ".join(f"-t {t}" for t in tags) if re.search(r"""["']-t ["']""", inner) else " ".join(tags)
    if inner.startswith("echo ") and "|" not in inner:
        return " ".join(expand(w, variables, substitutions) for w in _split_words(inner[5:]) if w not in ("-n", "-e"))
    return marker(f"$({inner[:80]})")


def _split_words(text: str) -> list[str]:
    try:
        lexer = shlex.shlex(text, posix=True)
        lexer.whitespace_split = True
        lexer.commenters = ""
        return list(lexer)
    except ValueError:
        return text.split()


def _mask(line: str, substitutions: dict) -> str:
    out, i = [], 0
    while i < len(line):
        if line.startswith("$(", i) and not line.startswith("$((", i):
            depth, j = 0, i + 1
            while j < len(line):
                if line[j] == "(":
                    depth += 1
                elif line[j] == ")":
                    depth -= 1
                    if depth == 0:
                        break
                j += 1
            key = len(substitutions)
            substitutions[key] = line[i + 2:j]
            out.append(f"__D0BSUBST{key}__")
            i = j + 1
        elif line[i] == "`":
            j = line.find("`", i + 1)
            j = len(line) if j < 0 else j
            key = len(substitutions)
            substitutions[key] = line[i + 1:j]
            out.append(f"__D0BSUBST{key}__")
            i = j + 1
        else:
            out.append(line[i])
            i += 1
    return "".join(out)


def _tokens(line: str) -> list[str]:
    try:
        lexer = shlex.shlex(line, posix=True, punctuation_chars=";&|()")
        lexer.whitespace_split = True
        lexer.commenters = "#"
        return list(lexer)
    except ValueError:
        return line.split()


def logical_lines(script: str) -> list[str]:
    """Continuations joined; heredoc bodies and blank or comment-only lines dropped."""
    lines, heredoc = [], None
    for line in re.sub(r"\\\r?\n", " ", script).splitlines():
        if heredoc is not None:
            if line.strip() == heredoc:
                heredoc = None
            continue
        match = _HEREDOC.search(line)
        if match:
            heredoc = match.group(2)
            line = line[:match.start()]
        if line.strip() and not line.strip().startswith("#"):
            lines.append(line)
    return lines


@dataclass
class ShellCommand:
    words: list[str]
    redirects: list[str]
    assignments: dict
    variables: dict
    cwd: str


def interpret(script: str, variables: dict, cwd: str = ".") -> list[ShellCommand]:
    """The simple commands of a script, in order, each with the variables visible to it. Assignments,
    `export` and `cd` update the state; `for` loops are unrolled."""
    state = dict(variables)
    commands: list[ShellCommand] = []
    loops: list[tuple[str, list[str], list[list[str]]]] = []
    substitutions: dict[int, str] = {}

    def run(raw_words: list[str]):
        nonlocal cwd
        words, redirects, j = [], [], 0
        while j < len(raw_words):
            match = _REDIRECT.match(raw_words[j])
            if match and not raw_words[j].startswith("__D0BSUBST"):
                target = match.group(2)
                if not target and j + 1 < len(raw_words):
                    target, j = raw_words[j + 1], j + 1
                redirects.append(expand(target, state, substitutions))
            else:
                words.append(raw_words[j])
            j += 1
        assignments = {}
        while words and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", words[0], re.S):
            key, _, value = words[0].partition("=")
            assignments[key] = expand(value, state, substitutions)
            words = words[1:]
        if not words:
            state.update(assignments)
            return
        if words[0] in ("export", "declare", "local", "readonly", "typeset"):
            for item in words[1:]:
                if "=" in item and not item.startswith("-"):
                    key, _, value = item.partition("=")
                    state[key] = expand(value, state, substitutions)
            return
        if words[0] == "cd":
            cwd = _norm(expand(words[1], state, substitutions), cwd) if len(words) > 1 else "."
            return
        expanded = []
        for word in words:
            value = expand(word, state, substitutions)
            if _SUBSTITUTION.fullmatch(word) and " " in value and UNRESOLVED not in value:
                expanded.extend(value.split())
            else:
                expanded.append(value)
        commands.append(ShellCommand(expanded, redirects, assignments, {**state, **assignments}, cwd))

    for line in logical_lines(script):
        simple, current = [], []
        for token in _tokens(_mask(line, substitutions)):
            if token in _SEPARATORS:
                simple.append(current)
                current = []
            else:
                current.append(token)
        simple.append(current)
        for words in simple:
            while words and words[0] in _KEYWORDS:
                words = words[1:]
            if not words:
                continue
            if words[0] == "for" and len(words) >= 3 and words[2] == "in":
                items = []
                for item in words[3:]:
                    if item in ("do",):
                        break
                    items.extend(expand(item, state, substitutions).split())
                loops.append((words[1], items, []))
                continue
            if words[0] == "done" and loops:
                variable, items, body = loops.pop()
                for item in items:
                    for body_words in body:
                        state[variable] = item
                        if loops:
                            loops[-1][2].append(body_words)
                        else:
                            run(body_words)
                continue
            if words[0] in ("[[", "[", "test", "case", "true", "false", ":"):
                continue
            if loops:
                loops[-1][2].append(words)
            else:
                run(words)
    return commands


def _norm(path: str, base: str = ".") -> str:
    text = str(path).strip()
    text = re.sub(r"^@workspace@/?", "", text)
    if text.startswith("/"):
        return posixpath.normpath(text)
    return posixpath.normpath(posixpath.join(base or ".", text or "."))


# ======================================================================= image references


_OWNER_URL = re.compile(r"(?i)((?:ghcr\.io|github\.com)[/:])([A-Za-z0-9][A-Za-z0-9_.-]*)(/agent-tooling)")
_OWNER_QUOTED_SLUG = re.compile(r"""(['"`])([A-Za-z0-9][A-Za-z0-9_.-]*)(/agent-tooling)""")


def redact(text: str) -> str:
    """A failure message with every repository owner but the public one replaced: a test reports a finding by
    class and place, never by a value that names another owner."""
    def url(m):
        return m.group(0) if m.group(2).lower() == seams.PUBLIC_OWNER else m.group(1) + "<non-public-owner>" + m.group(3)

    def slug(m):
        return m.group(0) if m.group(2).lower() == seams.PUBLIC_OWNER else m.group(1) + "<non-public-owner>" + m.group(3)
    return _OWNER_QUOTED_SLUG.sub(slug, _OWNER_URL.sub(url, str(text)))



def is_local_registry(ref: str) -> bool:
    return any(ref.lower().startswith(host + "/") for host in seams.REHEARSAL_REGISTRY_HOSTS)


def split_ref(ref: str) -> tuple[str, str | None, str | None]:
    """(name, tag, digest) of an image reference; a registry port is not a tag."""
    name, digest = (ref.split("@", 1) + [None])[:2] if "@" in ref else (ref, None)
    last = name.rsplit("/", 1)[-1]
    tag = None
    if ":" in last:
        tag = last.split(":", 1)[1]
        name = name[: len(name) - len(tag) - 1]
    return name, tag, digest


def is_digest_ref(ref: str) -> bool:
    """`name@sha256:<64 hex>`, or `name@` followed by a part the reader could not resolve."""
    name, _tag, digest = split_ref(ref)
    if not digest or "/" not in name or UNRESOLVED in name:
        return False
    return bool(re.fullmatch(r"sha256:[0-9a-f]{64}", digest) or digest.startswith(UNRESOLVED)
                or digest.startswith("sha256:" + UNRESOLVED))


# ======================================================================= acts


@dataclass
class Act:
    kind: str
    step: int
    detail: dict = field(default_factory=dict)

    def __repr__(self) -> str:
        return f"{self.kind}@step{self.step}{self.detail}"


@dataclass
class JobView:
    """One job, for one matrix combination, read under one scenario."""

    workflow: Workflow
    job_id: str
    combo: dict
    scenario: Scenario
    runner: str
    job_if: object
    acts: list[Act]
    texts: list[tuple[int, str, str]]   # (step, "uses" | "run", text)
    step_conditions: list
    outputs: dict
    job: dict = field(default_factory=dict)
    nested: str | None = None

    @property
    def label(self) -> str:
        suffix = f" {json.dumps(self.combo, sort_keys=True)}" if self.combo else ""
        nested = f" (calls {self.nested})" if self.nested else ""
        return f"{self.job_id}{nested}{suffix} on {self.runner}"

    def of(self, *kinds) -> list[Act]:
        return [a for a in self.acts if a.kind in kinds]

    def scripts(self) -> list[tuple[int, str]]:
        return [(step, text) for step, kind, text in self.texts if kind == "run"]


_LOGIN_COMMAND = re.compile(r"\b(?:docker|podman|buildah|helm)\s+(?:registry\s+)?login\b|\bcrane\s+auth\s+login\b"
                            r"|\bgh\s+auth\s+login\b|\boras\s+login\b|\bregctl\s+registry\s+login\b")
_RELEASE_ACTIONS = ("softprops/action-gh-release", "ncipollo/release-action", "actions/create-release",
                    "actions/upload-release-asset", "svenstaro/upload-release-action")
_ATTEST_ACTIONS = ("actions/attest-build-provenance", "actions/attest-sbom", "actions/attest")


def _list_input(value) -> list[str]:
    """A list-type action input: one item per line, or comma-separated items on a line that holds no
    `key=value` attribute (an attribute line such as `name=x,enable=true` is one item)."""
    if value is None:
        return []
    items = value if isinstance(value, list) else str(value).split("\n")
    out = []
    for item in items:
        text = str(item).strip()
        if not text:
            continue
        if "=" in text:
            out.append(text)
        else:
            out.extend(part.strip() for part in text.split(",") if part.strip())
    return out


def _key_values(value) -> dict:
    """`KEY=VALUE` lines (build-args, labels): one per line or list item."""
    found = {}
    items = value if isinstance(value, list) else str(value or "").split("\n")
    for item in items:
        key, eq, val = str(item).strip().partition("=")
        if eq and key.strip():
            found[key.strip()] = val.strip()
    return found


def _output_specs(value) -> list[dict]:
    specs = []
    items = value if isinstance(value, list) else str(value or "").split("\n")
    for item in items:
        text = str(item).strip()
        if not text:
            continue
        spec = {}
        for part in text.split(","):
            key, eq, val = part.strip().partition("=")
            spec[key.strip().lower()] = val.strip() if eq else "true"
        specs.append(spec)
    return specs


def _truthy_input(value) -> bool | None:
    if value is None:
        return False
    text = to_text(value).strip().lower()
    if UNRESOLVED in text:
        return None
    return text in ("true", "1", "yes")


@dataclass
class _Build:
    target: str | None
    file: str | None
    tags: list[str]
    build_args: dict
    platforms: list[str]
    push: bool | None
    by_digest: bool
    load: bool
    names: list[str]
    labels: dict
    via: str


def _docker_cli_build(words: list[str], cwd: str) -> _Build | None:
    if not words or posixpath.basename(words[0]) != "docker":
        return None
    rest = words[1:]
    head = [w for w in rest[:3] if not w.startswith("-")]
    if "build" not in head and "b" not in head[:2]:
        return None
    if head[:2] == ["buildx", "imagetools"]:
        return None
    key = "build" if "build" in rest else "b"
    args = rest[rest.index(key) + 1:]
    target = dockerfile = None
    tags, platforms, outputs, labels, build_args = [], [], [], {}, {}
    push = load = False
    positional, j = [], 0
    value_options = {"--cache-from", "--cache-to", "--secret", "--ssh", "--network", "--progress", "--builder",
                     "--iidfile", "--metadata-file", "--build-context", "--add-host", "--shm-size", "--ulimit",
                     "--attest", "--annotation", "--allow", "--call", "--provenance", "--sbom"}
    while j < len(args):
        arg = args[j]
        name, eq, value = arg.partition("=")
        nxt = args[j + 1] if j + 1 < len(args) else ""
        def val():
            return (value, 1) if eq else (nxt, 2)
        if name == "--target":
            target, step = val()
        elif name in ("-f", "--file"):
            dockerfile, step = val()
        elif name in ("-t", "--tag"):
            tag, step = val()
            tags.append(tag)
        elif name == "--platform":
            platform, step = val()
            platforms.extend(p.strip() for p in platform.split(",") if p.strip())
        elif name in ("-o", "--output"):
            output, step = val()
            outputs.append(output)
        elif name == "--build-arg":
            item, step = val()
            k, _, v = item.partition("=")
            build_args[k] = v
        elif name == "--label":
            item, step = val()
            k, _, v = item.partition("=")
            labels[k] = v
        elif arg == "--push":
            push, step = True, 1
        elif arg == "--load":
            load, step = True, 1
        elif arg.startswith("-"):
            step = 2 if (name in value_options and not eq) else 1
        else:
            positional.append(arg)
            step = 1
        j += step
    specs = _output_specs("\n".join(outputs))
    if any(s.get("push") == "true" or s.get("type") == "registry" for s in specs):
        push = True
    by_digest = any(s.get("push-by-digest") == "true" for s in specs)
    names = [n for s in specs for n in ([s["name"]] if s.get("name") else [])]
    context = positional[-1] if positional else "."
    resolved = _norm(dockerfile, cwd) if dockerfile else _norm(posixpath.join(context, "Dockerfile"), cwd)
    return _Build(target, resolved, tags, build_args, platforms, push, by_digest,
                  load or any(s.get("type") == "docker" for s in specs), names, labels, "docker build")


def _action_build(config: dict) -> _Build:
    context = to_text(config.get("context") or ".")
    dockerfile = _norm(to_text(config["file"])) if config.get("file") else _norm(posixpath.join(context, "Dockerfile"))
    specs = _output_specs(config.get("outputs"))
    push_input = _truthy_input(config.get("push"))
    pushes = push_input if push_input is not None else None
    if any(s.get("push") == "true" or s.get("type") == "registry" for s in specs):
        pushes = True
    build_args = _key_values(config.get("build-args"))
    labels = {**_key_values(config.get("labels")), **_key_values(config.get("annotations"))}
    platforms = [p.strip() for p in re.split(r"[,\n]", to_text(config.get("platforms") or "")) if p.strip()]
    return _Build(
        _clean(to_text(config["target"])) if config.get("target") else None, dockerfile,
        _list_input(config.get("tags")), build_args, platforms, pushes,
        any(s.get("push-by-digest") == "true" for s in specs),
        _truthy_input(config.get("load")) is True or any(s.get("type") == "docker" for s in specs),
        [s["name"] for s in specs if s.get("name")], labels, "docker/build-push-action")


def _clean(text: str) -> str:
    return text.strip().strip("'\"")


# ------------------------------------------------------------------ metadata-action


_SEMVER = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?$")


def metadata(config: dict, scenario: Scenario) -> dict:
    """docker/metadata-action's tags and labels for a push of `scenario.ref` (v5 semantics, the subset
    a tag push can produce: semver, pep440, match, ref, raw, sha; edge and schedule never fire)."""
    images = []
    for line in _list_input(config.get("images")):
        attrs = dict(part.partition("=")[::2] for part in line.split(",")) if "=" in line else {"name": line}
        if attrs.get("enable", "true").strip().lower() != "false":
            images.append(attrs.get("name", line).strip())
    flavor = {}
    for line in _list_input(config.get("flavor")):
        key, _, value = line.partition("=")
        flavor[key.strip().lower()] = value.strip()
    tag_name = scenario.ref_name if scenario.ref_type == "tag" else None
    values, latest_auto = [], False
    lines = [l.strip() for l in str(config.get("tags") or "type=schedule\ntype=ref,event=branch\n"
                                     "type=ref,event=tag\ntype=ref,event=pr").split("\n") if l.strip()]
    for line in lines:
        attrs = {}
        for part in line.split(","):
            key, eq, value = part.partition("=")
            attrs[key.strip().lower()] = value.strip() if eq else ""
        if attrs.get("enable", "true").lower() == "false":
            continue
        kind = attrs.get("type", "raw")
        value = None
        semver = _SEMVER.match(tag_name) if tag_name else None
        if kind in ("semver", "pep440") and semver:
            major, minor, patch, pre = semver.groups()
            pattern = attrs.get("pattern", "{{version}}")
            version = f"{major}.{minor}.{patch}" + (f"-{pre}" if pre else "")
            if pre and "{{version}}" not in pattern and "{{raw}}" not in pattern:
                continue
            value = (attrs.get("value") or pattern).replace("{{version}}", version).replace("{{raw}}", tag_name) \
                .replace("{{major}}", major).replace("{{minor}}", minor).replace("{{patch}}", patch)
            latest_auto = latest_auto or not pre
        elif kind == "match" and tag_name:
            found = re.search(attrs.get("pattern", ""), tag_name)
            if found:
                value = found.group(int(attrs.get("group", "0") or 0))
                latest_auto = True
        elif kind == "ref" and attrs.get("event") == "tag" and tag_name:
            value, latest_auto = tag_name, True
        elif kind == "raw" and "value" in attrs:
            value = attrs["value"]
        elif kind == "sha":
            value = "sha-" + SHA_VALUE
        if value is None:
            continue
        values.append(attrs.get("prefix", flavor.get("prefix", "")) + value + attrs.get("suffix", flavor.get("suffix", "")))
    latest = flavor.get("latest", "auto").lower()
    if latest == "true" or (latest == "auto" and latest_auto):
        values.append("latest")
    tags = [f"{image}:{value}" for image in images for value in values] if images else values
    labels = {
        "org.opencontainers.image.source": "https://github.com/" + marker("github.repository"),
        "org.opencontainers.image.url": "https://github.com/" + marker("github.repository"),
        "org.opencontainers.image.revision": SHA_VALUE,
        "org.opencontainers.image.licenses": marker("repository licence"),
    }
    return {"tags": tags, "labels": labels}


# ------------------------------------------------------------------ the job reader


@lru_cache(maxsize=None)
def _view_cached(job_id: str, combo_key: str, scenario: Scenario, path: str, mtime: float) -> JobView:
    workflow = _load(Path(path))
    combo = json.loads(combo_key)
    return _read_job(workflow, job_id, combo, scenario)


def local_target(uses: str) -> Path | None:
    """The file a local `uses: ./...` names: a reusable workflow, or a directory's action.yml."""
    if not str(uses).startswith("./"):
        return None
    path = REPO_ROOT / str(uses)[2:].split("@", 1)[0]
    if path.is_file():
        return path
    for name in ("action.yml", "action.yaml"):
        if (path / name).is_file():
            return path / name
    return None


@lru_cache(maxsize=None)
def _views_cached(job_id: str, combo_key: str, scenario: Scenario, path: str, mtime: float) -> tuple:
    workflow = _load(Path(path))
    job = workflow.jobs[job_id]
    called = local_target(job.get("uses", "")) if isinstance(job.get("uses"), str) else None
    if called is None:
        return (_view_cached(job_id, combo_key, scenario, path, mtime),)
    combo = json.loads(combo_key)
    inner = _load(called)
    ctx = _base_context(workflow, job, combo, scenario, None)
    caller_if = condition(job.get("if"), ctx)
    inputs = {}
    declared = ((((inner.data or {}).get("on") or (inner.data or {}).get(True) or {}) or {}).get("workflow_call") or {})
    for name, spec in ((declared.get("inputs") or {}) if isinstance(declared, dict) else {}).items():
        if isinstance(spec, dict) and "default" in spec:
            inputs[str(name)] = interpolate(spec["default"], ctx)
    inputs.update(interpolate(job.get("with") or {}, ctx) if isinstance(job.get("with"), dict) else {})
    out = []
    for nested_id, nested in inner.jobs.items():
        if not isinstance(nested, dict):
            continue
        for nested_combo in matrix_combinations(nested):
            view = _read_job(inner, str(nested_id), nested_combo, scenario, inputs=Context(inputs, name="inputs"))
            both = [caller_if, view.job_if]
            view.job_if = False if False in both else (None if None in both else True)
            view.nested, view.job_id, view.workflow = str(nested_id), job_id, workflow
            view.combo = {**combo, **nested_combo}
            out.append(view)
    return tuple(out)


def views(workflow: Workflow, scenario: Scenario, job_id: str | None = None) -> list[JobView]:
    """Every job (or one), for every matrix combination, read under the scenario. A job that calls a local
    reusable workflow is read as the jobs it calls (each with the caller's id, its `with:` as `inputs`, and the
    caller's `if` joined to its own)."""
    out = []
    for jid, job in workflow.jobs.items():
        if job_id is not None and jid != job_id:
            continue
        if not isinstance(job, dict):
            continue
        for combo in matrix_combinations(job):
            out.extend(_views_cached(str(jid), json.dumps(combo, sort_keys=True), scenario, str(workflow.path),
                                     workflow.path.stat().st_mtime))
    return out


def job_dicts(workflow: Workflow, job_id: str) -> list[dict]:
    """The job, and the jobs of the local reusable workflow it calls."""
    job = workflow.jobs.get(job_id) or {}
    called = local_target(job.get("uses", "")) if isinstance(job.get("uses"), str) else None
    nested = list(_load(called).jobs.values()) if called is not None and called.suffix in (".yml", ".yaml") and \
        called.name not in ("action.yml", "action.yaml") else []
    return [job, *[j for j in nested if isinstance(j, dict)]]


def _needs_context(workflow: Workflow, job: dict, scenario: Scenario) -> Context:
    needs = Context(closed=True, name="needs")
    for needed in needs_of(job):
        outputs = Context(name=f"needs.{needed}.outputs")
        target = workflow.jobs.get(needed)
        combos = matrix_combinations(target) if isinstance(target, dict) else [{}]
        if isinstance(target, dict) and len(combos) == 1 and not scenario.needs_failed:
            view = _view_cached(needed, json.dumps(combos[0], sort_keys=True), scenario, str(workflow.path),
                                workflow.path.stat().st_mtime)
            outputs = Context(view.outputs, name=f"needs.{needed}.outputs")
        needs[needed] = Context({"result": "failure" if scenario.needs_failed else "success", "outputs": outputs},
                                name=f"needs.{needed}")
    return needs


def _base_context(workflow: Workflow, job: dict, combo: dict, scenario: Scenario, runner: str | None,
                  inputs: Context | None = None) -> dict:
    failed = scenario.needs_failed and bool(needs_of(job))
    status = ({"success": False, "failure": True, "always": True, "cancelled": False} if failed
              else {"success": True, "failure": False, "always": True, "cancelled": False})
    return {
        "github": scenario.github(),
        "matrix": Context(combo, closed=bool(combo) or not (job.get("strategy") or {}).get("matrix"), name="matrix"),
        "needs": _needs_context(workflow, job, scenario),
        "inputs": inputs if inputs is not None else Context(name="inputs"),
        "vars": Context(name="vars"),
        "secrets": Context({"GITHUB_TOKEN": TOKEN_VALUE}, name="secrets"),
        "runner": Context({"arch": {seams.ARM64_RUNNER: "ARM64", seams.AMD64_RUNNER: "X64"}.get(runner or "")
                           or Unknown("runner.arch"), "os": "Linux", "temp": "@runner.temp@"}, name="runner"),
        "strategy": Context(name="strategy"),
        "job": Context(name="job"),
        "env": Context(closed=True, name="env"),
        "steps": Context(closed=True, name="steps"),
        "__status__": status,
    }


def resolve_runner(workflow: Workflow, job: dict, combo: dict, scenario: Scenario, inputs: Context | None = None) -> str:
    runs_on = job.get("runs-on")
    ctx = _base_context(workflow, {**job, "needs": []}, combo, scenario, None, inputs)
    if isinstance(runs_on, list):
        runs_on = ",".join(str(r) for r in runs_on)
    return to_text(interpolate(str(runs_on), ctx)) if runs_on is not None else marker("runs-on")


def _read_job(workflow: Workflow, job_id: str, combo: dict, scenario: Scenario, inputs: Context | None = None) -> JobView:
    job = workflow.jobs[job_id]
    runner = resolve_runner(workflow, job, combo, scenario, inputs)
    ctx = _base_context(workflow, job, combo, scenario, runner, inputs)
    job_if = condition(job.get("if"), ctx)
    env = dict(ctx["env"])
    for scope in ((workflow.data or {}).get("env"), job.get("env")):
        if isinstance(scope, dict):
            for key, value in scope.items():
                ctx["env"] = Context(env, closed=True, name="env")
                env[str(key)] = to_text(interpolate(value, ctx))
    ctx["env"] = Context(env, closed=True, name="env")
    defaults = ((job.get("defaults") or {}).get("run") or {}) if isinstance(job.get("defaults"), dict) else {}
    top_defaults = (((workflow.data or {}).get("defaults") or {}).get("run") or {})
    default_dir = _norm(defaults.get("working-directory") or top_defaults.get("working-directory") or ".")
    acts: list[Act] = []
    texts: list[tuple[int, str, str]] = []
    step_conditions = []
    persistent = dict(env)  # env + GITHUB_ENV writes
    local_targets: dict[str, str] = {}
    metadata_of: dict[str, dict] = {}
    last_metadata: dict | None = None
    steps = job.get("steps") if isinstance(job.get("steps"), list) else []
    queue = [(float(i), s if isinstance(s, dict) else {}, None) for i, s in enumerate(steps)]
    while queue:
        index, step, composite_inputs = queue.pop(0)
        if composite_inputs is not None:
            ctx["inputs"] = composite_inputs
        elif inputs is not None:
            ctx["inputs"] = inputs
        else:
            ctx["inputs"] = Context(name="inputs")
        action = local_target(step.get("uses", "")) if isinstance(step.get("uses"), str) else None
        if action is not None and action.name in ("action.yml", "action.yaml"):
            definition = _load(action).data or {}
            runs = definition.get("runs") or {}
            if isinstance(runs, dict) and runs.get("using") == "composite" and isinstance(runs.get("steps"), list):
                ctx["env"] = Context(persistent, closed=True, name="env")
                if condition(step.get("if"), ctx) is False:
                    continue  # GitHub never runs a step whose condition is false in this scenario
                given = {}
                for name, spec in (definition.get("inputs") or {}).items():
                    if isinstance(spec, dict) and "default" in spec:
                        given[str(name)] = to_text(interpolate(spec["default"], ctx))
                if isinstance(step.get("with"), dict):
                    given.update({str(k): to_text(interpolate(v, ctx)) for k, v in step["with"].items()})
                nested = [(index + (n + 1) / 1000.0, sub if isinstance(sub, dict) else {}, Context(given, name="inputs"))
                          for n, sub in enumerate(runs["steps"])]
                queue[:0] = nested
                continue
        ctx["env"] = Context(persistent, closed=True, name="env")
        step_env = dict(persistent)
        if isinstance(step.get("env"), dict):
            for key, value in step["env"].items():
                step_env[str(key)] = to_text(interpolate(value, ctx))
        ctx["env"] = Context(step_env, closed=True, name="env")
        step_conditions.append(condition(step.get("if"), ctx))
        if step_conditions[-1] is False:
            continue  # GitHub never runs a step whose condition is false in this scenario
        step_id = str(step.get("id")) if step.get("id") is not None else None
        uses = str(step.get("uses") or "").strip()
        uses_name = uses.split("@", 1)[0].lower()
        config = interpolate(step.get("with") or {}, ctx) if isinstance(step.get("with"), dict) else {}
        texts.append((index, "uses", json.dumps({"uses": uses, "with": config, "name": step.get("name")}, default=str)))
        outputs: dict[str, str] = {}
        if uses:
            if uses_name == "docker/login-action" or "login" in uses_name:
                acts.append(Act("login", index, {"registry": to_text(config.get("registry", "docker.io")),
                                                 "uses": uses}))
            if uses_name == "docker/build-push-action":
                build = _action_build(config)
                _record_build(acts, index, build, local_targets)
                outputs["digest"] = marker(f"steps.{step_id}.outputs.digest")
            if uses_name in ("docker/setup-qemu-action", "tonistiigi/binfmt"):
                acts.append(Act("emulation", index, {"uses": uses}))
            if uses_name == "docker/metadata-action":
                last_metadata = metadata(config, scenario)
                if step_id:
                    metadata_of[step_id] = last_metadata
                outputs["tags"] = "\n".join(last_metadata["tags"])
                outputs["labels"] = "\n".join(f"{k}={v}" for k, v in last_metadata["labels"].items())
                outputs["json"] = json.dumps(last_metadata)
                persistent["DOCKER_METADATA_OUTPUT_TAGS"] = outputs["tags"]
                persistent["DOCKER_METADATA_OUTPUT_JSON"] = outputs["json"]
            if uses_name in _RELEASE_ACTIONS:
                acts.append(Act("release", index, {"uses": uses, "with": config}))
            if uses_name in _ATTEST_ACTIONS or "attest" in uses_name:
                acts.append(Act("attest", index, {"uses": uses}))
            if uses_name == "actions/setup-node":
                acts.append(Act("setup-node", index, {"node-version": to_text(config.get("node-version", ""))}))
            if uses_name == "actions/checkout":
                acts.append(Act("checkout", index, {"ref": to_text(config.get("ref", ""))}))
            if any(word in uses_name for word in ("publish", "push-to", "upload-release", "gh-release")) and not \
                    any(a.step == index for a in acts):
                acts.append(Act("registry-write", index, {"uses": uses}))
        script = step.get("run")
        if isinstance(script, str):
            text = to_text(interpolate(script, ctx))
            texts.append((index, "run", text))
            cwd = _norm(step["working-directory"]) if step.get("working-directory") else default_dir
            variables = {**scenario.default_env(runner), **step_env}
            for command in interpret(text, variables, cwd):
                _read_command(acts, index, command, local_targets, last_metadata, outputs, persistent)
            if "AGENT_TOOLING_TEST_REFERENCE_BYTES" in text:
                cases = {m.group(1): int(m.group(2)) for m in
                         re.finditer(r"([A-Za-z0-9_]+/[A-Za-z0-9_]+)\)\s*bytes=(\d+)", text)}
                acts.append(Act("size-reference", index, {"cases": cases}))
        if step_id:
            steps_ctx = dict(ctx["steps"])
            steps_ctx[step_id] = Context({"outputs": Context(outputs, name=f"steps.{step_id}.outputs"),
                                          "outcome": "success", "conclusion": "success"}, name=f"steps.{step_id}")
            ctx["steps"] = Context(steps_ctx, closed=True, name="steps")
    ctx["env"] = Context(persistent, closed=True, name="env")
    job_outputs = {}
    if isinstance(job.get("outputs"), dict):
        for key, value in job["outputs"].items():
            job_outputs[str(key)] = to_text(interpolate(value, ctx))
    return JobView(workflow, job_id, combo, scenario, runner, job_if, acts, texts, step_conditions, job_outputs, job)


def _record_build(acts: list[Act], index: int, build: _Build, local_targets: dict) -> None:
    detail = {"target": build.target, "file": build.file, "tags": build.tags, "build_args": build.build_args,
              "platforms": build.platforms, "by_digest": build.by_digest, "names": build.names,
              "labels": build.labels, "via": build.via, "push": build.push, "load": build.load}
    acts.append(Act("build", index, detail))
    for tag in build.tags:
        local_targets[tag] = build.target
    if build.push is not False:
        destinations = build.names + [t for t in build.tags]
        acts.append(Act("push", index, {**detail, "destinations": destinations or [marker("destination")],
                                        "certain": build.push is True}))


def _read_command(acts: list[Act], index: int, command: ShellCommand, local_targets: dict, last_metadata,
                  outputs: dict, persistent: dict) -> None:
    words = command.words
    if not words:
        return
    head = posixpath.basename(words[0])
    line = " ".join(words)
    for target in command.redirects:
        if target in ("@GITHUB_ENV@", "@GITHUB_OUTPUT@") and head in ("echo", "printf"):
            payload = " ".join(w for w in words[1:] if w not in ("-n", "-e"))
            for item in payload.split("\\n"):
                key, eq, value = item.partition("=")
                if eq and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", key.strip()):
                    (persistent if target == "@GITHUB_ENV@" else outputs)[key.strip()] = value
            return
    if _LOGIN_COMMAND.search(line):
        acts.append(Act("login", index, {"command": line[:200]}))
        return
    if head == "docker":
        build = _docker_cli_build(words, command.cwd)
        if build is not None:
            _record_build(acts, index, build, local_targets)
            return
        rest = [w for w in words[1:]]
        verbs = [w for w in rest if not w.startswith("-")]
        if verbs[:1] == ["image"]:
            verbs = verbs[1:]
        if verbs[:3] == ["buildx", "imagetools", "create"]:
            args = rest[rest.index("create") + 1:]
            tags, sources, j = [], [], 0
            while j < len(args):
                arg = args[j]
                name, eq, value = arg.partition("=")
                if name in ("-t", "--tag"):
                    tags.append(value if eq else (args[j + 1] if j + 1 < len(args) else ""))
                    j += 1 if eq else 2
                    continue
                if name in ("-f", "--file", "--builder", "--annotation", "--progress"):
                    j += 1 if eq else 2
                    continue
                if not arg.startswith("-"):
                    sources.append(arg)
                j += 1
            acts.append(Act("manifest", index, {"tags": tags, "sources": sources, "via": "imagetools create"}))
            if tags:
                acts.append(Act("tag", index, {"tags": tags, "via": "imagetools create"}))
            return
        if verbs[:2] == ["manifest", "create"]:
            positional = [w for w in rest[rest.index("create") + 1:] if not w.startswith("-")]
            acts.append(Act("manifest", index, {"tags": positional[:1], "sources": positional[1:],
                                                "via": "docker manifest create"}))
            acts.append(Act("tag", index, {"tags": positional[:1], "via": "docker manifest create"}))
            return
        if verbs[:2] == ["manifest", "push"]:
            acts.append(Act("manifest", index, {"tags": [w for w in rest if not w.startswith("-")][2:3],
                                                "sources": [], "via": "docker manifest push"}))
            return
        if verbs[:1] == ["push"]:
            ref = next((w for w in rest[rest.index("push") + 1:] if not w.startswith("-")), "")
            acts.append(Act("push", index, {"destinations": [ref], "target": local_targets.get(ref),
                                            "by_digest": False, "tags": [ref], "via": "docker push",
                                            "certain": True}))
            return
        if verbs[:1] == ["pull"]:
            ref = next((w for w in rest[rest.index("pull") + 1:] if not w.startswith("-")), "")
            acts.append(Act("pull", index, {"ref": ref}))
            return
        if verbs[:1] == ["tag"]:
            positional = [w for w in rest[rest.index("tag") + 1:] if not w.startswith("-")]
            if len(positional) >= 2:
                local_targets[positional[1]] = local_targets.get(positional[0])
            return
        return
    if head in ("crane", "gcrane", "regctl", "skopeo", "oras"):
        verb = next((w for w in words[1:] if not w.startswith("-")), "")
        if verb in ("push", "copy", "cp", "tag", "mutate", "append", "index", "image", "rebase", "delete"):
            acts.append(Act("registry-write", index, {"command": line[:200]}))
            if verb == "tag":
                positional = [w for w in words[2:] if not w.startswith("-")]
                acts.append(Act("tag", index, {"tags": positional[1:2] or positional[:1], "via": head}))
        return
    if head == "cosign" and any(w in ("sign", "attest", "attach") for w in words[1:3]):
        acts.append(Act("attest", index, {"command": line[:200]}))
        return
    if head == "gh":
        if words[1:2] == ["release"] and len(words) > 2 and words[2] in ("create", "upload", "edit"):
            acts.append(Act("release", index, {"command": words}))
        elif words[1:2] == ["api"] and "releases" in line and re.search(r"(-X|--method)\s*(POST|PATCH|PUT)|\s-[fF]\s", line):
            acts.append(Act("release", index, {"command": words}))
        elif words[1:2] == ["attestation"] and "verify" not in words:
            acts.append(Act("attest", index, {"command": words}))
        return
    if head in ("curl", "wget") and re.search(r"(?:api|uploads)\.github\.com|ghcr\.io", line) and \
            re.search(r"-X\s*(POST|PUT|PATCH)|--request\s*(POST|PUT|PATCH)|--data|-d\s|-F\s|--upload-file|-T\s", line):
        acts.append(Act("registry-write", index, {"command": line[:200]}))
        return
    python = re.fullmatch(r"python[0-9.]*", head) is not None
    if python and len(words) > 1 and words[1].endswith("agent_sdk_absence.py"):
        acts.append(Act("sdk-scan", index, {"args": words[2:], "script": _norm(words[1], command.cwd)}))
        return
    if python and len(words) > 1 and words[1].endswith("check_ops_variant.py"):
        acts.append(Act("ops-check", index, {"args": words[2:], "env": command.variables}))
        return
    invocation = _pytest(words, command.cwd)
    if invocation is not None:
        acts.append(Act("pytest", index, {**invocation, "env": command.variables}))
        return
    if head == "npm":
        prefix, rest, j = None, [], 1
        while j < len(words):
            if words[j] in ("--prefix", "-C") and j + 1 < len(words):
                prefix, j = words[j + 1], j + 2
                continue
            if words[j].startswith("--prefix="):
                prefix = words[j].split("=", 1)[1]
            else:
                rest.append(words[j])
            j += 1
        positional = [w for w in rest if not w.startswith("-")]
        if positional[:1] and positional[0] in ("ci", "clean-install", "install-clean"):
            acts.append(Act("npm-ci", index, {"directory": _norm(prefix, command.cwd) if prefix else command.cwd,
                                              "args": rest}))


_PYTEST_VALUE_OPTIONS = {"-k", "-p", "-c", "-o", "-W", "-n", "--basetemp", "--rootdir", "--confcutdir", "--junitxml",
                         "--junit-xml", "--maxfail", "--tb", "--durations", "--deselect", "--ignore", "--ignore-glob",
                         "--timeout", "--log-level", "--override-ini", "--import-mode", "--report-log", "-r"}


def _pytest(words: list[str], cwd: str) -> dict | None:
    start = None
    for i, word in enumerate(words):
        if posixpath.basename(word) in ("pytest", "py.test"):
            start = i
            break
        if word == "-m" and i + 1 < len(words) and words[i + 1] == "pytest" and i > 0 and \
                re.fullmatch(r"python[0-9.]*", posixpath.basename(words[i - 1])):
            start = i + 1
            break
    if start is None:
        return None
    args, targets, markexpr, options, j = words[start + 1:], [], None, [], 0
    while j < len(args):
        arg = args[j]
        name, eq, _ = arg.partition("=")
        if arg == "-m":
            markexpr = args[j + 1] if j + 1 < len(args) else ""
            j += 2
        elif arg.startswith("-m") and not arg.startswith("--") and len(arg) > 2:
            markexpr, j = arg[2:], j + 1
        elif arg.startswith("-"):
            options.append(name)
            j += 2 if (name in _PYTEST_VALUE_OPTIONS and not eq) else 1
        else:
            targets.append(_norm(arg.split("::", 1)[0], cwd))
            j += 1
    return {"targets": targets or [cwd], "markexpr": markexpr, "options": options,
            "node_filters": [a for a in args if "::" in a], "args": list(args), "cwd": cwd}


# ======================================================================= classification


PUBLISHING_ACTS = ("login", "manifest", "tag", "attest", "release", "registry-write")


def remote_pushes(view: JobView) -> list[Act]:
    return [a for a in view.of("push") if any(not is_local_registry(d) for d in a.detail["destinations"])]


def publishing_reasons(workflow: Workflow, job_id: str) -> list[str]:
    """Why a job publishes, over every scenario (a step whose condition is false in all of them never runs)."""
    reasons = set()
    job = workflow.jobs.get(job_id) or {}
    permissions = effective_permissions(workflow, job)
    if isinstance(permissions, str):
        if permissions != "read-all":
            reasons.add(f"permissions {permissions!r}")
    else:
        writes = sorted(k for k, v in (permissions or {}).items() if str(v).lower() == "write")
        if writes:
            reasons.add(f"write permission {writes}")
    for scenario in ALL_SCENARIOS:
        for view in views(workflow, scenario, job_id):
            for act in view.acts:
                if act.kind in PUBLISHING_ACTS:
                    reasons.add(act.kind)
            for act in remote_pushes(view):
                reasons.add("push to " + ", ".join(sorted(set(d for d in act.detail["destinations"]
                                                             if not is_local_registry(d)))))
    return sorted(reasons)


def publishing_jobs(workflow: Workflow) -> dict[str, list[str]]:
    return {jid: why for jid in workflow.jobs if (why := publishing_reasons(workflow, jid))}


def step3_jobs(workflow: Workflow) -> list[str]:
    """Publishing jobs that create a manifest list, a tag, an attestation or a release (R6 step 3)."""
    found = []
    for jid in publishing_jobs(workflow):
        kinds = {a.kind for s in ALL_SCENARIOS for v in views(workflow, s, jid) for a in v.acts}
        if kinds & {"manifest", "tag", "attest", "release"}:
            found.append(jid)
    return found


def effective_permissions(workflow: Workflow, job: dict):
    """The job's `permissions`, else the workflow's; None when neither is set (the repository default)."""
    if isinstance(job, dict) and "permissions" in job:
        return job["permissions"] if job["permissions"] is not None else {}
    data = workflow.data or {}
    return data.get("permissions") if "permissions" in data else None


def image_suite_runs(view: JobView, *, suites=("tests/image", "tests/install", "tests/host")) -> list[Act]:
    """pytest acts in a job that select the image marker over one of the suites."""
    found = []
    for act in view.of("pytest"):
        expr = (act.detail.get("markexpr") or "").strip()
        if re.search(r"\bimage\b", expr) and not re.search(r"\bnot\s+image\b", expr) and \
                any(t == s or t.startswith(s + "/") for t in act.detail["targets"] for s in suites):
            found.append(act)
    return found


def view_runs_suite(view: JobView, suite: str) -> list[Act]:
    return [a for a in image_suite_runs(view, suites=(suite,))]


def describe(views_: list[JobView]) -> str:
    rows = []
    for view in views_:
        rows.append(f"  {view.label} [if={view.job_if}]: " + ", ".join(
            f"{a.kind}@{a.step}" + (f"({a.detail.get('target')})" if a.kind in ("build", "push") else "")
            for a in view.acts))
    return "\n".join(rows) or "  (no jobs)"
