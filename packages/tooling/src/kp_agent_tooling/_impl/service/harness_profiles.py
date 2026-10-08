"""Harness profiles (``agent-tooling.harness-profiles.v1``): how a harness is launched and bound.

A profile names a harness, its executable and the strategies that inject the
session identity, the desk memory MCP server and the capture hooks, plus the
parser used for capture. The defaults ship as package data
(``assets/harness-profiles.json``). An operator file, named by an optional
``harness_profiles_path`` in the registry descriptor, replaces or adds profiles
by ``harness`` id. Adding a harness whose strategies and parser already exist is
configuration only. Profiles never select a desk or admit a session.

Each strategy list is closed. OpenCode (order O2) added exactly one member to each:

- ``mcp`` ``config_content_env``: the desk memory server travels in a JSON configuration
  document held in one environment variable (``env``), under ``key``, in the local-command
  shape ``{type: local, command, environment, enabled}``. OpenCode has no MCP flag; it reads
  ``OPENCODE_CONFIG_CONTENT``, a per-process source, so the server is per launch.
- ``hooks`` ``plugin_env``: the launch's hook command, a JSON argv, travels in one
  environment variable (``env``); a plugin the launcher installs runs it for each event with
  the event payload on stdin. OpenCode has no hook command setting, only plugins.
- ``capture`` ``export``: the session is read through the harness's own session export, not
  a transcript file under a root. OpenCode keeps sessions in its own database and writes no
  transcript. Its one parser is ``opencode-export``; a transcript parser is refused here and
  an export parser is refused in ``transcript`` mode.
"""
from __future__ import annotations

import json
import re
from importlib.resources import files
from pathlib import Path

from kp_agent_tooling._impl import leaf
from kp_agent_tooling._impl.service.desk_binding import call_memo

SCHEMA = 'agent-tooling.harness-profiles.v1'
TRANSCRIPT_PARSERS = ('claude-jsonl', 'codex-rollout')
EXPORT_PARSERS = ('opencode-export',)
PARSERS = TRANSCRIPT_PARSERS + EXPORT_PARSERS
MAX_PROFILES = 64
MAX_EVENTS = 16
_FIELDS = {'harness', 'enabled', 'executable', 'session_id', 'mcp', 'hooks', 'capture', 'receipt_env'}
_HARNESS = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$')
_FLAG = re.compile(r'^--?[A-Za-z0-9][A-Za-z0-9-]{0,63}$')
_FIELD = re.compile(r'^[A-Za-z_][A-Za-z0-9_]{0,63}$')
_EVENT = re.compile(r'^[A-Z][A-Za-z]{0,63}$')
_ENV = re.compile(r'^[A-Z_][A-Z0-9_]{0,127}$')
_EXECUTABLE_NAME = re.compile(r'^[A-Za-z0-9._+-]{1,255}$')


class HarnessUnavailable(ValueError):
    """The harness has no enabled profile; a launcher may start it unbound."""


def _exact(value, required, name, optional=()):
    if not isinstance(value, dict) or not set(required) <= set(value) <= set(required) | set(optional):
        raise ValueError(f'{name} requires exactly: ' + ', '.join(sorted(required)))
    return value


def _match(value, pattern, name):
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise ValueError(f'invalid {name}')
    return value


def _session(value):
    strategy = value.get('strategy') if isinstance(value, dict) else None
    if strategy == 'mint':
        # The payload field is optional for mint: both default harnesses name it session_id.
        _exact(value, {'strategy', 'flag'}, 'session_id mint', optional={'field'})
        clean = {'strategy': 'mint', 'flag': _match(value['flag'], _FLAG, 'session_id flag')}
        if 'field' in value:
            clean['field'] = _match(value['field'], _FIELD, 'session_id field')
        return clean
    if strategy == 'hook':
        _exact(value, {'strategy', 'field'}, 'session_id hook')
        return {'strategy': 'hook', 'field': _match(value['field'], _FIELD, 'session_id field')}
    raise ValueError('session_id strategy must be mint or hook')


def _mcp(value):
    strategy = value.get('strategy') if isinstance(value, dict) else None
    if strategy == 'config_file_flag':
        _exact(value, {'strategy', 'flag'}, 'mcp config_file_flag')
        return {'strategy': strategy, 'flag': _match(value['flag'], _FLAG, 'mcp flag')}
    if strategy == 'config_override':
        _exact(value, {'strategy', 'key'}, 'mcp config_override')
        return {'strategy': strategy, 'key': _match(value['key'], _FIELD, 'mcp key')}
    if strategy == 'config_content_env':
        _exact(value, {'strategy', 'env', 'key'}, 'mcp config_content_env')
        return {'strategy': strategy, 'env': _match(value['env'], _ENV, 'mcp env'),
                'key': _match(value['key'], _FIELD, 'mcp key')}
    raise ValueError('mcp strategy must be config_file_flag, config_override or config_content_env')


def _events(value):
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_EVENTS:
        raise ValueError('hooks need 1..16 events')
    events = [_match(event, _EVENT, 'hook event') for event in value]
    if len(set(events)) != len(events):
        raise ValueError('hook events must be unique')
    return events


def _hooks(value):
    strategy = value.get('strategy') if isinstance(value, dict) else None
    if strategy == 'settings_file_flag':
        _exact(value, {'strategy', 'flag', 'events'}, 'hooks settings_file_flag')
        return {'strategy': strategy, 'flag': _match(value['flag'], _FLAG, 'hooks flag'),
                'events': _events(value['events'])}
    if strategy == 'config_override':
        _exact(value, {'strategy', 'events'}, 'hooks config_override')
        return {'strategy': strategy, 'events': _events(value['events'])}
    if strategy == 'plugin_env':
        _exact(value, {'strategy', 'env', 'events'}, 'hooks plugin_env')
        return {'strategy': strategy, 'env': _match(value['env'], _ENV, 'hooks env'),
                'events': _events(value['events'])}
    raise ValueError('hooks strategy must be settings_file_flag, config_override or plugin_env')


def _root(value):
    if (not isinstance(value, str) or not 1 <= len(value) <= 4096
            or not (value.startswith('/') or value.startswith('~/'))):
        raise ValueError('capture root must be absolute or start with ~/')
    return value


def _capture(value):
    mode = value.get('mode') if isinstance(value, dict) else None
    if mode == 'none':
        _exact(value, {'mode'}, 'capture none')
        return {'mode': 'none'}
    if mode == 'transcript':
        _exact(value, {'mode', 'parser', 'root'}, 'capture transcript')
        if value['parser'] not in TRANSCRIPT_PARSERS:
            raise ValueError('capture transcript parser must be one of: ' + ', '.join(TRANSCRIPT_PARSERS))
        return {'mode': 'transcript', 'parser': value['parser'], 'root': _root(value['root'])}
    if mode == 'export':
        _exact(value, {'mode', 'parser'}, 'capture export')
        if value['parser'] not in EXPORT_PARSERS:
            raise ValueError('capture export parser must be one of: ' + ', '.join(EXPORT_PARSERS))
        return {'mode': 'export', 'parser': value['parser']}
    raise ValueError('capture mode must be transcript, export or none')


def _executable(value):
    if not isinstance(value, str) or len(value) > 4096:
        raise ValueError('invalid executable')
    if value.startswith('/'):
        return value
    return _match(value, _EXECUTABLE_NAME, 'executable name')


def validate_profile(value):
    """One exact profile; unknown fields are refused."""
    _exact(value, _FIELDS, 'a harness profile')
    if type(value['enabled']) is not bool:
        raise ValueError('enabled must be true or false')
    return {'harness': _match(value['harness'], _HARNESS, 'harness id'),
            'enabled': value['enabled'],
            'executable': _executable(value['executable']),
            'session_id': _session(value['session_id']),
            'mcp': _mcp(value['mcp']),
            'hooks': _hooks(value['hooks']),
            'capture': _capture(value['capture']),
            'receipt_env': _match(value['receipt_env'], _ENV, 'receipt_env')}


def validate(document):
    """An ``agent-tooling.harness-profiles.v1`` document; returns profiles by harness id."""
    _exact(document, {'schema_version', 'profiles'}, 'a harness profile document')
    if document['schema_version'] != SCHEMA:
        raise ValueError('unsupported harness profile schema')
    profiles = document['profiles']
    if not isinstance(profiles, list) or not 1 <= len(profiles) <= MAX_PROFILES:
        raise ValueError('a harness profile document has 1..64 profiles')
    clean = [validate_profile(profile) for profile in profiles]
    by_id = {profile['harness']: profile for profile in clean}
    if len(by_id) != len(clean):
        raise ValueError('harness ids must be unique')
    return by_id


def default_profiles():
    return validate(json.loads(files('kp_agent_tooling').joinpath('assets/harness-profiles.json').read_text()))


def operator_profiles_path(descriptor_path):
    """The optional ``harness_profiles_path`` named by the registry descriptor, or None."""
    from kp_agent_tooling._impl.service.desk_registry import load_descriptor
    value = load_descriptor(descriptor_path).get('harness_profiles_path')
    return None if value is None else Path(value)


def load(descriptor_path=None):
    """Defaults, replaced or extended by id from the operator file the descriptor names.

    A named operator file that is missing, not private, or invalid is refused;
    there is no silent fallback to the defaults.
    """
    profiles = default_profiles()
    path = operator_profiles_path(descriptor_path) if descriptor_path is not None else None
    if path is not None:
        profiles.update(validate(leaf.read_private_json(path, memo=call_memo())))
    return profiles


def select(profiles, harness):
    """The enabled profile for ``harness``; unknown or disabled harnesses are refused."""
    if not isinstance(harness, str) or not _HARNESS.fullmatch(harness):
        raise HarnessUnavailable('harness id must be a safe profile id')
    profile = profiles.get(harness)
    if profile is None:
        raise HarnessUnavailable('no harness profile for this harness')
    if not profile['enabled']:
        raise HarnessUnavailable('the harness profile is disabled')
    return profile


def transcript_root(profile):
    """The capture root as an absolute path (``~/`` expands to the running user's home)."""
    capture = profile['capture']
    if capture['mode'] != 'transcript':
        return None
    root = capture['root']
    return Path.home() / root[2:] if root.startswith('~/') else Path(root)
