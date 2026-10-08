"""T4 P3: operator acts only.

Sessions not launched through the adapter bind only by `bind` or by an
installed project policy, and only to that policy's desk. `uninstall-hooks`
restores the file byte for byte and stops new bindings.
Falsifier: an arbitrary session auto-admitted, a binding to another desk, or a
non-identical restore.

Readings (reported under AMBIGUITY):
- `kp-agent-host --config <adapter config> bind <harness> --native-session-id
  <id> --desk <desk>`; provider, model and workspace of an operator bind are not
  asserted; binding an already bound session to another desk is refused;
- `install-hooks --project <dir> --harness claude|codex --desk <desk>` and
  `uninstall-hooks --project <dir> --harness claude|codex` (the order gives no
  arguments for uninstall; these mirror install without the desk);
- "idempotent": a second install leaves the files byte-identical to the first;
  a second uninstall changes nothing; a project without a settings file gets
  none back after uninstall;
- Claude: the file is the project's `.claude/settings.local.json`. Codex: the
  order says "the documented project or user configuration the harness profile
  names" without naming it, so both candidates (`<project>/.codex/config.toml`
  and `$HOME/.codex/config.toml`) are seeded with user content and both trees
  must be restored byte for byte; at least one must carry the adapter hook;
- a natively started session is a fake CLI started in the project without the
  adapter; it reads the project's hook configuration as the real CLI does;
- the policy binds a project session with `source: "operator"` to the policy's
  desk; a payload whose `cwd` is outside the project is refused; capture of a
  policy-bound session is not asserted;
- "stops new bindings": after uninstall, neither a native session (whose CLI no
  longer runs the adapter hook) nor a stale copy of the removed hook command can
  bind a new session; a session bound before uninstall stays bound;
- negative outcomes that have no later valid event in the same spool file are
  observed after a valid sentinel launch is ingested plus a 3 s settle.
"""
import json
import subprocess
import tomllib

from host_harness import (HostWorld, claude_payload, claude_transcript, invokes_host_adapter, new_session_id,
                          tree_bytes)

USER_CLAUDE_SETTINGS = (
    '{\n'
    '    "permissions": {"allow": ["Bash(ls:*)"]},\n'
    '    "hooks": {\n'
    '        "Stop": [{"hooks": [{"type": "command", "command": "true # user stop hook"}]}],\n'
    '        "PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "true # user pretool"}]}]\n'
    '    },\n'
    '    "env": {"USER_SETTING": "kept"}\n'
    '}')
USER_CODEX_PROJECT = (
    '# user project configuration\n'
    'model = "user-model"\n'
    '\n'
    '[mcp_servers.user_tool]\n'
    'command = "user-tool"\n'
    'args = ["--flag"]\n'
    '\n'
    '[[hooks.Stop]]\n'
    '[[hooks.Stop.hooks]]\n'
    'type = "command"\n'
    'command = "true # user codex stop hook"\n')
USER_CODEX_HOME = (
    '# user configuration\n'
    'approval_policy = "on-request"\n'
    '\n'
    '[profiles.quiet]\n'
    'model = "user-quiet-model"\n')


def _project(hw, name):
    path = hw.root / name
    path.mkdir()
    subprocess.run(['git', 'init', '-q', str(path)], check=True, capture_output=True)
    return path


def _adapter_commands(settings):
    hooks = settings.get('hooks') or {}
    return {event: [h['command'] for g in groups for h in (g.get('hooks') or [])
                    if invokes_host_adapter(h.get('command', ''))]
            for event, groups in hooks.items() if isinstance(groups, list)}


def _hook_commands(raw):
    """Every `command` string in a TOML or JSON document, however it is laid out."""
    found = []

    def walk(value):
        if isinstance(value, dict):
            if isinstance(value.get('command'), str):
                found.append(value['command'])
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)
    for parse in (lambda text: tomllib.loads(text), json.loads):
        try:
            walk(parse(raw.decode()))
            break
        except (UnicodeError, ValueError, tomllib.TOMLDecodeError):
            continue
    return found

def test_bind_admits_one_named_session_to_one_desk_with_source_operator(tmp_path):
    hw = HostWorld.create(tmp_path / 'w')
    desk = hw.save_desk()
    other_desk = hw.save_desk(role='Curator', name='Other desk')
    session, codex_session, stranger = new_session_id(), new_session_id(), new_session_id()

    hw.host('bind', 'claude', '--native-session-id', session, '--desk', desk).ok()
    hw.host('bind', 'codex', '--native-session-id', codex_session, '--desk', desk).ok()
    recorded = {b['native_session_id']: b for b in hw.bindings()}
    assert set(recorded) == {session, codex_session}, recorded
    for native, harness in ((session, 'claude'), (codex_session, 'codex')):
        b = recorded[native]
        assert (b['harness'], b['desk_id'], b['source'], b['parent_session_id']) == (harness, desk, 'operator', None), b
    assert hw.ready(session) and hw.own_keys(session) == [hw.desk_key(desk)]

    moved = hw.host('bind', 'claude', '--native-session-id', session, '--desk', other_desk)
    assert moved.code != 0, 'a bound session was rebound to another desk\n' + moved.describe()
    assert [b['desk_id'] for b in hw.binding_for(session)] == [desk]
    assert hw.own_keys(session) == [hw.desk_key(desk)]
    assert not hw.ready(stranger) and hw.binding_for(stranger) == []


def test_claude_install_hooks_merges_and_uninstall_restores_byte_for_byte(tmp_path):
    hw = HostWorld.create(tmp_path / 'w')
    desk = hw.save_desk()
    project = _project(hw, 'project-a')
    settings = project / '.claude' / 'settings.local.json'
    settings.parent.mkdir()
    settings.write_text(USER_CLAUDE_SETTINGS)
    original = settings.read_bytes()
    user = json.loads(original)

    hw.host('install-hooks', '--project', project, '--harness', 'claude', '--desk', desk).ok()
    installed = settings.read_bytes()
    merged = json.loads(installed)
    assert {k: v for k, v in merged.items() if k != 'hooks'} == {k: v for k, v in user.items() if k != 'hooks'}
    for event, groups in user['hooks'].items():
        for group in groups:
            assert group in merged['hooks'].get(event, []), f'user {event} hook clobbered: {merged["hooks"]}'
    adapter = _adapter_commands(merged)
    assert any(adapter.values()), f'no adapter hook was installed: {merged["hooks"]}'

    hw.host('install-hooks', '--project', project, '--harness', 'claude', '--desk', desk).ok()
    assert settings.read_bytes() == installed, 'a second install changed the settings file'

    hw.host('uninstall-hooks', '--project', project, '--harness', 'claude').ok()
    assert settings.read_bytes() == original, 'uninstall did not restore the original bytes'
    hw.host('uninstall-hooks', '--project', project, '--harness', 'claude').ok()
    assert settings.read_bytes() == original, 'a second uninstall changed the settings file'

    # A project with no settings file has none after uninstall.
    bare = _project(hw, 'project-b')
    hw.host('install-hooks', '--project', bare, '--harness', 'claude', '--desk', desk).ok()
    created = bare / '.claude' / 'settings.local.json'
    assert created.is_file() and any(_adapter_commands(json.loads(created.read_text())).values())
    hw.host('uninstall-hooks', '--project', bare, '--harness', 'claude').ok()
    assert not created.exists(), 'uninstall left a settings file where there was none'


def test_codex_install_hooks_keeps_user_configuration_and_uninstall_restores_byte_for_byte(tmp_path):
    hw = HostWorld.create(tmp_path / 'w')
    desk = hw.save_desk()
    project = _project(hw, 'project-codex')
    project_config = project / '.codex' / 'config.toml'
    home_config = hw.root / 'home' / '.codex' / 'config.toml'
    project_config.parent.mkdir()
    project_config.write_text(USER_CODEX_PROJECT)
    home_config.parent.mkdir(parents=True, exist_ok=True)
    home_config.write_text(USER_CODEX_HOME)
    roots = (project / '.codex', hw.root / 'home' / '.codex')
    original = tree_bytes(*roots)

    hw.host('install-hooks', '--project', project, '--harness', 'codex', '--desk', desk).ok()
    installed = tree_bytes(*roots)
    changed = {path for path in set(original) | set(installed) if original.get(path) != installed.get(path)}
    assert changed, 'install-hooks changed no Codex configuration'
    carriers = [path for path in changed if path in installed
                and any(invokes_host_adapter(c) for c in _hook_commands(installed[path]))]
    assert carriers, f'no changed Codex configuration names the adapter hook: {sorted(changed)}'
    for path in changed & set(original):
        before, after = tomllib.loads(original[path].decode()), tomllib.loads(installed[path].decode())
        for key, value in before.items():
            if key == 'hooks':
                for event, groups in value.items():
                    for group in groups:
                        assert group in after['hooks'].get(event, []), f'user {event} hook clobbered in {path}'
            else:
                assert after.get(key) == value, f'user setting {key!r} changed in {path}'

    hw.host('install-hooks', '--project', project, '--harness', 'codex', '--desk', desk).ok()
    assert tree_bytes(*roots) == installed, 'a second install changed the Codex configuration'
    hw.host('uninstall-hooks', '--project', project, '--harness', 'codex').ok()
    assert tree_bytes(*roots) == original, 'uninstall did not restore the Codex configuration byte for byte'
    hw.host('uninstall-hooks', '--project', project, '--harness', 'codex').ok()
    assert tree_bytes(*roots) == original, 'a second uninstall changed the Codex configuration'


def test_installed_policy_binds_project_sessions_to_its_desk_only(tmp_path):
    hw = HostWorld.create(tmp_path / 'w')
    desk = hw.save_desk(name='Policy desk')
    intruder = hw.save_desk(role='Curator', name='Intruder desk')
    project, elsewhere = _project(hw, 'project-p'), _project(hw, 'project-q')
    hw.host('install-hooks', '--project', project, '--harness', 'claude', '--desk', desk).ok()
    adapter = _adapter_commands(json.loads((project / '.claude' / 'settings.local.json').read_text()))
    assert adapter.get('Stop'), adapter

    # A payload from outside the project, run through the installed command, is refused.
    stranger = new_session_id()
    outside = claude_transcript(hw.claude_root, elsewhere, stranger)
    outside.parent.mkdir(parents=True, exist_ok=True)
    outside.write_text('')
    for command in adapter['Stop']:
        code, _, err, _ = hw.run_hook(command, claude_payload(stranger, outside, elsewhere), env=hw.env(),
                                      cwd=elsewhere)
        assert code in (0, 1), f'the hook exited {code}; 2 would block the harness\n{err[-2000:]}'

    # A session started natively in the project binds on its first hook, to the policy's desk only.
    session, marker = new_session_id(), 'Willow policy marker'
    record = hw.native_cli('claude', cwd=project, plan=[
        hw.claude_turn('stop', f'Remember {marker}.', f'Recorded {marker}.', session=session, cwd=project,
                       desk_id=intruder, binding_key=hw.desk_key(intruder), source='board', harness='intruder')])
    runs = [r for r in record['runs'] if invokes_host_adapter(r['command'])]
    assert runs and all(r['code'] == 0 for r in runs), record['runs']

    hw.ingest(lambda: hw.bound_now(session), settle=3.0)
    recorded = hw.binding_for(session)
    assert [(b['harness'], b['desk_id'], b['source'], b['parent_session_id']) for b in recorded] == [
        ('claude', desk, 'operator', None)], recorded
    assert hw.own_keys(session) == [hw.desk_key(desk)]
    assert hw.binding_for(stranger) == [], 'a session outside the project was admitted'
    assert [b for b in hw.bindings() if b['desk_id'] == intruder] == []


def test_uninstall_stops_new_bindings(tmp_path):
    hw = HostWorld.create(tmp_path / 'w')
    desk = hw.save_desk(name='Policy desk')
    project = _project(hw, 'project-p')
    settings = project / '.claude' / 'settings.local.json'
    hw.host('install-hooks', '--project', project, '--harness', 'claude', '--desk', desk).ok()
    adapter = _adapter_commands(json.loads(settings.read_text()))

    before = new_session_id()
    hw.native_cli('claude', cwd=project, plan=[
        hw.claude_turn('stop', 'Remember the early session.', 'Recorded.', session=before, cwd=project)])
    hw.ingest(lambda: hw.bound_now(before))

    hw.host('uninstall-hooks', '--project', project, '--harness', 'claude').ok()
    assert not settings.exists()
    after, stale = new_session_id(), new_session_id()
    record = hw.native_cli('claude', cwd=project, plan=[
        hw.claude_turn('stop', 'Remember the late session.', 'Recorded.', session=after, cwd=project)])
    assert not [r for r in record['runs'] if invokes_host_adapter(r['command'])], 'the removed hook still runs'
    transcript = claude_transcript(hw.claude_root, project, stale)
    transcript.parent.mkdir(parents=True, exist_ok=True)
    transcript.write_text('')
    for command in adapter['Stop']:
        code, _, err, _ = hw.run_hook(command, claude_payload(stale, transcript, project), env=hw.env(), cwd=project)
        assert code in (0, 1), f'the stale hook exited {code}; 2 would block the harness\n{err[-2000:]}'

    marker = 'Sentinel launch marker'
    sentinel = hw.launch('claude', desk, plan=[hw.claude_turn('stop', f'Remember {marker}.', 'Recorded.')]).ok()
    hw.ingest(lambda: hw.hits_now(sentinel.native, marker) >= 1, settle=3.0)
    assert hw.binding_for(after) == [] and hw.binding_for(stale) == [], 'a new session bound after uninstall'
    assert [b['desk_id'] for b in hw.binding_for(before)] == [desk], 'uninstall removed an existing binding'
    assert not hw.ready(stale)
