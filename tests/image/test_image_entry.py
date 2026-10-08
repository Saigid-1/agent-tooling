"""S2 container entry (`tooling-container`) in `product`: existing behaviors unchanged.

Each run uses the P2 posture and a fresh state root. The entry must be the
image's own ENTRYPOINT, or passing these on a plain image would prove nothing.
"""
from __future__ import annotations

import json
import time

import pytest

from image_harness import (
    docker, entrypoint_names, hardened_args, image_for, navigation_config, prepare_state, run_once, start,
)

pytestmark = pytest.mark.image


def _product_entry() -> str:
    image = image_for("product")
    names = entrypoint_names(image)
    assert "tooling-container" in names, f"product ENTRYPOINT is {names}; the contract's container entry is tooling-container"
    return image


def _tooling_run(tmp_path, *, navigation: bool) -> list[str]:
    state = prepare_state(tmp_path / "state", navigation=navigation)
    args = hardened_args(state)
    if navigation:
        args += ["-v", f"{navigation_config(tmp_path / 'config').resolve()}:/config:ro"]
    return args


def test_entry_health_is_ready_with_navigation_configuration(tmp_path):
    image = _product_entry()
    result = run_once(image, ["health"], run_args=_tooling_run(tmp_path, navigation=True), timeout=120)
    assert result.returncode == 0, f"health exited {result.returncode}: {(result.stdout + result.stderr)[-800:]}"
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    assert lines and json.loads(lines[-1]).get("status") == "ready", f"health printed {result.stdout!r}"


def test_entry_health_fails_without_navigation_configuration_or_board(tmp_path):
    image = _product_entry()
    result = run_once(image, ["health"], run_args=_tooling_run(tmp_path, navigation=False), timeout=120)
    assert result.returncode != 0, f"health succeeded with no navigation configuration and no board: {result.stdout!r}"


def test_entry_execs_any_other_command(tmp_path):
    image = _product_entry()
    result = run_once(image, ["sh", "-c", "echo exec-ok; exit 7"], run_args=_tooling_run(tmp_path, navigation=True),
                      timeout=120)
    assert result.returncode == 7 and "exec-ok" in result.stdout, \
        f"exec of another command: exit {result.returncode}, output {(result.stdout + result.stderr)[-500:]!r}"


def test_entry_wait_runs_until_sigterm_then_exits_zero(tmp_path):
    image = _product_entry()
    container, error = start(image, ["wait"], run_args=_tooling_run(tmp_path, navigation=True) + ["--network", "none"],
                             kind="wait")
    assert container is not None, error
    try:
        time.sleep(3)
        assert container.running(), f"`wait` did not keep running: {container.logs()[-800:]}"
        stopped = docker("stop", "-t", "15", container.name, timeout=60)
        assert stopped.returncode == 0, stopped.stderr
        code = container.state().get("State", {}).get("ExitCode")
        assert code == 0, f"`wait` exited {code} on SIGTERM, expected 0"
    finally:
        container.remove()
