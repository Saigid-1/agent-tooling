"""S1a seams: every name the S1a tests assume about the summarizer role, in ONE place.

Order: docs/work/orders/S1a-scheduled-summarizer.md (frozen). FEATURE names most of these; the TEST arm
wrote its reading of each one down here and nowhere else, so the meet reconciles them in this file alone.
Every test reaches the role through `s1a_harness`, which reads only these names. Nothing here imports
FEATURE's module: the role is reached through its console-script entry point, in-process.

The readings, the most conservative ones (each is listed under AMBIGUITY in the arm's report):

- component, Compose service and Compose profile: `summarizer` (P3: "A new component `summarizer`").
- the role's entry point: the console script `kp-agent-summarizer` (P3: "its console entry in
  packages/tooling/pyproject.toml"), loaded from the installed `console_scripts` entry points and called
  in-process. It takes `argv` (`main(argv)`) or reads `sys.argv` (`main()`); both are accepted.
- one tick: the role's command WITHOUT `--watch`. The watch loop is driven with
  `--watch --interval 1 --max-ticks N`, and the loop's pause between ticks is a timed
  `threading.Event.wait(interval)` in the role's (main) thread (the harness hooks it, and `time.sleep`, to
  act between ticks). [Meet: FEATURE's `summarizer_role.main`; the arm read `--max-passes` and `time.sleep`.]
- the role's inputs: `--approval <file>`, `--gateway-config <file>` and `--state <role state directory>`.
  The role never creates its state directory: the operator does, private (0700, owned by the role's user;
  `SummarizerRole._state_ready`). Each approved desk's stores (`queue.sqlite3`, `episodes.sqlite3`,
  `sessions.sqlite3`) come from that desk's memory configuration (its `state_root` and `catalog_path`).
  [Meet: the arm read `--catalog <desk catalog>` and `--root <state root>`.]
- the approval: ONE operator JSON file (0600) `approval.json`, schema `agent-tooling.summarizer-approval.v1`,
  with exactly the keys `schema_version`, `capability`, `model_id`, `reserves` (the five
  `memory_model_profile.py` reserves, by their receipt names) and `desks`: a list of
  `{"binding_key": <the desk's binding key>, "memory_config": <the absolute path of that desk's summarizer
  memory configuration, the one the operator admits>}`, each key and each path unique
  (`summarizer_role.load_approval`). [Meet: the arm read `desks` as a list of binding keys.]
- the gateway capability: `memory.summarize`, routed with `operation: chat` (it is not in the gateway's
  DEFAULT_OPERATIONS). The gateway configuration is the existing `agent-tooling.model-gateway.v1`.
- the role's admission: the operator's existing `admit` (desk_memory_runtime.admit) over a per-desk memory
  configuration whose `provider_instance` is `summarizer` and whose `provider_session_id` is a per-desk
  session id; `provider_id` is the route's provider (the gateway provider is NAMED `openrouter` and is of
  KIND `openrouter`, so both readings of "the route's provider" coincide); `model_id` is the approved model.
  The approval names each desk's memory configuration, which names the session. [Meet: the arm read that the
  role finds its admission itself.] A desk the approval names whose admission the operator has not made has
  its memory configuration written and no admission row.
- the role's status: "a status FEATURE names". It is read tolerantly: every JSON document the role prints
  (stdout or stderr, whole or per line) and every JSON file the run creates or changes anywhere under the
  test world. A status node is a JSON object with a string value equal to, or starting with,
  `not_configured`. "Naming the missing piece" is a case-insensitive token in that node (MISSING_TOKENS).
  [Meet: FEATURE prints one JSON line per tick (`status`, `missing`, a per-desk `desks` list) and writes
  `<state>/status.json` (`agent-tooling.summarizer-status.v1`); both are read. Its `missing` words
  (`approval`, `route`, `route_params`, `model_pin`, `budget`, `budget_estimate`, `key_file`, `state`,
  `admission`, `profile_changed`) carry the tokens unchanged.]
- the not-sent state of an attempt the gateway refused before sending: `not_sent` (the gateway ledger's
  own word for a call that never reached a provider, model_gateway.py `_COUNTED`). [Meet: FEATURE's
  `episodic_queue.NOT_SENT`, the same word.]
- the gateway ledger: under the gateway configuration's `artifact_root` (`.model-gateway/ledger.sqlite3`); the
  test world keeps `artifact_root` a directory of its own. [Meet: FEATURE's documentation sets it to the
  role's state directory; the role reads it only from the gateway configuration.]
- the listing read: a GET to `<route base_url>/models` (any query); a completion: a POST to
  `<route base_url>/chat/completions` (the gateway's openrouter chat path).
- the approved model: the baseline `z-ai/glm-5.3-flash` (memory_budget.DEFAULT_SUMMARY_MODEL).
"""
from __future__ import annotations

# ------------------------------------------------------------------------------- install and Compose
COMPONENT = 'summarizer'
SERVICE = 'summarizer'
PROFILE = 'summarizer'
# The installer plan that selects the role (order (c)).
SELECTING_COMPONENTS = 'tooling,summarizer'

# ------------------------------------------------------------------------------------ the role entry
ROLE_SCRIPT = 'kp-agent-summarizer'
APPROVAL_OPTION = '--approval'
GATEWAY_OPTION = '--gateway-config'
STATE_OPTION = '--state'
WATCH_ARGS = ('--watch', '--interval', '1', '--max-ticks')  # followed by the tick count
# The role's state directory, under the test world's state root; created by the operator (the harness).
ROLE_STATE_DIR = 'summarizer'
ROLE_STATE_MODE = 0o700


def role_argv(*, approval, gateway, state, watch_passes=None):
    """The role's argv (without the program name): one tick, or `watch_passes` ticks of the watch loop."""
    argv = [APPROVAL_OPTION, str(approval), GATEWAY_OPTION, str(gateway), STATE_OPTION, str(state)]
    if watch_passes is not None:
        argv += [*WATCH_ARGS, str(watch_passes)]
    return argv


# -------------------------------------------------------------------------------------- the approval
APPROVAL_FILE = 'approval.json'
APPROVAL_SCHEMA = 'agent-tooling.summarizer-approval.v1'
CAPABILITY = 'memory.summarize'
ROUTE_OPERATION = 'chat'
APPROVED_MODEL = 'z-ai/glm-5.3-flash'
RESERVES = {'system_tokens': 4096, 'tool_tokens': 1024, 'reasoning_tokens': 4096,
            'output_tokens': 8192, 'safety_tokens': 2048}


def approval_document(desks, *, model=APPROVED_MODEL, reserves=None, capability=CAPABILITY):
    """The operator's approval: `desks` is a list of (binding key, absolute memory configuration path)."""
    return {'schema_version': APPROVAL_SCHEMA,
            'desks': [{'binding_key': key, 'memory_config': str(config)} for key, config in desks],
            'model_id': model, 'reserves': dict(RESERVES if reserves is None else reserves),
            'capability': capability}


# -------------------------------------------------------------------------------- the role's admission
ROLE_PROVIDER_INSTANCE = 'summarizer'
GATEWAY_PROVIDER = 'openrouter'   # the gateway provider's name, which is also its kind


def desk_session_id(desk_tag, generation=1):
    """The per-desk session id of the role's admission (a new one for a new model, P1)."""
    return f'summarizer-{desk_tag}-{generation}'


# ------------------------------------------------------------------------------ status and states
NOT_CONFIGURED = 'not_configured'
NOT_SENT_STATE = 'not_sent'
# P3: the role reports `not_configured` "naming the missing piece": one token per piece (any case).
MISSING_TOKENS = {
    'approval': ('approval',),
    'route': ('route',),
    'budget': ('budget',),
    'estimate': ('estimate',),
    'key_file': ('key',),
    'admission': ('admission',),
}

# ----------------------------------------------------------------------------------- the wire
LISTING_SUFFIX = '/models'
COMPLETION_SUFFIX = '/chat/completions'
QUEUE_FILE = 'queue.sqlite3'
EPISODES_FILE = 'episodes.sqlite3'
SESSIONS_FILE = 'sessions.sqlite3'
GATEWAY_LEDGER = ('.model-gateway', 'ledger.sqlite3')  # under the gateway artifact_root
