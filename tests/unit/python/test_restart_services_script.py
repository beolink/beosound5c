"""services/system/restart-services.sh — the post-update service restart.

Pinned here: every active beo-* unit is restarted exactly once, beo-ui after
the backends, and beo-input last and alone (a caller inside beo-input's
cgroup dies when beo-input stops, so anything after it would be lost).
`systemctl` and `sleep` are stubs on PATH that record their arguments.
"""
from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "services" / "system" / "restart-services.sh"

KITCHEN = [
    "beo-bluetooth", "beo-http", "beo-input", "beo-masterlink",
    "beo-player-sonos", "beo-router", "beo-source-radio",
    "beo-source-spotify", "beo-ui",
]


def _run(tmp_path: Path, active: list[str], *args: str) -> list[str]:
    run_dir = Path(tempfile.mkdtemp(dir=tmp_path))
    stub_dir = run_dir / "bin"
    stub_dir.mkdir()
    calls = run_dir / "calls.log"
    listing = "".join(f"{u}.service loaded active running {u}\n" for u in active)
    (stub_dir / "systemctl").write_text(
        "#!/bin/bash\n"
        f'echo "systemctl $*" >> "{calls}"\n'
        f'[ "$1" = list-units ] && printf %s "{listing}"\n'
        "exit 0\n"
    )
    (stub_dir / "sleep").write_text(f'#!/bin/bash\necho "sleep $*" >> "{calls}"\n')
    for stub in stub_dir.iterdir():
        stub.chmod(0o755)
    env = dict(os.environ, PATH=f"{stub_dir}:{os.environ['PATH']}")
    proc = subprocess.run(["bash", str(SCRIPT), *args], env=env,
                          capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    return calls.read_text().splitlines() if calls.exists() else []


def _restarts(calls):
    return [c for c in calls if c.startswith("systemctl restart ")]


def test_input_last_and_alone_ui_after_backends(tmp_path):
    restarts = _restarts(_run(tmp_path, KITCHEN))
    assert restarts[-1] == "systemctl restart beo-input.service"
    assert restarts[-2] == "systemctl restart beo-ui.service"
    backends = restarts[0].split()[2:]
    assert "beo-input.service" not in backends
    assert "beo-ui.service" not in backends
    assert {"beo-router.service", "beo-player-sonos.service"} <= set(backends)


def test_every_active_unit_restarted_exactly_once(tmp_path):
    units = [u for r in _restarts(_run(tmp_path, KITCHEN)) for u in r.split()[2:]]
    assert sorted(units) == sorted(f"{u}.service" for u in KITCHEN)


def test_pause_before_ui_and_optional_start_delay(tmp_path):
    calls = _run(tmp_path, KITCHEN, "10")
    assert calls[0] == "sleep 10"
    ui = calls.index("systemctl restart beo-ui.service")
    assert calls[ui - 1] == "sleep 3"
    assert "sleep 10" not in _run(tmp_path, KITCHEN)


def test_missing_ui_and_input_are_skipped(tmp_path):
    calls = _run(tmp_path, ["beo-http", "beo-router"])
    assert _restarts(calls) == ["systemctl restart beo-http.service beo-router.service"]
    assert "sleep 3" not in calls


def test_nothing_active_restarts_nothing(tmp_path):
    assert _restarts(_run(tmp_path, [])) == []


def test_a_restart_helper_unit_is_never_restarted(tmp_path):
    """Restarting the unit the script itself runs from would loop forever
    (seen on office, Sep 2026, when the unit was still named beo-update-restart-*)."""
    calls = _run(tmp_path, ["beo-http", "beo-update-restart-2849324", "beo-router"])
    assert _restarts(calls) == ["systemctl restart beo-http.service beo-router.service"]
