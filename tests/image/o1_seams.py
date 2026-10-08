"""The seams the O1 real-binary tests assume, named once (order docs/work/orders/O1-opencode-board-launch.md,
A1 and amendment-1). The TEST arm writes these blind to FEATURE; the meet reconciles each with FEATURE's
names by editing this file (and apps/kanban/test/runtime/terminal/o1-seams.ts) only.

- `IMAGE_VARIABLE`: the image is O3's `opencode` target, named by O3's own variable
  (`opencode_pin.OPENCODE_IMAGE_VARIABLE`, AGENT_TOOLING_TEST_IMAGE_OPENCODE), built from the tree under test.
- `RUNNER`: the launch is the board's. `apps/kanban/test/runtime/terminal/o1-opencode-image-runner.ts` drives
  `TerminalSessionManager.startTaskSession` (the real adapter and the real environment composition); the
  test bundles it from this tree with apps/kanban's esbuild (so `npm ci` in apps/kanban is a precondition)
  and runs it with the image's node. node-pty is the image's own (`/app/node_modules`).
- `STUB_PROVIDER_DIR`: the model is a stub OpenAI-compatible provider on loopback inside the container.
  Its provider entry and default model reach OpenCode through the managed config directory, measured at the
  pin to be `/etc/opencode` on Linux (`opencode debug config` merges a file there). That file carries no
  `permission` key. It is the one channel that cannot be FEATURE's mechanism: the board cannot write it.
- "The card moves to review" is the Kanban plugin running the board's hook command with
  `hooks ingest --event to_review`. That command is `process.execPath` plus `process.argv[1]`, so inside the
  container it re-enters the runner, which records it instead of posting to a board server.
- The instrument for ask/allow/deny is `opencode run --format json` without `--auto` (it auto-rejects every
  ask), run with the launch's own environment, cwd and files, and its argv minus `--prompt <text>`. One
  translation, measured at the pin: the TUI takes its project directory from the process cwd, but `run`
  takes it from $PWD when PWD is set, and the board's environment carries the board's own PWD; so the
  instrument sets PWD to the launch's cwd (the directory the TUI works in). The
  classifications are OpenCode's own tool errors at the pin, held by the instrument controls:
  ASK_ERROR for an ask that was rejected, DENY_ERROR for a rule that denies. A side effect (the bash
  marker, the stub's fetch log, the external file) is an allow whatever the text says.
"""
from __future__ import annotations

from opencode_pin import OPENCODE_IMAGE_VARIABLE

IMAGE_VARIABLE = OPENCODE_IMAGE_VARIABLE
RUNNER = "apps/kanban/test/runtime/terminal/o1-opencode-image-runner.ts"
ESBUILD = "apps/kanban/node_modules/.bin/esbuild"
STUB_PROVIDER_DIR = "/etc/opencode"
STUB_PROVIDER_CONFIG = {
    "provider": {
        "o1stub": {
            "npm": "@ai-sdk/openai-compatible",
            "name": "o1 stub",
            "options": {"baseURL": "http://127.0.0.1:18431/v1", "apiKey": "o1-stub-placeholder"},
            "models": {"stub-model": {"name": "o1 stub model", "tool_call": True}},
        }
    },
    "model": "o1stub/stub-model",
    "small_model": "o1stub/stub-model",
}
# The environment of the host the board runs on, added to the container (never to the launch): with no
# network, OpenCode's background install of @opencode-ai/plugin into a config directory waits about 70 s
# per launch on npm's fetch retries before it fails and carries on (measured at the pin). No retries makes
# it fail at once; nothing else changes.
BOARD_HOST_ENV = {"npm_config_fetch_retries": "0"}
ASK_ERROR = "The user rejected permission"
DENY_ERROR = "The user has specified a rule which prevents"

# P1, verbatim.
POLICY = {"edit": "allow", "bash": "ask", "webfetch": "ask", "external_directory": "deny"}
# What the effective policy must do on the real binary (A1): bash and webfetch ask, an external write is denied.
REQUIRED = {"bash": "ask", "webfetch": "ask", "write": "deny"}

# The permission keys OpenCode knows at the pin (the O2 read, section 5), for the "every key" shape.
KNOWN_PERMISSION_KEYS = (
    "read", "edit", "glob", "grep", "list", "bash", "task", "external_directory", "todowrite", "question",
    "webfetch", "websearch", "lsp", "doom_loop", "skill",
)

# A1 and amendment-1 (a): the loosening shapes.
SHAPES = {
    "every-key-allow": {key: "allow" for key in KNOWN_PERMISSION_KEYS},
    "star-allow": {"*": "allow"},
    "bash-then-star-allow": {"bash": "allow", "*": "allow"},
    "bash-pattern-map-allow": {"bash": {"*": "allow"}},
}

# Where a loosening file sits: the task worktree's project file, and every HOME path
# apps/kanban/src/terminal/opencode-paths.ts lists for the config (test_home_placements_cover_opencode_paths
# holds this list equal to that file's).
PROJECT_FILE = ("project", "opencode.json")
HOME_FILES = (
    ("home", ".config/opencode/config.json"),
    ("home", ".config/opencode/opencode.jsonc"),
    ("home", ".config/opencode/opencode.json"),
    ("home", ".opencode/opencode.jsonc"),
    ("home", ".opencode/opencode.json"),
)
PLACEMENTS = {
    "project": (PROJECT_FILE,),
    **{f"home:{path}": ((base, path),) for base, path in HOME_FILES},
    "project+every-home-file": (PROJECT_FILE, *HOME_FILES),
}
