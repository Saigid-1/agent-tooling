"""The leaf: each shared invariant has one home (T11a).

Standard library only. It imports no product module, so every module, including
``deploy/image/container.py``, may import it at top level. In T11a every function
reproduces its call sites' behaviour byte for byte; where sites differ, the
difference is an explicit argument that each site sets to what it did before.

Public API
==========

Canonical JSON (P1)
-------------------
- ``canonical_json(value, *, ascii, allow_nan) -> str``: ``json.dumps`` with
  ``sort_keys=True`` and ``separators=(',', ':')``. ``ascii`` and ``allow_nan``
  are required, so each site names its variant. The four outputs:
  ascii+NaN allowed, ascii+NaN refused, UTF-8+NaN refused, UTF-8+NaN allowed.
- ``canonical_bytes(value, *, ascii, allow_nan) -> bytes``: the same, ``.encode()``.
- ``sorted_json(value) -> str``: the fifth form, ``json.dumps(value, sort_keys=True)``
  (default separators, not compact).

Digests (P2)
------------
- ``sha256_hex(data: bytes) -> str``.
- ``sha256_file(path) -> str``: ``Path(path).read_bytes()``, hashed.
- ``canonical_sha256(value, *, ascii, allow_nan) -> str``.
- ``sorted_sha256(value) -> str``: the digest of ``sorted_json(value).encode()``.
- ``content_id(prefix, value, *, ascii, allow_nan) -> str``:
  ``prefix + ':sha256:' + canonical_sha256(value)``.
- ``canonical_identity(namespace, value, *, ascii, allow_nan, refuse) -> str``:
  ``f'{namespace}:sha256:{sha256}'`` of the canonical text encoded UTF-8; a serialisation
  TypeError/ValueError is raised as ``refuse()`` from it, while an encoding error (a lone
  surrogate) is not translated: the embedding revision identity.
- One call that serialises and digests when a site also needs the serialisation:
  ``canonical_bytes_sha256(value, *, ascii, allow_nan) -> (bytes, hex)``,
  ``canonical_json_sha256(value, *, ascii, allow_nan) -> (str, hex)``, and
  ``canonical_sha256_with(value, data, *, ascii, allow_nan) -> hex`` (canonical bytes
  followed by raw ``data``: the model gateway's multipart boundary).
- ``canonical_token(value, *, ascii, allow_nan) -> str`` (unpadded urlsafe base64 of the
  canonical bytes, '.', their SHA-256: the session-import plan token) and
  ``canonical_token_bytes(token, *, max_bytes) -> bytes`` (its check).
- ``transcript_record_id(raw) -> str``: ``host-transcript-record:sha256:`` + SHA-256 of
  one raw transcript line.
- ``joined_sha256(parts, *, separator, errors='strict') -> str`` and
  ``coordinates_key(prefix, values, *, separator, refuse_message) -> str``: the
  repository-manifest identity and the desk ``binding_key`` scheme.
- ``deskdoc_chunk_id(repo_key, path, blob_sha, offset, length) -> str``: the
  ``deskdoc:`` chunk-ID scheme.
- ``codex_event_label(event) -> str``, ``codex_session_flags_source() -> str`` and
  ``codex_trust_entry(source, event, command, group_index, *, timeout, matcher=None)
  -> (key, 'sha256:…')``: the Codex hook trust entry Kanban's
  ``apps/kanban/src/terminal/codex-hook-config.ts`` computes for one hook group.

Private files (P3; T11b Q3)
---------------------------
T11a kept each site's guarantees through explicit arguments. T11b Q3 rules three of them
for every private write, here:

- **Created with its mode.** A private file is created with its mode (``os.open`` with
  O_CREAT and the mode, or ``mkstemp``'s 0600), so it is never observable with another
  mode. A mode is set on the descriptor (``os.fchmod``) only when the created file does not
  already have it (an existing file being overwritten, or an umask that removed owner
  bits), and always before any content is written (``_settle_mode``). Nothing chmods a file
  it has just created and written.
- **No symlinked final component.** Every write open (``open_fd`` with ``create`` or a
  write access) carries O_NOFOLLOW; there is no ``nofollow`` argument for a write.
- **Durable publications only.** A publication, publish-once (``os.link``) or replace
  (``os.replace``), fsyncs the file and then its parent directory (``sync_directory``),
  so the published artifact survives power loss once the call returns. A create-new
  write (O_EXCL at the final name) fsyncs only where its site asks (``fsync``); the
  hot-path ones (``launch_binding.write_private`` in a hook flow) do not.

- ``FILE_MODE`` (0600), ``DIRECTORY_MODE`` (0700); ``shared_bits(st_mode) -> int``:
  the group/other bits (non-zero means not private).
- ``open_fd(path, *, access, create=False, exclusive=False, append=False, truncate=False,
  nofollow=False, nonblock=False, cloexec=False, optional_flags=False,
  mode=FILE_MODE) -> int`` and ``open_fd_stream(path, stream_mode, **open_fd_options)``.
  ``nofollow`` is the read-side choice; a write open always has O_NOFOLLOW.
- ``write_all(descriptor, data, *, fsync)``: every byte, then fsync when asked (the host
  spool append).
- ``open_binary(path)``: ``open(path, 'rb')`` (a store lock file is opened through it).
- ``chmod_private(path, *, directory=False)``; ``mkdir_private(path, *, parents=False,
  exist_ok=False)``; ``make_temp_dir(*, prefix, dir) -> str``;
  ``ensure_private_dir(path, *, message) -> Path``; ``private_parent(path, *, message)
  -> Path``; ``owned_entry(path, kind, *, refuse)``; ``owned_private_dir(path, *, refuse)``.
- ``create_new_empty(path, *, access='w', optional_flags=False, fchmod=False)``
  (``fchmod``: the file ends exactly 0600 whatever the umask); ``touch_new_private(path)``
  (the same, ending exactly 0600).
- ``write_new_json(path, value)`` (O_EXCL at 0600, ends exactly 0600, no fsync);
  ``write_new_json_once(path, value, *, read, conflict)``;
  ``write_new_private(path, content, *, text=False, optional_flags=False, fsync)`` (O_EXCL
  with mode 0600); ``write_new_canonical(path, value, *, ascii, allow_nan, fsync) ->
  sha256``; ``overwrite_private(path, content)`` (create or truncate at 0600, ending
  exactly 0600); ``json_writer(value, *, newline=False, **dump_options)``.
- ``replace_file(path, content, *, cleanup, temp_prefix=None, sibling_tag=None,
  text=False, fchmod=False, chmod_mode=None, temp_mode=FILE_MODE, make_parents=False,
  skip_if_equal=False)``; ``replace_private_json(path, document, *, max_bytes,
  too_large, temp_prefix, result=None, **dump_options) -> result(document)``.
- ``publish_once(path, content, *, temp_prefix, temp_dir, cleanup, chmod_mode=None,
  on_exists=None)`` (temporary + fsync + ``os.link`` + parent fsync, never overwrites);
  ``publish_if_absent(path, make_content, *, temp_prefix, temp_dir)``;
  ``publish_content_addressed(root, value, *, ascii, allow_nan, id_prefix, name_prefix,
  temp_prefix, refuse, symlink_message, collision_message, suffix='.json') -> id``;
  ``publish_once_checked(path, data, *, temp_prefix, symlink_message,
  collision_message)``; ``private_named_temp(directory, prefix, content, *, fsync)
  -> Path``; ``link_private_once(target, content, *, temp_prefix, read, expected,
  conflict)``; ``write_private_json_once(target, document, *, read, conflict,
  temp_prefix) -> {'path', 'idempotent'}``; ``sync_directory(path)``.
- ``private_lock(path, *, value=None)`` and ``owned_directory_lock(path, *, message)``:
  ``flock(LOCK_EX)`` context managers; ``nonblocking_lock(path, *, record=None)``: ``flock(LOCK_EX |
  LOCK_NB)``, yielding whether it is held (never waits), with the holder's advisory record as the
  lock file's content (T12b's index lease).
- ``rename_into_place(built, target)``: a built file published by rename (T12b's reindex swap): both
  names ``lstat``-checked (regular, no symlink, one directory), fsync of the file, ``os.rename``,
  fsync of the directory.
- ``read_private_json(path, *, memo=None)``; ``read_owned_json(path, bound, *,
  refuse)``; ``read_secret_file(path, *, max_bytes, refuse) -> str``;
  ``read_regular_bytes(path, limit, *, not_regular, too_large) -> bytes``;
  ``checked_regular_file(path, *, suffix, max_bytes, refused, too_large) -> Path``.

``content`` is bytes, str, or a callable that writes to the open stream (so a
serialisation error still happens after the file is created, as before).
``cleanup`` names what a site did with its temporary: ``'on-error'`` (unlink if it
exists, then re-raise), ``'missing-ok'`` (always ``Path.unlink(missing_ok=True)``),
``'if-exists'`` (always, when ``os.path.exists``), ``'always'`` (``os.unlink``) or None.

Store paths (P5) and store files (P6)
-------------------------------------
- ``STORE_ROOT`` (``PurePosixPath('/state/memory')``), ``LEGACY_STORES``, ``STORE_KEYS``,
  ``TEMPLATE_KEY``; ``within(path, base) -> bool``; ``state_path(value, *, reject_control)``
  (a normalized absolute container path, or None); ``required_store_path(key, value, *,
  reject_control) -> str | None``: the one store-path rule, used by
  ``_impl/runtime_install.py``, ``deploy/image/container.py`` and
  ``assistant_host_cli.py``. ``reject_control`` keeps each consumer's handling of a value
  with a control character: runtime_install (True) does not read it as a path;
  container.py (False) normalizes it like any other value, so its preflight refuses
  exactly what it refused before. A ``config_template`` is an operator file (T11b Q4): it
  is required under /config whenever it is under /state, /state/memory included.
- One constant per store filename (``EPISODES_DB``, ``SESSIONS_DB``, ``SEARCH_INDEX_DB``,
  ``WORKSPACE_CAPTURE_DB``, ``SESSION_IMPORT_JOBS_DB``, ``SPOOL_INGEST_DB``, ``CAPTURE_DB``,
  ``QUEUE_DB``, ``TELEMETRY_DB``, ``ROLLOUT_CAPTURE_DB``, ``GATEWAY_LEDGER_DB``,
  ``REFERENCES_DB``; T12b: ``SEARCH_INDEX_BUILD_DB``, ``INDEX_LOCK``, ``INDEXER_HEALTH``) and
  ``store_path(anchor, name, *, sibling=False)``: ``anchor / name``,
  or ``anchor.with_name(name)`` for a file beside the store file ``anchor`` (it keeps the
  anchor's mark; it does not mark: the model gateway's ledger is not a store).

Store paths at open (T11b Q1)
-----------------------------
In the Docker runtime (``AGENT_MEMORY_VOLUME`` set: every role gets it from Compose, and
``docker exec`` and ``docker compose run`` inherit it) a store path that does not resolve
under /state/memory is refused, for reads and writes, with ``StoreOutsideVolume``
(a ValueError; ``category`` ``store_outside_volume``; the message names the path). A
process without the variable (a host install) is exempt; setting it only refuses more.

- **The mark is the type.** ``StorePath`` (a ``PosixPath``): a path derived from a
  ``state_root``, a ``roster_path``, ``launches/`` or a launch receipt. Every path derived
  from a StorePath (``/``, ``joinpath``, ``with_name``, ``with_suffix``, ``parent``,
  ``resolve``) is a StorePath; ``Path(store_path)`` is not, so the store classes keep the
  mark with ``as_path(path)``.
- **Derivation points mark and check:** ``mark_store(value)`` (a state_root, a roster_path,
  a launch receipt or a store a receipt names), ``store_file(state_root, name)`` and
  ``launches_dir(state_root)`` (``state_root/launches``). ``desk_memory_runtime.components()``
  derives its stores through them.
- **The generic helpers refuse only a marked path** (``check_store``): ``sqlite_connect`` and
  ``sqlite_uri``, ``open_fd`` and every write helper, the directory and lock helpers,
  ``chmod_private``, ``read_private_json`` and the other readers. An unmarked path is never
  refused here: the model gateway's ledger, ``references.sqlite3``, the code_references
  manifest, the knowledge lifecycle database under /state/knowledge and runtime_install's
  NOT_STORES directories are not stores.
- ``docker_runtime() -> bool``; ``error_category(error) -> str`` (what a CLI reports:
  ``store_outside_volume`` for this refusal, else the exception's type name).

SQLite (P4; T11b Q2)
--------------------
- ``sqlite_uri(path, mode, *, resolve) -> str``: ``Path.as_uri() + '?mode=' + mode``,
  resolving first when ``resolve``. T11b Q2 (one profile): every product connection other
  than ``':memory:'`` is a ``file:`` URI of the resolved path (``resolve=True``), so a symlinked
  component never names a file, or a journal or WAL beside it, other than the resolved
  target's. An open that may create the store uses ``mode=rwc`` (what a plain path did);
  ``sqlite_create`` opens the file it has just created with ``mode=rw``. Timeouts (sqlite3's
  5.0, and 10 and 30 where set), ``isolation_level=None`` and ``check_same_thread=False``
  stay where they were.
- ``sqlite_connect(database, *, mode=None, resolve=False, timeout=None,
  isolation_level=<not passed>, check_same_thread=True) -> sqlite3.Connection``.
  With ``mode`` the database is opened by that URI (``uri=True``). Only the
  options a site passes reach ``sqlite3.connect``: ``timeout=None`` is sqlite3's
  own default of 5.0 s and is not passed; ``isolation_level`` and
  ``check_same_thread`` are passed only when the site sets them. ``':memory:'``
  is an ordinary ``database``. No pragma is added, ``factory`` and
  ``cached_statements`` are never passed, and ``sqlite3.connect`` is looked up
  on the module at call time (``tests/t10_instruments.py`` wraps it). A plain-path
  ``database`` that is a StorePath reaches ``sqlite3.connect`` as a plain ``Path``.
- ``sqlite_create(path, **profile)``: a new store file created 0600 (``create_new_empty``)
  and then opened (a resolved ``mode=rw`` URI), so the database and the journal and WAL files SQLite derives from its
  mode are never wider (Q3); FileExistsError when it exists.

Outside the leaf
----------------
``OUTSIDE_THE_LEAF`` (the end of this module) names, by file:line and reason, every site
that stays outside the leaf: the embedded stdlib-only scripts, wire and file-content
hashes, streaming and raw-bytes digests, directory renames, the kept dead-code hooks,
the old-name delegations and the two store-filename texts. The sort_keys-only
``json.dumps`` calls that feed no digest (CLI output, stored payloads, the payload sizing
of ``verification_packet.encoded``) are not canonical serialisations and stay put.
"""
from __future__ import annotations

import base64
import fcntl
import hashlib
import json
import os
import re
import sqlite3
import stat
import tempfile
import uuid
from contextlib import contextmanager
from pathlib import Path, PurePosixPath


# --- (a) canonical JSON ----------------------------------------------------------------------------

def canonical_json(value, *, ascii, allow_nan):
    """``json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=ascii, allow_nan=allow_nan)``."""
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=ascii, allow_nan=allow_nan)


def canonical_bytes(value, *, ascii, allow_nan):
    return canonical_json(value, ascii=ascii, allow_nan=allow_nan).encode()


def sorted_json(value):
    """The sort_keys-only, non-compact form some persisted digests were taken over."""
    return json.dumps(value, sort_keys=True)


# --- (b) digests -----------------------------------------------------------------------------------

def sha256_hex(data):
    return hashlib.sha256(data).hexdigest()


def sha256_file(path):
    return sha256_hex(Path(path).read_bytes())


def canonical_sha256(value, *, ascii, allow_nan):
    return sha256_hex(canonical_bytes(value, ascii=ascii, allow_nan=allow_nan))


def sorted_sha256(value):
    return sha256_hex(sorted_json(value).encode())


def content_id(prefix, value, *, ascii, allow_nan):
    return prefix + ':sha256:' + canonical_sha256(value, ascii=ascii, allow_nan=allow_nan)


def canonical_identity(namespace, value, *, ascii, allow_nan, refuse):
    """``namespace:sha256:<hex>`` of the canonical text; only serialisation errors become ``refuse()``."""
    try:
        text = canonical_json(value, ascii=ascii, allow_nan=allow_nan)
    except (TypeError, ValueError) as error:
        raise refuse() from error
    return f'{namespace}:sha256:{sha256_hex(text.encode("utf-8"))}'


def canonical_bytes_sha256(value, *, ascii, allow_nan):
    """The canonical bytes and their SHA-256, for a site that also stores or sizes the bytes."""
    raw = canonical_bytes(value, ascii=ascii, allow_nan=allow_nan)
    return raw, sha256_hex(raw)


def canonical_json_sha256(value, *, ascii, allow_nan):
    """The canonical text and the SHA-256 of its encoding, for a site that also keeps the text."""
    text = canonical_json(value, ascii=ascii, allow_nan=allow_nan)
    return text, sha256_hex(text.encode())


def canonical_sha256_with(value, data, *, ascii, allow_nan):
    """SHA-256 of the canonical bytes of ``value`` followed by the raw bytes ``data``."""
    return sha256_hex(canonical_bytes(value, ascii=ascii, allow_nan=allow_nan) + data)


def canonical_token(value, *, ascii, allow_nan):
    """Unpadded urlsafe base64 of the canonical bytes, '.', and their SHA-256."""
    raw = canonical_bytes(value, ascii=ascii, allow_nan=allow_nan)
    return base64.urlsafe_b64encode(raw).decode().rstrip('=') + '.' + sha256_hex(raw)


def canonical_token_bytes(token, *, max_bytes):
    """The bytes a ``canonical_token`` carries; ValueError when the digest or the size bound fails."""
    body, digest = token.rsplit('.', 1)
    raw = base64.urlsafe_b64decode(body + '=' * (-len(body) % 4))
    if sha256_hex(raw) != digest or len(raw) > max_bytes:
        raise ValueError
    return raw


def transcript_record_id(raw):
    """The identity of one raw host-transcript line."""
    return 'host-transcript-record:sha256:' + sha256_hex(raw)


def joined_sha256(parts, *, separator, errors='strict'):
    """SHA-256 of the UTF-8 (``errors``) encoding of ``separator.join(parts)``."""
    return sha256_hex(separator.join(parts).encode('utf-8', errors))


def coordinates_key(prefix, values, *, separator, refuse_message):
    """``prefix`` + ``joined_sha256`` of non-empty str coordinates free of ``separator`` (else ValueError)."""
    if any(not isinstance(v, str) or not v or separator in v for v in values):
        raise ValueError(refuse_message)
    return prefix + joined_sha256(values, separator=separator)


def deskdoc_chunk_id(repo_key, path, blob_sha, offset, length):
    """``deskdoc:`` + SHA-256 of the NUL-joined coordinates (offset and length as decimal text)."""
    fields = (repo_key, path, blob_sha, str(offset), str(length))
    return 'deskdoc:' + sha256_hex('\0'.join(fields).encode('utf-8'))


def codex_event_label(event):
    """``PreCompact`` -> ``pre_compact``: the event name Codex's trust keys use."""
    return re.sub(r'(?<!^)(?=[A-Z])', '_', event).lower()


def codex_session_flags_source():
    """The pseudo-path Codex reports for hooks configured by ``-c`` session flags."""
    return r'C:\<session-flags>\config.toml' if os.name == 'nt' else '/<session-flags>/config.toml'


def codex_trust_entry(source, event, command, group_index, *, timeout, matcher=None):
    """Trust key and hash of one hook group (``hooks[0]`` of group ``group_index``) at ``source``.

    The hash is over the compact, key-sorted, UTF-8 JSON of the group with its
    ``event_name``; ``matcher`` is part of the group only when it is not None.
    """
    label = codex_event_label(event)
    group = {'hooks': [{'async': False, 'command': command, 'timeout': timeout, 'type': 'command'}]}
    if matcher is not None:
        group['matcher'] = matcher
    identity = canonical_json({'event_name': label, **group}, ascii=False, allow_nan=True)
    return f'{source}:{label}:{group_index}:0', 'sha256:' + sha256_hex(identity.encode())


# --- (d) SQLite ------------------------------------------------------------------------------------

_NOT_PASSED = object()


def sqlite_uri(path, mode, *, resolve):
    """The ``file:`` URI of ``path`` opened in ``mode`` (``ro``, ``rw``, ...); a marked store path
    outside the volume is refused first (Q1)."""
    path = as_path(check_store(path))
    return (path.resolve() if resolve else path).as_uri() + '?mode=' + mode


def sqlite_connect(database, *, mode=None, resolve=False, timeout=None, isolation_level=_NOT_PASSED,
                   check_same_thread=True):
    """``sqlite3.connect`` with exactly the profile the call site names."""
    if resolve and mode is None:
        raise TypeError('resolve applies only to a URI open (mode=...)')
    options = {}
    if mode is not None:
        database = sqlite_uri(database, mode, resolve=resolve)
        options['uri'] = True
    elif isinstance(database, StorePath):
        # The mark has done its work; sqlite3 receives the plain path the site always passed.
        database = Path(check_store(database))
    if timeout is not None:
        options['timeout'] = timeout
    if isolation_level is not _NOT_PASSED:
        options['isolation_level'] = isolation_level
    if check_same_thread is not True:
        options['check_same_thread'] = check_same_thread
    return sqlite3.connect(database, **options)


def sqlite_create(path, **profile):
    """A new store file created 0600 (O_EXCL|O_NOFOLLOW), then opened as a resolved ``mode=rw`` URI
    (Q2: the file exists, this call created it) with ``profile`` (timeout and the like).

    SQLite creates the journal and WAL files with the database's mode, so none of them is
    ever wider than 0600 (Q3: no create-then-chmod window). FileExistsError when ``path``
    exists.
    """
    create_new_empty(path)
    return sqlite_connect(path, mode='rw', resolve=True, **profile)


# --- (c) private files ------------------------------------------------------------------------------

FILE_MODE = 0o600
DIRECTORY_MODE = 0o700
_SHARED_BITS = 0o077


def shared_bits(mode):
    """The group and other permission bits of a ``st_mode``; non-zero means not private."""
    return mode & _SHARED_BITS


def open_fd(path, *, access, create=False, exclusive=False, append=False, truncate=False, nofollow=False,
            nonblock=False, cloexec=False, optional_flags=False, mode=FILE_MODE):
    """``os.open`` with the named flags; ``mode`` is passed only with ``create``.

    A write open (``create``, or ``access`` 'w' or 'rw') always carries O_NOFOLLOW (Q3: no write
    follows a symlinked final component); ``nofollow`` adds it to a read. ``optional_flags`` spells
    O_NOFOLLOW, O_NONBLOCK and O_CLOEXEC as ``getattr(os, name, 0)``. A marked store path outside
    the volume is refused before anything is opened (Q1).
    """
    check_store(path)

    def flag(name):
        return getattr(os, name, 0) if optional_flags else getattr(os, name)
    flags = {'r': os.O_RDONLY, 'w': os.O_WRONLY, 'rw': os.O_RDWR}[access]
    if append:
        flags |= os.O_APPEND
    if create:
        flags |= os.O_CREAT
    if exclusive:
        flags |= os.O_EXCL
    if truncate:
        flags |= os.O_TRUNC
    if nofollow or create or access != 'r':
        flags |= flag('O_NOFOLLOW')
    if nonblock:
        flags |= flag('O_NONBLOCK')
    if cloexec:
        flags |= flag('O_CLOEXEC')
    if create:
        return os.open(path, flags, mode)
    return os.open(path, flags)


def open_fd_stream(path, stream_mode, **open_fd_options):
    return os.fdopen(open_fd(path, **open_fd_options), stream_mode)


def write_all(descriptor, data, *, fsync):
    """Write every byte of ``data`` to ``descriptor``, then fsync it when ``fsync``."""
    view = memoryview(data)
    while view:
        view = view[os.write(descriptor, view):]
    if fsync:
        os.fsync(descriptor)


def open_binary(path):
    """``open(path, 'rb')``; a marked store path outside the volume is refused first (Q1)."""
    check_store(path)
    return open(path, 'rb')


def _settle_mode(descriptor, mode):
    """Give a just-opened file exactly ``mode`` before anything is written to it (Q3).

    ``os.fchmod`` runs only when the file does not already have the mode: an existing file being
    overwritten, or an umask that removed bits at creation. A file created with its mode is never
    chmodded.
    """
    if stat.S_IMODE(os.fstat(descriptor).st_mode) != mode:
        os.fchmod(descriptor, mode)


def sync_directory(path):
    """fsync the directory ``path``, so a name just linked or renamed into it survives power loss (Q3)."""
    descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def chmod_private(path, *, directory=False):
    check_store(path)
    os.chmod(path, DIRECTORY_MODE if directory else FILE_MODE)


def mkdir_private(path, *, parents=False, exist_ok=False):
    """``Path.mkdir`` at 0700 (the umask still applies, as before)."""
    check_store(path)
    path.mkdir(mode=DIRECTORY_MODE, parents=parents, exist_ok=exist_ok)


def make_temp_dir(*, prefix, dir):
    check_store(dir)
    return tempfile.mkdtemp(prefix=prefix, dir=dir)


def ensure_private_dir(path, *, message):
    """Create ``path`` 0700 if absent; it must then be a non-symlink directory of this user, private."""
    check_store(path)
    path = as_path(path)
    try:
        path.mkdir(mode=DIRECTORY_MODE)
    except FileExistsError:
        pass
    info = path.lstat()
    if path.is_symlink() or not path.is_dir() or info.st_uid != os.getuid() or shared_bits(info.st_mode):
        raise ValueError(message)
    return path


def private_parent(path, *, message):
    """``path`` as a Path when its parent is an existing, physical, private directory of this user."""
    target = Path(path)
    parent = target.parent
    if (not target.is_absolute() or target.is_symlink() or not parent.is_dir()
            or parent.resolve() != parent or parent.stat().st_uid != os.getuid()
            or shared_bits(parent.stat().st_mode)):
        raise ValueError(message)
    return target


def owned_entry(path, kind, *, refuse):
    """An existing ``kind`` ('file' or 'dir') owned by this user, not a symlink, not writable by others."""
    info = os.lstat(path)
    expected = stat.S_ISREG if kind == 'file' else stat.S_ISDIR
    if (stat.S_ISLNK(info.st_mode) or not expected(info.st_mode) or info.st_uid != os.getuid()
            or info.st_mode & 0o022):
        raise refuse(f'{path} must be a {"regular file" if kind == "file" else "directory"} owned by the '
                     'running user, not a symlink, and not writable by group or others')
    return info


def owned_private_dir(path, *, refuse):
    info = owned_entry(path, 'dir', refuse=refuse)
    if shared_bits(info.st_mode):
        raise refuse(f'{path} must be private (0700)')
    return Path(path)


def create_new_empty(path, *, access='w', optional_flags=False, fchmod=False):
    """An empty 0600 file, only where nothing exists (O_EXCL|O_NOFOLLOW); ``fchmod``: it ends exactly 0600."""
    descriptor = open_fd(path, access=access, create=True, exclusive=True, optional_flags=optional_flags)
    try:
        if fchmod:
            _settle_mode(descriptor, FILE_MODE)
    finally:
        os.close(descriptor)


def touch_new_private(path):
    """An empty file that must not exist, created 0600 (O_EXCL|O_NOFOLLOW), ending exactly 0600."""
    create_new_empty(path, fchmod=True)


def _emit(stream, content, *, fsync):
    if callable(content):
        content(stream)
    else:
        stream.write(content)
    if fsync:
        stream.flush()
        os.fsync(stream.fileno())


def json_writer(value, *, newline=False, **dump_options):
    """A ``content`` callable: ``json.dump(value, stream, **dump_options)`` and an optional newline."""
    def write(stream):
        json.dump(value, stream, **dump_options)
        if newline:
            stream.write('\n')
    return write


def write_new_json(path, value):
    """A new JSON file created 0600 (O_EXCL|O_NOFOLLOW), ending exactly 0600, then ``json.dump``.

    No fsync: a create-new write, run on the hook path. Never overwrites.
    """
    descriptor = open_fd(path, access='w', create=True, exclusive=True)
    with os.fdopen(descriptor, 'w') as out:
        _settle_mode(descriptor, FILE_MODE)
        json.dump(value, out)


def write_new_json_once(path, value, *, read, conflict):
    """``write_new_json``; an existing file must already hold ``value`` (``read``), else ``conflict()``."""
    try:
        write_new_json(path, value)
    except FileExistsError:
        if read(path) != value:
            raise conflict() from None


def write_new_private(path, content, *, text=False, optional_flags=False, fsync):
    """A new file created 0600 with O_EXCL|O_NOFOLLOW; never overwrites; fsync when the site asks."""
    descriptor = open_fd(path, access='w', create=True, exclusive=True, optional_flags=optional_flags)
    with os.fdopen(descriptor, 'w' if text else 'wb') as stream:
        _emit(stream, content, fsync=fsync)


def write_new_canonical(path, value, *, ascii, allow_nan, fsync):
    """``write_new_private`` of canonical JSON plus a newline; returns the SHA-256 of those bytes."""
    raw = (canonical_json(value, ascii=ascii, allow_nan=allow_nan) + '\n').encode()
    write_new_private(path, raw, fsync=fsync)
    return sha256_hex(raw)


def overwrite_private(path, content):
    """Create or truncate ``path`` at 0600 (O_NOFOLLOW) and write ``content`` (bytes or str).

    An existing file with another mode is set to 0600 before the new content is written; a new
    file is created 0600. No fsync.
    """
    descriptor = open_fd(path, access='w', create=True, truncate=True)
    with os.fdopen(descriptor, 'wb' if isinstance(content, bytes) else 'w') as stream:
        _settle_mode(descriptor, FILE_MODE)
        stream.write(content)


def replace_file(path, content, *, cleanup, temp_prefix=None, sibling_tag=None, text=False, fchmod=False,
                 chmod_mode=None, temp_mode=FILE_MODE, make_parents=False, skip_if_equal=False):
    """A publication by replace (Q3): write a temporary beside ``path``, fsync it, ``os.replace`` it over
    ``path``, then fsync the parent directory.

    The temporary is ``tempfile.mkstemp(prefix=temp_prefix, dir=path.parent)`` (0600), or, with
    ``sibling_tag``, ``.<name>.<sibling_tag>-<uuid hex>`` created O_EXCL|O_NOFOLLOW at
    ``temp_mode``. ``fchmod`` (0600) and ``chmod_mode`` make the temporary's mode exact before
    anything is written to it. ``skip_if_equal``: an existing ``path`` with the same bytes is left
    as it is, and one with other bytes is unlinked first.
    """
    check_store(path)
    if make_parents:
        path.parent.mkdir(parents=True, exist_ok=True)
    if skip_if_equal and path.exists():
        if path.read_bytes() != content:
            path.unlink()
        else:
            return
    if sibling_tag is None:
        descriptor, temporary = tempfile.mkstemp(prefix=temp_prefix, dir=path.parent)
    else:
        temporary = path.with_name(f'.{path.name}.{sibling_tag}-{uuid.uuid4().hex}')
        descriptor = open_fd(temporary, access='w', create=True, exclusive=True, mode=temp_mode)
    try:
        with os.fdopen(descriptor, 'w' if text else 'wb') as stream:
            if fchmod:
                _settle_mode(descriptor, FILE_MODE)
            if chmod_mode is not None:
                _settle_mode(descriptor, chmod_mode)
            _emit(stream, content, fsync=True)
        os.replace(temporary, path)
        sync_directory(path.parent)
    except BaseException:
        if cleanup == 'on-error' and os.path.lexists(temporary):
            os.unlink(temporary)
        raise
    finally:
        if cleanup == 'missing-ok':
            Path(temporary).unlink(missing_ok=True)
        elif cleanup == 'if-exists' and os.path.exists(temporary):
            os.unlink(temporary)


def replace_private_json(path, document, *, max_bytes, too_large, temp_prefix, result=None, **dump_options):
    """``replace_file`` (0600, temporary always removed) of ``json.dumps(document, ...)``.

    Returns ``result(document)`` after the write (``document`` when ``result`` is None).
    """
    data = json.dumps(document, **dump_options).encode()
    if len(data) > max_bytes:
        raise ValueError(too_large)
    replace_file(path, data, temp_prefix=temp_prefix, fchmod=True, cleanup='missing-ok')
    return document if result is None else result(document)


def publish_once(path, content, *, temp_prefix, temp_dir, cleanup, chmod_mode=None, on_exists=None):
    """A publication by link (Q3): write a ``mkstemp`` temporary, fsync it, ``os.link`` it to ``path``,
    then fsync the parent directory. Never overwrites.

    When ``path`` already exists the link fails, this call publishes nothing, and ``on_exists()`` (if
    any) decides. ``chmod_mode`` narrows the written temporary before the link (the 0o400 delivery).
    """
    check_store(path)
    check_store(temp_dir)
    descriptor, temporary = tempfile.mkstemp(prefix=temp_prefix, dir=temp_dir)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            _emit(stream, content, fsync=True)
        if chmod_mode is not None:
            os.chmod(temporary, chmod_mode)
        try:
            os.link(temporary, path)
        except FileExistsError:
            if on_exists is not None:
                on_exists()
        else:
            sync_directory(as_path(path).parent)
    finally:
        if cleanup == 'always':
            os.unlink(temporary)
        elif cleanup == 'if-exists' and os.path.exists(temporary):
            os.unlink(temporary)


def publish_if_absent(path, make_content, *, temp_prefix, temp_dir):
    """``publish_once`` of ``make_content()`` unless ``path`` exists or is a symlink."""
    if path.exists() or path.is_symlink():
        return
    publish_once(path, make_content(), temp_prefix=temp_prefix, temp_dir=temp_dir, cleanup='always')


def publish_content_addressed(root, value, *, ascii, allow_nan, id_prefix, name_prefix, temp_prefix, refuse,
                              symlink_message, collision_message, suffix='.json'):
    """Publish the canonical bytes of ``value`` once at ``root/<name_prefix><sha256><suffix>``.

    Returns ``<id_prefix>:sha256:<sha256>``.
    """
    raw, digest = canonical_bytes_sha256(value, ascii=ascii, allow_nan=allow_nan)
    target = root / (name_prefix + digest + suffix)
    if target.is_symlink():
        raise refuse(symlink_message)
    if target.exists():
        if target.read_bytes() != raw:
            raise refuse(collision_message)
    else:
        def collided():
            if target.read_bytes() != raw:
                raise refuse(collision_message)
        publish_once(target, raw, temp_prefix=temp_prefix, temp_dir=root, on_exists=collided, cleanup='always')
    return id_prefix + ':sha256:' + digest


def publish_once_checked(path, data, *, temp_prefix, symlink_message, collision_message):
    """Publish ``data`` once at ``path`` (fsync, link, parent fsync); an existing file must hold the
    same bytes (ValueError)."""
    check_store(path)
    if path.is_symlink():
        raise ValueError(symlink_message)
    try:
        descriptor, temporary = tempfile.mkstemp(prefix=temp_prefix, dir=path.parent)
        try:
            with os.fdopen(descriptor, 'wb') as stream:
                _emit(stream, data, fsync=True)
            if path.exists():
                if path.read_bytes() != data:
                    raise ValueError(collision_message)
            else:
                os.link(temporary, path)
                sync_directory(path.parent)
        finally:
            os.unlink(temporary)
    except FileExistsError:
        if path.read_bytes() != data:
            raise ValueError(collision_message)


def private_named_temp(directory, prefix, content, *, fsync):
    """A ``NamedTemporaryFile(delete=False)`` in ``directory``, created 0600 (``mkstemp``) and ending
    exactly 0600 before ``content`` is written."""
    check_store(directory)
    with tempfile.NamedTemporaryFile(dir=directory, prefix=prefix, delete=False) as stream:
        temporary = Path(stream.name)
        _settle_mode(stream.fileno(), FILE_MODE)
        _emit(stream, content, fsync=fsync)
    return temporary


def link_private_once(target, content, *, temp_prefix, read, expected, conflict):
    """A publication by link (Q3): ``private_named_temp`` beside ``target``, fsynced, then ``os.link``
    and the parent directory fsynced. Never overwrites.

    An existing ``target`` must read (``read``) as ``expected``, else ``conflict()``.
    """
    check_store(target)
    temporary = private_named_temp(target.parent, temp_prefix, content, fsync=True)
    try:
        os.link(temporary, target)
    except FileExistsError:
        if read(target) != expected:
            raise conflict()
    else:
        sync_directory(target.parent)
    finally:
        temporary.unlink(missing_ok=True)


def write_private_json_once(target, document, *, read, conflict, temp_prefix):
    """Indented, key-sorted JSON published once (fsync, link, parent fsync); an existing file must match."""
    data = (json.dumps(document, indent=2, sort_keys=True) + '\n').encode('utf-8')
    if target.exists():
        if read(target) != document:
            raise conflict()
        return {'path': str(target), 'idempotent': True}
    link_private_once(target, data, temp_prefix=temp_prefix, read=read, expected=document, conflict=conflict)
    return {'path': str(target), 'idempotent': False}


@contextmanager
def private_lock(path, *, value=None):
    """``flock(LOCK_EX)`` on a lock file opened O_RDWR|O_CREAT|O_NOFOLLOW (0600)."""
    descriptor = open_fd(path, access='rw', create=True)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield value
    finally:
        os.close(descriptor)


@contextmanager
def nonblocking_lock(path, *, record=None):
    """``flock(LOCK_EX | LOCK_NB)`` on a lock file opened O_RDWR|O_CREAT|O_NOFOLLOW (0600); yields True
    while this call holds it and False when another holder has it: it never waits (T12b B2, the index
    lease; precedent: the workspace capture lock). With ``record`` (bytes) the content of a lock file
    this call holds is replaced by it, the holder's advisory record; a lock it does not hold is left
    as it is."""
    descriptor = open_fd(path, access='rw', create=True)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            held = True
        except BlockingIOError:
            held = False
        if held and record is not None:
            os.ftruncate(descriptor, 0)
            write_all(descriptor, record, fsync=False)
        yield held
    finally:
        os.close(descriptor)


def rename_into_place(built, target):
    """Publish a built file by rename (T12b B3; T11b Q3): fsync of ``built``, the rename over ``target``,
    then fsync of their directory.

    O_NOFOLLOW does not apply to a rename, so both names are checked with ``lstat`` first: ``built`` must
    be a regular file, ``target`` a regular file or absent (never a symlink), and both must name the same
    directory. The descriptor fsynced is checked to be the file ``lstat`` saw."""
    check_store(built)
    check_store(target)
    built, target = as_path(built), as_path(target)
    if built.parent != target.parent or os.path.realpath(built.parent) != os.path.realpath(target.parent):
        raise ValueError(f'{built.name} and {target.name} are not in one directory; nothing was renamed')
    info = os.lstat(built)
    if not stat.S_ISREG(info.st_mode):
        raise ValueError(f'{built} is not a regular file (a symlink is refused); nothing was renamed')
    try:
        current = os.lstat(target)
    except FileNotFoundError:
        current = None
    if current is not None and not stat.S_ISREG(current.st_mode):
        raise ValueError(f'{target} is not a regular file (a symlink is refused); nothing was renamed')
    descriptor = open_fd(built, access='r', nofollow=True)
    try:
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
            raise ValueError(f'{built} changed before the rename; nothing was renamed')
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.rename(built, target)
    sync_directory(target.parent)


@contextmanager
def owned_directory_lock(path, *, message):
    """``flock(LOCK_EX)`` on the directory of ``path``, which must exist and belong to this user."""
    check_store(path)
    parent = path.parent
    if (not path.is_absolute() or path.is_symlink() or not parent.is_dir()
            or parent.is_symlink() or parent.stat().st_uid != os.getuid()):
        raise ValueError(message)
    descriptor = open_fd(parent, access='r')
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


def read_private_json(path, *, memo=None):
    """A private operator JSON file; with a per-call ``memo`` it is read at most once per call."""
    check_store(path)
    if memo is None:
        return _read_private_json(path)
    key = ('private-json', str(Path(path)))
    if key not in memo:
        memo[key] = _read_private_json(path)
    return json.loads(json.dumps(memo[key]))


def _read_private_json(path):
    path = Path(path)
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise ValueError('existing absolute operator file required')
    if shared_bits(path.stat().st_mode):
        raise ValueError('operator file must not be accessible to group or others; use mode 0600')
    if path.stat().st_uid != os.getuid():
        raise ValueError('operator file must belong to the running user')
    if path.stat().st_size > 262144:
        raise ValueError('operator file exceeds 256 KiB')
    return json.loads(path.read_text())


def read_owned_json(path, bound, *, refuse):
    check_store(path)
    owned_entry(path, 'file', refuse=refuse)
    with open(path, 'rb') as stream:
        raw = stream.read(bound + 1)
    if len(raw) > bound:
        raise refuse(f'{path} exceeds {bound} bytes')
    return json.loads(raw)


def read_secret_file(path, *, max_bytes, refuse):
    """A key from an owner-only regular file; ``refuse(reason)`` otherwise. Never echoes the contents."""
    try:
        if Path(path).is_symlink():
            raise refuse('symlink')
        fd = open_fd(path, access='r', nofollow=True, nonblock=True, cloexec=True, optional_flags=True)
    except refuse:
        raise
    except FileNotFoundError:
        raise refuse('missing') from None
    except OSError:
        raise refuse('unreadable') from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise refuse('not_regular_file')
        if info.st_uid != os.getuid():
            raise refuse('not_owner')
        if shared_bits(info.st_mode):
            raise refuse('permissions_too_open')
        if not 0 < info.st_size <= max_bytes:
            raise refuse('empty_or_oversized')
        data = os.read(fd, max_bytes + 1)
    finally:
        os.close(fd)
    try:
        key = data.decode('ascii').strip()
    except UnicodeDecodeError:
        raise refuse('invalid_content') from None
    if not key or len(key) > 512 or any(ord(c) < 33 or ord(c) == 127 for c in key):
        raise refuse('invalid_content')
    return key


def read_regular_bytes(path, limit, *, not_regular, too_large):
    """The bytes of a regular, non-symlink file of at most ``limit`` bytes (ValueError otherwise)."""
    check_store(path)
    if path.is_symlink() or not stat.S_ISREG(path.stat().st_mode):
        raise ValueError(not_regular)
    raw = path.read_bytes()
    if len(raw) > limit:
        raise ValueError(too_large)
    return raw


def checked_regular_file(path, *, suffix, max_bytes, refused, too_large):
    """An absolute, regular, non-symlink file with ``suffix`` of at most ``max_bytes`` (ValueError)."""
    check_store(path)
    path = Path(path)
    if not path.is_absolute() or path.is_symlink() or not path.is_file() or path.suffix != suffix:
        raise ValueError(refused)
    if path.stat().st_size > max_bytes:
        raise ValueError(too_large)
    return path


# --- (e) the store-path rule ------------------------------------------------------------------------

STORE_ROOT = PurePosixPath('/state/memory')
# Pre-T9b store directories on the /state bind; `kp-agent-install prepare` moves them.
LEGACY_STORES = ('registry', 'assistant', 'desk-memory')
# Operator-file keys that name a store path. A `config_template` (the assistant's memory.json
# template) is an operator file (T11b Q4): it belongs under /config and is refused under /state.
STORE_KEYS = ('state_root', 'roster_path')
TEMPLATE_KEY = 'config_template'
OPERATOR_ROOT = PurePosixPath('/config')
_CONTROL = re.compile(r'[\x00-\x1f\x7f]')


def within(path, base):
    """``path`` is ``base`` or below it (lexically)."""
    return path == base or base in path.parents


def state_path(value, *, reject_control):
    """A lexically normalized absolute container path, or None for anything else.

    With ``reject_control`` a value containing a control character is None too.
    """
    if not isinstance(value, str) or not value.startswith('/') or (reject_control and _CONTROL.search(value)):
        return None
    return PurePosixPath(os.path.normpath(value))


def required_store_path(key, value, *, reject_control):
    """The path a store-path value must become, or None when it is already where it belongs.

    `state_root` and `roster_path` are store paths: under /state/memory. A `config_template` is
    the assistant's template, an operator file (T11b Q4): anywhere under /state, /state/memory
    included, it is required under /config; elsewhere it is left alone (launches/ is under the
    template's own state_root, never beside the template). ``reject_control``: see ``state_path``.
    """
    path = state_path(value, reject_control=reject_control)
    if path is None:
        return None
    state, memory = PurePosixPath('/state'), STORE_ROOT
    if key == TEMPLATE_KEY:
        return f'a path under {OPERATOR_ROOT}' if within(path, state) else None
    if within(path, memory):
        return None
    if within(path, state) and path != state:
        return str(memory / path.relative_to(state))
    return f'a path under {STORE_ROOT}'


# --- (h) store filenames ----------------------------------------------------------------------------

EPISODES_DB = 'episodes.sqlite3'
SESSIONS_DB = 'sessions.sqlite3'
SEARCH_INDEX_DB = 'episode-search.sqlite3'
WORKSPACE_CAPTURE_DB = 'workspace-capture.sqlite3'
SESSION_IMPORT_JOBS_DB = 'session-import-jobs.sqlite3'
SPOOL_INGEST_DB = 'spool-ingest.sqlite3'
CAPTURE_DB = 'capture.sqlite3'
QUEUE_DB = 'queue.sqlite3'
TELEMETRY_DB = 'telemetry.sqlite3'
ROLLOUT_CAPTURE_DB = 'rollout-capture.sqlite3'
GATEWAY_LEDGER_DB = 'ledger.sqlite3'
REFERENCES_DB = 'references.sqlite3'
# T12b: the reindex builds beside the index and renames into place; the lease and the
# indexer role's health state are files of the state root (the volume root, for health).
SEARCH_INDEX_BUILD_DB = 'episode-search.build.sqlite3'
INDEX_LOCK = 'index.lock'
INDEXER_HEALTH = 'indexer-health.json'


def store_path(anchor, name, *, sibling=False):
    """Store file ``name`` in directory ``anchor``, or, with ``sibling``, beside the file ``anchor``.

    It keeps the anchor's mark (a StorePath anchor gives a StorePath) and adds none."""
    return anchor.with_name(name) if sibling else anchor / name


# --- (e2) store paths at open (T11b Q1) -------------------------------------------------------------

# Compose sets it in every role (`x-environment`); `docker exec` and `docker compose run` inherit it.
VOLUME_VARIABLE = 'AGENT_MEMORY_VOLUME'
STORE_REFUSAL = 'store_outside_volume'


class StorePath(type(Path())):
    """A store path: derived from a ``state_root``, a ``roster_path``, ``launches/`` or a launch receipt.

    The mark is the type. A path derived from a StorePath (``/``, ``joinpath``, ``with_name``,
    ``with_suffix``, ``parent``, ``resolve``) is a StorePath; ``Path(store_path)`` is a plain path,
    so a store class keeps the mark with ``as_path``.
    """
    __slots__ = ()


class StoreOutsideVolume(ValueError):
    """In the Docker runtime, a store path that does not resolve under /state/memory (Q1)."""
    category = STORE_REFUSAL


def docker_runtime():
    """This process runs in the Docker runtime: ``AGENT_MEMORY_VOLUME`` is set (a host install is exempt)."""
    return bool(os.environ.get(VOLUME_VARIABLE))


def check_store(path):
    """``path``; a marked store path that does not resolve under /state/memory is refused, in the Docker
    runtime, before anything is opened or created (StoreOutsideVolume). An unmarked path passes."""
    if isinstance(path, StorePath) and docker_runtime():
        resolved = os.path.realpath(path)
        if not within(PurePosixPath(resolved), STORE_ROOT):
            shown = str(path) if str(path) == resolved else f'{path} (it resolves to {resolved})'
            raise StoreOutsideVolume(f'{STORE_REFUSAL}: the store path {shown} is outside {STORE_ROOT}, '
                                     'the store volume')
    return path


def mark_store(value):
    """A derivation point: ``value`` (a state_root, a roster_path, a launch receipt, or a store a receipt
    names) as a StorePath, refused here when it is outside the volume. None stays None."""
    if value is None:
        return None
    return check_store(value if isinstance(value, StorePath) else StorePath(value))


def store_file(state_root, name):
    """The store file ``name`` of ``state_root``: a marked, checked store path."""
    return check_store(mark_store(state_root) / name)


def launches_dir(state_root):
    """``state_root/launches``: launches, their receipts and per-launch memory configs (T11b Q4: never
    beside a template, never ``state_root.parent``)."""
    return check_store(mark_store(state_root) / 'launches')


def as_path(path):
    """``Path(path)``, keeping a StorePath's mark."""
    return path if isinstance(path, StorePath) else Path(path)


def error_category(error):
    """The category a CLI reports: ``store_outside_volume`` for a refused store path, else the type name."""
    return error.category if isinstance(error, StoreOutsideVolume) else type(error).__name__


# --- what stays outside the leaf ---------------------------------------------------------------------

# Each entry is file:line at this commit, grouped by the reason the order or the Coordinator's
# allowlist rule admits. Data only; the leaf reads none of it.
OUTSIDE_THE_LEAF = {
    # stdlib-only scripts run under python3 -I -c (P2, P3, P4); unchanged
    'embedded_stdlib_script': (
        'packages/tooling/src/kp_agent_tooling/_impl/runtime_install.py:1078-1087 _INSPECT',
        'packages/tooling/src/kp_agent_tooling/_impl/runtime_install.py:1090-1097 _OWN',
        'packages/tooling/src/kp_agent_tooling/_impl/runtime_install.py:1101-1119 _CLEAR',
        'packages/tooling/src/kp_agent_tooling/_impl/runtime_install.py:1126-1258 _STORE_LIB',
        'packages/tooling/src/kp_agent_tooling/_impl/runtime_install.py:1289-1443 _MIGRATE',
        'packages/tooling/src/kp_agent_tooling/_impl/dependency_identity.py:124-130 script',
    ),
    # digest of the exact bytes sent over a wire, kept as a receipt field (scip_entry.py:41 is the ruled non-canonical one)
    'wire_hash': (
        'packages/tooling/src/kp_agent_tooling/_impl/service/scip_entry.py:41',
        'packages/tooling/src/kp_agent_tooling/_impl/service/episodic_summarizer.py:346',
    ),
    # digest of the exact bytes written to a file and recorded beside it
    'file_content_hash': (
        'extensions/ops/src/kp_agent_tooling_ops/capture_cli.py:52',
        'extensions/ops/src/kp_agent_tooling_ops/transcript_cli.py:38',
        'extensions/ops/src/kp_agent_tooling_ops/transcript_cli.py:44',
    ),
    # hashlib.sha256() with .update, or the empty-digest constant (P2)
    'streaming_digest': (
        'packages/tooling/src/kp_agent_tooling/_impl/service/claude_episode_capture.py:155',
        'packages/tooling/src/kp_agent_tooling/_impl/service/launch_binding.py:611',
        'packages/tooling/src/kp_agent_tooling/_impl/service/session_import_job.py:77',
        'packages/tooling/src/kp_agent_tooling/_impl/service/spool_ingest.py:520',
        'packages/tooling/src/kp_agent_tooling/_impl/service/spool_ingest.py:95',
    ),
    # inline hashlib.sha256(<raw bytes>).hexdigest() of file bytes, text or a received payload (Q1)
    'raw_bytes_digest': (
        'extensions/ops/src/kp_agent_tooling_ops/_impl/code_references/extraction.py:80',
        'extensions/ops/src/kp_agent_tooling_ops/_impl/code_references/portable_generation.py:28',
        'extensions/ops/src/kp_agent_tooling_ops/_impl/code_references/registry.py:62',
        'extensions/ops/src/kp_agent_tooling_ops/_impl/code_references/registry.py:92',
        'extensions/ops/src/kp_agent_tooling_ops/_impl/code_references/source_facts.py:89',
        'extensions/ops/src/kp_agent_tooling_ops/_impl/commit_rationale.py:109',
        'extensions/ops/src/kp_agent_tooling_ops/_impl/commit_rationale.py:123',
        'extensions/ops/src/kp_agent_tooling_ops/_impl/commit_rationale.py:348',
        'extensions/ops/src/kp_agent_tooling_ops/_impl/commit_rationale.py:470',
        'extensions/ops/src/kp_agent_tooling_ops/_impl/commit_rationale.py:193',
        'extensions/ops/src/kp_agent_tooling_ops/_impl/commit_rationale.py:303',
        'extensions/ops/src/kp_agent_tooling_ops/_impl/commit_rationale.py:658',
        'extensions/ops/src/kp_agent_tooling_ops/_impl/embeddings/desk_docs.py:100',
        'extensions/ops/src/kp_agent_tooling_ops/_impl/journey_registry.py:209',
        'extensions/ops/src/kp_agent_tooling_ops/_impl/lifecycle_matrix.py:254',
        'extensions/ops/src/kp_agent_tooling_ops/_impl/lifecycle_matrix.py:302',
        'extensions/ops/src/kp_agent_tooling_ops/_impl/lifecycle_matrix.py:323',
        'extensions/ops/src/kp_agent_tooling_ops/_impl/review_ledger.py:156',
        'extensions/ops/src/kp_agent_tooling_ops/_impl/service/delegation_attribution.py:48',
        'extensions/ops/src/kp_agent_tooling_ops/_impl/service/document_corpus.py:31',
        'extensions/ops/src/kp_agent_tooling_ops/_impl/service/document_corpus.py:63',
        'extensions/ops/src/kp_agent_tooling_ops/_impl/service/legacy_desk_import.py:109',
        'extensions/ops/src/kp_agent_tooling_ops/_impl/service/legacy_desk_import.py:63',
        'extensions/ops/src/kp_agent_tooling_ops/_impl/service/legacy_desk_import.py:74',
        'extensions/ops/src/kp_agent_tooling_ops/_impl/service/ops_tools.py:374',
        'extensions/ops/src/kp_agent_tooling_ops/_impl/service/ops_tools.py:254',
        'extensions/ops/src/kp_agent_tooling_ops/_impl/service/published_navigation.py:52',
        'extensions/ops/src/kp_agent_tooling_ops/_impl/service/published_navigation.py:58',
        'extensions/ops/src/kp_agent_tooling_ops/_impl/tool_discovery.py:151',
        'extensions/ops/src/kp_agent_tooling_ops/_impl/verification_adjacency.py:71',
        'extensions/ops/src/kp_agent_tooling_ops/_impl/verification_adjacency.py:83',
        'extensions/ops/src/kp_agent_tooling_ops/_impl/verification_correctness.py:15',
        'extensions/ops/src/kp_agent_tooling_ops/_impl/verification_correctness.py:16',
        'extensions/ops/src/kp_agent_tooling_ops/_impl/verification_correctness.py:31',
        'extensions/ops/src/kp_agent_tooling_ops/_impl/verification_correctness.py:32',
        'extensions/ops/src/kp_agent_tooling_ops/_impl/verification_packet.py:57',
        'extensions/ops/src/kp_agent_tooling_ops/_impl/verification_packet.py:30',
        'extensions/ops/src/kp_agent_tooling_ops/_impl/verification_packet.py:60',
        'extensions/ops/src/kp_agent_tooling_ops/_impl/verification_packet.py:62',
        'extensions/ops/src/kp_agent_tooling_ops/_impl/verification_packet.py:104',
        'extensions/ops/src/kp_agent_tooling_ops/_impl/verification_packet.py:46',
        'extensions/ops/src/kp_agent_tooling_ops/charter_import_cli.py:20',
        'extensions/ops/src/kp_agent_tooling_ops/charter_import_cli.py:78',
        'extensions/ops/src/kp_agent_tooling_ops/knowledge_publish_cli.py:148',
        'extensions/ops/src/kp_agent_tooling_ops/knowledge_publish_cli.py:118',
        'packages/tooling/src/kp_agent_tooling/_impl/dependency_identity.py:89',
        'packages/tooling/src/kp_agent_tooling/_impl/dependency_identity.py:106',
        'packages/tooling/src/kp_agent_tooling/_impl/dependency_identity.py:113',
        'packages/tooling/src/kp_agent_tooling/_impl/embeddings/embedders.py:79',
        'packages/tooling/src/kp_agent_tooling/_impl/navigation_discovery.py:201',
        'packages/tooling/src/kp_agent_tooling/_impl/navigation_environment.py:22',
        'packages/tooling/src/kp_agent_tooling/_impl/navigation_search_pages.py:73',
        'packages/tooling/src/kp_agent_tooling/_impl/navigation_search_pages.py:300',
        'packages/tooling/src/kp_agent_tooling/_impl/navigation_snapshot.py:52',
        'packages/tooling/src/kp_agent_tooling/_impl/navigation_snapshot.py:107',
        'packages/tooling/src/kp_agent_tooling/_impl/navigation_snapshot.py:36',
        'packages/tooling/src/kp_agent_tooling/_impl/navigation_snapshot.py:58',
        'packages/tooling/src/kp_agent_tooling/_impl/navigation_snapshot.py:98',
        'packages/tooling/src/kp_agent_tooling/_impl/navigation_snapshot.py:119',
        'packages/tooling/src/kp_agent_tooling/_impl/navigation_snapshot.py:41',
        'packages/tooling/src/kp_agent_tooling/_impl/navigation_snapshot.py:63',
        'packages/tooling/src/kp_agent_tooling/_impl/navigation_workspace.py:133',
        'packages/tooling/src/kp_agent_tooling/_impl/navigation_workspace.py:311',
        'packages/tooling/src/kp_agent_tooling/_impl/navigation_workspace.py:115',
        'packages/tooling/src/kp_agent_tooling/_impl/platform_snapshot.py:77',
        'packages/tooling/src/kp_agent_tooling/_impl/scip_navigation.py:71',
        'packages/tooling/src/kp_agent_tooling/_impl/service/agent_tooling.py:39',
        'packages/tooling/src/kp_agent_tooling/_impl/service/desk_catalog_setup.py:78',
        'packages/tooling/src/kp_agent_tooling/_impl/service/desk_profiles.py:255',
        'packages/tooling/src/kp_agent_tooling/_impl/service/episodic_summarizer.py:350',
        'packages/tooling/src/kp_agent_tooling/_impl/service/import_context.py:305',
        'packages/tooling/src/kp_agent_tooling/_impl/service/launch_binding.py:482',
        'packages/tooling/src/kp_agent_tooling/_impl/service/model_gateway.py:624',
        'packages/tooling/src/kp_agent_tooling/_impl/service/model_gateway.py:669',
        'packages/tooling/src/kp_agent_tooling/_impl/service/model_gateway.py:620',
        'packages/tooling/src/kp_agent_tooling/_impl/service/serena_navigation.py:114',
        'packages/tooling/src/kp_agent_tooling/_impl/service/serena_navigation.py:184',
        'packages/tooling/src/kp_agent_tooling/_impl/service/spool_ingest.py:190',
        'packages/tooling/src/kp_agent_tooling/_impl/service/workspace_context.py:443',
        'packages/tooling/src/kp_agent_tooling/_impl/service/workspace_context.py:465',
        'packages/tooling/src/kp_agent_tooling/_impl/service/workspace_context.py:501',
        'packages/tooling/src/kp_agent_tooling/_impl/source_citations.py:57',
        'packages/tooling/src/kp_agent_tooling/_impl/tool_delivery.py:50',
        'packages/tooling/src/kp_agent_tooling/_impl/tool_delivery.py:78',
        'packages/tooling/src/kp_agent_tooling/_impl/tool_delivery.py:49',
        'packages/tooling/src/kp_agent_tooling/_impl/tool_delivery.py:55',
        'packages/tooling/src/kp_agent_tooling/_impl/typescript_context.py:18',
        'packages/tooling/src/kp_agent_tooling/_impl/typescript_context.py:30',
        'packages/tooling/src/kp_agent_tooling/_impl/workspace_setup.py:127',
        'packages/tooling/src/kp_agent_tooling/_impl/workspace_setup.py:136',
        'packages/tooling/src/kp_agent_tooling/refresh_cli.py:446',
        'packages/tooling/src/kp_agent_tooling/refresh_cli.py:447',
        'packages/tooling/src/kp_agent_tooling/refresh_cli.py:448',
        'packages/tooling/src/kp_agent_tooling/refresh_cli.py:520',
        'packages/tooling/src/kp_agent_tooling/refresh_cli.py:449',
        'packages/tooling/src/kp_agent_tooling/refresh_cli.py:660',
        'packages/tooling/src/kp_agent_tooling/refresh_cli.py:541',
        'packages/tooling/src/kp_agent_tooling/refresh_cli.py:668',
        'packages/tooling/src/kp_agent_tooling/refresh_cli.py:623',
    ),
    # renames of whole directories, not private files (staging, tombstones, migration and its undo).
    # T11b Q3 adopts the wider P3 set: every site left at T11a head moved into the leaf except
    # workspace_setup.py:178, which renames the setup bundle's staging directory into place (its
    # files are written 0600 by overwrite_private first); it is named here, in this class.
    'directory_rename': (
        'packages/tooling/src/kp_agent_tooling/_impl/refresh_retention.py:211',
        'packages/tooling/src/kp_agent_tooling/_impl/runtime_install.py:1620',
        'packages/tooling/src/kp_agent_tooling/_impl/runtime_install.py:1629',
        'packages/tooling/src/kp_agent_tooling/_impl/service/model_gateway.py:458',
        'packages/tooling/src/kp_agent_tooling/_impl/workspace_setup.py:178',
    ),
    # P8 allowlist: http.server hooks, and the production embedder kept for T13
    'dead_code_kept': (
        'packages/tooling/src/kp_agent_tooling/_impl/workspace_setup_server.py:48 get_request',
        'packages/tooling/src/kp_agent_tooling/_impl/workspace_setup_server.py:75 log_message',
        'packages/tooling/src/kp_agent_tooling/_impl/workspace_setup_server.py:124 do_GET',
        'packages/tooling/src/kp_agent_tooling/_impl/workspace_setup_server.py:144 do_POST',
        'packages/tooling/src/kp_agent_tooling/_impl/embeddings/embedders.py:198 ProductionLocalEmbedding',
    ),
    # P9: one-statement delegations, or import aliases, under names tests and modules use
    'old_name_delegation': (
        'extensions/ops/src/kp_agent_tooling_ops/_impl/behavior_model.py:13 digest',
        'extensions/ops/src/kp_agent_tooling_ops/_impl/code_references/models.py:11 canonical_digest',
        'extensions/ops/src/kp_agent_tooling_ops/_impl/code_references/retrieval.py:32 _chunk_id',
        'extensions/ops/src/kp_agent_tooling_ops/_impl/host_transcript_capture.py:34 _canonical',
        'extensions/ops/src/kp_agent_tooling_ops/_impl/observations.py:12 encoded',
        'extensions/ops/src/kp_agent_tooling_ops/_impl/observations.py:16 digest',
        'extensions/ops/src/kp_agent_tooling_ops/_impl/review_ledger.py:18 canonical_digest',
        'extensions/ops/src/kp_agent_tooling_ops/_impl/service/document_coordinates.py:29 _chunk_id',
        'extensions/ops/src/kp_agent_tooling_ops/_impl/trial_response_capture.py:34 _canonical',
        'extensions/ops/src/kp_agent_tooling_ops/_impl/verification_finding.py:19 finding_digest',
        'extensions/ops/src/kp_agent_tooling_ops/knowledge_publish_cli.py:30 _write',
        'packages/tooling/src/kp_agent_tooling/_impl/embeddings/revision.py:55 _sha256_identity',
        'packages/tooling/src/kp_agent_tooling/_impl/navigation_search_pages.py:41 _canonical',
        'packages/tooling/src/kp_agent_tooling/_impl/navigation_search_pages.py:52 _save',
        'packages/tooling/src/kp_agent_tooling/_impl/navigation_search_pages.py:146 _store_manifest',
        'packages/tooling/src/kp_agent_tooling/_impl/navigation_snapshot.py:20 _write_once',
        'packages/tooling/src/kp_agent_tooling/_impl/navigation_snapshot.py:25 _read_regular',
        'packages/tooling/src/kp_agent_tooling/_impl/repository_manifest.py:46 _identity',
        'packages/tooling/src/kp_agent_tooling/_impl/runtime_install.py:166 canonical',
        'packages/tooling/src/kp_agent_tooling/_impl/runtime_install.py:963 _write',
        'packages/tooling/src/kp_agent_tooling/_impl/runtime_install.py:992 _placeholder',
        'packages/tooling/src/kp_agent_tooling/_impl/scip_navigation.py:15 _bytes',
        'packages/tooling/src/kp_agent_tooling/_impl/scip_navigation.py:19 _atomic',
        'packages/tooling/src/kp_agent_tooling/_impl/scip_navigation.py:123 digest',
        'packages/tooling/src/kp_agent_tooling/_impl/semantic_index.py:50 _canonical',
        'packages/tooling/src/kp_agent_tooling/_impl/semantic_index.py:236 _write_once',
        'packages/tooling/src/kp_agent_tooling/_impl/service/claude_episode_capture.py:37 _digest',
        'packages/tooling/src/kp_agent_tooling/_impl/service/desk_catalog_setup.py:30 _private_parent',
        'packages/tooling/src/kp_agent_tooling/_impl/service/desk_catalog_setup.py:123 _write_private_json',
        'packages/tooling/src/kp_agent_tooling/_impl/service/desk_identity.py:17 binding_key',
        'packages/tooling/src/kp_agent_tooling/_impl/service/desk_memory_runtime.py:19 private_json',
        'packages/tooling/src/kp_agent_tooling/_impl/service/desk_registry.py:40 _private_json',
        'packages/tooling/src/kp_agent_tooling/_impl/service/desk_registry.py:123 _locked',
        'packages/tooling/src/kp_agent_tooling/_impl/service/desk_registry.py:126 _write',
        'packages/tooling/src/kp_agent_tooling/_impl/service/desk_write_scope.py:9 _digest',
        'packages/tooling/src/kp_agent_tooling/_impl/service/episodic_handoff.py:5 encoded_size',
        'packages/tooling/src/kp_agent_tooling/_impl/service/episodic_memory.py:69 _bytes',
        'packages/tooling/src/kp_agent_tooling/_impl/service/episodic_memory.py:73 _id',
        'packages/tooling/src/kp_agent_tooling/_impl/service/episodic_queue.py:21 _json',
        'packages/tooling/src/kp_agent_tooling/_impl/service/episodic_queue.py:25 _digest',
        'packages/tooling/src/kp_agent_tooling/_impl/service/host_adapter.py:97 _owned',
        'packages/tooling/src/kp_agent_tooling/_impl/service/host_adapter.py:101 _private_dir',
        'packages/tooling/src/kp_agent_tooling/_impl/service/host_adapter.py:105 _read_json',
        'packages/tooling/src/kp_agent_tooling/_impl/service/host_adapter.py:110 _write_new',
        'packages/tooling/src/kp_agent_tooling/_impl/service/host_adapter.py:116 _replace',
        'packages/tooling/src/kp_agent_tooling/_impl/service/host_adapter.py:435 codex_trust_entry',
        'packages/tooling/src/kp_agent_tooling/_impl/service/launch_binding.py:63 write_private',
        'packages/tooling/src/kp_agent_tooling/_impl/service/launch_binding.py:68 _write_once',
        'packages/tooling/src/kp_agent_tooling/_impl/service/launch_binding.py:73 private_dir',
        'packages/tooling/src/kp_agent_tooling/_impl/service/launch_binding.py:77 _locked',
        'packages/tooling/src/kp_agent_tooling/_impl/service/launch_binding.py:126 codex_trust_entry',
        'packages/tooling/src/kp_agent_tooling/_impl/service/memory_budget.py:32 _digest',
        'packages/tooling/src/kp_agent_tooling/_impl/service/model_gateway.py:259 read_api_key',
        'packages/tooling/src/kp_agent_tooling/_impl/service/model_gateway.py:438 _write_private',
        'packages/tooling/src/kp_agent_tooling/_impl/service/model_gateway_providers.py:113 _json',
        'packages/tooling/src/kp_agent_tooling/_impl/service/native_history_import.py:35 _regular',
        'packages/tooling/src/kp_agent_tooling/_impl/service/session_bindings.py:34 _digest',
        'packages/tooling/src/kp_agent_tooling/_impl/service/session_import_job.py:56 _json',
        'packages/tooling/src/kp_agent_tooling/_impl/service/session_sources.py:969 _sealed_legacy',
        'packages/tooling/src/kp_agent_tooling/_impl/service/spool_ingest.py:89 locked',
        'packages/tooling/src/kp_agent_tooling/_impl/service/summary_contract.py:23 request_bytes',
        'packages/tooling/src/kp_agent_tooling/_impl/workspace_setup.py:16 encoded',
        'packages/tooling/src/kp_agent_tooling/_impl/workspace_setup.py:20 digest',
        'packages/tooling/src/kp_agent_tooling/refresh_cli.py:44 atomic',
        'packages/tooling/src/kp_agent_tooling/refresh_cli.py:91 digest_json',
        'extensions/ops/src/kp_agent_tooling_ops/_impl/host_transcript_capture.py:15 _sha = leaf.sha256_hex',
        'extensions/ops/src/kp_agent_tooling_ops/_impl/trial_response_capture.py:15 _sha = leaf.sha256_hex',
        'packages/tooling/src/kp_agent_tooling/_impl/runtime_install.py:56 sha256 = leaf.sha256_hex',
        'packages/tooling/src/kp_agent_tooling/_impl/semantic_index.py:27 _digest = leaf.sha256_hex',
        'packages/tooling/src/kp_agent_tooling/_impl/service/host_adapter.py:45 _sha = leaf.sha256_hex',
        'packages/tooling/src/kp_agent_tooling/_impl/service/native_history_import.py:18 _sha = leaf.sha256_hex',
        'packages/tooling/src/kp_agent_tooling/_impl/service/session_import_job.py:21 _sha = leaf.sha256_hex',
        'packages/tooling/src/kp_agent_tooling/_impl/service/workspace_capture.py:18 _sha = leaf.sha256_hex',
        'packages/tooling/src/kp_agent_tooling/refresh_cli.py:29 digest_file = leaf.sha256_file',
    ),
    # P6: a docstring, and the embedded suffix check
    'store_filename_text': (
        'packages/tooling/src/kp_agent_tooling/_impl/runtime_install.py:1126 embedded _STORE_LIB suffix check',
        'packages/tooling/src/kp_agent_tooling/_impl/service/spool_ingest.py:1 module docstring',
    ),
}
