"""The T1 order's contract, as data the boundary tests read.

Order: docs/work/orders/T1-extension-seam.md, frozen at its merge commit (ADR AT-0004).
Everything here comes from the order text, the frozen disposition CSV, or the
pre-move tool surface captured from the merge-base tree (fixtures/). Nothing here
comes from an implementation of the move.
"""
from __future__ import annotations

import csv
import hashlib
import json
from functools import lru_cache
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
# The pre-move product tree the frozen capture was taken from, by content: the sha256 of
# `git ls-tree -r <tree> -- packages deploy extensions scripts` at the order's merge base (PRODUCT_PATHS).
# The capture command refuses any other product tree, and the fixture records the same value. That tree
# is in the private history only, so fixtures/pre_move_tools_list.json cannot be regenerated from the
# public history as it stands.
PRODUCT_PATHS = ("packages", "deploy", "extensions", "scripts")
PRE_MOVE_PRODUCT_SHA256 = "6a005fa36790d8fcbcc60862e01b69a8386bf30c1218b9270b1718e9f07c652f"

# Frozen input: "one row per module (149) ... It is authoritative."
DISPOSITIONS = REPO_ROOT / "docs" / "work" / "inventory" / "T1-module-dispositions.csv"
DISPOSITIONS_SHA256 = "873641514469f2042401a223c31712a262bd78af88d73adff83a216c19641752"

CORE_SRC = REPO_ROOT / "packages" / "tooling" / "src"
CORE_PROJECT = REPO_ROOT / "packages" / "tooling"
EXTENSION_PROJECT = REPO_ROOT / "extensions" / "ops"
EXTENSION_DISTRIBUTION = "kp-agent-tooling-ops"
EXTENSION_PACKAGE = "kp_agent_tooling_ops"

FIXTURES = HERE / "fixtures"
PRE_MOVE_SURFACE = FIXTURES / "pre_move_tools_list.json"  # frozen; not regenerable outside the private history
FAKE_GATEWAY_CATALOG = FIXTURES / "fake_gateway_catalog.json"

# P1: "No module under packages/tooling/src imports kp_agent_tooling_ops, a MOVE
# module, or kp_core/kp_ops."
FOREIGN_ROOTS = (EXTENSION_PACKAGE, "kp_core", "kp_ops")

# Interface surface: the local knowledge.platform and knowledge.symbol
# (local_knowledge -> platform_snapshot/scip_entry) remain core.
LOCAL_KNOWLEDGE_TOOLS = frozenset({"knowledge.platform", "knowledge.symbol"})


def is_extension_tool_name(name: str) -> bool:
    """The extension registers verification.*, lifecycle.evidence, knowledge.rationale,
    and the portable and gateway knowledge.* tools. knowledge.platform/knowledge.symbol
    are core when served by the local knowledge provider (the only way the core-only
    configurations in these tests serve them)."""
    return (name.startswith("verification.")
            or name in {"lifecycle.evidence", "knowledge.rationale"}
            or (name.startswith("knowledge.") and name not in LOCAL_KNOWLEDGE_TOOLS))


@lru_cache(maxsize=1)
def dispositions() -> tuple[dict, ...]:
    raw = DISPOSITIONS.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    assert digest == DISPOSITIONS_SHA256, (
        f"the frozen input {DISPOSITIONS.relative_to(REPO_ROOT)} changed (sha256 {digest}); "
        "the order freezes it at the merge base, so these tests refuse to re-derive P1 from it")
    rows = tuple(csv.DictReader(raw.decode().splitlines()))
    assert len(rows) == 149, f"expected 149 disposition rows, read {len(rows)}"
    return rows


def modules_with(disposition: str) -> frozenset[str]:
    return frozenset(row["module"] for row in dispositions() if row["disposition"] == disposition)


def paths_with(*wanted: str) -> tuple[str, ...]:
    return tuple(row["path"] for row in dispositions() if row["disposition"] in wanted)


def is_forbidden_module(name: str) -> bool:
    """True for kp_agent_tooling_ops, kp_core, kp_ops (and submodules) and for every MOVE
    module (and, for a MOVE package, its submodules)."""
    if any(name == root or name.startswith(root + ".") for root in FOREIGN_ROOTS):
        return True
    moved = modules_with("MOVE")
    return name in moved or any(name.startswith(module + ".") for module in moved)


def is_extension_module(name: str) -> bool:
    """P3's 'extension module': the extension package itself or any MOVE module."""
    moved = modules_with("MOVE")
    return (name == EXTENSION_PACKAGE or name.startswith(EXTENSION_PACKAGE + ".")
            or name in moved or any(name.startswith(module + ".") for module in moved))


@lru_cache(maxsize=1)
def pre_move_surface() -> dict:
    surface = json.loads(PRE_MOVE_SURFACE.read_text())
    recorded = surface.get("pre_move_product_sha256")
    assert recorded == PRE_MOVE_PRODUCT_SHA256, (
        f"{PRE_MOVE_SURFACE.relative_to(REPO_ROOT)} records the product tree {recorded!r}, not the order's merge "
        f"base {PRE_MOVE_PRODUCT_SHA256}; the expected surfaces come only from the capture at that base")
    return surface


def pre_move_tools(config_name: str) -> list[dict]:
    return pre_move_surface()["configs"][config_name]["tools"]
