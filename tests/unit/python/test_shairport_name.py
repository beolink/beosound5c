"""Tests for the AirPlay name shairport-sync advertises.

Same shape as test_librespot_name.py: the installer writes
shairport-sync.conf once, reconcile-services.sh fixes the name up from
config.json on every install, deploy and config save, and the name is the
one Spotify Connect uses so the device is listed the same way in both pickers.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "services"))

from lib import shairport_config as sc  # noqa: E402

SHAIRPORT_CONF = """// BeoSound 5c — shairport-sync (AirPlay 2 receiver)
general = {
\tname = "BeoSound 5c";
\toutput_backend = "pulseaudio";
\tignore_volume_control = "yes";
};
"""


@pytest.fixture
def conf(tmp_path):
    path = tmp_path / "shairport-sync.conf"
    path.write_text(SHAIRPORT_CONF)
    return path


@pytest.fixture
def config(tmp_path):
    def _write(device):
        path = tmp_path / "config.json"
        path.write_text(json.dumps({"device": device}))
        return path
    return _write


def test_name_matches_spotify_connect(config):
    assert sc.name_from_config(config("Office")) == "BeoSound 5c Office"
    assert sc.name_from_config(config("BeoSound 5c Kitchen")) == "BeoSound 5c Kitchen"
    assert sc.name_from_config(config("")) == "BeoSound 5c"


def test_sync_rewrites_only_the_name_line(conf, config):
    assert sc.sync_from_config(config("Office"), conf) == "BeoSound 5c Office"
    text = conf.read_text()
    assert '\tname = "BeoSound 5c Office";\n' in text
    # Everything else untouched, including the tab indentation.
    assert '\toutput_backend = "pulseaudio";' in text
    assert text.startswith("// BeoSound 5c")


def test_sync_is_idempotent(conf, config):
    cfg = config("Office")
    assert sc.sync_from_config(cfg, conf) is not None
    before = conf.stat().st_mtime_ns
    assert sc.sync_from_config(cfg, conf) is None
    assert conf.stat().st_mtime_ns == before


def test_missing_conf_means_no_airplay_here(tmp_path, config):
    assert sc.sync_from_config(config("Office"), tmp_path / "absent.conf") is None


def test_quotes_in_device_name_are_escaped(conf, config):
    sc.sync_from_config(config('Rolf "Lounge"'), conf)
    assert 'name = "BeoSound 5c Rolf \\"Lounge\\"";' in conf.read_text()


def test_conf_without_name_line_is_left_alone(tmp_path, config, capsys):
    path = tmp_path / "shairport-sync.conf"
    path.write_text("general = {\n\toutput_backend = \"pulseaudio\";\n};\n")
    assert sc.sync_from_config(config("Office"), path) is None
    assert "no name line" in capsys.readouterr().err
    assert "name =" not in path.read_text()


def test_cli_name_mode_prints_only_the_name(config, capsys):
    assert sc.main(["x", "--name", str(config("Office"))]) == 0
    assert capsys.readouterr().out == "BeoSound 5c Office\n"
