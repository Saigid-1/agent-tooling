"""One-process, deadline-bounded reads of many committed blobs.

``git cat-file --batch`` (or ``--batch-check``) is started once for an ordered
list of object ids and its records are consumed on demand.  This replaces the
one-or-two-subprocesses-per-file read model, whose cost was ~15 ms per file
(fork/exec), with a single streamed process whose cost is the bytes read.

Records are served in request order; a record read ahead of its request is
retained so duplicate object ids (identical files at two paths) are served
without re-reading.  Callers bound memory by bounding the ids they request.
"""
import os
import select
import subprocess
import threading
import time


class BatchReadTimeout(TimeoutError):
    """The caller's deadline passed while waiting on the batch stream."""


class BlobBatch:
    def __init__(self, root, blob_ids, *, deadline, check_only=False):
        self.root = root
        self.deadline = deadline
        self.check_only = check_only
        seen = set()
        self.blob_ids = [b for b in blob_ids if not (b in seen or seen.add(b))]
        self._process = None
        self._buffer = b''
        self._records = {}
        self._eof = False

    # -- lifecycle -----------------------------------------------------------
    def _start(self):
        env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
        mode = '--batch-check=%(objectname) %(objecttype) %(objectsize)' if self.check_only else '--batch'
        self._process = subprocess.Popen(
            ['git', '--no-optional-locks', 'cat-file', mode], cwd=self.root, env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        payload = ('\n'.join(self.blob_ids) + '\n').encode('ascii')
        process = self._process

        def feed():
            try:
                process.stdin.write(payload)
                process.stdin.close()
            except (BrokenPipeError, OSError, ValueError):
                pass
        threading.Thread(target=feed, daemon=True).start()

    def close(self):
        process, self._process = self._process, None
        if process is None:
            return
        if process.poll() is None:
            process.kill()
        process.wait()
        for stream in (process.stdout, process.stdin):
            if stream is not None:
                stream.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # -- stream plumbing -----------------------------------------------------
    def _read_more(self):
        if self._eof:
            return False
        wait = self.deadline - time.monotonic()
        if wait <= 0:
            raise BatchReadTimeout('batch read deadline exhausted')
        if not select.select([self._process.stdout], [], [], wait)[0]:
            raise BatchReadTimeout('batch read deadline exhausted')
        chunk = os.read(self._process.stdout.fileno(), 1 << 20)
        if not chunk:
            self._eof = True
            return False
        self._buffer += chunk
        return True

    def _read_line(self):
        while b'\n' not in self._buffer:
            if not self._read_more():
                raise ValueError('batch stream ended before a record header')
        line, self._buffer = self._buffer.split(b'\n', 1)
        return line.decode('ascii')

    def _read_exact(self, count):
        while len(self._buffer) < count:
            if not self._read_more():
                raise ValueError('batch stream ended inside a record')
        data, self._buffer = self._buffer[:count], self._buffer[count:]
        return data

    def _next_record(self):
        fields = self._read_line().split()
        if len(fields) == 2 and fields[1] == 'missing':
            return fields[0], None
        if len(fields) != 3:
            raise ValueError('invalid batch record header')
        sha, kind, size = fields[0], fields[1], int(fields[2])
        if self.check_only:
            return sha, (kind, size, None)
        data = self._read_exact(size + 1)[:size]
        return sha, (kind, size, data)

    # -- public --------------------------------------------------------------
    def get(self, blob):
        """Return ``(object_type, size, data_or_None)`` for a requested id.

        Raises ``BatchReadTimeout`` at the deadline and ``ValueError`` for an id
        that was never requested or an object git reports missing.
        """
        if blob in self._records:
            return self._records[blob]
        if blob not in self.blob_ids:
            raise ValueError('object was not part of this batch')
        if self._process is None:
            self._start()
        while True:
            sha, record = self._next_record()
            if record is None:
                if sha == blob:
                    raise ValueError('object unavailable in repository')
                continue
            self._records[sha] = record
            if sha == blob:
                return record
