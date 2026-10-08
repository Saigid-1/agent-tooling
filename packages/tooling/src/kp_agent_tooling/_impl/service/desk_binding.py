"""Harness-independent, owner-selected desk binding for an existing session.

The session ID is supplied by the host adapter. The desk identity is stored by
the host-owned ledger and never accepted from a model tool argument.
"""

from __future__ import annotations

import contextvars
import re
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from typing import Protocol
from kp_agent_tooling._impl import leaf
from kp_agent_tooling._impl.service.desk_identity import BindingRecord

class DeskAuthority(Protocol):
    def resolve(self, *, tenant_id: str, role: str, repo_key: str) -> BindingRecord: ...


_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")

# One memory call resolves admission and the registry at most once. The memo
# lives only for the duration of one call (and the construction that precedes
# a tools object's first call); it never carries store content across calls.
_CALL = contextvars.ContextVar("kp_agent_tooling_memory_call", default=None)


@contextmanager
def call_scope(seed=None):
    """Per-call memo for admission and registry reads; nested scopes share it."""
    current = _CALL.get()
    if current is not None:
        if seed:
            for key, value in seed.items():
                current.setdefault(key, value)
        yield current
        return
    memo = dict(seed or {})
    token = _CALL.set(memo)
    try:
        yield memo
    finally:
        _CALL.reset(token)


def call_memo():
    """The active per-call memo, or None outside a memory call."""
    return _CALL.get()


class DeskLaunchConflict(ValueError):
    """A provider session already belongs to another desk or model selection."""


class DeskLaunchUnavailable(RuntimeError):
    """Durable admission state could not be verified."""


@dataclass(frozen=True, slots=True)
class DeskLaunchReceipt:
    provider_session_id: str
    provider_instance: str
    binding_key: str
    tenant_id: str
    role: str
    repo_key: str
    provider_id: str
    model_id: str


class DeskSessionLedger:
    """Immutable session admission; closing a session never deletes desk memory."""

    def __init__(self, path: str | Path, *, provider_instance: str):
        if not _ID.fullmatch(provider_instance):
            raise ValueError("safe harness instance ID required")
        self.path = leaf.as_path(path).expanduser().resolve()
        self.provider_instance = provider_instance

    def initialize(self) -> None:
        if self.path.exists():
            raise DeskLaunchConflict("desk session ledger already exists; do not reset admission")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with leaf.sqlite_create(self.path) as db:
            db.execute("""CREATE TABLE desk_session_launches (
                provider_instance TEXT NOT NULL,
                provider_session_id TEXT NOT NULL,
                binding_key TEXT NOT NULL,
                tenant_id TEXT NOT NULL,
                role TEXT NOT NULL,
                repo_key TEXT NOT NULL,
                provider_id TEXT NOT NULL,
                model_id TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (provider_instance, provider_session_id)
            )""")

    def assert_ready(self) -> None:
        try:
            with leaf.sqlite_connect(self.path, mode="ro", resolve=True) as db:
                if db.execute("PRAGMA quick_check").fetchone() != ("ok",):
                    raise DeskLaunchUnavailable("desk launch ledger is corrupt")
                columns = db.execute("PRAGMA table_info(desk_session_launches)").fetchall()
                if {row[1] for row in columns} != {
                    "provider_instance", "provider_session_id", "binding_key",
                    "tenant_id", "role", "repo_key", "provider_id",
                    "model_id", "created_at",
                }:
                    raise DeskLaunchUnavailable("desk launch ledger schema is incomplete")
        except sqlite3.Error as error:
            raise DeskLaunchUnavailable("desk launch ledger unavailable") from error

    def admit(self, receipt: DeskLaunchReceipt) -> DeskLaunchReceipt:
        if (not isinstance(receipt, DeskLaunchReceipt)
                or receipt.provider_instance != self.provider_instance
                or not _ID.fullmatch(receipt.provider_session_id)):
            raise ValueError("valid harness session ID required")
        if any(not isinstance(v, str) or not v or len(v.encode()) > 512 for v in (
                receipt.binding_key, receipt.tenant_id, receipt.role, receipt.repo_key,
                receipt.provider_id, receipt.model_id)):
            raise ValueError("bounded nonempty admission coordinates required")
        self.assert_ready()
        try:
            with leaf.sqlite_connect(self.path, mode="rw", resolve=True) as db:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute(
                    "SELECT binding_key,tenant_id,role,repo_key,provider_id,model_id "
                    "FROM desk_session_launches WHERE provider_instance=? AND provider_session_id=?",
                    (self.provider_instance, receipt.provider_session_id),
                ).fetchone()
                expected = (
                    receipt.binding_key, receipt.tenant_id, receipt.role,
                    receipt.repo_key, receipt.provider_id, receipt.model_id,
                )
                if row is None:
                    db.execute(
                        "INSERT INTO desk_session_launches "
                        "(provider_instance,provider_session_id,binding_key,tenant_id,role,repo_key,provider_id,model_id) "
                        "VALUES (?,?,?,?,?,?,?,?)",
                        (self.provider_instance, receipt.provider_session_id, *expected),
                    )
                elif row != expected:
                    raise DeskLaunchConflict("provider session is already bound to another selection")
        except sqlite3.Error as error:
            raise DeskLaunchUnavailable("desk launch ledger unavailable") from error
        return receipt

    def _ledger_key(self, provider_session_id):
        return ("ledger", str(self.path), self.provider_instance, provider_session_id)

    def admission_row(self, provider_session_id: str) -> tuple:
        """The immutable ledger row of this exact session.

        The ledger integrity check and the row read happen at most once per
        memory call; outside a call they happen on every invocation.
        """
        if not isinstance(provider_session_id, str) or not _ID.fullmatch(provider_session_id):
            raise ValueError("valid harness session ID required")
        memo = _CALL.get()
        key = self._ledger_key(provider_session_id)
        if memo is not None and key in memo:
            return memo[key]
        self.assert_ready()
        try:
            with leaf.sqlite_connect(self.path, mode="ro", resolve=True) as db:
                row = db.execute(
                    "SELECT binding_key,tenant_id,role,repo_key,provider_id,model_id "
                    "FROM desk_session_launches WHERE provider_instance=? AND provider_session_id=?",
                    (self.provider_instance, provider_session_id),
                ).fetchone()
        except sqlite3.Error as error:
            raise DeskLaunchUnavailable("desk launch ledger unavailable") from error
        if row is None:
            raise DeskLaunchUnavailable("session has no desk admission")
        if memo is not None:
            memo[key] = row
        return row

    def resolve(self, provider_session_id: str, registry: DeskAuthority) -> DeskLaunchReceipt:
        memo = _CALL.get()
        key = ("admission", str(self.path), self.provider_instance, provider_session_id, id(registry))
        if memo is not None and key in memo:
            return memo[key]
        row = self.admission_row(provider_session_id)
        binding = registry.resolve(tenant_id=row[1], role=row[2], repo_key=row[3])
        if binding.binding_key != row[0]:
            raise DeskLaunchConflict("registered binding changed")
        receipt = DeskLaunchReceipt(provider_session_id, self.provider_instance, *row)
        if memo is not None:
            memo[key] = receipt
        return receipt
