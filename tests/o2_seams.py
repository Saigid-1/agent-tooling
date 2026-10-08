"""O2 seams: every name and reading the O2 TEST arm assumes, written ONCE, here and nowhere else.

Order: docs/work/orders/O2-opencode-third-harness.md (frozen 2026-10-06). The TEST arm wrote these blind to
FEATURE. The meet reconciles each seam with FEATURE's spelling by editing THIS module only. Every O2 test
reads its assumptions from here: the Python tests import it, the board tests (vitest) read the same values
through `python3 tests/o2_seams.py --json`, and the image tests hand them to the in-container runner.

Fixed by the order (not seams, listed so the meet can tell them apart):
- the profile id is `opencode`, and the board asks for it (`harness: input.agentId`);
- the session id uses the existing `hook` strategy (R1);
- the hook events are exactly Stop (root `session.status` idle), PreCompact (`experimental.session.compacting`)
  and SessionEnd (the board terminal's exit); `CAPTURE_EVENTS` and the event pattern are unchanged (R1);
- the launched process carries OPENCODE_DISABLE_AUTOUPDATE=1 and OPENCODE_DISABLE_SHARE=1 (R2);
- the export child runs with external plugins off (`--pure` / OPENCODE_PURE) and the R2 disable flags (A2);
- no agent-tooling source names `opencode.db` or `OPENCODE_DB` (R3);
- the npm registry and `models.opencode.ai` are never contacted (A1).

Assumed (TEST, blind). Each item names where it is used.

O2-S1  PROFILE MEMBERS. No test spells FEATURE's new strategy members or its parser: every test reads them
       from the packaged `opencode` profile (`default_profiles()['opencode']`). Assumed only: the parser is
       at `capture.parser` and is a member of `harness_profiles.PARSERS` (the order: "PARSERS gains one
       parser"), and the session field the hook reads is the profile's `session_id.field` (F3, F4, F5).
O2-S2  HOOK CARRIER. The command the plugin runs for an event is the one the prepare output's
       `files.hook_settings` names for that event (the T3 A1 carrier: Claude-settings shape
       {"hooks": {"<Event>": [{"hooks": [{"type": "command", "command", "timeout"}]}]}}), run through
       `/bin/sh -c` in the workspace with the payload as JSON on stdin and the launch's environment
       (`env_additions` applied) (F3, F4).
O2-S3  STOP PAYLOAD. What the plugin knows at a root `session.status` idle: exactly
       {"hook_event_name": <event>, <profile session field>: <OpenCode session id>, "cwd": <workspace>} (F3, F4).
O2-S4  CAPTURE SOURCE. `opencode export <session id>` (the order's first-named source; A2 names the export
       child), the executable resolved on the hook's PATH. The host tests' fake `opencode` answers it with
       the shape measured on the pinned binary in TEST's scratch: {"info": Session.Info, "messages":
       [{"info": ..., "parts": [...]}]}, part ids `prt_…`, message ids `msg_…`, session ids `ses_…`; progress
       ("Exporting session: <id>") on stderr. `opencode --version` answers the pin (F3, F4, F5).
O2-S5  EVENT IDS. R3's "event ids derive from OpenCode's part ids" is read as: every captured event's
       `event_id` contains the id of the part it came from, verbatim (F3).
O2-S6  LEDGER AND QUEUE. The capture ledger's name is FEATURE's; tests observe capture only through the
       desk-memory MCP tools (`memory.list`, `memory.episode_directory`, `memory.read_event`,
       `memory.search`) of a memory config for that exact session, and the queue through
       `kp-agent-memory-queue status`, at the receipt's `queue` path (as rollout capture's) (F1, F3).
O2-S7  ONE JOB. "One queue job is enqueued" is read as exactly one job for the desk after the turn's Stop,
       and still exactly one after the board terminal exits (SessionEnd recaptures nothing new) (F1).
O2-S8  SEARCH BOUND. "Within T12b's bound" is T12b r4's "searchable within 2x the indexer's interval": the
       in-container indexer runs the compose command `kp-agent-desk indexer --root /state/memory --watch
       --interval 2`, stores sit under /state/memory with AGENT_MEMORY_VOLUME set (the board role's
       runtime), and the bound runs from the first poll that lists the episode to the first `memory.search`
       hit (polled every POLL_SECONDS) (F1).
O2-S9  THE BOARD LAUNCH. TerminalSessionManager.startTaskSession with `deskId` (T3 A2) and
       KANBAN_LAUNCH_BINDING_COMMAND = ["/usr/local/bin/kp-agent-launch", "--config", <registry operator
       config>] (the compose board role's), the real PTY (node-pty from the image) and the TUI with --prompt
       (F1, F2). On the host the PTY spawn is executed as a stub `opencode` (O1-S3) (F6).
O2-S10 STUB PROVIDER. As O1: OpenCode's managed config directory (`/etc/opencode`, read-only) carries the
       stub provider `o2stub` (`@ai-sdk/openai-compatible`, loopback port STUB_PORT) and the default model;
       it is the one channel FEATURE's mechanism cannot be (F1, F2).
O2-S11 NETWORK. The container runs with `--network none`; the npm registry and the models host resolve to
       127.0.0.1 through `--add-host`, where a recorder accepts on 443 and 80 and logs each connection with
       its TLS SNI or Host header, so an attempt is seen and fails. `--sysctl
       net.ipv4.ip_unprivileged_port_start=0` lets the image's user bind them. Measured on the pinned binary
       at base: a plain launch reaches both hosts at once (F1).
O2-S12 INSTALL ARTIFACTS. Secondary to O2-S11: a `node_modules`, `package.json`, `package-lock.json`,
       `bun.lock` or `bun.lockb` that appears during the run under the launch's HOME, the board runtime
       home, the registry state or /state/tmp. `.gitignore` is NOT counted: measured at the pin, OpenCode
       writes it into every writable config directory even when node_modules is present (F1).
O2-S13 PER-LAUNCH CARRIER (F2). The carrier is FEATURE's. The `kp_desk_memory` servers a launch "sees" are
       the `mcp` entries of `opencode debug config` run with the launch's own environment, its argv minus
       `--prompt <text>`, its cwd and PWD=cwd (O1's instrument translation); each local entry is started as
       OpenCode starts one (`command` array, the launch environment plus `environment`, the launch cwd) and
       asked `memory.connection_status` and `memory.bindings`. Measured at the pin: `debug config` prints
       `mcp` entries with their `environment` intact and redacts provider keys.
O2-S14 KEY FILE (F6). The operator's OpenRouter key file the model gateway uses, DOCKER.md's
       `/config/models/openrouter.key`, mounted read-only into the board role. In the image a sentinel key
       sits there; on the host the board is told of a sentinel file through KEY_FILE_VARIABLE (a guess;
       the meet names FEATURE's way, if any). A "provider key" is that file's content.
O2-S15 SOURCE GUARD (F4). "agent-tooling source" is every tracked file under SOURCE_ROOTS (product code,
       image and compose files, scripts and config; not tests or docs). Positive control: the files that
       name the `opencode` profile's parser are scanned.
O2-S16 OPEN-FILE AUDIT (F4). "agent-tooling's own process" is every Python process of the hook: a
       `sitecustomize` on PYTHONPATH installs an audit hook recording `open`, `os.open`, `sqlite3.connect`,
       `subprocess.Popen` and `os.exec*`/`os.posix_spawn` events. The fake `opencode` runs `python3 -I`, so
       the export child is not audited (it is the one allowed reader). Children of the hook that run
       `opencode` may only use EXPORT_SUBCOMMANDS.
O2-S17 BINDING ENV (F6). The board applies the binding's `env_additions` wholesale, as the Codex adapter does.
"""
from __future__ import annotations

import json
import sys

# ------------------------------------------------------------------------------------- fixed by the order
HARNESS = 'opencode'
SESSION_STRATEGY = 'hook'
EVENTS = ('Stop', 'PreCompact', 'SessionEnd')
CAPTURE_EVENTS = ('Stop', 'PreCompact', 'SessionEnd')
EVENT_PATTERN = r'^[A-Z][A-Za-z]{0,63}$'
DISABLE_ENV = {'OPENCODE_DISABLE_AUTOUPDATE': '1', 'OPENCODE_DISABLE_SHARE': '1'}
PURE_FLAG, PURE_ENV = '--pure', 'OPENCODE_PURE'
DB_NAMES = ('opencode.db', 'opencode.db-wal', 'opencode.db-shm')
DB_WORDS = ('opencode.db', 'OPENCODE_DB')
NPM_REGISTRY_HOST = 'registry.npmjs.org'
MODELS_HOST = 'models.opencode.ai'
# The existing profiles at the order's base, verbatim: "the existing claude and codex entries are unchanged".
BASE_PROFILES = {
    'claude': {'harness': 'claude', 'enabled': True, 'executable': 'claude',
               'session_id': {'strategy': 'mint', 'flag': '--session-id'},
               'mcp': {'strategy': 'config_file_flag', 'flag': '--mcp-config'},
               'hooks': {'strategy': 'settings_file_flag', 'flag': '--settings',
                         'events': ['Stop', 'PreCompact', 'SessionEnd']},
               'capture': {'mode': 'transcript', 'parser': 'claude-jsonl', 'root': '~/.claude/projects'},
               'receipt_env': 'KP_AGENT_LAUNCH_RECEIPT'},
    'codex': {'harness': 'codex', 'enabled': True, 'executable': 'codex',
              'session_id': {'strategy': 'hook', 'field': 'session_id'},
              'mcp': {'strategy': 'config_override', 'key': 'mcp_servers'},
              'hooks': {'strategy': 'config_override', 'events': ['UserPromptSubmit', 'Stop']},
              'capture': {'mode': 'transcript', 'parser': 'codex-rollout', 'root': '~/.codex/sessions'},
              'receipt_env': 'KP_AGENT_LAUNCH_RECEIPT'},
}
BASE_PARSERS = ('claude-jsonl', 'codex-rollout')
# The strategy members at the order's base, per slot (harness_profiles.py:50-114).
BASE_STRATEGIES = {'session_id': ('mint', 'hook'), 'mcp': ('config_file_flag', 'config_override'),
                   'hooks': ('settings_file_flag', 'config_override'), 'capture': ('none', 'transcript')}

# ------------------------------------------------------------------------------------------ O2-S1..S7
PARSER_FIELD = ('capture', 'parser')                   # O2-S1
HOOK_SETTINGS_FILE = 'hook_settings'                   # O2-S2: prepare output files.<this>
STOP_PAYLOAD_KEYS = ('hook_event_name', 'cwd')         # O2-S3, plus the profile's session field
EXPORT_SUBCOMMAND = 'export'                           # O2-S4
EXPORT_SUBCOMMANDS = ('export', '--version')           # O2-S16: what a child of the hook may ask opencode
PROGRESS_LINE = 'Exporting session: {session}'         # O2-S4: stderr, as measured
EVENT_ID_CONTAINS_PART_ID = True                       # O2-S5
RECEIPT_QUEUE_FIELD = 'queue'                          # O2-S6
QUEUE_SCRIPT = 'kp-agent-memory-queue'                 # O2-S6
ONE_JOB = 1                                            # O2-S7

# --------------------------------------------------------------------------------------------- O2-S8
INDEXER_INTERVAL_SECONDS = 2.0
SEARCH_BOUND_SECONDS = 2 * INDEXER_INTERVAL_SECONDS
POLL_SECONDS = 0.25
INDEXER_COMMAND = ['kp-agent-desk', 'indexer', '--root', '/state/memory', '--watch', '--interval', '2']
VOLUME_VARIABLE, VOLUME_VALUE = 'AGENT_MEMORY_VOLUME', 'o2-test-memory'
STORE_ROOT = '/state/memory'
CAPTURE_DEADLINE_SECONDS = 120                         # turn end to the episode, generous on a loaded host

# --------------------------------------------------------------------------------------------- O2-S9
LAUNCH_BINDING_VARIABLE = 'KANBAN_LAUNCH_BINDING_COMMAND'
IMAGE_LAUNCH_EXECUTABLE = '/usr/local/bin/kp-agent-launch'
BOARD_LAUNCH = {'agentId': 'opencode', 'binary': 'opencode', 'args': [], 'autonomousModeEnabled': True,
                'workspaceId': 'o2-workspace'}
RUNNER = 'apps/kanban/test/runtime/terminal/o2-test-image-runner.ts'
ESBUILD = 'apps/kanban/node_modules/.bin/esbuild'

# -------------------------------------------------------------------------------------------- O2-S10
STUB_PROVIDER_DIR = '/etc/opencode'
STUB_PORT = 18432
STUB_PROVIDER_CONFIG = {
    'provider': {'o2stub': {'npm': '@ai-sdk/openai-compatible', 'name': 'o2 stub',
                            'options': {'baseURL': f'http://127.0.0.1:{STUB_PORT}/v1',
                                        'apiKey': 'o2-stub-placeholder'},
                            'models': {'stub-model': {'name': 'o2 stub model', 'tool_call': True}}}},
    'model': 'o2stub/stub-model', 'small_model': 'o2stub/stub-model',
}
# O1's measured host environment for the board container (o1_seams.BOARD_HOST_ENV): no npm fetch retries.
BOARD_HOST_ENV = {'npm_config_fetch_retries': '0'}

# -------------------------------------------------------------------------------------- O2-S11, S12
RECORDER_PORTS = (443, 80)
UNPRIVILEGED_PORTS_SYSCTL = 'net.ipv4.ip_unprivileged_port_start=0'
INSTALL_ARTIFACTS = ('node_modules', 'package.json', 'package-lock.json', 'bun.lock', 'bun.lockb')

# -------------------------------------------------------------------------------------------- O2-S13
MCP_SERVER_NAME = 'kp_desk_memory'                     # launch_binding.SERVER_NAME at base, for messages only
DEBUG_CONFIG_ARGV = ['debug', 'config']

# -------------------------------------------------------------------------------------------- O2-S14
KEY_FILE_IN_IMAGE = '/config/models/openrouter.key'
KEY_FILE_VARIABLE = 'KANBAN_OPENCODE_OPENROUTER_KEY_FILE'
KEY_ENV_NAMES = ('OPENROUTER_API_KEY',)

# -------------------------------------------------------------------------------------------- O2-S15
SOURCE_ROOTS = ('packages/tooling/src', 'extensions/ops/src', 'apps/kanban/src', 'apps/kanban/web-ui/src',
                'apps/kanban/scripts', 'deploy', 'config', 'scripts')

# -------------------------------------------------------------------------------------------- O2-S16
AUDIT_EVENTS = ('open', 'os.open', 'sqlite3.connect', 'subprocess.Popen', 'os.exec', 'os.posix_spawn')


def as_json() -> dict:
    """The values the board tests read (vitest runs `python3 tests/o2_seams.py --json`)."""
    return {'harness': HARNESS, 'events': list(EVENTS), 'disable_env': DISABLE_ENV,
            'launch_binding_variable': LAUNCH_BINDING_VARIABLE, 'board_launch': BOARD_LAUNCH,
            'key_file_variable': KEY_FILE_VARIABLE, 'key_env_names': list(KEY_ENV_NAMES),
            'stub_port': STUB_PORT, 'stub_provider_config': STUB_PROVIDER_CONFIG,
            'npm_registry_host': NPM_REGISTRY_HOST, 'models_host': MODELS_HOST,
            'install_artifacts': list(INSTALL_ARTIFACTS), 'search_bound_seconds': SEARCH_BOUND_SECONDS,
            'poll_seconds': POLL_SECONDS, 'capture_deadline_seconds': CAPTURE_DEADLINE_SECONDS,
            'image_launch_executable': IMAGE_LAUNCH_EXECUTABLE, 'store_root': STORE_ROOT,
            'debug_config_argv': DEBUG_CONFIG_ARGV, 'key_file_in_image': KEY_FILE_IN_IMAGE,
            'receipt_queue_field': RECEIPT_QUEUE_FIELD, 'one_job': ONE_JOB}


if __name__ == '__main__':
    if sys.argv[1:] == ['--json']:
        print(json.dumps(as_json()))
    else:
        print('usage: python3 tests/o2_seams.py --json', file=sys.stderr)
        raise SystemExit(2)
