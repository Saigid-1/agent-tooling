"""Host-owned card attribution. Does not launch agents or change admission."""
import argparse
import json
import sys
import sqlite3
from kp_agent_tooling._impl.service.episodic_memory import EpisodeUnavailable
from kp_agent_tooling._impl.service.desk_binding import DeskLaunchUnavailable
from kp_agent_tooling._impl.service.host_card_bridge import attach_card


def _main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--config', required=True)
    p.add_argument('--event', required=True)
    p.add_argument('--apply', action='store_true')
    args = p.parse_args()
    print(json.dumps(attach_card(args.config, args.event, apply=args.apply)))


def main():
    try:
        _main()
        return 0
    except (ValueError, OSError, sqlite3.Error, EpisodeUnavailable, DeskLaunchUnavailable) as error:
        print(f'host card: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
