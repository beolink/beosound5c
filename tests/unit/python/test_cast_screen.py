"""beo-cast, the parts that do not need a Chromecast on the network.

The Cast protocol itself lives behind pychromecast and is only exercised on
real hardware. What is worth pinning here is everything around it: that the
module imports without the dependency present, that the URL handed to a
receiver is one it can actually fetch, and that "where the UI belongs" and
"whether it got there" stay separate — a receiver asleep at boot must still
be retried rather than quietly forgotten.
"""

import importlib

import pytest

cast = importlib.import_module("cast")


@pytest.fixture
def screen(monkeypatch):
    # cfg() reads /etc/beosound5c/config.json on a device; with no config the
    # defaults apply, which is what most of these assert.
    monkeypatch.setattr(cast, "cfg", lambda *a, **k: k.get("default"))
    return cast.CastScreen()


def test_imports_without_pychromecast_installed():
    """The import must not drag in the Cast library.

    beo-cast runs on devices where nobody has asked for mirroring, and an
    optional dependency that breaks start-up is worse than a missing feature.
    pychromecast is imported inside the methods that use it.
    """
    assert "pychromecast" not in dir(cast)


def test_default_app_id_is_the_url_loading_receiver(screen):
    assert screen._app_id == cast.DEFAULT_APP_ID


def test_url_override_is_used_verbatim(monkeypatch):
    monkeypatch.setattr(cast, "cfg",
                        lambda section, key, default=None:
                        "http://example.invalid/wall" if key == "url" else default)
    assert cast.CastScreen()._ui_url("10.0.0.5") == "http://example.invalid/wall"


def test_url_points_at_this_device_not_localhost(screen, monkeypatch):
    """A receiver fetching "localhost" would fetch from itself."""
    monkeypatch.setattr(cast, "_local_address_towards", lambda host: "10.0.0.9")
    url = screen._ui_url("10.0.0.5")
    assert url.startswith("http://10.0.0.9/")
    assert "localhost" not in url


def test_url_carries_a_cache_buster(screen, monkeypatch):
    """DashCast ignores a load for the URL it is already showing, so two
    casts in a row must not produce the same string."""
    monkeypatch.setattr(cast, "_local_address_towards", lambda host: "10.0.0.9")
    monkeypatch.setattr(cast.time, "time", lambda: 1000)
    first = screen._ui_url("10.0.0.5")
    monkeypatch.setattr(cast.time, "time", lambda: 1001)
    assert screen._ui_url("10.0.0.5") != first


def test_host_lookup_uses_the_discovered_list(screen):
    screen._receivers = [{"name": "Hall", "model": "x", "host": "10.0.0.7"}]
    assert screen._host_of("Hall") == "10.0.0.7"
    assert screen._host_of("Nowhere") is None


@pytest.mark.asyncio
async def test_failed_cast_still_records_where_the_ui_belongs(screen, monkeypatch):
    """Asleep at boot is not the same as not wanted.

    start() records the target before attempting, so the reconcile loop keeps
    trying once the receiver comes back. Reporting it as casting would be a
    lie, hence the separate flag.
    """
    async def refresh():
        screen._receivers = [{"name": "Hall", "model": "x", "host": "10.0.0.7"}]
    monkeypatch.setattr(screen, "refresh", refresh)
    monkeypatch.setattr(cast, "_local_address_towards", lambda host: "10.0.0.9")

    def boom(*a):
        raise LookupError("asleep")
    monkeypatch.setattr(screen, "_cast_sync", boom)

    with pytest.raises(LookupError):
        await screen.start("Hall")

    assert screen._target == "Hall"     # still where it belongs
    assert screen._active is False      # but not showing


@pytest.mark.asyncio
async def test_stop_clears_both(screen, monkeypatch):
    screen._target = "Hall"
    screen._active = True
    monkeypatch.setattr(screen, "_stop_sync", lambda receiver: None)
    await screen.stop()
    assert screen._target is None
    assert screen._active is False


@pytest.mark.asyncio
async def test_stop_is_a_no_op_when_nothing_is_cast(screen, monkeypatch):
    called = []
    monkeypatch.setattr(screen, "_stop_sync", lambda receiver: called.append(receiver))
    await screen.stop()
    assert called == []
