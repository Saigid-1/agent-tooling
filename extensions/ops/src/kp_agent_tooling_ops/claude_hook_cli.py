#!/usr/bin/env python3
"""Operator-scoped Claude capture hooks; no inference or global configuration edits."""
import argparse
import json
from pathlib import Path
import shlex
import sys

from kp_agent_tooling._impl import leaf
from kp_agent_tooling._impl.service.episodic_memory_tools import from_config
from kp_agent_tooling._impl.service.episodic_queue import ConsolidationQueue
from kp_agent_tooling._impl.service.claude_episode_capture import ClaudeEpisodeCapture
from kp_agent_tooling._impl.service.claude_memory_hook import HookTelemetry,handle_hook


def main():
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('memory-config','capture-ledger','queue','telemetry','transcript'):
        p.add_argument('--'+name,required=True,type=Path)
    p.add_argument('--native-session-id')
    p.add_argument('--seed-offset',type=int)
    p.add_argument('--seed-prefix-sha256')
    p.add_argument('--seed-provenance',type=Path)
    p.add_argument('--batch-bytes',type=int,default=32768)
    p.add_argument('--host-command-prefix',help='Trusted host argv prefix for scoped hook settings, e.g. absolute docker exec -i container')
    p.add_argument('--hook-transcript-path',type=Path,help='Exact host payload transcript path when Docker uses a different container path')
    p.add_argument('action',choices=['initialize','bind','seed','settings','hook','status'])
    args=p.parse_args()
    adapter=None
    payload=None
    try:
        from kp_agent_tooling._impl.service.desk_memory_runtime import components
        configuration, _, _, _ = components(args.memory_config)
        workspace=leaf.mark_store(configuration['state_root']).resolve()
        for path in (args.capture_ledger,args.queue,args.telemetry):
            if not path.is_absolute() or path.is_symlink() or not path.resolve().is_relative_to(workspace):
                raise ValueError('state must remain in the configured private state root')
        if not args.memory_config.is_absolute() or not args.transcript.is_absolute():
            raise ValueError('absolute operator paths required')
        if args.hook_transcript_path and not args.hook_transcript_path.is_absolute():
            raise ValueError('absolute host hook transcript path required')
        if not 1000<=args.batch_bytes<=200000:
            raise ValueError('bounded source batch required')
        if args.action=='settings':
            prefix=shlex.split(args.host_command_prefix) if args.host_command_prefix else []
            if prefix and (not Path(prefix[0]).is_absolute() or any('\n' in arg or '\r' in arg for arg in prefix)):
                raise ValueError('host command prefix needs an absolute executable and one-line arguments')
            cmd=prefix+[sys.executable,str(Path(__file__).resolve()),
                 '--memory-config',str(args.memory_config),'--capture-ledger',str(args.capture_ledger),
                 '--queue',str(args.queue),'--telemetry',str(args.telemetry),'--transcript',str(args.transcript),
                 '--batch-bytes',str(args.batch_bytes),'hook']
            if args.native_session_id:
                cmd[-1:-1]=['--native-session-id',args.native_session_id]
            if args.hook_transcript_path:
                cmd[-1:-1]=['--hook-transcript-path',str(args.hook_transcript_path)]
            hook={'type':'command','command':shlex.join(cmd),'timeout':30}
            print(json.dumps({'hooks':{event:[{'hooks':[hook]}] for event in
                                      ('Stop','PreCompact','SessionEnd')}},indent=2))
            return 0
        adapter=from_config(args.memory_config)
        queue=ConsolidationQueue(leaf.mark_store(args.queue),store=adapter.store)
        from kp_agent_tooling._impl.service.episodic_search import EpisodicSearchIndex
        index=EpisodicSearchIndex(leaf.store_path(adapter.store.path, leaf.SEARCH_INDEX_DB, sibling=True),episode_store=adapter.store)
        capture=ClaudeEpisodeCapture(leaf.mark_store(args.capture_ledger),native_session_id=args.native_session_id,
            hook_transcript_path=args.hook_transcript_path,index=index)
        telemetry=HookTelemetry(leaf.mark_store(args.telemetry))
        if args.action=='initialize':
            # Refuse partial reinitialization; queue is initialized separately.
            if any(path.exists() for path in (args.capture_ledger,args.telemetry)):
                raise ValueError('capture or telemetry state already exists')
            queue.list(adapter.session,limit=1)
            capture.initialize()
            telemetry.initialize()
            print(json.dumps({'status':'initialized','model_requests':0}))
        elif args.action=='bind':
            print(json.dumps(capture.bind(adapter.session,store=adapter.store,transcript_path=args.transcript)))
        elif args.action=='seed':
            if args.seed_provenance is None:
                raise ValueError('operator receipt required')
            print(json.dumps(capture.seed(adapter.session,store=adapter.store,transcript_path=args.transcript,
                offset=args.seed_offset,prefix_sha256=args.seed_prefix_sha256,
                provenance=json.loads(args.seed_provenance.read_text()))))
        elif args.action=='status':
            print(json.dumps({'capture':capture.status(adapter.session,store=adapter.store,
                transcript_path=args.transcript),'queue':queue.metrics(adapter.session),
                'capture_receipts':capture.receipts(adapter.session,store=adapter.store),
                'receipts':telemetry.recent(adapter.store,adapter.session)}))
        else:
            raw=sys.stdin.buffer.read(65537)
            if len(raw)>65536:
                raise ValueError('hook payload exceeds bound')
            payload=json.loads(raw)
            handle_hook(payload,session=adapter.session,transcript_path=args.transcript,
                        store=adapter.store,queue=queue,capture=capture,telemetry=telemetry,
                        source_batch_bytes=args.batch_bytes,hook_transcript_path=args.hook_transcript_path)
            print(json.dumps({'suppressOutput':True}))
        return 0
    except Exception as error:
        # Never echo paths, transcripts, credentials or raw exception bodies.
        print(json.dumps({'status':'error','category':leaf.error_category(error),
            'message':'Claude memory capture did not complete. Inspect the hook status; do not infer that pending source was saved.'}),file=sys.stderr)
        return 2 if isinstance(payload,dict) and payload.get('hook_event_name')=='PreCompact' else 1
    finally:
        if adapter is not None:
            adapter.close()


if __name__=='__main__':
    raise SystemExit(main())
