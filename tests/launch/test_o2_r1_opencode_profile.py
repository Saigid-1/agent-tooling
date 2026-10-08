"""O2 R1 and F5: an `opencode` profile within closed lists (docs/work/orders/O2-opencode-third-harness.md).

GREEN-IF the packaged profiles carry an `opencode` entry built from exactly one new member of each
strategy enum and one new parser, the `claude` and `codex` entries are unchanged, and every unknown
strategy, parser, field or dotted event name is still refused. RED at base: there is no `opencode`
profile, and a `config_content_env`, `plugin_env` or `export` member is refused.
Mutants (each RED here): drop the opencode entry; accept an unknown parser; let `export` take a
transcript parser.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from kp_agent_tooling._impl.service import harness_profiles as hp
from kp_agent_tooling._impl.service import launch_binding as lb

PACKAGED = Path(hp.__file__).resolve().parents[2] / 'assets' / 'harness-profiles.json'

OPENCODE = {
    'harness': 'opencode', 'enabled': True, 'executable': 'opencode',
    'session_id': {'strategy': 'hook', 'field': 'session_id'},
    'mcp': {'strategy': 'config_content_env', 'env': 'OPENCODE_CONFIG_CONTENT', 'key': 'mcp'},
    'hooks': {'strategy': 'plugin_env', 'env': 'KP_AGENT_LAUNCH_HOOK_COMMAND',
              'events': ['Stop', 'PreCompact', 'SessionEnd']},
    'capture': {'mode': 'export', 'parser': 'opencode-export'},
    'receipt_env': 'KP_AGENT_LAUNCH_RECEIPT',
}
# The base entries, verbatim: O2 leaves them unchanged.
CLAUDE = {
    'harness': 'claude', 'enabled': True, 'executable': 'claude',
    'session_id': {'strategy': 'mint', 'flag': '--session-id'},
    'mcp': {'strategy': 'config_file_flag', 'flag': '--mcp-config'},
    'hooks': {'strategy': 'settings_file_flag', 'flag': '--settings', 'events': ['Stop', 'PreCompact', 'SessionEnd']},
    'capture': {'mode': 'transcript', 'parser': 'claude-jsonl', 'root': '~/.claude/projects'},
    'receipt_env': 'KP_AGENT_LAUNCH_RECEIPT',
}
CODEX = {
    'harness': 'codex', 'enabled': True, 'executable': 'codex',
    'session_id': {'strategy': 'hook', 'field': 'session_id'},
    'mcp': {'strategy': 'config_override', 'key': 'mcp_servers'},
    'hooks': {'strategy': 'config_override', 'events': ['UserPromptSubmit', 'Stop']},
    'capture': {'mode': 'transcript', 'parser': 'codex-rollout', 'root': '~/.codex/sessions'},
    'receipt_env': 'KP_AGENT_LAUNCH_RECEIPT',
}


def test_the_packaged_profiles_gain_opencode_and_keep_claude_and_codex():
    document = json.loads(PACKAGED.read_text())
    by_id = {profile['harness']: profile for profile in document['profiles']}
    assert [p['harness'] for p in document['profiles']] == ['claude', 'codex', 'opencode']
    assert by_id['claude'] == CLAUDE and by_id['codex'] == CODEX
    assert by_id['opencode'] == OPENCODE
    assert hp.default_profiles()['opencode'] == OPENCODE
    assert hp.select(hp.default_profiles(), 'opencode') == OPENCODE


def test_exactly_one_new_parser_and_the_capture_vocabulary_is_unchanged():
    assert hp.PARSERS == ('claude-jsonl', 'codex-rollout', 'opencode-export')
    assert hp.TRANSCRIPT_PARSERS == ('claude-jsonl', 'codex-rollout')
    assert hp.EXPORT_PARSERS == ('opencode-export',)
    # Events keep the existing vocabulary: the plugin maps OpenCode's to these.
    assert lb.CAPTURE_EVENTS == ('Stop', 'PreCompact', 'SessionEnd')
    assert hp._EVENT.pattern == r'^[A-Z][A-Za-z]{0,63}$'


def _with(path, value):
    profile = copy.deepcopy(OPENCODE)
    target = profile
    for key in path[:-1]:
        target = target[key]
    if value is KeyError:
        del target[path[-1]]
    else:
        target[path[-1]] = value
    return profile


REFUSED = {
    'unknown mcp strategy': (('mcp',), {'strategy': 'config_content_file', 'env': 'X', 'key': 'mcp'}),
    'mcp content env without key': (('mcp',), {'strategy': 'config_content_env', 'env': 'OPENCODE_CONFIG_CONTENT'}),
    'mcp content env with a flag': (('mcp', 'flag'), '--mcp'),
    'mcp content env, lowercase variable': (('mcp', 'env'), 'opencode_config_content'),
    'unknown hooks strategy': (('hooks',), {'strategy': 'plugin_file', 'events': ['Stop']}),
    'plugin env without env': (('hooks',), {'strategy': 'plugin_env', 'events': ['Stop']}),
    'plugin env, dotted OpenCode event': (('hooks', 'events'), ['session.idle']),
    'plugin env, no events': (('hooks', 'events'), []),
    'unknown capture mode': (('capture',), {'mode': 'database', 'parser': 'opencode-export'}),
    'unknown parser': (('capture', 'parser'), 'opencode-sqlite'),
    'export with a transcript parser': (('capture', 'parser'), 'codex-rollout'),
    'export with a root': (('capture', 'root'), '~/.local/share/opencode'),
    'transcript with the export parser': (('capture',), {'mode': 'transcript', 'parser': 'opencode-export',
                                                         'root': '~/.local/share/opencode'}),
    'unknown session strategy': (('session_id',), {'strategy': 'choose', 'field': 'session_id'}),
    'unknown profile field': (('database',), 'x'),
    'missing capture': (('capture',), KeyError),
}


@pytest.mark.parametrize('case', sorted(REFUSED))
def test_unknown_strategies_parsers_fields_and_events_are_refused(case):
    path, value = REFUSED[case]
    with pytest.raises(ValueError):
        hp.validate_profile(_with(path, value))


def test_the_profile_document_with_opencode_validates_and_an_operator_can_disable_it():
    document = {'schema_version': hp.SCHEMA, 'profiles': [CLAUDE, CODEX, {**OPENCODE, 'enabled': False}]}
    profiles = hp.validate(document)
    with pytest.raises(hp.HarnessUnavailable):
        hp.select(profiles, 'opencode')
    assert hp.transcript_root(profiles['opencode']) is None
