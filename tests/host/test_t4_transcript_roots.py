"""T4: launch and install-hooks stay inside the adapter's transcript roots.

Coordinator clarification (2026-10-01): "`launch`/`install-hooks` refuse when a
profile's root is not under the adapter config's `transcript_roots`."

Readings (reported under AMBIGUITY):
- the profile checked is the one for the harness being launched or installed;
  "under" includes equal;
- the adapter config's `transcript_roots` here names only the Claude root; the
  shipped profiles' roots expand against the launching user's HOME (a scratch
  home), so the Codex root is outside and the Claude root is inside;
- a refused launch executes no CLI and records no binding; a refused
  install-hooks leaves every candidate Codex configuration byte-identical;
- the same adapter config still launches and installs Claude (control).
"""
import json
import subprocess

from host_harness import HostWorld, codex_meta, invokes_host_adapter, new_session_id, tree_bytes


def test_launch_and_install_hooks_refuse_a_profile_root_outside_the_transcript_roots(tmp_path):
    root = (tmp_path / 'w').resolve()
    hw = HostWorld.create(root, transcript_roots=[root / 'home' / '.claude' / 'projects'])
    desk = hw.save_desk()

    session = new_session_id()
    refused = hw.launch('codex', desk, provider='openai', plan=[
        hw.codex_turn('stop', session, 'Remember the outside root.', meta=codex_meta(session, hw.workspace))])
    assert refused.run.code != 0, 'a launch whose profile root is outside the transcript roots ran\n' + refused.describe()
    assert refused.record is None, 'the Codex CLI was executed for a refused launch'
    assert hw.bindings() == []

    project = hw.root / 'project-codex'
    project.mkdir()
    subprocess.run(['git', 'init', '-q', str(project)], check=True, capture_output=True)
    (project / '.codex').mkdir()
    (project / '.codex' / 'config.toml').write_text('model = "user-model"\n')
    roots = (project / '.codex', hw.root / 'home' / '.codex')
    before = tree_bytes(*roots)
    run = hw.host('install-hooks', '--project', project, '--harness', 'codex', '--desk', desk)
    assert run.code != 0, 'install-hooks accepted a profile root outside the transcript roots\n' + run.describe()
    assert tree_bytes(*roots) == before, 'a refused install-hooks changed the Codex configuration'

    # Control: the Claude root is inside, so the same config launches and installs Claude.
    launched = hw.launch('claude', desk).ok()
    assert [b['desk_id'] for b in hw.binding_for(launched.native)] == [desk]
    claude_project = hw.root / 'project-claude'
    claude_project.mkdir()
    hw.host('install-hooks', '--project', claude_project, '--harness', 'claude', '--desk', desk).ok()
    settings = (claude_project / '.claude' / 'settings.local.json').read_text()
    assert _installs_adapter_hook(settings), settings


def _installs_adapter_hook(text):
    commands = []

    def walk(value):
        if isinstance(value, dict):
            if isinstance(value.get('command'), str):
                commands.append(value['command'])
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)
    walk(json.loads(text))
    return any(invokes_host_adapter(c) for c in commands)
