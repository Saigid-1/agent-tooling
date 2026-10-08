#!/usr/local/bin/python
"""Container lifecycle only. Admission and tool execution remain explicit.

Role readiness: a role command whose operator files are absent (refresh's request,
workspace capture's config and policy) waits for them and logs `not_configured`
instead of exiting. `health` reports the same state, naming the files relative to
the runtime root; the container is up and doing what it should, so health still
exits 0 and the configuration scope says `not_configured`.

Store preflight (T9b), at the start of every role of a rendered runtime root (the
manifest sets AGENT_MEMORY_VOLUME and AGENT_RUNTIME_ROOT_MOUNTS): the role refuses to
start, with a named reason, when /state/memory is not the project's volume according to
the mount table, when the store is not prepared (the volume root is not this user with
mode 0700, or a pre-T9b store directory is still on the /state bind), or when an operator
file under /config names a store path outside /state/memory or a `config_template` under
/state (the assistant's template is an operator file under /config, T11b Q4). It never
creates a store on the bind. Tools started later with `docker exec` are not checked here:
the leaf refuses their store paths at open (`store_outside_volume`, T11b Q1).

Refusals wait (T12a): a role's long-running command (`wait`, `kanban`, a `--watch` loop)
refused by the store preflight, for any reason, stays up instead of exiting 3, so the
restart policy never loops on a refusal. It logs the refusal as JSON, with its RECOVERY,
whenever it changes, re-checks every REFUSAL_POLL_SECONDS, and execs the role command
only once the preflight passes; until then `health` reports `refused` and exits 1. The
wait is the preflight's alone: nothing a role command raises after its exec is seen
here. A one-off command (`docker compose run ... find`) is still refused at once, exit 3.
"""
import http.client
import json
import os
from pathlib import Path, PurePosixPath
import re
import signal
import shutil
import stat
import sys
import time

# The installed package's leaf (kp-agent-tooling is installed in product-base): the one
# store-path rule and the private-directory helper.
from kp_agent_tooling._impl import leaf

# The board role keeps its own state root, which has no navigation configuration.
BOARD_COMMAND = 'kanban'
BOARD_PORT = 3486
# Options that name operator files in a role command, by its executable.
OPERATOR_OPTIONS = {'kp-agent-refresh': ('--request',),
                    'kp-agent-workspace-capture': ('--config', '--policy')}


def role_command():
    """The argv this container was started with: PID 1 (docker-init's, when present)."""
    try:
        raw = Path('/proc/1/cmdline').read_bytes()
    except OSError:
        return []
    return [part.decode(errors='replace') for part in raw.split(b'\0') if part]


def operator_files(command):
    """The operator files a role command names, in order."""
    found = []
    for index, token in enumerate(command):
        options = OPERATOR_OPTIONS.get(PurePosixPath(token).name)
        for position in range(index + 1, len(command) - 1) if options else ():
            if command[position] in options:
                found.append(command[position + 1])
    return found


def runtime_path(path):
    """A container path as the operator names it, relative to the runtime root."""
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


def readiness():
    """`ready`, or `not_configured` with the unwritten operator files of this role."""
    missing = [path for path in operator_files(role_command()) if unwritten(path)]
    if not missing:
        return {'status': 'ready', 'scope': 'lifecycle-and-configuration'}
    return {'status': 'not_configured', 'scope': 'lifecycle-and-configuration',
            'missing': [runtime_path(path) for path in missing],
            'detail': 'the role is up and waits for these operator files'}


def check():
    marker = Path('/state/.ops-tooling-volume')
    if marker.is_symlink() or not marker.is_file() or marker.read_text().strip() != 'ops-tooling-state-v1':
        raise ValueError('Required state volume marker absent; no automatic fallback')
    if not Path('/config/navigation.json').is_file():
        raise ValueError('Required navigation configuration absent')
    value = json.loads(Path('/config/navigation.json').read_text())
    if value.get('schema_version') != 'ops.agent-tooling.v1':
        raise ValueError('Invalid navigation configuration')
    # Health covers lifecycle/configuration, not live provider coverage or freshness.
    for key in ('snapshot_registry', 'navigation_registry_path'):
        path = Path(value[key])
        if not path.is_absolute() or path.is_symlink() or not path.is_dir():
            raise ValueError('Required state directory missing: ' + key)


STORE = leaf.STORE_ROOT
# Pre-T9b store directories on the /state bind; `kp-agent-install prepare` moves them.
LEGACY_STORES = leaf.LEGACY_STORES
STORE_KEYS = leaf.STORE_KEYS
TEMPLATE_KEY = leaf.TEMPLATE_KEY
OCTAL = re.compile(r'\\([0-7]{3})')


class StoreRefused(Exception):
    def __init__(self, reason, detail):
        super().__init__(detail)
        self.reason, self.detail = reason, detail


def _unescape(field):
    return OCTAL.sub(lambda match: chr(int(match.group(1), 8)), field)


def mounts():
    """{mount point: root within its filesystem} from /proc/self/mountinfo (the last mount wins)."""
    found = {}
    try:
        lines = Path('/proc/self/mountinfo').read_text().splitlines()
    except OSError:
        return found
    for line in lines:
        fields = line.split()
        if len(fields) > 4:
            found[_unescape(fields[4])] = _unescape(fields[3])
    return found




def operator_store_paths(base=Path('/config')):
    """Operator JSON files under /config naming a store path outside /state/memory, or a
    `config_template` under /state (T11b Q4: the assistant's template is an operator file
    under /config; the exemption for one under /state/memory/assistant is gone). Only files
    under /config are read: a template in the volume is never opened here."""
    found, seen = [], 0
    for current, dirnames, filenames in os.walk(base):
        dirnames[:] = sorted(name for name in dirnames if not os.path.islink(os.path.join(current, name)))
        for name in sorted(filenames):
            path = Path(current) / name
            if not name.endswith('.json') or path.is_symlink():
                continue
            seen += 1
            if seen > 2000:
                return found
            try:
                if path.stat().st_size > 1_000_000:
                    continue
                value = json.loads(path.read_bytes())
            except (OSError, ValueError):
                continue
            if not isinstance(value, dict):
                continue
            for key in (*STORE_KEYS, TEMPLATE_KEY):
                required = leaf.required_store_path(key, value.get(key), reject_control=False)
                if required:
                    found.append(f'{path} {key} {value[key]} (required: {required})')
    return found


def store_preflight():
    """Refuse (StoreRefused) unless /state/memory is the project's prepared volume and no operator
    file names a store path outside it. A container that is not a role of a rendered runtime root
    (neither variable set) is not checked."""
    volume = os.environ.get('AGENT_MEMORY_VOLUME')
    if not volume and not os.environ.get('AGENT_RUNTIME_ROOT_MOUNTS'):
        return
    if not volume:
        raise StoreRefused('manifest_without_store_volume', 'this runtime root\'s manifest predates the store '
                           'volume: re-run kp-agent-install plan and apply, then prepare')
    root = mounts().get(str(STORE))
    if root is None:
        raise StoreRefused('store_not_mounted', f'{STORE} is not a mount point: the project volume {volume} '
                           'is not mounted, so the store would be on the /state bind')
    if not root.endswith(f'/volumes/{volume}/_data'):
        raise StoreRefused('store_not_project_volume', f'{STORE} is mounted from {root!r}, not from the '
                           f'project volume {volume}')
    info = os.stat(STORE)
    if (info.st_uid, info.st_gid, info.st_mode & 0o7777) != (os.getuid(), os.getgid(), 0o700):
        raise StoreRefused('store_unprepared', f'the volume {volume} is owned {info.st_uid}:{info.st_gid} mode '
                           f'{oct(info.st_mode & 0o7777)}, not {os.getuid()}:{os.getgid()} mode 0o700: run '
                           'kp-agent-install prepare')
    left = []
    for name in LEGACY_STORES:
        try:
            with os.scandir(f'/state/{name}') as entries:
                if any(entries):
                    left.append(f'/state/{name}')
        except (FileNotFoundError, NotADirectoryError):
            continue
        except OSError:
            left.append(f'/state/{name} (unreadable)')
    if left:
        raise StoreRefused('store_unprepared', f'pre-T9b store directories are still on the /state bind: '
                           f'{", ".join(left)}: run kp-agent-install prepare')
    named = operator_store_paths()
    if named:
        raise StoreRefused('operator_file_names_outside_store', 'operator files name store paths outside '
                           f'{STORE}; edit them first: ' + '; '.join(named[:10]))


# The operator's way out of each store refusal, which a role waits out instead of exiting
# (T12a A2). The role stays a running container of the project, so `prepare`
# (writers_running) still refuses while it waits: an unprepared store needs stop, prepare, up.
RECOVERY = {
    'store_unprepared': 'docker compose stop, then kp-agent-install prepare, then docker compose up -d',
    'store_not_mounted': 'this container has no volume at /state/memory and its mounts cannot change: re-run '
                         'kp-agent-install plan and apply with this installer, then docker compose up -d '
                         '--force-recreate',
    'operator_file_names_outside_store': 'edit the named operator files; the role starts within one '
                                         're-check, without a stop',
    'store_not_project_volume': 'this container\'s mounts cannot change: remove it and start the role from '
                                'the runtime root\'s rendered manifest (docker compose up -d)',
    'manifest_without_store_volume': 'docker compose stop, re-run kp-agent-install plan and apply, then '
                                     'prepare, then docker compose up -d',
}
DEFAULT_RECOVERY = 'see docs/DOCKER.md, "Restarts and refusals"'
# How often a waiting role runs the store preflight again.
REFUSAL_POLL_SECONDS = 10


def refusal(error):
    return {'status': 'refused', 'scope': 'store-preflight', 'reason': error.reason, 'detail': error.detail}


def refuse(error):
    print(json.dumps(refusal(error)), file=sys.stderr, flush=True)
    raise SystemExit(3)


def long_running(command):
    """A role's own command, which waits out a refusal: `wait`, the board, or a `--watch` loop."""
    return command[:1] in (['wait'], [BOARD_COMMAND]) or '--watch' in command


def await_store(command, clock=time, interval=REFUSAL_POLL_SECONDS):
    """Return once the store preflight passes. A long-running role command waits out every
    refusal, logging each new one; a one-off command exits 3."""
    shown = None
    while True:
        try:
            store_preflight()
        except StoreRefused as error:
            if not long_running(command):
                refuse(error)
            if (error.reason, error.detail) != shown:
                print(json.dumps({**refusal(error), 'waiting': True, 'recheck_seconds': interval,
                                  'recovery': RECOVERY.get(error.reason, DEFAULT_RECOVERY)}),
                      file=sys.stderr, flush=True)
                shown = (error.reason, error.detail)
            clock.sleep(interval)
            continue
        if shown is not None:
            print(json.dumps({'status': 'cleared', 'scope': 'store-preflight', 'reason': shown[0],
                              'detail': 'the store preflight passes; starting the role command'}),
                  file=sys.stderr, flush=True)
        return


def role_argv(command):
    """The role's own command within PID 1's argv: what follows `tooling-container`."""
    for index, token in enumerate(command):
        if PurePosixPath(token).name == 'tooling-container':
            return command[index + 1:]
    return command


def board_status():
    """Board health is that the HTTP port answers; any HTTP status is an answer."""
    connection = http.client.HTTPConnection('127.0.0.1', BOARD_PORT, timeout=5)
    try:
        connection.request('GET', '/')
        return connection.getresponse().status
    except (OSError, http.client.HTTPException):
        return None
    finally:
        connection.close()


def stop(*_):
    sys.exit(0)


def main():
    command = sys.argv[1:]
    if command == ['health']:
        try:
            store_preflight()
        except StoreRefused as error:
            print(json.dumps(refusal(error)))
            raise SystemExit(1)
        if role_argv(role_command())[:1] == [BOARD_COMMAND]:
            status = board_status()
            print(json.dumps({'status': 'unavailable' if status is None else 'ready', 'scope': 'board-http',
                              'http_status': status}))
            raise SystemExit(1 if status is None else 0)
    else:
        # A signal ends the wait (the role may be PID 1 without an init).
        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        await_store(command)
    if command[:1] == [BOARD_COMMAND]:
        os.execvp(command[0], command)
    try:
        check()
    except ValueError:
        status = board_status() if command == ['health'] else None
        if status is None:
            raise
        print(json.dumps({'status': 'ready', 'scope': 'board-http', 'http_status': status}))
        return
    provider = Path('/state/serena-home/serena_config.yml')
    if not provider.exists():
        leaf.mkdir_private(provider.parent, exist_ok=True)
        shutil.copyfile('/usr/local/share/ops-tooling/serena.yml', provider)
    if command == ['health']:
        print(json.dumps(readiness(), separators=(',', ':')))
    elif command == ['wait']:
        while True:
            signal.pause()
    elif command:
        os.execvp(command[0], command)
    else:
        raise ValueError('Container command required')


if __name__ == '__main__':
    main()
