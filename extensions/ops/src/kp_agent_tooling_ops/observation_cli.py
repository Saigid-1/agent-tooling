"""Operator-controlled immutable observation imports and reads."""
import argparse
import json
from pathlib import Path

from kp_agent_tooling_ops._impl.observations import ObservationStore
from kp_agent_tooling_ops._impl.observation_adapters import serena_observation


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--registry', required=True)
    parser.add_argument('action', choices=['import-serena', 'import-record', 'list', 'read', 'assess'])
    parser.add_argument('--input', help='Path to one complete JSON response or canonical observation')
    parser.add_argument('--subject', help='Explicit subject for import or list filter')
    parser.add_argument('--id', help='Content identity for read')
    parser.add_argument('--scope', help='Expected scope JSON file for assess')
    parser.add_argument('--ids', help='JSON array of content identities for assess')
    args = parser.parse_args()
    store = ObservationStore(args.registry)
    if args.action in {'import-serena', 'import-record'}:
        if not args.input or (args.action == 'import-serena' and not args.subject):
            parser.error('input and explicit subject required for Serena import; input required for record import')
        value = json.loads(Path(args.input).read_text())
        record = serena_observation(value, args.subject) if args.action == 'import-serena' else value
        result = {'id': store.append(record)}
    elif args.action == 'list':
        result = store.list(subject=args.subject)
    elif args.action == 'read':
        if not args.id: parser.error('--id required')
        result = store.read_bounded(args.id)
    else:
        if not args.scope or not args.ids: parser.error('--scope and --ids required')
        result = store.assess(json.loads(args.ids), json.loads(Path(args.scope).read_text()))
    print(json.dumps(result))


if __name__ == '__main__':
    main()
