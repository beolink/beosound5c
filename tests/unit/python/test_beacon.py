"""Tests for services/lib/beacon.py — UUID stability and beacon payload."""
from __future__ import annotations

import sys
import uuid
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "services"))

from lib.beacon import (
    _LEGACY_NAMESPACE,
    _get_or_create_device_id,
    _build_payload,
)


# ── UUID persistence ──────────────────────────────────────────────────────────

def test_uuid_created_on_first_call(tmp_path):
    """A UUID is generated and written when no device_id file exists."""
    result = _get_or_create_device_id(str(tmp_path))
    id_file = tmp_path / "device_id"
    assert id_file.exists()
    assert result == id_file.read_text().strip()


def test_uuid_is_random_uuid4(tmp_path):
    """The id is a random UUID4 — never derived from hardware.

    A derived id would be reversible: the source that derives it is public,
    so anyone could enumerate the (small) input space and recover the MAC.
    """
    result = _get_or_create_device_id(str(tmp_path))
    assert uuid.UUID(result).version == 4


def test_uuid_is_unique_per_install(tmp_path):
    """Two installs on the same machine get different ids — nothing about the
    host feeds into the value."""
    install_a = tmp_path / "install_a"
    install_b = tmp_path / "install_b"
    install_a.mkdir()
    install_b.mkdir()

    assert _get_or_create_device_id(str(install_a)) != _get_or_create_device_id(str(install_b))


def test_uuid_stable_across_calls(tmp_path):
    """Same UUID returned on every subsequent call — simulates reboots."""
    first = _get_or_create_device_id(str(tmp_path))
    second = _get_or_create_device_id(str(tmp_path))
    third = _get_or_create_device_id(str(tmp_path))
    assert first == second == third


def test_uuid_stable_when_file_pre_exists(tmp_path):
    """Pre-existing UUID is preserved verbatim (keeps beacon history
    continuity for older deployments)."""
    known_id = "aaaaaaaa-bbbb-4ccc-dddd-eeeeeeeeeeee"
    (tmp_path / "device_id").write_text(known_id + "\n")
    result = _get_or_create_device_id(str(tmp_path))
    assert result == known_id


def test_legacy_mac_derived_id_is_replaced(tmp_path):
    """A stored id that is uuid5(namespace, our own MAC) is rotated out —
    that value leaks the MAC to anyone holding this source."""
    mac = "b8:27:eb:12:34:56"
    legacy_id = str(uuid.uuid5(_LEGACY_NAMESPACE, mac))
    (tmp_path / "device_id").write_text(legacy_id + "\n")

    with patch("lib.beacon._is_legacy_mac_derived", side_effect=lambda v: v == legacy_id):
        result = _get_or_create_device_id(str(tmp_path))

    assert result != legacy_id
    assert uuid.UUID(result).version == 4
    assert (tmp_path / "device_id").read_text().strip() == result


def test_legacy_check_matches_only_own_mac(tmp_path):
    """The rotation check reads this host's interfaces, so an unrelated
    device's id is never mistaken for a legacy one."""
    from lib.beacon import _is_legacy_mac_derived

    mac = "b8:27:eb:12:34:56"
    mock_open = MagicMock()
    mock_open.return_value.__enter__.return_value.read.return_value = mac + "\n"

    with patch("builtins.open", mock_open):
        assert _is_legacy_mac_derived(str(uuid.uuid5(_LEGACY_NAMESPACE, mac))) is True
        assert _is_legacy_mac_derived(str(uuid.uuid4())) is False


def test_corrupt_id_file_is_replaced(tmp_path):
    """A truncated/garbage device_id file yields a fresh random id."""
    (tmp_path / "device_id").write_text("not-a-uuid\n")
    result = _get_or_create_device_id(str(tmp_path))
    assert uuid.UUID(result).version == 4


def test_uuid_survives_ota_exclude_list():
    """device_id is in _UPDATE_EXCLUDES so OTA rsync never clobbers it."""
    # Read the list directly from source rather than importing the full module
    # (input.py imports `hid` which isn't available in the test environment).
    source = (REPO_ROOT / "services" / "input.py").read_text()
    # Extract the _UPDATE_EXCLUDES list as text and check for the entry
    import ast, re
    m = re.search(r"_UPDATE_EXCLUDES\s*=\s*(\[.*?\])", source, re.DOTALL)
    assert m, "_UPDATE_EXCLUDES not found in input.py"
    excludes = ast.literal_eval(m.group(1))
    assert "device_id" in excludes, "device_id must be in _UPDATE_EXCLUDES"


def test_uuid_survives_deploy_ignore():
    """.deployignore lists device_id so deploy.sh --delete never removes it."""
    deployignore = REPO_ROOT / ".deployignore"
    if not deployignore.exists():
        pytest.skip(".deployignore not present in this repo")
    lines = deployignore.read_text().splitlines()
    assert "device_id" in lines, ".deployignore must contain 'device_id'"


def test_uuid_fallback_on_unwritable_dir(tmp_path):
    """Returns 'unknown' gracefully if the file can't be written — never raises."""
    ro_dir = tmp_path / "readonly"
    ro_dir.mkdir(mode=0o555)
    result = _get_or_create_device_id(str(ro_dir))
    assert result == "unknown"


# ── Payload shape ─────────────────────────────────────────────────────────────

def test_payload_contains_required_keys(tmp_path):
    (tmp_path / "VERSION").write_text("v0.8.0\n")
    with patch("lib.beacon._get_or_create_device_id", return_value="test-uuid"), \
         patch("lib.config.load_config", return_value={
             "device": "Test", "player": {"type": "sonos"},
             "volume": {"type": "beolab5"}, "spotify": {},
         }):
        payload = _build_payload(str(tmp_path))

    assert payload["device_id"] == "test-uuid"
    assert payload["version"] == "v0.8.0"
    assert isinstance(payload["sources"], list)
    assert payload["player_type"] == "sonos"
    assert payload["volume_type"] == "beolab5"


def test_payload_sources_excludes_system_sections(tmp_path):
    (tmp_path / "VERSION").write_text("v0.8.0\n")
    config = {
        "device": "x", "menu": {}, "scenes": [], "player": {}, "volume": {},
        "home_assistant": {}, "transport": {}, "showing": {}, "join": {},
        "bluetooth": {}, "remote": {},
        "spotify": {"client_id": "abc"},
        "cd": {"device": "/dev/sr0"},
        "radio": {},
    }
    with patch("lib.beacon._get_or_create_device_id", return_value="x"), \
         patch("lib.config.load_config", return_value=config):
        payload = _build_payload(str(tmp_path))

    assert set(payload["sources"]) == {"spotify", "cd", "radio"}


def test_payload_version_fallback_when_no_file(tmp_path):
    with patch("lib.beacon._get_or_create_device_id", return_value="x"), \
         patch("lib.config.load_config", return_value={}):
        payload = _build_payload(str(tmp_path))
    assert payload["version"] == "unknown"


# ── Consent gate: no ping before the owner has seen the toggle ────────────────

def _run_send_beacon(tmp_path, config, *, opt_out=False):
    """Run send_beacon() with HTTP stubbed out. Returns True if it posted."""
    import asyncio

    from lib import beacon as beacon_mod

    if opt_out:
        (tmp_path / "NO_TELEMETRY").write_text("")
    (tmp_path / "VERSION").write_text("v0.9.5\n")

    posted: list[dict] = []

    class _Resp:
        async def json(self):
            return {"country": "Sweden"}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def post(self, url, json=None):
            posted.append(json)
            return _Resp()

    with patch("lib.beacon.aiohttp.ClientSession", return_value=_Session()), \
         patch("lib.beacon._get_or_create_device_id", return_value="test-uuid"), \
         patch("lib.config.load_config", return_value=config):
        asyncio.run(beacon_mod.send_beacon(str(tmp_path)))

    return bool(posted)


def test_no_beacon_before_setup_is_saved(tmp_path):
    """A freshly installed, never-configured device must stay silent — the
    owner has not seen the telemetry toggle yet."""
    assert not _run_send_beacon(tmp_path, {"device": "BeoSound 5c", "setup_complete": False})


def test_no_beacon_when_setup_complete_is_missing(tmp_path):
    """Absent flag (pre-web-setup configs) is treated as not-set-up."""
    assert not _run_send_beacon(tmp_path, {"device": "BeoSound 5c"})


def test_beacon_sent_once_setup_is_saved(tmp_path):
    assert _run_send_beacon(tmp_path, {"device": "BeoSound 5c", "setup_complete": True})


def test_opt_out_still_wins_after_setup(tmp_path):
    assert not _run_send_beacon(
        tmp_path, {"device": "BeoSound 5c", "setup_complete": True}, opt_out=True
    )


def test_unreadable_config_stays_silent(tmp_path):
    """If config cannot be read we cannot know consent — send nothing."""
    import asyncio

    from lib import beacon as beacon_mod

    (tmp_path / "VERSION").write_text("v0.9.5\n")
    with patch("lib.config.load_config", side_effect=OSError("no config")), \
         patch("lib.beacon.aiohttp.ClientSession", side_effect=AssertionError("must not post")):
        asyncio.run(beacon_mod.send_beacon(str(tmp_path)))


def test_device_configs_declare_setup_complete():
    """Deployed device configs must carry the flag explicitly — otherwise a
    deploy would overwrite the post-update migration and silence the beacon.
    default.json is the fresh-install template and stays false."""
    import json

    for path in sorted((REPO_ROOT / "config").glob("*.json")):
        if path.name.endswith(".default.json"):
            continue
        config = json.loads(path.read_text())
        if not isinstance(config, dict) or "device" not in config:
            continue
        assert "setup_complete" in config, f"{path.name} has no setup_complete"
        expected = path.name != "default.json"
        assert config["setup_complete"] is expected, (
            f"{path.name}: setup_complete should be {expected}"
        )


def test_post_update_migrates_legacy_configs():
    """Pre-flag devices get setup_complete=true on update instead of going
    quiet; the migration must not touch an explicit false."""
    text = (REPO_ROOT / "install" / "post-update.sh").read_text()
    assert "setup_complete" in text
    assert "'setup_complete' in config" in text, "must skip configs that already have the key"
