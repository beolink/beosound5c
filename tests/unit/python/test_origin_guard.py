"""Tests for the cross-site guard on beo-input's mutating endpoints.

POST /config and POST /update/run have no authentication — a trusted LAN is
the security model. That must not extend to *any site the owner visits*: a
browser will happily let evil.example.com POST to a private address, which
without this guard is enough to rewrite a device's config or start an update.

See request_origin_ok() in services/input.py and the Origin forwarding in
services/http_server.py.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "services"))

# `hid` is a native USB-HID binding that only exists on the device; stub it so
# input.py imports on a dev machine and in CI.
sys.modules.setdefault("hid", type(sys)("hid"))

import input as beo_input  # noqa: E402  (services/input.py)


class FakeRequest:
    """Minimal stand-in for aiohttp's Request — headers are all we read."""

    def __init__(self, origin=None, host="192.168.1.42", forwarded_host=None, method="POST"):
        self.headers = {}
        if origin is not None:
            self.headers["Origin"] = origin
        if host is not None:
            self.headers["Host"] = host
        if forwarded_host is not None:
            self.headers["X-Forwarded-Host"] = forwarded_host
        self.method = method


DEVICE_NAMES = {"localhost", "127.0.0.1", "::1", "[::1]",
                "beosound5c-kitchen", "beosound5c-kitchen.local", "192.168.1.42"}


@pytest.fixture(autouse=True)
def _stub_local_names():
    with patch.object(beo_input, "_local_host_names", return_value=DEVICE_NAMES):
        yield


# ── Allowed callers ───────────────────────────────────────────────────────────

def test_no_origin_is_allowed():
    """Home Assistant, curl and the port-80 proxy send no Origin."""
    assert beo_input.request_origin_ok(FakeRequest(origin=None))


def test_setup_page_on_the_device_is_allowed():
    """The page was loaded from the same host the request is going to."""
    assert beo_input.request_origin_ok(
        FakeRequest(origin="http://192.168.1.42", forwarded_host="192.168.1.42")
    )


def test_setup_page_reached_by_hostname_is_allowed():
    assert beo_input.request_origin_ok(
        FakeRequest(origin="http://beosound5c-kitchen.local",
                    forwarded_host="beosound5c-kitchen.local")
    )


def test_kiosk_on_localhost_is_allowed():
    assert beo_input.request_origin_ok(
        FakeRequest(origin="http://localhost:8000", forwarded_host="localhost:8000")
    )


def test_origin_with_port_matches_host_without_port():
    """Origin carries no path but may carry a port; Host may differ in port."""
    assert beo_input.request_origin_ok(
        FakeRequest(origin="http://192.168.1.42:80", forwarded_host="192.168.1.42")
    )


def test_other_name_this_device_answers_to_is_allowed():
    """Reached by IP, page loaded via mDNS name — still this device."""
    assert beo_input.request_origin_ok(
        FakeRequest(origin="http://beosound5c-kitchen", forwarded_host="192.168.1.42")
    )


# ── Rejected callers ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("origin", [
    "https://evil.example.com",
    "http://evil.example.com:8080",
    "http://192.168.1.99",              # another host on the same LAN
    "https://beosound5c.com",           # even the project's own site
    "null",                             # sandboxed iframe / file://
    "http://beosound5c-kitchen.evil.com",   # suffix trickery
])
def test_foreign_origins_are_rejected(origin):
    assert not beo_input.request_origin_ok(
        FakeRequest(origin=origin, forwarded_host="192.168.1.42")
    )


def test_malformed_origin_is_rejected():
    assert not beo_input.request_origin_ok(
        FakeRequest(origin="not a url", forwarded_host="192.168.1.42")
    )


# ── CORS headers ──────────────────────────────────────────────────────────────

def test_allowed_origin_is_echoed_never_wildcarded():
    headers = beo_input._cors_headers(
        FakeRequest(origin="http://192.168.1.42", forwarded_host="192.168.1.42")
    )
    assert headers["Access-Control-Allow-Origin"] == "http://192.168.1.42"
    assert headers["Vary"] == "Origin"


def test_rejected_origin_gets_no_cors_header():
    headers = beo_input._cors_headers(
        FakeRequest(origin="https://evil.example.com", forwarded_host="192.168.1.42")
    )
    assert "Access-Control-Allow-Origin" not in headers


def test_mutating_handlers_have_no_wildcard_cors():
    """A wildcard on these endpoints would let a foreign page read the
    response even when the request itself is refused."""
    src = (REPO_ROOT / "services" / "input.py").read_text()
    for name in ("handle_config_save", "handle_update_run"):
        start = src.index(f"async def {name}(request):")
        end = src.index("\nasync def ", start + 10)
        body = src[start:end]
        assert "request_origin_ok" in body, f"{name} does not check the Origin"
        assert "'*'" not in body, f"{name} still sends a wildcard CORS header"


def test_proxy_forwards_origin_and_host():
    """The port-80 proxy must not launder a foreign Origin into a local one."""
    src = (REPO_ROOT / "services" / "http_server.py").read_text()
    assert "'Origin'" in src and "X-Forwarded-Host" in src
    # Preflights for proxied paths must go upstream, not be answered locally.
    assert "_proxy_to_input(self, 'OPTIONS')" in src
