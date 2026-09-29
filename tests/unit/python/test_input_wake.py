"""Any physical input on a dark screen wakes the BS5c like a short power press.

The nav wheel, the volume wheel, LEFT/RIGHT/GO and a real laser movement all
turn the backlight back on; the waking input itself is swallowed so a wheel
turn on a black screen doesn't scroll a menu or change a volume you can't
see. Laser jitter (±1 position at rest) must never wake the screen.

See input_wakes() and wake_from_input() in services/input.py.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "services"))

# `hid` is a native USB-HID binding that only exists on the device.
sys.modules.setdefault("hid", type(sys)("hid"))

import input as beo_input  # noqa: E402  (services/input.py)


NAV = {"direction": "clock", "speed": 3}
VOL = {"direction": "counter", "speed": 1}


@pytest.fixture(autouse=True)
def _reset_laser_ref():
    beo_input._laser_ref_off = None
    yield
    beo_input._laser_ref_off = None


def _screen(on: bool):
    return patch.object(beo_input, "is_backlight_on", return_value=on)


# ── screen on: never a wake, and the laser reference is dropped ──────────

@pytest.mark.parametrize("nav,vol,btn", [
    (NAV, None, None), (None, VOL, None), (None, None, {"button": "go"}),
    (None, None, None),
])
def test_screen_on_never_wakes(nav, vol, btn):
    beo_input._laser_ref_off = 40
    with _screen(True):
        assert beo_input.input_wakes(nav, vol, btn, 60) is None
    assert beo_input._laser_ref_off is None


# ── screen off: wheels and buttons wake immediately ──────────────────────

def test_nav_wheel_wakes():
    with _screen(False):
        assert beo_input.input_wakes(NAV, None, None, 50) == "nav wheel"


def test_volume_wheel_wakes():
    with _screen(False):
        assert beo_input.input_wakes(None, VOL, None, 50) == "volume wheel"


@pytest.mark.parametrize("button", ["left", "right", "go"])
def test_buttons_wake(button):
    with _screen(False):
        assert beo_input.input_wakes(None, None, {"button": button}, 50) == f"{button} button"


def test_power_button_is_not_a_wake_reason():
    # parse_report already toggled the screen for it; treating it as a wake
    # too would re-light a screen the user just turned off.
    with _screen(False):
        assert beo_input.input_wakes(None, None, {"button": "power"}, 50) is None


# ── laser: needs a real movement, not sensor jitter ──────────────────────

def test_laser_first_sample_sets_reference_without_waking():
    with _screen(False):
        assert beo_input.input_wakes(None, None, None, 50) is None
    assert beo_input._laser_ref_off == 50


def test_laser_jitter_does_not_wake():
    with _screen(False):
        beo_input.input_wakes(None, None, None, 50)
        for pos in (51, 49, 52, 48, 51):
            assert beo_input.input_wakes(None, None, None, pos) is None


def test_laser_real_movement_wakes():
    with _screen(False):
        beo_input.input_wakes(None, None, None, 50)
        assert beo_input.input_wakes(None, None, None, 50 + beo_input.LASER_WAKE_DELTA) == "laser"


def test_laser_reference_is_where_the_screen_went_dark():
    # Drifting one step at a time still adds up to a wake once the total
    # movement crosses the threshold — the reference doesn't creep along.
    with _screen(False):
        beo_input.input_wakes(None, None, None, 50)
        assert beo_input.input_wakes(None, None, None, 51) is None
        assert beo_input.input_wakes(None, None, None, 52) is None
        assert beo_input.input_wakes(None, None, None, 53) == "laser"


# ── the wake itself behaves like a short power press ─────────────────────

def test_wake_from_input_lights_screen_clicks_and_touches_router():
    calls = []
    with patch.object(beo_input, "set_backlight", side_effect=lambda on: calls.append(("backlight", on))), \
         patch.object(beo_input, "do_click", side_effect=lambda: calls.append(("click",))), \
         patch.object(beo_input, "_output_power") as output_power, \
         patch.object(beo_input.asyncio, "run_coroutine_threadsafe",
                      side_effect=lambda coro, loop: (calls.append(("router", loop)), coro.close())):
        beo_input.wake_from_input("nav wheel", "LOOP")

    assert calls[:2] == [("backlight", True), ("click",)]
    assert ("router", "LOOP") in calls
    output_power.assert_called_once_with(beo_input.ROUTER_TOUCH)
