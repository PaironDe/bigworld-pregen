"""Ablaufsteuerung des Workers: holen, generieren, prüfen, packen, hochladen."""

from __future__ import annotations

import collections
import json
import shlex
import shutil
import sys
import tarfile
import threading
import time
from pathlib import Path

from .. import __version__
from ..common import anvil, package, template_files
from ..common.hashing import sha256_file
from ..common.tiles import Tile
from . import procutil, sync
from .client import ApiError, Client, Offline
from .java import ensure_java
from .server import Aborted, MinecraftGenerator, ServerError, free_port, tail_text
from .settings import Settings, system_info
from .simulate import FakeGenerator

PHASES = {
    "idle": "Bereit",
    "connecting": "Verbinde mit Koordinator",
    "syncing": "Lade Mods und Configs",
    "java": "Bereite Java vor",
    "waiting": "Warte auf Arbeit",
    "template": "Erzeuge Vorlage-Welt",
    "starting": "Starte Minecraft-Server",
    "generating": "Generiere",
    "verifying": "Prüfe Chunks",
    "packaging": "Packe Kachel",
    "uploading": "Lade hoch",
    "offline": "Koordinator nicht erreichbar",
    "done": "Alles fertig",
    "stopping": "Stoppe",
    "error": "Fehler",
}


class Engine:
    def __init__(self, base: Path, simulate: dict | None = None):
        self.base = base
        base.mkdir(parents=True, exist_ok=True)
        self.settings_path = base / "settings.json"
        self.settings = Settings.load(self.settings_path)
        self.simulate = simulate
        self.lock = threading.RLock()
        self.state = self._load_state()
        self.logbuf: collections.deque[str] = collections.deque(maxlen=500)
        self.status: dict = {"phase": "idle", "message": "", "tile": None, "kind": None, "progress": 0.0,
                             "chunks": 0, "cps": 0.0, "eta": None, "last_error": None, "upload": None,
                             "tiles_done": 0, "bundle": None}
        self.running = False
        self.abort = threading.Event()
        self.lease_abort = threading.Event()
        self.thread: threading.Thread | None = None
        self.manifest: dict | None = None
        self.java: str | None = None
        self.overrides: list[str] = []
        self.generator = None
        self._kill_stale_java()
        threading.Thread(target=self._uploader, daemon=True, name="uploader").start()

    # Pfade -----------------------------------------------------------------------
    @property
    def bundle_dir(self) -> Path:
        return self.base / "bundle"

    @property
    def server_dir(self) -> Path:
        return self.base / "server"

    @property
    def outbox(self) -> Path:
        return self.base / "outbox"

    @property
    def overrides_dir(self) -> Path:
        return self.base / "overrides"

    # Zustand ----------------------------------------------------------------------
    def _load_state(self) -> dict:
        p = self.base / "state.json"
        try:
            s = json.loads(p.read_text())
        except (OSError, json.JSONDecodeError):
            s = {}
        s.setdefault("outbox", [])
        return s

    def save_state(self) -> None:
        with self.lock:
            p = self.base / "state.json"
            tmp = p.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.state, indent=2))
            tmp.replace(p)

    def log(self, msg: str) -> None:
        line = f"{time.strftime('%H:%M:%S')}  {msg}"
        self.logbuf.append(line)
        try:
            print(line, flush=True)
        except (OSError, UnicodeEncodeError):
            pass

    def set_status(self, **kw) -> None:
        with self.lock:
            self.status.update(kw)

    def phase(self, name: str, message: str = "") -> None:
        self.set_status(phase=name, message=message or PHASES.get(name, name))

    def snapshot(self) -> dict:
        with self.lock:
            return {
                **self.status,
                "phase_label": PHASES.get(self.status["phase"], self.status["phase"]),
                "running": self.running,
                "settings": self.settings.public(),
                "outbox": [{k: v for k, v in o.items() if k != "path"} for o in self.state["outbox"]],
                "log": list(self.logbuf)[-200:],
                "version": __version__,
                "simulate": bool(self.simulate),
            }

    def update_settings(self, data: dict) -> None:
        with self.lock:
            for k, v in data.items():
                if k in ("token", "admin_token") and v in ("", "gesetzt"):
                    continue  # leeres Feld = unverändert lassen
                if hasattr(self.settings, k):
                    cur = getattr(self.settings, k)
                    setattr(self.settings, k, type(cur)(v) if cur is not None and v is not None else v)
            self.settings.save(self.settings_path)

    def client(self) -> Client:
        if not self.settings.token:
            raise ApiError(0, "Kein Token eingetragen")
        return Client(self.settings.coordinator_url, self.settings.token)

    # Start/Stopp ------------------------------------------------------------------
    def start(self) -> None:
        with self.lock:
            if self.running:
                return
            self.running = True
            self.abort.clear()
            self.thread = threading.Thread(target=self._run, daemon=True, name="engine")
            self.thread.start()

    def stop(self) -> None:
        with self.lock:
            self.running = False
            self.abort.set()
            self.lease_abort.set()
        self.phase("stopping")

    def _sleep(self, seconds: float) -> None:
        self.abort.wait(seconds)

    def _kill_stale_java(self) -> None:
        pid = self.state.get("java_pid")
        if pid and procutil.pid_alive_with(int(pid), str(self.server_dir)):
            self.log(f"Beende übrig gebliebenen Minecraft-Server (PID {pid})")
            procutil.kill(int(pid))
        self.state["java_pid"] = None

    def _on_pid(self, pid: int | None) -> None:
        with self.lock:
            self.state["java_pid"] = pid
        self.save_state()

    # Hauptschleife ------------------------------------------------------------------
    def _run(self) -> None:
        failures = 0
        try:
            while self.running and not self.abort.is_set():
                try:
                    finished = self._cycle()
                    failures = 0
                    if finished:
                        break
                except Offline as e:
                    self.log(str(e))
                    self.phase("offline", str(e))
                    self._sleep(30)
                except Aborted:
                    break
                except (ApiError, ServerError, sync.SyncError, RuntimeError, ValueError, OSError,
                        tarfile.TarError) as e:
                    if self.abort.is_set():
                        break
                    failures += 1
                    msg = str(e)
                    self.log(f"FEHLER: {msg}")
                    if isinstance(e, ServerError) and e.tail:
                        self.log("Letzte Server-Ausgabe:\n" + tail_text(e.tail))
                    self.set_status(last_error=msg)
                    if isinstance(e, ApiError) and e.status in (401, 403, 426):
                        self.phase("error", msg)
                        break
                    if failures >= 3:
                        self.phase("error", f"Dreimal hintereinander fehlgeschlagen: {msg}")
                        break
                    self._sleep(20)
        finally:
            if self.generator:
                try:
                    self.generator.shutdown()
                except Exception:  # noqa: BLE001
                    pass
            with self.lock:
                self.running = False
            if self.status["phase"] not in ("error", "done"):
                self.phase("idle")
            self.set_status(tile=None, kind=None, cps=0.0, eta=None)

    def _register(self, c: Client) -> str:
        info = {**system_info(), "version": __version__, "overrides": self.overrides,
                "simulate": bool(self.simulate)}
        wid = c.register(self.settings.name, info)
        with self.lock:
            self.state["worker_id"] = wid
        self.save_state()
        return wid

    def _prepare(self, c: Client) -> None:
        self.phase("connecting")
        self._register(c)
        self.phase("syncing")
        manifest = c.manifest()
        n = sync.sync_bundle(c, manifest, self.bundle_dir, log=self.log)
        self.overrides = sync.install_into_server(manifest, self.bundle_dir, self.server_dir, self.overrides_dir)
        if self.overrides:
            self.log("Lokale Overrides aktiv: " + ", ".join(self.overrides))
        self._register(c)  # Overrides mitteilen
        self.log(f"Bundle {manifest['hash'][:12]} aktuell ({n} Dateien geladen, {len(manifest['files'])} gesamt)")
        if not self.simulate:
            self.phase("java")
            self.java = ensure_java(self.base, self.settings.java_path, log=self.log)
        self.manifest = manifest
        self.set_status(bundle=manifest["hash"])

    def _make_generator(self):
        if self.simulate:
            return FakeGenerator(self.server_dir / "world", delay=self.simulate.get("delay", 0.0),
                                 diff=self.simulate.get("diff", False), log=self.log)
        return MinecraftGenerator(
            self.server_dir, self.java, int(self.settings.ram_gb),
            shlex.split(self.settings.extra_jvm_args, posix=sys.platform != "win32"),
            self.manifest["launcher"], self.base / "logs" / "server.log", log=self.log, on_pid=self._on_pid,
        )

    def _cycle(self) -> bool:
        c = self.client()
        if not self.manifest:
            self._prepare(c)
        wid = self.state["worker_id"]
        try:
            resp = c.acquire(wid, self.manifest["hash"])
        except ApiError as e:
            if e.status == 409:
                self.log("Bundle hat sich geändert, synchronisiere neu")
                self.manifest = None
                return False
            if e.status == 401 and "registrieren" in str(e):
                self._register(c)
                return False
            raise
        if resp.get("wait"):
            self.phase("waiting", resp.get("reason", ""))
            self._sleep(30)
            return False
        if resp.get("done"):
            self.phase("done", "Alle Kacheln sind fertig")
            self.log("Koordinator meldet: alles fertig")
            return True
        self.lease_abort.clear()
        if resp["kind"] == "template":
            self._do_template(c, wid, resp)
        else:
            self._do_tile(c, wid, resp)
        return not self.settings.keep_going

    # Heartbeat --------------------------------------------------------------------
    def _heartbeat_loop(self, c: Client, wid: str, lid: str, stop: threading.Event, every: int) -> None:
        while not stop.wait(every):
            try:
                s = self.status
                r = c.heartbeat(wid, lid, phase=s["phase"], progress=s["progress"], chunks_done=s["chunks"],
                                cps=s["cps"], eta=s["eta"], message=s["message"][:200])
                if r.get("abort"):
                    self.log(f"Koordinator bricht die Kachel ab: {r.get('reason')}")
                    self.lease_abort.set()
                    return
            except Offline:
                pass
            except ApiError as e:
                self.log(f"Heartbeat abgelehnt: {e}")

    class _Combined:
        """Ein Abbruchsignal aus Stopp-Knopf und Koordinator."""

        def __init__(self, a: threading.Event, b: threading.Event):
            self.a, self.b = a, b

        def is_set(self) -> bool:
            return self.a.is_set() or self.b.is_set()

        def wait(self, t: float) -> bool:
            end = time.time() + t
            while time.time() < end:
                if self.is_set():
                    return True
                time.sleep(min(0.5, end - time.time()))
            return self.is_set()

    def _progress(self, chunks: int, percent: float, eta, cps: float) -> None:
        self.set_status(chunks=chunks, progress=max(0.0, min(1.0, percent / 100.0)), eta=eta, cps=cps,
                        phase="generating", message=f"{PHASES['generating']}: {percent:.1f} %")

    # Vorlage ------------------------------------------------------------------------
    def _do_template(self, c: Client, wid: str, lease: dict) -> None:
        lid = lease["lease_id"]
        self.log("Auftrag: Vorlage-Welt erzeugen")
        self.set_status(tile="Vorlage", kind="template", progress=0.0, chunks=0, cps=0.0, eta=None)
        self.phase("template")
        src = self.bundle_dir / "template"
        if not (src / "level.dat").is_file():
            raise ValueError("Bundle enthält keine template/level.dat")
        sync.prepare_world(self.server_dir, src)
        sync.write_server_files(self.server_dir, self.manifest["properties"], free_port())
        stop_hb = threading.Event()
        threading.Thread(target=self._heartbeat_loop, args=(c, wid, lid, stop_hb, lease["heartbeat_seconds"]),
                         daemon=True).start()
        try:
            self.generator = self._make_generator()
            self.generator.make_template(self._Combined(self.abort, self.lease_abort))
            world = self.server_dir / "world"
            files = template_files.collect(world)
            missing = [r for r in template_files.REQUIRED if r not in {f[0] for f in files}]
            if missing:
                raise ValueError(f"Vorlage unvollständig, es fehlt: {', '.join(missing)}")
            self.log("Vorlage enthält: " + ", ".join(f[0] for f in files if not f[0].startswith("datapacks/")))
            self.outbox.mkdir(parents=True, exist_ok=True)
            tar_path = self.outbox / f"template-{lid}.tar"
            package.write_package(tar_path, {"kind": "template", "lease_id": lid, "worker": self.settings.name},
                                  files)
            self.phase("uploading", "Lade Vorlage hoch")
            res = c.upload(wid, lid, tar_path, sha256_file(tar_path))
            self.log(f"Vorlage hochgeladen: {res.get('result')}")
            tar_path.unlink(missing_ok=True)
        except (Aborted, ServerError, ValueError, OSError) as e:
            self._release(c, wid, lid, e)
            raise
        finally:
            stop_hb.set()
            self.set_status(tile=None, kind=None)

    def _release(self, c: Client, wid: str, lid: str, e: Exception) -> None:
        try:
            if isinstance(e, Aborted):
                if self.lease_abort.is_set() and not self.abort.is_set():
                    return  # Koordinator hat abgebrochen, nichts freizugeben
                c.release(wid, lid, "Vom Nutzer gestoppt")
            else:
                tail = tail_text(e.tail, 8) if isinstance(e, ServerError) else ""
                c.release(wid, lid, f"{e}\n{tail}"[:290], failed=True)
        except (Offline, ApiError):
            pass

    # Kacheln --------------------------------------------------------------------------
    def _template_world(self, c: Client, sha: str) -> Path:
        d = self.base / "templates" / sha
        world = d / "world"
        if (world / "level.dat").is_file():
            return world
        if d.exists():
            shutil.rmtree(d)
        d.mkdir(parents=True)
        tar_path = d / "template.tar"
        self.log("Lade Vorlage-Welt")
        c.download(f"/api/v1/template/{self.manifest['hash']}", tar_path, sha)
        with tarfile.open(tar_path) as tar:
            for m in tar.getmembers():
                if m.isfile() and template_files.member_allowed(m.name):
                    target = world / m.name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with open(target, "wb") as out:
                        shutil.copyfileobj(tar.extractfile(m), out)
        return world

    def _do_tile(self, c: Client, wid: str, lease: dict) -> None:
        lid = lease["lease_id"]
        tile = Tile.from_dict(lease["tile"])
        dim_path = lease["dim_path"]
        label = f"{tile.dim.replace('minecraft:', '')} {tile.tx},{tile.tz}"
        kind = lease["kind"]
        self.log(f"Auftrag: {'Vergleichs-' if kind == 'verify' else ''}Kachel {label} "
                 f"(x {tile.x0}…{tile.x1 - 1}, z {tile.z0}…{tile.z1 - 1}, {tile.chunk_count:,} Chunks)")
        self.set_status(tile=label, kind=kind, progress=0.0, chunks=0, cps=0.0, eta=None)
        stop_hb = threading.Event()
        threading.Thread(target=self._heartbeat_loop, args=(c, wid, lid, stop_hb, lease["heartbeat_seconds"]),
                         daemon=True).start()
        abort = self._Combined(self.abort, self.lease_abort)
        try:
            tmpl = self._template_world(c, lease["template_sha256"])
            world = self.server_dir / "world"
            cur = self.state.get("current") or {}
            if cur.get("lease_id") == lid and cur.get("world_ready") and (world / "level.dat").exists():
                self.log("Setze angefangene Kachel fort")
            else:
                sync.prepare_world(self.server_dir, tmpl)
            with self.lock:
                self.state["current"] = {"lease_id": lid, "tile": tile.key, "world_ready": True}
            self.save_state()
            self.generator = self._make_generator()
            for attempt in range(3):
                sync.write_server_files(self.server_dir, self.manifest["properties"], free_port())
                self.phase("starting")
                self.generator.generate(tile, dim_path, abort, self._progress)
                self.phase("verifying")
                problems = self._verify(world, dim_path, tile)
                if not problems:
                    break
                self.log(f"Prüfung: {problems[0]}" + (f" (+{len(problems) - 1} weitere)" if len(problems) > 1 else ""))
                if attempt == 2:
                    raise ValueError(f"Kachel nach 3 Versuchen unvollständig: {problems[0]}")
                self.log("Generiere fehlende Chunks nach")
            self.phase("packaging")
            files = package.collect_tile_files(world, dim_path, tile)
            self.outbox.mkdir(parents=True, exist_ok=True)
            tar_path = self.outbox / f"{lid}.tar"
            meta = {"kind": kind, "lease_id": lid, "tile": tile.to_dict(), "dim_path": dim_path,
                    "worker": self.settings.name, "bundle_hash": self.manifest["hash"], "created": time.time(),
                    "files": [f[0] for f in files]}
            package.write_package(tar_path, meta, files)
            sha = sha256_file(tar_path)
            with self.lock:
                self.state["outbox"].append({"lease_id": lid, "kind": kind, "tile": label, "path": str(tar_path),
                                             "sha256": sha, "size": tar_path.stat().st_size, "attempts": 0})
                self.state["current"] = None
            self.save_state()
            try:
                c.heartbeat(wid, lid, phase="upload", progress=1.0, chunks_done=tile.chunk_count, cps=0,
                            eta=0, message="wartet auf Upload")
            except (Offline, ApiError):
                pass
            shutil.rmtree(world, ignore_errors=True)
            self.log(f"Kachel {label} fertig, {tar_path.stat().st_size / 1e6:.0f} MB im Upload-Ausgang")
        except (Aborted, ServerError, ValueError, OSError, tarfile.TarError) as e:
            self._release(c, wid, lid, e)
            if not isinstance(e, Aborted) or self.abort.is_set():
                with self.lock:
                    self.state["current"] = None
                self.save_state()
            if isinstance(e, Aborted) and self.lease_abort.is_set() and not self.abort.is_set():
                self.log("Kachel vom Koordinator abgebrochen, hole die nächste")
                return
            raise
        finally:
            stop_hb.set()
            self.set_status(tile=None, kind=None)

    def _verify(self, world: Path, dim_path: str, tile: Tile) -> list[str]:
        region_dir = (world / dim_path / "region") if dim_path else (world / "region")
        problems = []
        for rx, rz in tile.regions():
            p = region_dir / f"r.{rx}.{rz}.mca"
            if not p.is_file():
                hits = [q.parent.relative_to(world).as_posix() for q in world.rglob(p.name)]
                if hits:
                    raise ValueError(f"Regionsdateien liegen unter {hits[0]} statt {dim_path}/region. "
                                     "Bitte den Dimensionspfad in der Koordinator-Konfiguration anpassen.")
                problems.append(f"{p.name} fehlt")
                continue
            res = anvil.check_region_complete(p)
            if not res.ok:
                problems.append(f"{p.name}: {len(res.missing)} fehlen, {len(res.not_full)} unfertig"
                                + (f", Fehler: {res.errors[0]}" if res.errors else ""))
        return problems

    # Upload-Ausgang ---------------------------------------------------------------------
    def _uploader(self) -> None:
        last_hb = 0.0
        while True:
            with self.lock:
                items = list(self.state["outbox"])
            if not items or not self.settings.token:
                self.set_status(upload=None)
                time.sleep(2)
                continue
            item = items[0]
            path = Path(item["path"])
            try:
                c = self.client()
                wid = self.state.get("worker_id") or self._register(c)
                if time.time() - last_hb > 60:
                    for other in items:
                        try:
                            c.heartbeat(wid, other["lease_id"], phase="upload", progress=1.0, message="wartet auf Upload")
                        except ApiError:
                            pass
                    last_hb = time.time()
                if not path.is_file():
                    raise FileNotFoundError(str(path))

                def prog(done: int, total: int) -> None:
                    self.set_status(upload={"tile": item["tile"], "done": done, "total": total})

                res = c.upload(wid, item["lease_id"], path, item["sha256"], progress=prog)
                self.log(f"Upload {item['tile']} fertig ({res.get('result')})")
                self._drop(item, path)
                with self.lock:
                    self.status["tiles_done"] += 1
            except Offline:
                time.sleep(30)
            except FileNotFoundError:
                self.log(f"Paket {path.name} fehlt, wird aus dem Ausgang entfernt")
                self._drop(item, path)
            except ApiError as e:
                if e.status in (403, 404, 409):
                    self.log(f"Upload {item['tile']} verworfen: {e}")
                    self._drop(item, path)
                elif e.status == 422 and item.get("attempts", 0) < 2:
                    self.log(f"Upload {item['tile']} beschädigt angekommen, lade neu hoch")
                    with self.lock:
                        item["attempts"] = item.get("attempts", 0) + 1
                    self.save_state()
                else:
                    self.log(f"Upload {item['tile']} fehlgeschlagen: {e}")
                    time.sleep(30)
            except OSError as e:
                self.log(f"Upload-Fehler: {e}")
                time.sleep(30)
            finally:
                self.set_status(upload=None)

    def _drop(self, item: dict, path: Path) -> None:
        path.unlink(missing_ok=True)
        with self.lock:
            self.state["outbox"] = [o for o in self.state["outbox"] if o["lease_id"] != item["lease_id"]]
        self.save_state()
