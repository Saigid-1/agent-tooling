"""The names D0a-2's tests assume, named once (order docs/work/orders/D0a2-readme-and-harnesses.md).

The TEST arm wrote these blind to FEATURE. The meet reconciles each SEAM with FEATURE's names by
editing this file only, never an assertion. The second part holds what the order (or an earlier
order's merged tree) states: file paths and the symbols the F1 facts are read from. Those change
only with the order or the tree, and are kept here so that no test restates them.

Seams (assumptions about FEATURE's words):
- `README_STANCE_TITLE`: the README section that carries the release stance (P2: a "Release
  stance" section). Matched against the heading's text, case-insensitive, emphasis removed.
- `README_FIRST_RUN_TITLE`: the README section(s) that point a stranger at the first run (P1: "first
  run, as a LINK to DOCKER.md"). A regex searched in each heading's text.
- `HARNESS_ENTRIES`: the five HARNESSES.md entries of P3, each a regex searched in a heading's text
  (level 2 or deeper). Each heading serves at most one entry; the first heading that matches wins.
- `HARNESS_OPENCODE_ENTRY`: the entry F6 reads (a key of `HARNESS_ENTRIES`).
- `INSTALL_POINTER`: what counts as an entry's install pointer (P3: "how the user installs it
  themselves (or that it ships in the optional image)").
- `STATUS`: what counts as an entry's status (P3: "its status, Built or not, never 'supported'
  without Built"): the word Status, then within three words the word Built.
- `DOCKER_SEARCH_ANSWERS`: the words the text after the search call uses for its result (P4, F7).
- `UNBOUND_EGRESS`: the three things F6's sentence names (P3: "an unbound OpenCode launch reaches the
  npm registry for OpenCode's own plugin install"): an unbound launch, npm, and a registry.
"""
from __future__ import annotations

# ----------------------------------------------------------------- seams

README_STANCE_TITLE = "Release stance"
README_FIRST_RUN_TITLE = r"(?i)\bfirst run\b"

HARNESS_ENTRIES = {
    "Claude Code": r"(?i)^(?!.*\bcodex\b).*\bclaude code\b",
    "Codex": r"(?i)^(?!.*\bclaude\b).*\bcodex\b",
    "OpenCode": r"(?i)\bopencode\b",
    "the model gateway": r"(?i)\bgateway\b",
    "the in-board Cline agent and the Claude Agent SDK": r"(?i)\bcline\b|\bagent sdk\b|\bin-board\b|^claude$",
}
HARNESS_OPENCODE_ENTRY = "OpenCode"

INSTALL_POINTER = r"(?i)\binstall\w*|\bships?\b|\bshipped\b|\bpull\b"
STATUS = r"(?i)\bstatus\b[\s*_:.—–-]*(?:[\w'()-]+\s+){0,3}built\b"

DOCKER_SEARCH_ANSWERS = "a search that answers"

UNBOUND_EGRESS = (r"(?i)\bunbound\b", r"(?i)\bnpm", r"(?i)\bregistry\b")

# ----------------------------------------------------------------- stated by the order (or the tree)

README = "README.md"
HARNESSES = "docs/HARNESSES.md"
DOCKER = "docs/DOCKER.md"
RELEASE_STANCE = "docs/RELEASE-STANCE.md"  # D0b's; the README copies it (P2)

# F7: the first run's own steps run from D0b's heading to the board-tasks subsection's heading.
DOCKER_FIRST_RUN_HEADING = "First run from an empty root"
DOCKER_BOARD_TASKS_HEADING = "Board tasks with in-container agents"
SEARCH_CALL = "memory.search"

# F3: the commands a README first-run section must not restate.
RESTATED_TOOLS = ("kp-agent-install", "docker compose", "docker-compose")

# F8: where the tree pins each version.
OPENCODE_LOCK = "deploy/image/opencode/package-lock.json"
OPENCODE_PACKAGE = "opencode-ai"
OPENCODE_ARG = "OPENCODE_VERSION"
DOCKERFILE = "deploy/Dockerfile"
AGENTS_LOCK = "deploy/image/agents/package-lock.json"
CLAUDE_CODE_PACKAGE = "@anthropic-ai/claude-code"
CODEX_PACKAGE = "@openai/codex"
# Pins F8 also honours when a doc names them (the tree's pin, not one the order lists).
KANBAN_LOCK = "apps/kanban/package-lock.json"
AGENT_SDK_PACKAGE = "@anthropic-ai/claude-agent-sdk"
AGENT_SDK_ADAPTER_PACKAGE = "ai-sdk-provider-claude-code"
PROJECT_PYPROJECTS = ("packages/tooling/pyproject.toml", "extensions/ops/pyproject.toml")

# F1: the symbols each fact is read from.
O2_HOST_REFUSAL_TEST = ("tests/launch/test_o2_r2_opencode_prepare.py::"
                        "test_a_host_launch_of_opencode_is_refused_before_anything_is_written")
OPENCODE_HARNESS = "opencode"
SUMMARIZER_SERVICE = "summarizer"
COMPOSE_ASSET = "assets/deploy/compose.yaml"  # package data of kp_agent_tooling
K1_SERVER = "apps/kanban/src/server/runtime-server.ts"
K1_RUNTIME = "apps/kanban/src/cline-sdk/cline-session-runtime.ts"
D0F_INSTALL_ROUTER = "apps/kanban/src/trpc/app-router.ts"
D0F_INSTALL_PROCEDURE = "installClaudeAgentSdk"
KANBAN_PACKAGE = "apps/kanban/package.json"
