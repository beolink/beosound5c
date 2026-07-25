#!/usr/bin/env python3
"""BeoSound 5c HTTP server with no-cache headers.

Drop-in replacement for `python3 -m http.server 8000`.
Adds Cache-Control: no-store to every response so Chromium's
in-memory HTTP cache never serves stale files (playlist JSON,
JS, CSS, etc.).  This is appropriate for a local kiosk app.
"""

import http.server
import sys
import urllib.request
import urllib.error

sys.path.insert(0, __file__.rsplit('/', 1)[0])  # ensure services/ is on path
from lib.endpoints import input_url  # noqa: E402

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8000

# Paths proxied to beo-input (port 8767)
_PROXY_PREFIXES = ('/config', '/update/', '/discover/', '/info')


def _proxy_to_input(handler, method: str) -> bool:
    """Forward request to beo-input on port 8767. Returns True if handled."""
    if not any(handler.path == p or handler.path.startswith(p)
               for p in _PROXY_PREFIXES):
        return False

    url = input_url(handler.path)
    body = None
    if method == 'POST':
        length = int(handler.headers.get('Content-Length', 0))
        body = handler.rfile.read(length) if length else b''
    ct = handler.headers.get('Content-Type', 'application/json')

    # Device discovery (SSDP + subnet IP-scan fallback) can legitimately take
    # longer than a normal config call, so give /discover/ a wider window —
    # otherwise a slow-but-successful scan surfaces to the UI as a 502.
    timeout = 30 if handler.path.startswith('/discover/') else 15

    # Pass the browser's Origin and the host it dialled through to beo-input.
    # Its mutating endpoints (POST /config, /update/run) reject cross-site
    # requests — dropping these headers here would turn this proxy into the
    # bypass (see request_origin_ok() in services/input.py).
    fwd_headers = {'Content-Type': ct}
    origin = handler.headers.get('Origin')
    if origin:
        fwd_headers['Origin'] = origin
    if handler.headers.get('Host'):
        fwd_headers['X-Forwarded-Host'] = handler.headers['Host']

    def _relay(status, payload, content_type, upstream_headers=None):
        handler.send_response(status)
        handler.send_header('Content-Type', content_type)
        # Mirror upstream's CORS decision instead of forcing a wildcard.
        acao = (upstream_headers or {}).get('Access-Control-Allow-Origin')
        if acao:
            handler.send_header('Access-Control-Allow-Origin', acao)
        elif not origin:
            handler.send_header('Access-Control-Allow-Origin', '*')
        handler.send_header('Vary', 'Origin')
        handler.end_headers()
        handler.wfile.write(payload)

    try:
        req = urllib.request.Request(url, data=body, method=method,
                                     headers=fwd_headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            _relay(resp.status, resp.read(),
                   resp.headers.get('Content-Type', 'application/json'),
                   resp.headers)
    except urllib.error.HTTPError as e:
        _relay(e.code, e.read(), 'application/json', e.headers)
    except Exception as e:
        handler.send_response(502)
        handler.send_header('Content-Type', 'text/plain')
        handler.end_headers()
        handler.wfile.write(str(e).encode())
    return True


class NoCacheHandler(http.server.SimpleHTTPRequestHandler):
    def _redirect_config(self):
        self.send_response(302)
        self.send_header('Location', '/softarc/config.html')
        self.end_headers()

    def do_OPTIONS(self):
        # Proxied paths answer their own preflight — beo-input refuses
        # cross-site origins there, and answering for it here would hand a
        # foreign page permission it should not get.
        if _proxy_to_input(self, 'OPTIONS'):
            return
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()

    def do_GET(self):
        if self.path == '/config':
            self._redirect_config()
            return
        if _proxy_to_input(self, 'GET'):
            return
        super().do_GET()

    def do_HEAD(self):
        if self.path == '/config':
            self._redirect_config()
            return
        super().do_HEAD()

    def do_POST(self):
        if _proxy_to_input(self, 'POST'):
            return
        self.send_response(404)
        self.end_headers()

    def end_headers(self):
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        super().end_headers()


if __name__ == "__main__":
    with http.server.ThreadingHTTPServer(("", PORT), NoCacheHandler) as httpd:
        print(f"Serving on port {PORT} (no-cache)")
        httpd.serve_forever()
