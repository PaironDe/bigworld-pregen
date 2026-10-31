"""HTTP-API und Weboberfläche des Koordinators (FastAPI)."""

from __future__ import annotations

import hmac
import logging
import threading
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .. import PROTOCOL_VERSION, __version__
from ..common.hashing import sha256_file
from . import bundle, merge
from .config import Config
from .state import State, StateError

log = logging.getLogger("bigworld.api")

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
MAX_CHUNK = 64 * 1024 * 1024


class RegisterBody(BaseModel):
    name: str
    info: dict = Field(default_factory=dict)
    protocol: int = PROTOCOL_VERSION


class LeaseBody(BaseModel):
    worker_id: str
    bundle_hash: str


class HeartbeatBody(BaseModel):
    worker_id: str
    phase: str = ""
    progress: float = 0
    chunks_done: int = 0
    cps: float = 0
    eta: int | None = None
    message: str = ""


class ReleaseBody(BaseModel):
    worker_id: str
    reason: str = ""
    failed: bool = False


class CompleteBody(BaseModel):
    worker_id: str
    sha256: str
    size: int


class PriorityBody(BaseModel):
    dim: str
    x0: int
    z0: int
    x1: int
    z1: int
    label: str = ""


class ReorderBody(BaseModel):
    ids: list[int]


class PauseBody(BaseModel):
    paused: bool


class ResetBody(BaseModel):
    keys: list[str]


def create_app(cfg: Config, state: State | None = None) -> FastAPI:
    state = state or State(cfg)
    app = FastAPI(title="Big World Koordinator", version=__version__, docs_url=None, redoc_url=None)
    app.state.cfg = cfg
    app.state.st = state
    cfg.incoming_dir.mkdir(parents=True, exist_ok=True)

    def token_of(request: Request) -> str:
        h = request.headers.get("authorization", "")
        return h[7:].strip() if h.lower().startswith("bearer ") else ""

    def require_worker(request: Request) -> str:
        t = token_of(request)
        if hmac.compare_digest(t, cfg.worker_token) or hmac.compare_digest(t, cfg.admin_token):
            return t
        raise HTTPException(401, "Token fehlt oder ist falsch")

    def require_admin(request: Request) -> str:
        if hmac.compare_digest(token_of(request), cfg.admin_token):
            return "admin"
        raise HTTPException(403, "Admin-Token erforderlich")

    @app.exception_handler(StateError)
    async def _state_error(_: Request, exc: StateError):
        return JSONResponse({"detail": str(exc)}, status_code=exc.status)

    # Öffentlich (nur Versionsinfo) -------------------------------------------
    @app.get("/api/v1/info")
    def info():
        return {"service": "bigworld-coordinator", "version": __version__, "protocol": PROTOCOL_VERSION}

    @app.get("/api/v1/whoami")
    def whoami(request: Request, _=Depends(require_worker)):
        return {"admin": hmac.compare_digest(token_of(request), cfg.admin_token)}

    # Bundle --------------------------------------------------------------------
    @app.get("/api/v1/bundle/manifest")
    def manifest(_=Depends(require_worker)):
        h = bundle.current_hash(cfg)
        if not h:
            raise HTTPException(503, "Auf dem Koordinator ist noch kein Bundle gebaut")
        return bundle.load_manifest(cfg, h)

    @app.get("/api/v1/bundle/{bundle_hash}/files/{rel:path}")
    def bundle_file(bundle_hash: str, rel: str, _=Depends(require_worker)):
        if bundle_hash != bundle.current_hash(cfg):
            raise HTTPException(409, "Bundle ist nicht mehr aktuell")
        try:
            p = bundle.file_path(cfg, bundle_hash, rel)
        except ValueError:
            raise HTTPException(400, "Ungültiger Pfad")
        if not p.is_file():
            raise HTTPException(404, "Datei nicht im Bundle")
        return FileResponse(p, media_type="application/octet-stream")

    # Worker ----------------------------------------------------------------------
    @app.post("/api/v1/workers/register")
    def register(body: RegisterBody, _=Depends(require_worker)):
        if body.protocol != PROTOCOL_VERSION:
            raise HTTPException(426, f"Worker-Version passt nicht (Protokoll {body.protocol}, "
                                     f"Koordinator {PROTOCOL_VERSION}). Bitte Worker aktualisieren.")
        return {"worker_id": state.register_worker(body.name, body.info)}

    @app.post("/api/v1/leases")
    def acquire(body: LeaseBody, _=Depends(require_worker)):
        return state.acquire(body.worker_id, body.bundle_hash, bundle.current_hash(cfg))

    @app.post("/api/v1/leases/{lid}/heartbeat")
    def heartbeat(lid: str, body: HeartbeatBody, _=Depends(require_worker)):
        return state.heartbeat(body.worker_id, lid, body.model_dump())

    @app.post("/api/v1/leases/{lid}/release")
    def release(lid: str, body: ReleaseBody, _=Depends(require_worker)):
        state.release(body.worker_id, lid, body.reason, body.failed)
        return {"ok": True}

    def _part(lid: str) -> Path:
        if not all(c in "0123456789abcdef" for c in lid) or len(lid) != 32:
            raise HTTPException(400, "Ungültige Lease-ID")
        return cfg.incoming_dir / f"{lid}.part"

    @app.get("/api/v1/leases/{lid}/upload")
    def upload_status(lid: str, _=Depends(require_worker)):
        state.lease(lid)
        p = _part(lid)
        return {"size": p.stat().st_size if p.exists() else 0}

    @app.put("/api/v1/leases/{lid}/upload")
    async def upload_chunk(lid: str, request: Request, _=Depends(require_worker)):
        lease = state.lease(lid)
        if lease["status"] not in ("active", "expired"):
            raise HTTPException(409, f"Lease ist {lease['status']}")
        p = _part(lid)
        current = p.stat().st_size if p.exists() else 0
        try:
            offset = int(request.headers.get("x-offset", "-1"))
        except ValueError:
            offset = -1
        if offset != current:
            return JSONResponse({"detail": "Offset passt nicht", "size": current}, status_code=409)
        limit = int(cfg.max_upload_gb * 1024**3)
        written = 0
        with open(p, "ab") as f:
            async for chunk in request.stream():
                written += len(chunk)
                if written > MAX_CHUNK or current + written > limit:
                    f.truncate(current)
                    raise HTTPException(413, "Teil oder Paket zu groß")
                f.write(chunk)
        state.touch_upload(lid)
        return {"size": current + written}

    @app.post("/api/v1/leases/{lid}/complete")
    def complete(lid: str, body: CompleteBody, _=Depends(require_worker)):
        p = _part(lid)
        if not p.exists():
            raise HTTPException(404, "Kein Upload vorhanden")
        size = p.stat().st_size
        if size != body.size:
            raise HTTPException(409, f"Größe passt nicht ({size} statt {body.size})")
        sha = sha256_file(p)
        if sha != body.sha256:
            p.unlink()
            raise HTTPException(422, "SHA256 stimmt nicht, Upload verworfen. Bitte neu hochladen.")
        lease, action = state.begin_complete(body.worker_id, lid)
        tar_path = cfg.incoming_dir / f"{lid}.tar"
        p.replace(tar_path)
        if action == "duplicate":
            tar_path.unlink()
            return {"ok": True, "result": "duplicate"}
        if action == "verify":
            try:
                merge.keep_verify_copy(cfg, state, lease, tar_path)
            except ValueError as e:
                state.release(body.worker_id, lid, str(e), failed=True)
                tar_path.unlink(missing_ok=True)
                raise HTTPException(422, f"Vergleichspaket ungültig: {e}")
            return {"ok": True, "result": "verify-stored"}
        threading.Thread(target=merge.merge_package, args=(cfg, state, lease, tar_path), daemon=True).start()
        return {"ok": True, "result": "merging"}

    # Status und Verwaltung -----------------------------------------------------
    @app.get("/api/v1/status")
    def status(_=Depends(require_worker)):
        s = state.status()
        s["bundle"] = bundle.current_hash(cfg)
        return s

    @app.post("/api/v1/priorities/preview")
    def priority_preview(body: PriorityBody, _=Depends(require_worker)):
        return {"tiles": state.preview_priority(body.dim, body.x0, body.z0, body.x1, body.z1)}

    @app.post("/api/v1/priorities")
    def priority_add(body: PriorityBody, _=Depends(require_admin)):
        try:
            return state.add_priority(body.dim, body.x0, body.z0, body.x1, body.z1, body.label)
        except KeyError:
            raise HTTPException(400, "Unbekannte Dimension")

    @app.delete("/api/v1/priorities/{pid}")
    def priority_delete(pid: int, _=Depends(require_admin)):
        state.delete_priority(pid)
        return {"ok": True}

    @app.post("/api/v1/priorities/reorder")
    def priority_reorder(body: ReorderBody, _=Depends(require_admin)):
        state.reorder_priorities(body.ids)
        return {"ok": True}

    @app.post("/api/v1/pause")
    def pause(body: PauseBody, _=Depends(require_admin)):
        state.set_setting("paused", "1" if body.paused else "0")
        return {"paused": body.paused}

    @app.post("/api/v1/tiles/reset")
    def tiles_reset(body: ResetBody, _=Depends(require_admin)):
        return {"reset": state.reset_tiles(body.keys)}

    # Weboberfläche -----------------------------------------------------------
    app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")

    @app.get("/", response_class=HTMLResponse)
    def index():
        return (WEB_DIR / "coordinator.html").read_text(encoding="utf-8")

    return app
