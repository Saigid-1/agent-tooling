"""T4 P1: host launch.

A `claude` launch through the adapter executes the real executable with the
user's arguments and environment unchanged, plus T3's injections; records a
binding with `source: "host"`; makes the memory MCP reachable in both runtime
modes; and captures a Stop event through spool ingestion, after which
`memory.search` finds that turn. Codex behaves the same, with binding on the
first hook.
Falsifier: user arguments altered or dropped, an unreachable MCP in either mode,
or no capture.

Readings (reported under AMBIGUITY):
- `kp-agent-host --config <adapter config> launch ...`: the config is a global
  option, as in the package's other CLIs; the workspace is the launching cwd;
- "executes" means exec: the CLI replaces the adapter process (same PID);
- "unchanged" means: the user's arguments appear once, contiguous, in order and
  byte-identical; every other token is one of the profile's injection flags
  (Claude `--session-id`/`--settings`/`--mcp-config`, Codex `-c`/`--config`) with
  its value; the CLI must still read the user's arguments as theirs (Claude's
  `--mcp-config` is variadic, so a following prompt would be read as a config);
  every variable of the launching environment reaches the CLI with its value
  (shell-maintained PWD, OLDPWD, SHLVL and _ excluded), plus the profile's
  receipt variable; other added variables are not asserted;
- the hooks call `kp-agent-host ... hook --launch <id>` (the console script, or
  `-m kp_agent_tooling.host_cli`, the module the order's write scope names) for
  every profile event, and never the synchronous T3 hook or docker; until
  ingestion runs nothing is bound (Codex) or captured;
- docker mode: `prepare` runs through `docker exec -i <container> kp-agent-launch
  ... prepare` and the memory server is `docker exec -i <container>
  kp-agent-memory --config <path> serve`, never `-t`; both are observed through
  the docker stand-in, which shares paths with the host. The docker-mode tests
  use the shipped profiles with absolute transcript roots (`~` then never
  depends on the runtime's HOME); the local tests use the shipped profiles.
"""
from pathlib import Path

from t12b_seams import drain  # T12b B4: the one drain helper
from host_harness import (CLAUDE_EVENTS, CODEX_EVENTS, RECEIPT_ENV, HostWorld, assert_hooks_call_the_adapter,
                          assert_user_args_and_env_unchanged, call_server, codex_meta, exec_parts, has_interactive,
                          has_tty, memory_servers, new_session_id, row_carries)

# A leading positional prompt, an adapter-looking flag after `--`, shell metacharacters and double spaces.
CLAUDE_ARGS = ['Summarise the lantern ledger', '--model', 'user-chosen-model', '--append-system-prompt',
               'Keep  two spaces, a "quote", a $HOME and a ; semicolon', '--permission-mode', 'plan', '--verbose']
CODEX_ARGS = ['--model', 'user-chosen-model', '-c', 'model_reasoning_effort="high"', '--desk', 'not-for-the-adapter',
              'Summarise the lantern ledger']
USER_ENV = {'T4_USER_SETTING': 'kept  as "is" with $HOME', 'T4_EMPTY_SETTING': ''}


def _assert_claude_injections(launched):
    flags = [flag for flag, _ in launched.injections()]
    assert flags.count('--session-id') == 1 and '--settings' in flags and '--mcp-config' in flags, (
        f'missing Claude injections: {launched.argv}')
    assert launched.record['config']['session_ids'] == [launched.native], launched.record['config']


def _assert_codex_injections(launched):
    keys = [value.partition('=')[0] for _, value in launched.injections()]
    assert any(k == 'mcp_servers' or k.startswith('mcp_servers.') for k in keys), keys
    assert any(k == 'hooks' or k.startswith('hooks.') for k in keys), keys


def _binding(hw, session):
    recorded = hw.binding_for(session)
    assert len(recorded) == 1, recorded
    return recorded[0]


def _assert_memory_served(launched, desk, *, docker):
    servers = memory_servers(launched)
    assert servers, f'no generated MCP server serves desk memory: {launched.servers}'
    drain(launched.hw.world.operator)  # T12b B4: the indexer, not the hook, indexes
    for name, server in servers.items():
        parts = exec_parts([server.get('command', ''), *server.get('args', [])])
        if docker:
            assert parts is not None, f'docker mode: memory server {name} is not `docker exec`: {server}'
            options, container, command = parts
            assert container == launched.hw.container, (name, server)
            assert has_interactive(options) and not has_tty(options), f'{name} must use -i and never -t: {server}'
            assert Path(command[0]).name == 'kp-agent-memory' and '--config' in command and command[-1] == 'serve', (
                name, server)
        replies = call_server(launched, server, [('memory.connection_status', {}),
                                                 ('memory.search', {'query': 'Lantern'}), ('memory.bindings', {})])
        status, search, bindings = replies
        assert not status.is_error and status.value['status'] == 'ready', (name, status.value)
        assert not search.is_error, (name, search.value)
        assert [b['binding_key'] for b in bindings.value['bindings'] if b['own']] == [launched.hw.desk_key(desk)], (
            name, bindings.value)


def _assert_prepared_through_docker_exec(hw):
    calls = [c for c in hw.docker_calls() if c.get('command')]
    prepares = [c for c in calls if Path(c['command'][0]).name == 'kp-agent-launch' and 'prepare' in c['command']]
    assert prepares, f'docker mode did not prepare through `docker exec ... kp-agent-launch prepare`: {calls}'
    for call in prepares:
        assert call['container'] == hw.container and call['interactive'] and not call['tty'], call


# ---------------------------------------------------------------------------- local


def test_claude_local_launch_execs_the_cli_with_user_arguments_and_environment_unchanged(tmp_path):
    hw = HostWorld.create(tmp_path / 'w')
    desk = hw.save_desk()
    launched = hw.launch('claude', desk, provider='anthropic', model='fixture-model', user_args=CLAUDE_ARGS,
                         env_extra=USER_ENV).ok()

    assert_user_args_and_env_unchanged(launched)
    _assert_claude_injections(launched)
    assert launched.record['env'].get(RECEIPT_ENV), 'the profile receipt variable is not injected'
    assert_hooks_call_the_adapter(launched, CLAUDE_EVENTS)
    recorded = _binding(hw, launched.native)
    assert {k: recorded[k] for k in ('harness', 'provider', 'model', 'desk_id', 'source', 'workspace',
                                     'parent_session_id')} == {
        'harness': 'claude', 'provider': 'anthropic', 'model': 'fixture-model', 'desk_id': desk, 'source': 'host',
        'workspace': str(hw.workspace), 'parent_session_id': None}
    assert hw.docker_calls() == [], 'local mode called docker'


def test_claude_local_launch_serves_desk_memory_over_mcp(tmp_path):
    hw = HostWorld.create(tmp_path / 'w')
    desk = hw.save_desk()
    launched = hw.launch('claude', desk).ok()
    _assert_memory_served(launched, desk, docker=False)
    assert hw.docker_calls() == [], 'local mode called docker'


def test_claude_local_stop_is_captured_through_spool_ingestion(tmp_path):
    hw = HostWorld.create(tmp_path / 'w')
    desk = hw.save_desk()
    marker = 'Cedar host spool marker'
    launched = hw.launch('claude', desk, plan=[
        hw.claude_turn('stop', f'Remember {marker}.', f'Recorded {marker}.')]).ok()
    native, launch_id = launched.native, launched.launch_id
    runs = launched.runs('stop')
    assert runs and all(r['code'] == 0 for r in runs), runs
    rows = hw.spool_rows(launch_id)
    assert any(row_carries(row, runs[0]['payload']) and launch_id in str(row) for row in rows), rows
    assert hw.hits(native, marker) == 0, 'the hook captured synchronously instead of spooling'

    hw.ingest(lambda: hw.hits_now(native, marker) >= 1)
    assert hw.hits(native, marker) >= 1
    assert [b['desk_id'] for b in hw.binding_for(native)] == [desk]
    assert hw.docker_calls() == [], 'local mode called docker'


def test_codex_local_launch_binds_on_first_hook_and_captures_through_the_spool(tmp_path):
    hw = HostWorld.create(tmp_path / 'w')
    desk = hw.save_desk()
    session, marker = new_session_id(), 'Alder host codex marker'
    launched = hw.launch('codex', desk, provider='openai', model='fixture-model', user_args=CODEX_ARGS,
                         env_extra=USER_ENV, plan=[
                             hw.codex_turn('prompt', session, f'Remember {marker}.', event='UserPromptSubmit',
                                           meta=codex_meta(session, hw.workspace)),
                             hw.codex_turn('stop', session, f'Recorded {marker}.')]).ok()
    assert_user_args_and_env_unchanged(launched)
    _assert_codex_injections(launched)
    assert launched.record['env'].get(RECEIPT_ENV), 'the profile receipt variable is not injected'
    assert_hooks_call_the_adapter(launched, CODEX_EVENTS)
    for label in ('prompt', 'stop'):
        assert launched.runs(label) and all(r['code'] == 0 for r in launched.runs(label)), launched.describe()
    assert hw.bindings() == [], 'the hook bound the session itself instead of spooling'

    hw.ingest(lambda: hw.bound_now(session) and hw.hits_now(session, marker) >= 1)
    recorded = _binding(hw, session)
    assert {k: recorded[k] for k in ('harness', 'provider', 'model', 'desk_id', 'source', 'workspace',
                                     'parent_session_id')} == {
        'harness': 'codex', 'provider': 'openai', 'model': 'fixture-model', 'desk_id': desk, 'source': 'host',
        'workspace': str(hw.workspace), 'parent_session_id': None}
    assert hw.hits(session, marker) >= 1
    _assert_memory_served(launched, desk, docker=False)
    assert hw.docker_calls() == [], 'local mode called docker'


# ---------------------------------------------------------------------------- docker


def test_claude_docker_launch_prepares_and_serves_memory_through_docker_exec_i(tmp_path):
    hw = HostWorld.create(tmp_path / 'w', mode='docker', profiles='absolute')
    desk = hw.save_desk()
    marker = 'Birch docker spool marker'
    launched = hw.launch('claude', desk, provider='anthropic', model='fixture-model', user_args=CLAUDE_ARGS,
                         env_extra=USER_ENV, plan=[
                             hw.claude_turn('stop', f'Remember {marker}.', f'Recorded {marker}.')]).ok()
    assert_user_args_and_env_unchanged(launched)
    _assert_claude_injections(launched)
    assert_hooks_call_the_adapter(launched, CLAUDE_EVENTS)
    _assert_prepared_through_docker_exec(hw)
    native = launched.native
    recorded = _binding(hw, native)
    assert (recorded['harness'], recorded['desk_id'], recorded['source']) == ('claude', desk, 'host'), recorded
    _assert_memory_served(launched, desk, docker=True)

    assert launched.runs('stop') and all(r['code'] == 0 for r in launched.runs('stop')), launched.describe()
    assert hw.hits(native, marker) == 0, 'the hook captured synchronously instead of spooling'
    hw.ingest(lambda: hw.hits_now(native, marker) >= 1)
    assert hw.hits(native, marker) >= 1


def test_codex_docker_launch_binds_on_first_hook_and_serves_memory_through_docker_exec_i(tmp_path):
    hw = HostWorld.create(tmp_path / 'w', mode='docker', profiles='absolute')
    desk = hw.save_desk()
    session, marker = new_session_id(), 'Rowan docker codex marker'
    launched = hw.launch('codex', desk, provider='openai', model='fixture-model', user_args=CODEX_ARGS,
                         env_extra=USER_ENV, plan=[
                             hw.codex_turn('prompt', session, f'Remember {marker}.', event='UserPromptSubmit',
                                           meta=codex_meta(session, hw.workspace)),
                             hw.codex_turn('stop', session, f'Recorded {marker}.')]).ok()
    assert_user_args_and_env_unchanged(launched)
    _assert_codex_injections(launched)
    assert_hooks_call_the_adapter(launched, CODEX_EVENTS)
    _assert_prepared_through_docker_exec(hw)
    assert hw.bindings() == [], 'the hook bound the session itself instead of spooling'

    hw.ingest(lambda: hw.bound_now(session) and hw.hits_now(session, marker) >= 1)
    recorded = _binding(hw, session)
    assert (recorded['harness'], recorded['desk_id'], recorded['source']) == ('codex', desk, 'host'), recorded
    assert hw.hits(session, marker) >= 1
    _assert_memory_served(launched, desk, docker=True)
