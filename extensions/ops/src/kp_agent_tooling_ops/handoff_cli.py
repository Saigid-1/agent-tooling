#!/usr/bin/env python3
"""Validate submitted findings and check a final-text handoff in one adapter process.

The former ``kp-agent-tooling --config C handoff --arguments J`` action; run it as
``python -m kp_agent_tooling_ops.handoff_cli --config C --arguments J``. The server's
OPS tool provider must be installed, because validation uses ``verification.finding``.
"""
import argparse
import json
from kp_agent_tooling._impl.service.agent_tooling import AgentTooling


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',required=True)
    parser.add_argument('--arguments',default='{}')
    args=parser.parse_args();adapter=AgentTooling(args.config)
    from kp_agent_tooling_ops._impl.verification_handoff import check_handoff
    payload=json.loads(args.arguments)
    submissions=payload.get('findings',[])
    if not 1<=len(submissions)<=20:raise ValueError('1..20 submitted findings required')
    validations=[adapter.call('verification.finding',{'mode':'validate','finding':f}) for f in submissions]
    check=check_handoff(payload['final_text'],[v['finding_sha256'] for v in validations],adapter.validation_receipts)
    result={'status':check['status'],'validations':validations,'handoff':check}
    failed=isinstance(result,dict) and result.get('status') in {'error','unavailable','invalid'}
    result=adapter.delivery.deliver(result)
    print(json.dumps(result))
    if failed or (isinstance(result,dict) and result.get('status')=='error'):raise SystemExit(1)
if __name__=='__main__':main()
