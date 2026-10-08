#!/usr/bin/env python3
"""Operator entry for durable consolidation; one bounded worker job per invocation."""
import argparse
import json
import os
from pathlib import Path
import sys

from kp_agent_tooling._impl import leaf
from kp_agent_tooling._impl.service.episodic_memory_tools import from_config
from kp_agent_tooling._impl.service.episodic_queue import ConsolidationQueue
from kp_agent_tooling._impl.service.episodic_summarizer import OpenRouterEpisodeSummarizer
from kp_agent_tooling._impl.service.memory_budget import DEFAULT_SUMMARY_MODEL, load_memory_budget_receipt


def summary_options(model_id, *, json_output=None, reasoning_effort=None, disable_reasoning=False):
    baseline = model_id == DEFAULT_SUMMARY_MODEL
    return {
        'json_output': baseline if json_output is None else json_output,
        'reasoning_effort': ('low' if baseline and not disable_reasoning else None)
            if reasoning_effort is None else reasoning_effort,
    }


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--config',required=True,help='Existing admitted memory tools configuration')
    p.add_argument('--queue',required=True,type=Path)
    p.add_argument('action',choices=['initialize','enqueue','status','work-once'])
    p.add_argument('--episode',action='append',default=[])
    p.add_argument('--reason',choices=['batch','context_threshold','session_end','manual'],default='manual')
    p.add_argument('--job')
    p.add_argument('--profile',type=Path)
    p.add_argument('--approved-digests',type=Path,help='Operator-approved visible-event SHA256 JSON list')
    p.add_argument('--key-env')
    p.add_argument('--worker',default='episodic-cli')
    p.add_argument('--max-proposal-calls',type=int,default=4)
    p.add_argument('--timeout-seconds',type=float,default=120,help='Per-call deadline, greater than 0 and at most 120 seconds; never enables retry')
    p.add_argument('--disable-reasoning',action='store_true',help='Explicit opt-in; fresh model metadata must advertise optional reasoning')
    p.add_argument('--allow-provider-fallbacks',action=argparse.BooleanOptionalAction,default=True,help='Allow fallback among eligible providers (default); no client retries')
    p.add_argument('--require-zdr',action=argparse.BooleanOptionalAction,default=True,help='Require zero retention and deny data collection (default); no privacy downgrade on failure')
    p.add_argument('--diagnostics',action='store_true',help='Emit content-free request and transport diagnostics to stderr immediately')
    p.add_argument('--reasoning-effort',choices=['low','medium','high','max'],help='Explicit effort advertised by fresh model metadata')
    p.add_argument('--json-output',action=argparse.BooleanOptionalAction,default=None,help='GLM baseline defaults to JSON; other models opt in after metadata validation')
    args=p.parse_args()
    adapter=None
    try:
        from kp_agent_tooling._impl.service.desk_memory_runtime import components
        configuration, _, _, _ = components(args.config)
        workspace=leaf.mark_store(configuration['state_root']).resolve()
        for path in (args.queue,):
            if not path.is_absolute() or path.is_symlink() or not path.resolve().is_relative_to(workspace):
                raise ValueError('state must remain in the configured private state root')
        adapter=from_config(args.config)
        queue=ConsolidationQueue(leaf.mark_store(args.queue),store=adapter.store)
        if args.action=='initialize':
            queue.initialize()
            result={'status':'initialized','model_requests':0}
        elif args.action=='enqueue':
            result=queue.enqueue(adapter.session,episode_ids=args.episode,reason=args.reason)
        elif args.action=='status':
            result=queue.get(adapter.session,args.job) if args.job else queue.list(adapter.session)
        else:
            if not args.profile or not args.approved_digests or not args.key_env:
                raise ValueError('profile, exact source approval and key environment reference required')
            if args.profile.stat().st_size>262144 or args.approved_digests.stat().st_size>65536:
                raise ValueError('bounded configuration required')
            approved=json.loads(args.approved_digests.read_text())
            if (not isinstance(approved,list) or not 1<=len(approved)<=512 or
                any(not isinstance(d,str) or len(d)!=64 or any(c not in '0123456789abcdef' for c in d)
                    for d in approved)):
                raise ValueError('exact visible-event digest allowlist required')
            profile_receipt=json.loads(args.profile.read_text())
            budget=load_memory_budget_receipt(profile_receipt)
            selected=summary_options(budget.profile.model_id,json_output=args.json_output,
                reasoning_effort=args.reasoning_effort,disable_reasoning=args.disable_reasoning)
            args.json_output=selected['json_output']
            args.reasoning_effort=selected['reasoning_effort']
            if args.disable_reasoning:
                model=profile_receipt['raw_model_entry']
                if ('reasoning' not in model.get('supported_parameters',[]) or
                        model.get('reasoning',{}).get('mandatory') is not False):
                    raise ValueError('model metadata does not establish optional reasoning')
            if args.reasoning_effort and (args.disable_reasoning or 'reasoning' not in profile_receipt['raw_model_entry'].get('supported_parameters',[]) or args.reasoning_effort not in profile_receipt['raw_model_entry'].get('reasoning',{}).get('supported_efforts',[])):
                raise ValueError('model metadata does not establish selected reasoning effort')
            if args.json_output and 'response_format' not in profile_receipt['raw_model_entry'].get('supported_parameters',[]):
                raise ValueError('model metadata does not establish JSON output support')
            policy = None
            if configuration['schema_version'] == 'ops.assistant-memory.local.v1':
                from kp_agent_tooling._impl.service.assistant_memory_policy import load_policy
                policy = load_policy(configuration['assistant_policy_path'])
            proposer=OpenRouterEpisodeSummarizer(api_key=os.environ[args.key_env],budget=budget,timeout=args.timeout_seconds,disable_reasoning=args.disable_reasoning,json_output=args.json_output,reasoning_effort=args.reasoning_effort,allow_fallbacks=args.allow_provider_fallbacks,require_zdr=args.require_zdr,diagnostic_sink=(lambda event: print(json.dumps(event),file=sys.stderr,flush=True)) if args.diagnostics else None, trusted_policy=policy)
            result=queue.run_once(adapter.session,args.worker,propose=proposer,budget=budget,
                approve_sources=lambda digests:all(d in approved for d in digests),
                max_proposal_calls=args.max_proposal_calls)
            if result is None:result={'status':'idle','model_requests':0}
        print(json.dumps(result))
    except Exception as error:
        print(json.dumps({'status':'error','category':leaf.error_category(error),
            'guidance':'Inspect the desk-bound queue status and configuration; uncertain requests are not retried.'}))
        return 1
    finally:
        if adapter is not None:adapter.close()
    return 0


if __name__=='__main__':
    raise SystemExit(main())
