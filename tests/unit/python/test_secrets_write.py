"""Tests for writing /etc/beosound5c/secrets.env from the web setup UI.

This used to run `sudo cat` / `sudo tee` against a root-owned file, which only
worked because Raspberry Pi OS Imager grants the first user blanket NOPASSWD
sudo. Without that file — a hand-created user, a hardened Pi — an HA token
entered in the setup UI was dropped with nothing but a log line, and the UI
still said "saved". The file is now owned by the service user and written
directly, and a failure is reported instead of swallowed.
"""
from __future__ import annotations

import asyncio
import os
import stat
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "services"))
sys.modules.setdefault("hid", type(sys)("hid"))

import input as beo_input  # noqa: E402

EXISTING = """# BeoSound 5c Secrets

HA_TOKEN="old-token"
MQTT_USER="someone"
MQTT_PASSWORD=""
TAILSCALE_AUTH_KEY="tskey-keep-me"
"""


@pytest.fixture
def secrets_file(tmp_path, monkeypatch):
    path = tmp_path / "secrets.env"
    path.write_text(EXISTING)
    path.chmod(0o600)
    monkeypatch.setattr(beo_input, "SECRETS_PATH", str(path))
    return path


def _write(updates):
    return asyncio.run(beo_input._write_secrets(updates))


# ── Rendering ─────────────────────────────────────────────────────────────────

def test_updates_replace_and_preserve(secrets_file):
    assert _write({"HA_TOKEN": "new-token", "MQTT_PASSWORD": "hunter2"}) is True

    text = secrets_file.read_text()
    assert 'HA_TOKEN="new-token"' in text
    assert 'MQTT_PASSWORD="hunter2"' in text
    assert 'MQTT_USER="someone"' in text, "untouched keys must survive"
    assert 'TAILSCALE_AUTH_KEY="tskey-keep-me"' in text
    assert "# BeoSound 5c Secrets" in text, "comments must survive"
    assert "old-token" not in text


def test_new_key_is_appended(secrets_file):
    assert _write({"BRAND_NEW": "value"}) is True
    assert 'BRAND_NEW="value"' in secrets_file.read_text()


@pytest.mark.parametrize("secret", [
    'quote"inside',
    r"back\slash",
    r'both"and\slash',
    "spaces and #hash",
])
def test_special_characters_survive_a_round_trip(secrets_file, secret):
    """The file is sourced as shell by systemd, so quoting must hold."""
    _write({"HA_TOKEN": secret})
    line = [ln for ln in secrets_file.read_text().splitlines()
            if ln.startswith("HA_TOKEN=")][0]

    # Parse it back the way a shell would.
    import shlex
    parsed = shlex.split(line.split("=", 1)[1])[0]
    assert parsed == secret


def test_missing_file_is_created(tmp_path, monkeypatch):
    path = tmp_path / "secrets.env"
    monkeypatch.setattr(beo_input, "SECRETS_PATH", str(path))
    assert _write({"HA_TOKEN": "t"}) is True
    assert path.exists()
    assert 'HA_TOKEN="t"' in path.read_text()


# ── Permissions and atomicity ─────────────────────────────────────────────────

def test_file_stays_owner_only(secrets_file):
    _write({"HA_TOKEN": "t"})
    mode = stat.S_IMODE(secrets_file.stat().st_mode)
    assert mode == 0o600, f"credentials file must stay 0600, got {oct(mode)}"


def test_no_temp_file_left_behind(secrets_file, tmp_path):
    _write({"HA_TOKEN": "t"})
    leftovers = [p.name for p in tmp_path.iterdir() if p.name.startswith(".secrets.env.")]
    assert leftovers == []


def test_original_survives_a_failed_write(secrets_file, tmp_path):
    """A crash mid-write must not truncate the credentials that were there."""
    with patch("os.replace", side_effect=OSError("disk full")):
        assert _write({"HA_TOKEN": "new"}) is False

    assert secrets_file.read_text() == EXISTING
    leftovers = [p.name for p in tmp_path.iterdir() if p.name.startswith(".secrets.env.")]
    assert leftovers == [], "failed write must clean up its temp file"


def test_failure_is_reported_not_swallowed(secrets_file):
    """The regression that started this: an unwritable file returned success."""
    with patch("os.replace", side_effect=PermissionError("read-only")):
        assert _write({"HA_TOKEN": "new"}) is False


def test_no_sudo_in_the_secrets_path():
    """Writing credentials must not depend on a distro's sudo defaults.

    Checks the code, not the prose — the docstring explains the old sudo path
    on purpose.
    """
    import ast
    import inspect

    src = inspect.getsource(beo_input._write_secrets_sync)
    tree = ast.parse(src)
    ast.get_docstring(tree.body[0]) and tree.body[0].body.pop(0)  # drop docstring
    code = ast.unparse(tree)
    assert "sudo" not in code


def test_installers_hand_the_file_to_the_service_user():
    for rel in ("install/lib/config-utils.sh",
                "services/system/install-services.sh",
                "install/post-update.sh"):
        text = (REPO_ROOT / rel).read_text()
        assert "SECRETS" in text.upper()
        assert "chown" in text, f"{rel} must chown secrets.env to the service user"


def test_config_save_reports_secret_failures():
    """handle_config_save must return a warning status the UI can show."""
    src = (REPO_ROOT / "services" / "input.py").read_text()
    assert "secrets_warning" in src
    assert "'status': 'warning'" in src
    ui = (REPO_ROOT / "web" / "softarc" / "config.html").read_text()
    assert "'warning'" in ui, "setup UI must surface the warning"
