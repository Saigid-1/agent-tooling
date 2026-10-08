"""Create a memory budget receipt from supplied or fetched OpenRouter metadata.

Fetching calls only the models metadata endpoint; this script makes no model call.
Select one exact model and route for the trial.
"""

import argparse
import json
import os
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kp_agent_tooling._impl.service.memory_budget import DEFAULT_SUMMARY_MODEL, memory_budget, openrouter_profile
from kp_agent_tooling._impl.service.openrouter_models import OpenRouterModelRegistryClient, _openrouter_models_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--metadata-json', type=Path)
    source.add_argument('--fetch', action='store_true', help='fetch public models metadata (no model call)')
    parser.add_argument('--api-key-env', default='OPENROUTER_API_KEY')
    parser.add_argument('--model-id', default=DEFAULT_SUMMARY_MODEL, help='Summary baseline; explicit model selection remains supported')
    parser.add_argument('--provider-id', default='openrouter')
    parser.add_argument('--observed-at', help='ISO 8601 timestamp of supplied metadata fetch')
    parser.add_argument('--route-json', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    for name in ('system', 'tool', 'reasoning', 'output-tokens', 'safety'):
        parser.add_argument('--' + name, type=int, required=True)
    args = parser.parse_args()
    if args.fetch:
        key = os.environ.get(args.api_key_env)
        if not key:
            parser.error('fetch requires API key in selected environment variable')
        payload = _openrouter_models_json(key, 5.0)
        observed = datetime.now(timezone.utc)
    else:
        if not args.observed_at:
            parser.error('--observed-at required with supplied metadata')
        payload = json.loads(args.metadata_json.read_text())
        observed = datetime.fromisoformat(args.observed_at.replace('Z', '+00:00'))
    registry = OpenRouterModelRegistryClient._parse(payload, 'offline-metadata-only')
    if args.model_id not in registry:
        parser.error('selected model absent or invalid in models response')
    entries = [row for row in payload['data'] if isinstance(row, dict) and row.get('id') == args.model_id]
    if len(entries) != 1:
        parser.error('selected model must appear exactly once')
    route = json.loads(args.route_json.read_text()) if args.route_json else None
    profile = openrouter_profile(entries[0], provider_id=args.provider_id,
                                 observed_at=observed, route=route)
    budget = memory_budget(profile, system_tokens=args.system, tool_tokens=args.tool,
                           reasoning_tokens=args.reasoning, output_tokens=args.output_tokens,
                           safety_tokens=args.safety)
    result = {'schema_version': 'ops.memory-model-profile.v1',
              'raw_model_entry': entries[0], 'selected_route': route,
              'profile': {**asdict(profile), 'observed_at': profile.observed_at.isoformat()},
              'budget': {'system_tokens': budget.system_tokens,
                         'tool_tokens': budget.tool_tokens,
                         'reasoning_tokens': budget.reasoning_tokens,
                         'output_tokens': budget.output_tokens,
                         'safety_tokens': budget.safety_tokens,
                         'input_tokens': budget.input_tokens},
              'chunk_counter': 'utf8-byte-estimate',
              'generated_at': datetime.now(timezone.utc).isoformat()}
    if args.output.exists() or args.output.is_symlink():
        parser.error('output exists; receipts are immutable')
    args.output.write_text(json.dumps(result, sort_keys=True, indent=2) + '\n')
    args.output.chmod(0o600)


if __name__ == '__main__':
    main()
