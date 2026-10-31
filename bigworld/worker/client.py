"""HTTP-Client für die Koordinator-API (urllib, keine Abhängigkeiten)."""

from __future__ import annotations

import json
import os
import shutil
import urllib.error
import urllib.request
from pathlib import Path

from .. import PROTOCOL_VERSION
from ..common.hashing import sha256_file

UPLOAD_CHUNK = 32 * 1024 * 1024


class ApiError(Exception):
    def __init__(self, status: int, message: str, body: dict | None = None):
        super().__init__(message)
        self.status = status
        self.body = body or {}


class Offline(Exception):
    """Koordinator nicht erreichbar (Netzwerk)."""


class Client:
    def __init__(self, url: str, token: str, timeout: int = 60):
        self.url = url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def _request(self, method: str, path: str, body=None, headers=None, raw: bytes | None = None,
                 timeout: int | None = None):
        data = raw
        h = {"Authorization": f"Bearer {self.token}", "User-Agent": "bigworld-worker"}
        if body is not None:
            data = json.dumps(body).encode()
            h["Content-Type"] = "application/json"
        if headers:
            h.update(headers)
        req = urllib.request.Request(self.url + path, data=data, method=method, headers=h)
        try:
            return urllib.request.urlopen(req, timeout=timeout or self.timeout)
        except urllib.error.HTTPError as e:
            try:
                payload = json.loads(e.read() or b"{}")
            except json.JSONDecodeError:
                payload = {}
            raise ApiError(e.code, payload.get("detail") or f"HTTP {e.code}", payload) from None
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
            raise Offline(f"Koordinator {self.url} nicht erreichbar: {getattr(e, 'reason', e)}") from None

    def json(self, method: str, path: str, body=None, timeout: int | None = None):
        with self._request(method, path, body=body, timeout=timeout) as r:
            raw = r.read()
            return json.loads(raw) if raw else {}

    # API ------------------------------------------------------------------------
    def register(self, name: str, info: dict) -> str:
        return self.json("POST", "/api/v1/workers/register",
                         {"name": name, "info": info, "protocol": PROTOCOL_VERSION})["worker_id"]

    def manifest(self) -> dict:
        return self.json("GET", "/api/v1/bundle/manifest")

    def status(self) -> dict:
        return self.json("GET", "/api/v1/status")

    def acquire(self, wid: str, bundle_hash: str) -> dict:
        return self.json("POST", "/api/v1/leases", {"worker_id": wid, "bundle_hash": bundle_hash})

    def heartbeat(self, wid: str, lid: str, **data) -> dict:
        return self.json("POST", f"/api/v1/leases/{lid}/heartbeat", {"worker_id": wid, **data}, timeout=30)

    def release(self, wid: str, lid: str, reason: str, failed: bool = False) -> None:
        self.json("POST", f"/api/v1/leases/{lid}/release", {"worker_id": wid, "reason": reason, "failed": failed})

    def download(self, path: str, dest: Path, expected_sha: str | None = None) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".part")
        with self._request("GET", path, timeout=300) as r, open(tmp, "wb") as f:
            shutil.copyfileobj(r, f, 1024 * 1024)
        if expected_sha and sha256_file(tmp) != expected_sha:
            tmp.unlink(missing_ok=True)
            raise ApiError(0, f"Prüfsumme von {dest.name} stimmt nicht")
        os.replace(tmp, dest)

    def upload(self, wid: str, lid: str, path: Path, sha: str, progress=None) -> dict:
        """Lädt mit Wiederaufnahme hoch. Wirft Offline bei Netzproblemen (später erneut versuchen)."""
        size = path.stat().st_size
        offset = self.json("GET", f"/api/v1/leases/{lid}/upload")["size"]
        if offset > size:
            raise ApiError(0, "Koordinator hat mehr Daten als die Datei groß ist")
        with open(path, "rb") as f:
            while offset < size:
                f.seek(offset)
                chunk = f.read(UPLOAD_CHUNK)
                try:
                    with self._request("PUT", f"/api/v1/leases/{lid}/upload", raw=chunk,
                                       headers={"X-Offset": str(offset),
                                                "Content-Type": "application/octet-stream"},
                                       timeout=600) as r:
                        offset = json.loads(r.read())["size"]
                except ApiError as e:
                    if e.status == 409 and "size" in e.body:
                        offset = int(e.body["size"])
                        continue
                    raise
                if progress:
                    progress(offset, size)
        return self.json("POST", f"/api/v1/leases/{lid}/complete",
                         {"worker_id": wid, "sha256": sha, "size": size}, timeout=600)
