"""Bounded retention of refresh generations under one output root.

A generation is a ``snapshot-*`` directory the refresh created. Pruning is fail
safe: it runs only when a snapshot registry is configured and every reference
surface is readable; otherwise nothing is removed and the receipt says why.

When it runs, the only generations left are the ones the published profile (and
its catalog) reference, the ones the snapshot registry references, the ones the
operator's listed reference files mention (a knowledge catalog pinned separately
from refresh, for example), the ones a live process visibly holds, unreferenced
generations without ``profile.json`` (their provenance is unknown, so they are
kept and counted as ``retained_unknown``), and at most ``retain - 1`` most recent
unreferenced complete generations. Only the refresh cycle itself removes its own
failed partial build.

Listed reference files are read afresh by every pass, so the file as it is at that
moment decides, never an earlier copy. A listed file that is missing, unreadable,
not a regular file or over the read budget means the pass removes nothing.

Removal is atomic per generation: the directory is renamed to a tombstone in the
same root (one ``rename``), then deleted. Readers see the whole generation or no
generation. Every removal, and every surface consulted, is listed in the receipt.
"""
import json
import os
from pathlib import Path
import re
import shutil
import stat

GENERATION = re.compile(r'snapshot-[A-Za-z0-9_]+\Z')
TOMBSTONE = '.removing-'
RECEIPT = 'retention-receipt.json'
SCHEMA = 'agent-tooling.refresh-retention-receipt.v1'
REFERENCE_FILE_BYTES = 16_000_000


class RetentionUnavailable(ValueError):
    """A reference surface could not be read; nothing may be removed."""


def generations(root):
    """Real generation directories directly under root; symlinks are never followed."""
    names = []
    for entry in os.scandir(root):
        if GENERATION.match(entry.name) and entry.is_dir(follow_symlinks=False):
            names.append(entry.name)
    return names


def _spellings(root):
    root = Path(root)
    return {os.path.abspath(root), str(root.resolve())}


def _names_in_text(text, roots):
    found = set()
    for spelling in roots:
        pattern = re.escape(spelling.rstrip('/')) + r'/(snapshot-[A-Za-z0-9_]+)'
        found.update(re.findall(pattern, text))
    return found


def _names_in_value(value, roots):
    found = set()
    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, dict):
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)
        elif isinstance(item, str) and item.startswith('/'):
            normal = os.path.normpath(item)
            for spelling in roots:
                prefix = spelling.rstrip('/') + '/'
                if normal.startswith(prefix):
                    head = normal[len(prefix):].split('/', 1)[0]
                    if GENERATION.match(head):
                        found.add(head)
    return found


def _mentions(raw, roots):
    """The mention rules: absolute paths under the root, in the text or in JSON values."""
    text = raw.decode('utf-8', errors='replace')
    found = _names_in_text(text, roots)
    try:
        found |= _names_in_value(json.loads(text), roots)
    except ValueError:
        pass  # the textual scan above still applies
    return found


def references_in_file(path, roots):
    """Generation names a JSON (or other text) file mentions by absolute path."""
    with open(path, 'rb') as stream:
        raw = stream.read(REFERENCE_FILE_BYTES + 1)
    if len(raw) > REFERENCE_FILE_BYTES:
        raise RetentionUnavailable('reference file exceeds the retention read budget: ' + Path(path).name)
    return _mentions(raw, roots)


def listed_references(paths, roots):
    """Generations the operator's listed reference files mention, read as they are now.

    The snapshot registry's mention rules apply (``_mentions``). A listed file that
    is missing, unreadable, not a regular file or over the read budget refuses
    removal: the error names the file and the cause.
    """
    found = set()
    for listed in paths:
        try:
            path = os.fspath(listed)
            if not isinstance(path, str) or not os.path.isabs(path):
                raise RetentionUnavailable('retention reference file is not an absolute path: ' + str(listed))
            # Non-blocking open: a FIFO or device at a listed path cannot stall the pass.
            descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
            with open(descriptor, 'rb') as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    raise RetentionUnavailable('retention reference file is not a regular file: ' + path)
                raw = stream.read(REFERENCE_FILE_BYTES + 1)
        except RetentionUnavailable:
            raise
        except (OSError, TypeError, ValueError) as error:
            raise RetentionUnavailable('retention reference file unreadable: ' + str(listed) +
                                       ' (' + type(error).__name__ + ')') from error
        if len(raw) > REFERENCE_FILE_BYTES:
            raise RetentionUnavailable('retention reference file exceeds the retention read budget: ' + path)
        try:
            found |= _mentions(raw, roots)
        except RecursionError as error:  # a value scan that cannot finish must not shrink the keep-set
            raise RetentionUnavailable('retention reference file unreadable: ' + path +
                                       ' (RecursionError)') from error
    return found


def published_references(publication, roots):
    """The published profile and the catalog it names; unreadable means refuse removal."""
    try:
        found = references_in_file(publication, roots)
        profile = json.loads(Path(publication).read_text())
        if profile.get('knowledge_config'):
            found |= references_in_file(profile['knowledge_config'], roots)
    except (OSError, ValueError) as error:
        raise RetentionUnavailable('published profile unreadable: ' + type(error).__name__) from error
    return found


def registry_references(registry, roots):
    if registry is None:
        raise RetentionUnavailable('snapshot_registry_not_configured')
    directory = Path(registry)
    if directory.is_symlink() or not directory.is_dir():
        raise RetentionUnavailable('configured snapshot registry is not an existing directory')
    found = set()
    try:
        for entry in os.scandir(directory):
            if entry.is_file(follow_symlinks=False):
                found |= references_in_file(entry.path, roots)
    except OSError as error:
        raise RetentionUnavailable('snapshot registry unreadable: ' + type(error).__name__) from error
    return found


def live_handle_references(roots, proc='/proc'):
    """Working directories, roots, open files and mappings of visible processes.

    Returns (names, surface). Only processes visible in this PID namespace are
    observed; without a /proc filesystem the surface is reported not observable.
    """
    proc = Path(proc)
    if not (proc / 'self').exists():
        return set(), 'not_observable'
    found = set()
    for entry in os.scandir(proc):
        if not entry.name.isdigit():
            continue
        targets = []
        for link in ('cwd', 'root'):
            try:
                targets.append(os.readlink(os.path.join(entry.path, link)))
            except OSError:
                pass
        try:
            for fd in os.scandir(os.path.join(entry.path, 'fd')):
                try:
                    targets.append(os.readlink(fd.path))
                except OSError:
                    pass
        except OSError:
            pass
        try:
            with open(os.path.join(entry.path, 'maps'), 'rb') as stream:
                for line in stream.read(8_000_000).decode('utf-8', errors='replace').splitlines():
                    fields = line.split(None, 5)
                    if len(fields) == 6:
                        targets.append(fields[5])
        except OSError:
            pass
        for target in targets:
            found |= _names_in_value(target.replace(' (deleted)', ''), roots)
    return found, str(proc)


def remove_generation(root, name):
    """Rename to a tombstone (atomic), then delete. Returns an error string or None."""
    source = Path(root) / name
    tombstone = Path(root) / (TOMBSTONE + name)
    try:
        os.rename(source, tombstone)
    except OSError as error:
        return 'rename_failed: ' + type(error).__name__
    shutil.rmtree(tombstone, ignore_errors=True)
    return 'tombstone_not_fully_deleted' if tombstone.exists() else None


def _recency(root, name):
    try:
        return Path(root, name).stat().st_mtime_ns
    except OSError:
        return 0


def prune(root, *, publication, retain, registry=None, checked_at=None, proc='/proc',
          retention_references=None):
    """Apply the retention bound after a successful publish; always returns a receipt.

    ``retention_references`` lists the operator's reference files (absolute paths).
    None, the default, consults none and leaves the receipt exactly as without it.
    """
    root = Path(root)
    roots = _spellings(root)
    listed = None if retention_references is None else list(retention_references)
    receipt = {'schema_version': SCHEMA, 'checked_at': checked_at, 'retain_generations': retain,
               'surfaces': {'published_profile': str(publication),
                            'snapshot_registry': str(registry) if registry is not None else 'not_configured'},
               'removed': [], 'errors': []}
    if listed is not None:
        receipt['surfaces']['reference_files'] = [str(path) for path in listed]
    try:
        # Fail safe: without a configured registry its references are unknowable.
        registered = registry_references(registry, roots)
        names = set(generations(root))
        published = published_references(publication, roots)
        # Read now, every pass: the file as it is at this moment decides.
        mentioned = listed_references(listed, roots) if listed is not None else set()
        live, surface = live_handle_references(roots, proc)
    except (OSError, RetentionUnavailable) as error:
        receipt.update(status='skipped', reason=str(error) if isinstance(error, RetentionUnavailable)
                       else type(error).__name__)
        return receipt
    receipt['surfaces']['live_handles'] = surface
    referenced = published | registered | mentioned | live
    unreferenced = names - referenced
    # Never published here, and not this cycle's own build: provenance unknown, keep.
    unknown = sorted(name for name in unreferenced if not (root / name / 'profile.json').is_file())
    complete = sorted(unreferenced - set(unknown), key=lambda name: (_recency(root, name), name), reverse=True)
    kept = complete[:max(retain - 1, 0)]
    plan = [(name, 'beyond_retention') for name in complete[max(retain - 1, 0):]]
    # Tombstones left by an interrupted earlier removal are finished first.
    try:
        for entry in os.scandir(root):
            if entry.name.startswith(TOMBSTONE) and entry.is_dir(follow_symlinks=False):
                shutil.rmtree(entry.path, ignore_errors=True)
    except OSError as error:
        receipt['errors'].append({'generation': None, 'reason': 'tombstone_cleanup',
                                  'error': type(error).__name__})
    for name, reason in plan:
        error = remove_generation(root, name)
        if error is None:
            receipt['removed'].append({'generation': name, 'reason': reason})
        else:
            receipt['errors'].append({'generation': name, 'reason': reason, 'error': error})
    outcome = {'status': 'pruned',
               'published': sorted(published & names),
               'referenced_by_registry': sorted(registered & names)}
    if listed is not None:
        outcome['referenced_by_reference_files'] = sorted(mentioned & names)
    outcome.update(referenced_by_live_handles=sorted(live & names),
                   retained_unreferenced=kept,
                   retained_unknown=unknown,
                   retained_unknown_count=len(unknown))
    receipt.update(outcome)
    return receipt
