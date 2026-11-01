"""Lokale Weboberfläche des Workers (nur 127.0.0.1, mit Sitzungsschlüssel)."""

from __future__ import annotations

import hmac
import json
import re
import secrets
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from .client import ApiError, Client, Offline
from .engine import Engine


def web_dir() -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
    return base / "web" if (base / "web").is_dir() else Path(__file__).resolve().parent.parent / "web"


class UI:
    def __init__(self, engine: Engine, port: int = 8771):
        self.engine = engine
        self.key = secrets.token_urlsafe(18)
        self.quit = threading.Event()
        handler = self._handler()
        try:
            self.httpd = ThreadingHTTPServer(("127.0.0.1", port), handler)
        except OSError:
            self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = self.httpd.server_address[1]

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/?k={self.key}"

    def serve(self) -> None:
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def _handler(self):
        ui = self
        engine = self.engine
        files = {
            "/": ("worker.html", "text/html; charset=utf-8"),
            "/static/map.js": ("map.js", "text/javascript; charset=utf-8"),
            "/static/style.css": ("style.css", "text/css; charset=utf-8"),
            "/static/areas.js": ("areas.js", "text/javascript; charset=utf-8"),
        }

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):  # keine Zugriffsprotokolle in der Konsole
                pass

            def _host_ok(self) -> bool:
                host = (self.headers.get("Host") or "").split(":")[0]
                return host in ("127.0.0.1", "localhost")

            def _auth(self) -> bool:
                return hmac.compare_digest(self.headers.get("X-Key", ""), ui.key)

            def _send(self, code: int, body: bytes, ctype: str = "application/json") -> None:
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Frame-Options", "DENY")
                self.end_headers()
                self.wfile.write(body)

            def _json(self, code: int, obj) -> None:
                self._send(code, json.dumps(obj).encode())

            def _body(self) -> dict:
                n = int(self.headers.get("Content-Length") or 0)
                if n > 1_000_000:
                    return {}
                try:
                    return json.loads(self.rfile.read(n) or b"{}")
                except json.JSONDecodeError:
                    return {}

            def do_GET(self):
                if not self._host_ok():
                    return self._send(403, b"forbidden", "text/plain")
                path = urlparse(self.path).path
                if path in files:
                    name, ctype = files[path]
                    return self._send(200, (web_dir() / name).read_bytes(), ctype)
                if not self._auth():
                    return self._json(403, {"detail": "Sitzungsschlüssel fehlt"})
                if path == "/api/state":
                    return self._json(200, engine.snapshot())
                if path == "/api/coord-status":
                    try:
                        return self._json(200, engine.client().status())
                    except (ApiError, Offline) as e:
                        return self._json(502, {"detail": str(e)})
                return self._json(404, {"detail": "unbekannt"})

            def do_POST(self):
                if not self._host_ok() or not self._auth():
                    return self._json(403, {"detail": "verboten"})
                path = urlparse(self.path).path
                body = self._body()
                if path == "/api/start":
                    engine.start()
                    return self._json(200, {"ok": True})
                if path == "/api/stop":
                    engine.stop()
                    return self._json(200, {"ok": True})
                if path == "/api/settings":
                    if engine.running:
                        return self._json(409, {"detail": "Erst stoppen, dann Einstellungen ändern"})
                    engine.update_settings(body)
                    engine.manifest = None
                    return self._json(200, engine.snapshot()["settings"])
                if path == "/api/coord":
                    # Weiterleitung an den Koordinator, nur für die Bereichsverwaltung
                    method = str(body.get("method", "GET")).upper()
                    target = str(body.get("path", ""))
                    if method not in ("GET", "POST", "DELETE") or not re.fullmatch(
                            r"/api/v1/areas(/preview|/reorder|/\d+)?", target):
                        return self._json(403, {"detail": "nicht erlaubt"})
                    s = engine.settings
                    if not s.token:
                        return self._json(403, {"detail": "Erst den Token in den Einstellungen eintragen"})
                    c = Client(s.coordinator_url, s.token)
                    try:
                        with c._request(method, target, body=body.get("body") if method != "GET" else None,
                                        headers={"X-Worker-Name": s.name}) as r:
                            raw = r.read()
                        return self._send(200, raw or b"{}")
                    except ApiError as e:
                        return self._json(e.status or 502, {"detail": str(e)})
                    except Offline as e:
                        return self._json(502, {"detail": str(e)})
                if path == "/api/quit":
                    engine.stop()
                    ui.quit.set()
                    return self._json(200, {"ok": True})
                return self._json(404, {"detail": "unbekannt"})

        return H
