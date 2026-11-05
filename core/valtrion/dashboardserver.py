"""The local server of the dashboard.

Read-only: it answers GET with the page, a snapshot and two icons, and nothing else (no way to start, stop or change anything). It listens on
127.0.0.1 only, answers only requests whose Host header names it (so a page on the web cannot reach it by rebinding a DNS name), and wants
a token that is kept in `<home>/dashboard.json`; the browser gets it once in the link and then keeps it in a cookie."""
from __future__ import annotations

import hmac
import json
import os
import secrets
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from . import brandassets, core, dashboard, dashboardpage

DEFAULT_PORT = 47821
PORT_TRIES = 10
COOKIE = "valtrion_dashboard"
SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": "default-src 'none'; img-src 'self' data:; style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src 'self'",
}


def state_path(home: Path | None = None) -> Path:
    return (home or core.valtrion_home()) / "dashboard.json"


def read_state(home: Path | None = None) -> dict | None:
    try:
        document = core.read_json(state_path(home))
    except (OSError, ValueError):
        return None
    return document if isinstance(document, dict) and "port" in document and "token" in document else None


def url_for(state: dict) -> str:
    return f"http://127.0.0.1:{state['port']}/?t={state['token']}"


class _Handler(BaseHTTPRequestHandler):
    server_version = "ValtrionDashboard"
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args) -> None:  # noqa: A002 - a view must not write to the console of whoever started it
        pass

    # -- plumbing ---------------------------------------------------------------------------------------------------------------

    def _send(self, status: int, body: bytes, content_type: str, extra: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for name, value in {**SECURITY_HEADERS, **(extra or {})}.items():
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _text(self, status: int, text: str) -> None:
        self._send(status, text.encode("utf-8"), "text/plain; charset=utf-8")

    def _host_ok(self) -> bool:
        port = self.server.server_address[1]
        return (self.headers.get("Host") or "").lower() in (f"127.0.0.1:{port}", f"localhost:{port}")

    def _cookie_token(self) -> str:
        for part in (self.headers.get("Cookie") or "").split(";"):
            name, _, value = part.strip().partition("=")
            if name == COOKIE:
                return value
        return ""

    def _authorized(self, query_token: str) -> bool:
        expected = self.server.token  # type: ignore[attr-defined]
        for given in (query_token, self._cookie_token(), self.headers.get("X-Valtrion-Token") or ""):
            if given and hmac.compare_digest(given.encode(), expected.encode()):
                return True
        return False

    # -- requests ---------------------------------------------------------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802
        if not self._host_ok():
            return self._text(HTTPStatus.FORBIDDEN, "wrong host")
        parts = urlsplit(self.path)
        query_token = (parse_qs(parts.query).get("t") or [""])[0]
        if parts.path == "/api/health":
            return self._send(HTTPStatus.OK, json.dumps({"ok": True, "pid": os.getpid()}).encode(), "application/json")
        if not self._authorized(query_token):
            return self._text(HTTPStatus.FORBIDDEN, "Open the dashboard from the Valtrion tray icon or with `valtrion dashboard`.")
        if query_token:  # keep the token in a cookie and out of the address bar
            cookie = f"{COOKIE}={self.server.token}; Path=/; HttpOnly; SameSite=Strict"  # type: ignore[attr-defined]
            return self._send(HTTPStatus.SEE_OTHER, b"", "text/plain", {"Location": parts.path or "/", "Set-Cookie": cookie})
        if parts.path in ("/", "/index.html"):
            return self._send(HTTPStatus.OK, dashboardpage.page().encode("utf-8"), "text/html; charset=utf-8")
        if parts.path == "/api/snapshot":
            try:
                body = json.dumps(self.server.snapshot()).encode("utf-8")  # type: ignore[attr-defined]
            except Exception as error:  # noqa: BLE001 - the page shows "disconnected"; the server stays up
                return self._text(HTTPStatus.INTERNAL_SERVER_ERROR, f"snapshot failed: {type(error).__name__}")
            return self._send(HTTPStatus.OK, body, "application/json")
        if parts.path == "/favicon.png":
            return self._send(HTTPStatus.OK, brandassets.favicon_png(), "image/png")
        return self._text(HTTPStatus.NOT_FOUND, "not found")

    do_HEAD = do_GET  # noqa: N815

    def _refuse(self) -> None:
        self._send(HTTPStatus.METHOD_NOT_ALLOWED, b"read-only", "text/plain", {"Allow": "GET, HEAD"})

    do_POST = do_PUT = do_DELETE = do_PATCH = _refuse  # noqa: N815


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, port: int, token: str, collector: "dashboard.Collector | dashboard.Fleet") -> None:
        super().__init__(("127.0.0.1", port), _Handler)
        self.token = token
        self._collector = collector
        self._lock = threading.Lock()

    def snapshot(self) -> dict:
        with self._lock:
            return self._collector.snapshot()


def start(home: Path | None = None, port: int = DEFAULT_PORT, collector: "dashboard.Collector | dashboard.Fleet | None" = None) -> tuple[DashboardServer, dict]:
    """Bind the first free port from `port`, write the state file and serve in a thread. Returns the server and its state."""
    base = home or core.valtrion_home()
    token = secrets.token_hex(24)
    last_error: OSError | None = None
    server = None
    for candidate in range(port, port + PORT_TRIES):
        try:
            server = DashboardServer(candidate, token, collector or dashboard.Fleet(base))
            break
        except OSError as error:
            last_error = error
    if server is None:
        raise OSError(f"no free port from {port} to {port + PORT_TRIES - 1}: {last_error}")
    state = {"pid": os.getpid(), "port": server.server_address[1], "token": token, "startedUtc": core.format_utc(core.now())}
    base.mkdir(parents=True, exist_ok=True)
    state_path(base).write_text(json.dumps(state), encoding="utf-8")
    threading.Thread(target=server.serve_forever, name="valtrion-dashboard", daemon=True).start()
    threading.Thread(target=lambda: server.snapshot(), name="valtrion-dashboard-warmup", daemon=True).start()  # the first page need not wait
    return server, state


def stop(server: DashboardServer, home: Path | None = None) -> None:
    server.shutdown()
    server.server_close()
    try:
        state = read_state(home)
        if state is not None and state.get("pid") == os.getpid():
            state_path(home).unlink(missing_ok=True)
    except OSError:
        pass
