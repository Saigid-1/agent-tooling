"""Operator-selected Claude sidebar binding; reuses portable capture and MCP.

The launch contract selects one workspace/store. Hook payloads can neither select
another desk nor grant admission. No outbound model calls occur here. The file,
transcript, hook-settings and MCP helpers are the shared launch-binding core's.
"""
import argparse
import fcntl
import json
from pathlib import Path
import sys
import uuid

from kp_agent_tooling._impl import leaf
from kp_agent_tooling._impl.service.desk_memory_runtime import private_json, components, initialize, admit
from kp_agent_tooling._impl.service.episodic_memory_tools import from_config
from kp_agent_tooling._impl.service.episodic_queue import ConsolidationQueue
from kp_agent_tooling._impl.service.episodic_search import EpisodicSearchIndex
from kp_agent_tooling._impl.service.claude_episode_capture import ClaudeEpisodeCapture
from kp_agent_tooling._impl.service.claude_memory_hook import HookTelemetry, handle_hook
from kp_agent_tooling._impl.service.launch_binding import (
    claude_transcript, hook_command, hook_settings, memory_server, write_private,
)


def prepare(binding_path, workspace, task):
    binding = private_json(binding_path)
    if set(binding) != {'schema_version','config_template','transcript_root'} or binding['schema_version'] != 'agent.assistant-host.v1':
        raise ValueError('invalid assistant host binding')
    # The template is an operator file (T11b Q4): never on /state, where it once sat beside its
    # launches. The leaf's store-path rule names where it belongs; nothing under /state is read.
    required = leaf.required_store_path(leaf.TEMPLATE_KEY, binding['config_template'], reject_control=False)
    if required:
        raise ValueError(f"assistant config_template {binding['config_template']!r} is on /state; "
                         f'write the template as an operator file ({required}) and name it in the binding')
    template = Path(binding['config_template'])
    config = private_json(template)
    if config.get('schema_version') != 'ops.assistant-memory.local.v1':
        raise ValueError('isolated assistant configuration required')
    if Path(workspace).resolve() != Path(config['workspace_root']).resolve() or not task.startswith('__home_agent__:'):
        raise ValueError('assistant binding does not select this workspace/sidebar')
    root = leaf.mark_store(config['state_root'])
    # Validate private physical roots and single assistant ownership before mutation.
    _, registry, _, _ = components(template)
    native = str(uuid.uuid4())
    if not leaf.store_path(root, leaf.SESSIONS_DB).exists():
        initialize(template)
    # Launches live in the template's own state_root (T11b Q4), never beside the template.
    launches = leaf.launches_dir(root)
    leaf.mkdir_private(launches, exist_ok=True)
    if launches.is_symlink() or leaf.shared_bits(launches.stat().st_mode):
        raise ValueError('private launch directory required')
    run = launches / native
    leaf.mkdir_private(run)
    config.update(provider_instance='kanban-claude-assistant', provider_session_id=native)
    config_path = run / 'memory.json'
    write_private(config_path, config)
    admitted = admit(config_path, desk_id=registry.list_bindings()[0].binding_key,
                     provider_id='claude-code', model_id='host-selected-unverified')
    # Claude project transcript layout uses a sanitized absolute cwd.
    transcript = claude_transcript(binding['transcript_root'], Path(workspace).resolve(), native)
    if not transcript.is_absolute():
        raise ValueError('absolute transcript root required')
    paths = {name:str(leaf.store_path(root, file)) for name, file in
             (('capture', leaf.CAPTURE_DB), ('queue', leaf.QUEUE_DB), ('telemetry', leaf.TELEMETRY_DB))}
    adapter = from_config(config_path)
    try:
        for name, obj in [('queue',ConsolidationQueue(paths['queue'],store=adapter.store)),
                          ('capture',ClaudeEpisodeCapture(paths['capture'])),
                          ('telemetry',HookTelemetry(paths['telemetry']))]:
            if not Path(paths[name]).exists(): obj.initialize()
    finally:
        adapter.close()
    receipt = dict(schema_version='agent.assistant-launch.v1', config=str(config_path),
                   native_session_id=native, task_id=task, workspace=str(Path(workspace).resolve()),
                   transcript=str(transcript), binding_key=admitted['binding_key'], **paths)
    launch_path = run / 'launch.json'
    write_private(launch_path, receipt)
    command = hook_command('kp_agent_tooling.assistant_host_cli', '--launch', launch_path, 'hook')
    settings = run / 'hooks.json'
    write_private(settings, hook_settings(('SessionStart','Stop','PreCompact','SessionEnd'), command))
    mcp = run / 'mcp.json'
    write_private(mcp, {'mcpServers':{'assistant-memory':memory_server(config_path)}})
    return {'native_session_id':native, 'settings_path':str(settings), 'mcp_path':str(mcp),
            'launch_receipt':str(launch_path), 'binding_key':admitted['binding_key']}


def hook(launch_path, payload):
    launch_path = leaf.mark_store(launch_path)  # a launch receipt: a store path
    receipt = private_json(launch_path)
    config, _, _, _ = components(leaf.mark_store(receipt['config']))
    if config['schema_version'] != 'ops.assistant-memory.local.v1':
        raise ValueError('isolated assistant configuration required')
    root = leaf.mark_store(config['state_root']).resolve()
    # A launch of this state root is under its launches/ (T11b Q4); one written beside an old
    # /state template is refused, and its session is relaunched (T9b D3).
    if launch_path.parent.parent.resolve() != leaf.launches_dir(config['state_root']).resolve():
        raise ValueError('launch receipt is not a launch of this state root')
    for key in ('capture','queue','telemetry'):
        p=Path(receipt[key])
        if p.is_symlink() or not p.resolve().is_relative_to(root):
            raise ValueError('assistant capture state escaped isolated root')
    if (payload.get('session_id') != receipt['native_session_id'] or
        Path(payload.get('cwd','')).resolve() != Path(receipt['workspace']) or
        Path(payload.get('transcript_path','')).resolve() != Path(receipt['transcript']).resolve()):
        raise ValueError('hook does not match exact launched assistant session')
    if payload.get('hook_event_name') == 'SessionStart':
        return {'hookSpecificOutput':{'hookEventName':'SessionStart','additionalContext':
            'You are the Workspace Assistant. Your assistant-memory MCP tools use an isolated desk store. '
            'Use memory.search/read_event for relevant context; cite sources and distinguish observations from inferences. '
            'Stop and pre-compaction hooks capture and index visible transcript events automatically; do not claim capture without a receipt. '
            'No global desk-memory access is configured for this trial.'}}
    adapter = from_config(leaf.mark_store(receipt['config']))
    try:
        index = EpisodicSearchIndex(leaf.store_path(root, leaf.SEARCH_INDEX_DB),episode_store=adapter.store)
        capture = ClaudeEpisodeCapture(leaf.mark_store(receipt['capture']),native_session_id=receipt['native_session_id'],index=index)
        # Identity is fixed by the launch receipt, not selected by the hook payload.
        capture.bind(adapter.session,store=adapter.store,transcript_path=Path(receipt['transcript']))
        handle_hook(payload,session=adapter.session,transcript_path=Path(receipt['transcript']),
                    store=adapter.store,queue=ConsolidationQueue(leaf.mark_store(receipt['queue']),store=adapter.store),
                    capture=capture,telemetry=HookTelemetry(leaf.mark_store(receipt['telemetry'])))
        return {'suppressOutput':True}
    finally:
        adapter.close()


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--binding',type=Path); p.add_argument('--workspace'); p.add_argument('--task')
    p.add_argument('--launch',type=Path); p.add_argument('action',choices=['prepare','hook'])
    a=p.parse_args()
    try:
        if a.action=='prepare':
            if not a.binding or not a.workspace or not a.task: raise ValueError('launch coordinates required')
            # Serialize preparation so parallel browser requests cannot partially initialize shared state.
            with a.binding.open('rb') as lock:
                fcntl.flock(lock,fcntl.LOCK_EX)
                result=prepare(a.binding,a.workspace,a.task)
        else:
            raw=sys.stdin.buffer.read(65537)
            if len(raw)>65536: raise ValueError('bounded hook payload required')
            result=hook(a.launch,json.loads(raw))
        print(json.dumps(result)); return 0
    except Exception as error:
        print(json.dumps({'status':'error','category':leaf.error_category(error),
            'message':'Assistant binding/capture failed; inspect launch and hook receipts. No fallback store.'}),file=sys.stderr)
        return 2

if __name__=='__main__': raise SystemExit(main())
