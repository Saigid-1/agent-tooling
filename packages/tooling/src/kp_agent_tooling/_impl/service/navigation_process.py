"""Bounded subprocess execution for portable navigation workers.

This module has no AST index, cache, or legacy CLI dependency.
"""
from __future__ import annotations

import os
from pathlib import Path
import selectors
import signal
import subprocess
import time

class NavigationSubprocessError(ValueError):
    """A known process exit with explicitly unavailable underlying diagnostics."""

    def __init__(self, returncode):
        super().__init__('navigation subprocess failed')
        self.returncode = returncode


class NavigationProcessRunner:
    def __init__(self, tool_root: Path, *, timeout: float = 45):
        self.tool_root = Path(tool_root).resolve()
        self.timeout = float(timeout)
        if not 0 < self.timeout <= 300:
            raise ValueError('navigation timeout must be positive and at most 300 seconds')

    def run(self, argv, *, cwd, max_bytes=65536, pythonpath=True):
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(('GIT_', 'PYTHON'))}
        env.update(PYTHONDONTWRITEBYTECODE='1', PYTHONNOUSERSITE='1', GIT_TERMINAL_PROMPT='0')
        if pythonpath:
            env['PYTHONPATH'] = str(self.tool_root)
        deadline = time.monotonic() + self.timeout
        with subprocess.Popen(argv, cwd=cwd, env=env, stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL, start_new_session=True) as child:
            try:
                output = bytearray()
                with selectors.DefaultSelector() as selector:
                    selector.register(child.stdout, selectors.EVENT_READ)
                    while selector.get_map():
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise TimeoutError('navigation deadline exceeded')
                        for key, _ in selector.select(remaining):
                            chunk = os.read(key.fd, min(65536, max_bytes + 1 - len(output)))
                            if not chunk:
                                selector.unregister(key.fileobj)
                                break
                            output.extend(chunk)
                            if len(output) > max_bytes:
                                raise ValueError('navigation output bound exceeded')
                child.wait(timeout=max(0.001, deadline - time.monotonic()))
                if child.returncode:
                    raise NavigationSubprocessError(child.returncode)
                return bytes(output)
            except BaseException:
                # Kill descendants too: the CLI starts git subprocesses.
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                except PermissionError:
                    # Darwin answers EPERM, where Linux answers 0, when no member of the
                    # group can be signalled because each has exited or is exiting (the
                    # leader is not yet reaped). The group is this child's own session, so
                    # a live member would have been signalled. Either way nothing is left
                    # to kill, and the caller gets the original error, not EPERM.
                    pass
                child.wait()
                raise

