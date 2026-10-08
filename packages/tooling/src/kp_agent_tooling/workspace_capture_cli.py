"""Operator-only bounded native workspace capture; no desk admission or model calls.

`watch` started before its operator files exist (the compose `capture` role on a new
runtime root) reports `not_configured`, naming them, and waits for them instead of
exiting: the role's supervisor keeps ingesting the host spool meanwhile.

`watch` survives its passes (T12a): a pass that raises anything prints one JSON line
(`status: error`, its `category`, the message capped at 512 characters and the attempt
number) and the loop sleeps with bounded exponential backoff, from the interval up to
10x the interval; a clean pass resets it. Only a signal, reaching `--max-passes`, or
operator-file absence (the `not_configured` wait, which parks it) ends or pauses it.
"""
import argparse
import json
import os
from pathlib import PurePosixPath
import stat
import time

from kp_agent_tooling._impl.service.desk_memory_runtime import components, private_json
from kp_agent_tooling._impl.service.workspace_capture import WorkspaceCapture, CaptureError, SCHEMA

# How often a not-configured watch looks for its operator files again.
NOT_CONFIGURED_POLL_SECONDS = 10
# A failed pass waits interval * 2**(attempt - 1), at most this many intervals.
BACKOFF_CAP_INTERVALS = 10
MESSAGE_CHARS = 512


def runtime_path(path):
    """A path as the operator names it: relative to the runtime root when the role
    declares where that root's directories are mounted (AGENT_RUNTIME_ROOT_MOUNTS)."""
    try:
        mounts = json.loads(os.environ.get('AGENT_RUNTIME_ROOT_MOUNTS') or '{}')
    except ValueError:
        mounts = {}
    pure = PurePosixPath(path)
    for target, relative in (mounts.items() if isinstance(mounts, dict) else ()):
        if isinstance(target, str) and isinstance(relative, str) and pure.is_absolute() \
                and pure.is_relative_to(target):
            return str(PurePosixPath(relative, pure.relative_to(target)))
    return str(path)


def unwritten(path):
    """An operator file not yet written: nothing at the path, or an empty regular file."""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return True
    return stat.S_ISREG(info.st_mode) and info.st_size == 0


def _await_operator_files(paths, interval, clock=time):
    """Report the unwritten operator files whenever that set changes; return once all are written."""
    shown = None
    while True:
        missing = [str(path) for path in paths if unwritten(path)]
        if not missing:
            return
        if missing != shown:
            print(json.dumps({'schema_version': SCHEMA, 'status': 'not_configured',
                              'missing': [runtime_path(path) for path in missing], 'missing_paths': missing,
                              'detail': 'workspace capture waits for its operator files and starts once they '
                                        'exist; nothing is captured until then'}, sort_keys=True), flush=True)
            shown = missing
        clock.sleep(interval)


def category(error):
    """The exception's class family: its declared `category`, else its class name."""
    declared = getattr(error, 'category', None)
    return declared if isinstance(declared, str) and declared else type(error).__name__


def backoff(interval, attempt):
    """Seconds to wait after the `attempt`-th consecutive failed pass (1-based)."""
    return min(interval * 2 ** (attempt - 1), interval * BACKOFF_CAP_INTERVALS)


def watch(worker, paths, interval, max_passes=None, clock=time):
    """Run capture passes until a signal or `max_passes`; never end on a pass error.

    `clock` is the loop's only source of waiting (`clock.sleep(seconds)`); tests inject one.
    Returns 0, or 1 when the last pass (at `max_passes`) failed.
    """
    passes = attempt = 0
    while True:
        passes += 1
        last = max_passes is not None and passes >= max_passes
        try:
            _await_operator_files(paths, min(NOT_CONFIGURED_POLL_SECONDS, interval), clock)
            result = worker().once()
        except Exception as error:  # a signal (KeyboardInterrupt, SystemExit) still ends the loop
            attempt += 1
            delay = backoff(interval, attempt)
            report = {'schema_version': SCHEMA, 'status': 'error', 'category': category(error),
                      'exception': type(error).__name__, 'message': str(error)[:MESSAGE_CHARS],
                      'attempt': attempt, 'pass': passes}
            if not last:
                report['retry_in_seconds'] = delay
            print(json.dumps(report, sort_keys=True), flush=True)
        else:
            attempt, delay = 0, interval
            print(json.dumps(result, sort_keys=True), flush=True)
        if last:
            return 1 if attempt else 0
        clock.sleep(delay)


def main(argv=None, *, clock=time):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True, help='Existing private desk-memory configuration')
    parser.add_argument('--policy', required=True, help='Private operator capture policy')
    parser.add_argument('action', choices=('preview', 'once', 'watch'))
    parser.add_argument('--interval-seconds', type=int, default=30)
    parser.add_argument('--max-passes', type=int, default=None,
                        help='watch: stop after this many passes (default: unbounded)')
    args = parser.parse_args(argv)
    try:
        def worker():
            policy = private_json(args.policy)
            config, registry, ledger, store = components(args.config)
            admission = ledger.resolve(config['provider_session_id'], registry)
            if policy.get('tenant_id') != admission.tenant_id:
                raise CaptureError('capture tenant differs from the configured admitted tenant')
            return WorkspaceCapture(store, policy)
        if args.action == 'preview':
            result = worker().preview()
        elif args.action == 'once':
            result = worker().once()
        else:
            if not 5 <= args.interval_seconds <= 3600:
                raise CaptureError('watch interval must be 5..3600 seconds')
            if args.max_passes is not None and args.max_passes < 1:
                raise CaptureError('watch --max-passes must be at least 1')
            return watch(worker, (args.config, args.policy), args.interval_seconds, args.max_passes, clock)
        print(json.dumps(result, sort_keys=True))
        return 0 if result['status'] == 'ok' else 1
    except (CaptureError, OSError, ValueError) as error:
        print(json.dumps({'schema_version': SCHEMA, 'status': 'error', 'message': str(error)[:512]}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
