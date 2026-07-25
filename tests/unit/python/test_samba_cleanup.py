"""Tests for install/modules/samba-cleanup.sh.

The script rewrites /etc/samba/smb.conf and can purge packages, so the two
branches that matter are pinned here: it must remove only its own stanza, and
it must not touch the daemons when the owner has shares of their own.

`systemctl` and `apt-get` are shadowed by no-op stubs on PATH, which also lets
the test assert which commands the script would have run.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "install" / "modules" / "samba-cleanup.sh"

SHARE = """
[USB-Music]
    comment = Auto-mounted USB music drives
    path = /mnt/usb-music
    read only = yes
    guest ok = yes
    browseable = yes
    follow symlinks = yes
    wide links = yes
"""

DEFAULTS = """[global]
   workgroup = WORKGROUP

[printers]
   path = /var/spool/samba
"""


def _run(tmp_path: Path, smb_conf_text: str) -> tuple[subprocess.CompletedProcess, str, list[str]]:
    """Run the cleanup script against a temp smb.conf. Returns the process,
    the resulting file contents, and the commands the stubs recorded."""
    conf = tmp_path / "smb.conf"
    conf.write_text(smb_conf_text)

    stub_dir = tmp_path / "bin"
    stub_dir.mkdir()
    calls = tmp_path / "calls.log"
    for name in ("systemctl", "apt-get"):
        stub = stub_dir / name
        stub.write_text(f'#!/bin/bash\necho "{name} $*" >> "{calls}"\nexit 0\n')
        stub.chmod(0o755)

    env = dict(os.environ)
    env["PATH"] = f"{stub_dir}:{env['PATH']}"
    env["SMB_CONF"] = str(conf)

    proc = subprocess.run(
        ["bash", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=30
    )
    recorded = calls.read_text().splitlines() if calls.exists() else []
    return proc, conf.read_text(), recorded


def test_removes_only_the_usb_music_stanza(tmp_path):
    proc, result, calls = _run(tmp_path, DEFAULTS + SHARE)

    assert proc.returncode == 0, proc.stderr
    assert "[USB-Music]" not in result
    assert "guest ok" not in result
    # Everything else survives verbatim.
    assert "[global]" in result and "workgroup = WORKGROUP" in result
    assert "[printers]" in result and "path = /var/spool/samba" in result
    # Nothing else uses samba -> daemons stopped and packages purged.
    assert any("disable --now smbd" in c for c in calls)
    assert any("purge -y samba" in c for c in calls)


def test_keeps_samba_when_owner_has_other_shares(tmp_path):
    own_share = "\n[Media]\n    path = /srv/media\n"
    proc, result, calls = _run(tmp_path, DEFAULTS + SHARE + own_share)

    assert proc.returncode == 0, proc.stderr
    assert "[USB-Music]" not in result
    assert "[Media]" in result and "path = /srv/media" in result
    assert not any("purge" in c for c in calls), "must not purge a samba in use"
    assert not any("disable" in c for c in calls), "must not disable a samba in use"


def test_is_a_noop_without_the_share(tmp_path):
    proc, result, calls = _run(tmp_path, DEFAULTS)

    assert proc.returncode == 0, proc.stderr
    assert result == DEFAULTS, "untouched when our stanza was never there"
    assert calls == []


def test_backs_up_before_rewriting(tmp_path):
    _run(tmp_path, DEFAULTS + SHARE)
    backup = tmp_path / "smb.conf.bs5c-backup"
    assert backup.exists()
    assert "[USB-Music]" in backup.read_text()


def test_installer_no_longer_installs_samba():
    """The apt package list and the share stanza are both gone for good."""
    packages = (REPO_ROOT / "install" / "modules" / "system-packages.sh").read_text()
    assert "samba" not in packages.replace("samba-cleanup.sh", ""), (
        "system-packages.sh must not install samba or re-add a share"
    )
    assert "[USB-Music]" not in packages


@pytest.mark.parametrize(
    "caller",
    ["install/install.sh", "install/post-update.sh", "install/modules/system-packages.sh"],
)
def test_cleanup_runs_on_every_update_path(caller):
    """New installs, git-pull updates and OTA updates all invoke the cleanup."""
    assert "samba-cleanup.sh" in (REPO_ROOT / caller).read_text()
