"""The post-update service restart must outlive the process that starts it.

input.py spawns the restart helper from inside beo-input, so the helper is
killed the moment systemd stops beo-input — everything not yet issued is
lost. `systemctl restart a b c` issues one job per unit in order, which is
why the alphabetical list (beo-bluetooth, beo-http, beo-input, …) restarted
exactly three units and left the router, player and sources on the old code
until the next reboot (kitchen, v0.10.0 update, Sep 2026).

See _restart_plan() in services/input.py.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "services"))

sys.modules.setdefault("hid", type(sys)("hid"))

import input as beo_input  # noqa: E402  (services/input.py)


KITCHEN = [
    "beo-bluetooth", "beo-http", "beo-input", "beo-masterlink",
    "beo-player-sonos", "beo-router", "beo-source-radio",
    "beo-source-spotify", "beo-ui",
]


def _restart_steps(plan):
    return [p for p in plan if p.startswith("sudo systemctl restart ")]


def test_input_is_restarted_last_and_alone():
    plan = beo_input._restart_plan(KITCHEN)
    steps = _restart_steps(plan)
    assert steps[-1] == "sudo systemctl restart beo-input"
    for step in steps[:-1]:
        assert "beo-input" not in step.split()


def test_every_active_service_is_restarted_exactly_once():
    plan = beo_input._restart_plan(KITCHEN)
    restarted = [
        unit for step in _restart_steps(plan) for unit in step.split()[3:]
    ]
    assert sorted(restarted) == sorted(KITCHEN)


def test_ui_is_restarted_after_the_backends_and_before_input():
    plan = beo_input._restart_plan(KITCHEN)
    steps = _restart_steps(plan)
    backends = steps[0]
    assert "beo-router" in backends and "beo-player-sonos" in backends
    assert "beo-ui" not in backends.split()
    assert steps[1] == "sudo systemctl restart beo-ui"
    # A pause between the backends and the UI so Chromium loads against
    # services that are already up.
    assert plan.index("sleep 3") < plan.index(steps[1])


def test_missing_ui_or_input_are_simply_skipped():
    plan = beo_input._restart_plan(["beo-http", "beo-router"])
    assert _restart_steps(plan) == ["sudo systemctl restart beo-http beo-router"]
    assert "sleep 3" not in plan


def test_nothing_active_still_yields_no_restart_commands():
    assert _restart_steps(beo_input._restart_plan([])) == []


def test_steps_are_independent_not_chained_with_and():
    """One failed restart must not skip beo-ui and beo-input."""
    import subprocess
    from unittest.mock import patch

    with patch.object(subprocess, "Popen") as popen:
        beo_input._spawn_restart(beo_input._restart_plan(KITCHEN))
    cmd = popen.call_args.args[0]
    assert cmd[:2] == ["bash", "-c"]
    assert " && " not in cmd[2]
    assert cmd[2].endswith("sudo systemctl restart beo-input")
    assert popen.call_args.kwargs["start_new_session"] is True


def test_a_restart_helper_unit_is_never_in_the_plan():
    plan = beo_input._restart_plan(KITCHEN + ["beo-update-restart-42"])
    assert not any("update-restart" in step for step in plan)
