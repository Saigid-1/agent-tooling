"""Session fixtures for the S2 image suite. A fixture never fails or skips on a
missing variable; it hands the reason to the test, which then FAILS."""
from __future__ import annotations

import os

import pytest

import json

from image_harness import (
    DESK_REGISTRY, OPERATOR_OVERRIDES, PASSTHROUGH_ARGS, PASSTHROUGH_PORT, PRODUCT, Board, image_argv, start_board,
)


def _board(tmp_path_factory, name, **options):
    image = os.environ.get(PRODUCT, "").strip()
    if not image:
        return Board(error=f"{PRODUCT} is unset. The image suite requires it and fails rather than skips.")
    return start_board(image, tmp_path_factory.mktemp(name), **options)


@pytest.fixture(scope="session")
def product_board(tmp_path_factory):
    """`kanban` with no arguments, under the P2 run (read-only root, no capabilities, non-root, /state bind)."""
    board = _board(tmp_path_factory, "board-state")
    try:
        yield board
    finally:
        if board.container:
            board.container.remove()


@pytest.fixture(scope="session")
def writable_board(tmp_path_factory):
    """The same run with a writable root filesystem, so `docker diff` can show writes outside /state."""
    board = _board(tmp_path_factory, "board-writable-state", read_only=False, kind="board-writable")
    try:
        yield board
    finally:
        if board.container:
            board.container.remove()


@pytest.fixture(scope="session")
def passthrough_board(tmp_path_factory):
    """`kanban <args>` with operator overrides of the three integration defaults."""
    board = _board(tmp_path_factory, "board-passthrough-state", command=["kanban", *PASSTHROUGH_ARGS],
                   port=PASSTHROUGH_PORT, env=OPERATOR_OVERRIDES, kind="board-passthrough")
    try:
        yield board
    finally:
        if board.container:
            board.container.remove()


@pytest.fixture(scope="session")
def configured_board(tmp_path_factory):
    """`kanban` with the desk registry configured as the order states: the image's
    default argv plus an operator-supplied `--config <path>` naming an existing file."""
    image = os.environ.get(PRODUCT, "").strip()
    if not image:
        board = Board(error=f"{PRODUCT} is unset. The image suite requires it and fails rather than skips.")
    else:
        try:
            default, detail = image_argv(image, DESK_REGISTRY)
        except pytest.fail.Exception as exc:
            default, detail = None, str(exc)
        if default is None:
            board = Board(error=f"no image default to configure: {detail}")
        else:
            config = tmp_path_factory.mktemp("board-config")
            (config / "desks.json").write_text("{}\n")  # existence is the stated condition; the board is not asked to run it
            (config / "desks.json").chmod(0o644)
            config.chmod(0o755)
            configured = [*default, "--config", "/config/desks.json"]
            board = start_board(image, tmp_path_factory.mktemp("board-configured-state"),
                                env={DESK_REGISTRY: json.dumps(configured)},
                                extra_args=["-v", f"{config.resolve()}:/config:ro"], kind="board-configured")
            board.configured = configured
    try:
        yield board
    finally:
        if board.container:
            board.container.remove()
