"""Zustand des Koordinators in SQLite: Kacheln, Leases, Worker, Prioritäten."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path

from ..common.tiles import Tile, tiles_for_dimension, tiles_touching_area
from .config import Config

SCHEMA = """
CREATE TABLE IF NOT EXISTS tiles (
    key TEXT PRIMARY KEY,
    dim TEXT NOT NULL,
    tx INTEGER NOT NULL, tz INTEGER NOT NULL,
    rx0 INTEGER NOT NULL, rx1 INTEGER NOT NULL,
    rz0 INTEGER NOT NULL, rz1 INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',   -- pending | leased | merging | done | failed
    priority INTEGER,                         -- Rang des ersten Prioritätsbereichs, NULL = keiner
    attempts INTEGER NOT NULL DEFAULT 0,
    done_at REAL, done_by TEXT, bundle_hash TEXT, size INTEGER,
    last_error TEXT,
    in_world INTEGER NOT NULL DEFAULT 1       -- 0 = außerhalb des aktuellen Radius
);
CREATE TABLE IF NOT EXISTS leases (
    id TEXT PRIMARY KEY,
    tile_key TEXT NOT NULL,
    worker_id TEXT NOT NULL,
    kind TEXT NOT NULL,                       -- gen | verify
    status TEXT NOT NULL,                     -- active | completed | expired | released | failed | superseded
    created REAL NOT NULL, expires REAL NOT NULL, updated REAL NOT NULL,
    phase TEXT, progress REAL DEFAULT 0, chunks_done INTEGER DEFAULT 0,
    cps REAL DEFAULT 0, eta INTEGER, message TEXT,
    bundle_hash TEXT
);
CREATE INDEX IF NOT EXISTS leases_tile ON leases(tile_key);
CREATE INDEX IF NOT EXISTS leases_status ON leases(status);
CREATE TABLE IF NOT EXISTS workers (
    id TEXT PRIMARY KEY,
    name TEXT UNIQUE NOT NULL,
    info TEXT,
    first_seen REAL, last_seen REAL,
    tiles_done INTEGER NOT NULL DEFAULT 0,
    chunks_done INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS priorities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dim TEXT NOT NULL,
    x0 INTEGER NOT NULL, z0 INTEGER NOT NULL, x1 INTEGER NOT NULL, z1 INTEGER NOT NULL,
    label TEXT, position INTEGER NOT NULL, created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS verify_copies (
    tile_key TEXT NOT NULL, lease_id TEXT NOT NULL, worker_id TEXT NOT NULL,
    path TEXT NOT NULL, sha256 TEXT, created REAL NOT NULL,
    PRIMARY KEY (tile_key, worker_id)
);
CREATE TABLE IF NOT EXISTS verify_results (
    tile_key TEXT PRIMARY KEY, result TEXT NOT NULL, created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (k TEXT PRIMARY KEY, v TEXT);
"""


class StateError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def now() -> float:
    return time.time()


class State:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        cfg.data_dir.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(str(cfg.db_path), check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript(SCHEMA)
        self.sync_tiles()

    # Hilfen -----------------------------------------------------------------
    def q(self, sql: str, *args) -> list[sqlite3.Row]:
        with self.lock:
            return self.db.execute(sql, args).fetchall()

    def x(self, sql: str, *args) -> None:
        with self.lock:
            self.db.execute(sql, args)

    def setting(self, k: str, default: str | None = None) -> str | None:
        rows = self.q("SELECT v FROM settings WHERE k=?", k)
        return rows[0]["v"] if rows else default

    def set_setting(self, k: str, v: str) -> None:
        self.x("INSERT INTO settings(k, v) VALUES(?, ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", k, v)

    @property
    def paused(self) -> bool:
        return self.setting("paused", "0") == "1"

    # Kacheln ----------------------------------------------------------------
    def all_tiles(self) -> list[Tile]:
        out = []
        for d in self.cfg.dimensions:
            out += tiles_for_dimension(d.name, d.radius, self.cfg.tile_regions)
        return out

    def sync_tiles(self) -> None:
        """Kacheltabelle an die Konfiguration anpassen (neue anlegen, nichts Fertiges löschen)."""
        with self.lock:
            stored = self.setting("tile_regions")
            if stored and int(stored) != self.cfg.tile_regions:
                n_done = self.q("SELECT COUNT(*) c FROM tiles WHERE status='done'")[0]["c"]
                if n_done:
                    raise StateError(
                        f"tile_regions wurde von {stored} auf {self.cfg.tile_regions} geändert, "
                        f"es sind aber schon {n_done} Kacheln fertig. Zurücksetzen mit "
                        "`bigworld-coord reset --all` oder alten Wert behalten."
                    )
                self.db.execute("DELETE FROM tiles")
            self.set_setting("tile_regions", str(self.cfg.tile_regions))
            wanted = {t.key: t for t in self.all_tiles()}
            existing = {r["key"]: r for r in self.db.execute("SELECT * FROM tiles")}
            self.db.execute("BEGIN")
            for key, t in wanted.items():
                if key in existing:
                    r = existing[key]
                    if (r["rx0"], r["rx1"], r["rz0"], r["rz1"]) != (t.rx0, t.rx1, t.rz0, t.rz1):
                        # Randkachel wurde durch größeren Radius größer: neu generieren
                        self.db.execute(
                            "UPDATE tiles SET rx0=?, rx1=?, rz0=?, rz1=?, in_world=1, "
                            "status=CASE WHEN status='done' THEN 'pending' ELSE status END WHERE key=?",
                            (t.rx0, t.rx1, t.rz0, t.rz1, key),
                        )
                    elif not r["in_world"]:
                        self.db.execute("UPDATE tiles SET in_world=1 WHERE key=?", (key,))
                else:
                    self.db.execute(
                        "INSERT INTO tiles(key, dim, tx, tz, rx0, rx1, rz0, rz1) VALUES(?,?,?,?,?,?,?,?)",
                        (key, t.dim, t.tx, t.tz, t.rx0, t.rx1, t.rz0, t.rz1),
                    )
            for key in set(existing) - set(wanted):
                self.db.execute("UPDATE tiles SET in_world=0 WHERE key=?", (key,))
            self.db.execute("COMMIT")
            self._recompute_priorities()

    def tile(self, key: str) -> Tile:
        rows = self.q("SELECT * FROM tiles WHERE key=?", key)
        if not rows:
            raise StateError(f"Unbekannte Kachel {key}", 404)
        return _tile_from_row(rows[0])

    # Prioritäten ------------------------------------------------------------
    def _recompute_priorities(self) -> None:
        with self.lock:
            self.db.execute("UPDATE tiles SET priority=NULL")
            prios = self.db.execute("SELECT * FROM priorities ORDER BY position, id").fetchall()
            tiles_by_dim: dict[str, list[Tile]] = {}
            for r in self.db.execute("SELECT * FROM tiles WHERE in_world=1"):
                tiles_by_dim.setdefault(r["dim"], []).append(_tile_from_row(r))
            for rank, p in enumerate(prios):
                for t in tiles_touching_area(tiles_by_dim.get(p["dim"], []), p["x0"], p["z0"], p["x1"], p["z1"]):
                    self.db.execute(
                        "UPDATE tiles SET priority=? WHERE key=? AND (priority IS NULL OR priority>?)",
                        (rank, t.key, rank),
                    )

    def add_priority(self, dim: str, x0: int, z0: int, x1: int, z1: int, label: str = "") -> dict:
        self.cfg.dimension(dim)
        with self.lock:
            pos = self.q("SELECT COALESCE(MAX(position), -1) + 1 AS p FROM priorities")[0]["p"]
            self.x(
                "INSERT INTO priorities(dim, x0, z0, x1, z1, label, position, created) VALUES(?,?,?,?,?,?,?,?)",
                dim, min(x0, x1), min(z0, z1), max(x0, x1), max(z0, z1), label, pos, now(),
            )
            self._recompute_priorities()
            pid = self.q("SELECT last_insert_rowid() AS i")[0]["i"]
            return {"id": pid}

    def delete_priority(self, pid: int) -> None:
        with self.lock:
            self.x("DELETE FROM priorities WHERE id=?", pid)
            self._recompute_priorities()

    def reorder_priorities(self, ids: list[int]) -> None:
        with self.lock:
            for pos, pid in enumerate(ids):
                self.x("UPDATE priorities SET position=? WHERE id=?", pos, pid)
            self._recompute_priorities()

    def preview_priority(self, dim: str, x0: int, z0: int, x1: int, z1: int) -> list[str]:
        tiles = [_tile_from_row(r) for r in self.q("SELECT * FROM tiles WHERE dim=? AND in_world=1", dim)]
        return [t.key for t in tiles_touching_area(tiles, x0, z0, x1, z1)]

    # Worker -----------------------------------------------------------------
    def register_worker(self, name: str, info: dict) -> str:
        name = name.strip()[:64]
        if not name:
            raise StateError("Workername fehlt")
        with self.lock:
            rows = self.q("SELECT id FROM workers WHERE name=?", name)
            if rows:
                wid = rows[0]["id"]
                self.x("UPDATE workers SET info=?, last_seen=? WHERE id=?", json.dumps(info), now(), wid)
            else:
                wid = uuid.uuid4().hex[:12]
                self.x(
                    "INSERT INTO workers(id, name, info, first_seen, last_seen) VALUES(?,?,?,?,?)",
                    wid, name, json.dumps(info), now(), now(),
                )
            return wid

    def touch_worker(self, wid: str) -> None:
        rows = self.q("SELECT id FROM workers WHERE id=?", wid)
        if not rows:
            raise StateError("Unbekannter Worker, bitte neu registrieren", 401)
        self.x("UPDATE workers SET last_seen=? WHERE id=?", now(), wid)

    # Leases -----------------------------------------------------------------
    def expire_leases(self) -> None:
        with self.lock:
            t = now()
            for r in self.q("SELECT * FROM leases WHERE status='active' AND expires<?", t):
                self.x("UPDATE leases SET status='expired', updated=? WHERE id=?", t, r["id"])
                if r["kind"] == "gen":
                    self.x(
                        "UPDATE tiles SET status='pending' WHERE key=? AND status='leased' AND NOT EXISTS "
                        "(SELECT 1 FROM leases WHERE tile_key=? AND status='active' AND kind='gen')",
                        r["tile_key"], r["tile_key"],
                    )

    def _timeout(self) -> float:
        return self.cfg.lease_timeout_minutes * 60

    # Template --------------------------------------------------------------
    def template_sha(self, bundle_hash: str) -> str | None:
        return self.setting(f"template:{bundle_hash}")

    def set_template(self, bundle_hash: str, sha: str) -> None:
        self.set_setting(f"template:{bundle_hash}", sha)

    def acquire(self, wid: str, bundle_hash: str, current_bundle: str | None) -> dict:
        if not current_bundle:
            raise StateError("Auf dem Koordinator ist noch kein Bundle gebaut", 503)
        if bundle_hash != current_bundle:
            raise StateError(
                f"Bundle passt nicht (Worker {bundle_hash[:12]}, Koordinator {current_bundle[:12]}). "
                "Worker muss neu synchronisieren.",
                409,
            )
        with self.lock:
            self.touch_worker(wid)
            self.expire_leases()
            if self.paused:
                return {"wait": True, "reason": "Generierung ist pausiert"}
            # Eine eventuell noch laufende eigene Lease zurückgeben (Neustart des Workers).
            # Leases, deren Ergebnis gerade hochgeladen wird, zählen nicht: der Worker
            # generiert währenddessen schon die nächste Kachel.
            own = self.q(
                "SELECT * FROM leases WHERE worker_id=? AND status='active' AND COALESCE(phase,'')!='upload' "
                "ORDER BY created",
                wid,
            )
            if own:
                return self._lease_response(own[0])

            # Ohne Vorlage-Welt für dieses Bundle wird zuerst die Vorlage erzeugt
            if not self.template_sha(bundle_hash):
                running = self.q("SELECT 1 FROM leases WHERE kind='template' AND status='active'")
                if running:
                    return {"wait": True, "reason": "Vorlage-Welt wird gerade von einem anderen Worker erzeugt"}
                return self._create_lease(wid, "template", "template", bundle_hash)

            verify = self._pick_verify(wid)
            if verify:
                return self._create_lease(wid, verify, "verify", bundle_hash)

            rows = self.q(
                "SELECT * FROM tiles WHERE status='pending' AND in_world=1 "
                "ORDER BY priority IS NULL, priority"
            )
            if rows:
                order = {d.name: i for i, d in enumerate(self.cfg.dimensions)}
                best = min(
                    rows,
                    key=lambda r: (
                        r["priority"] is None,
                        r["priority"] if r["priority"] is not None else 0,
                        order.get(r["dim"], 99),
                        _tile_from_row(r).distance_from_origin(),
                    ),
                )
                return self._create_lease(wid, best["key"], "gen", bundle_hash)

            busy = self.q("SELECT COUNT(*) c FROM tiles WHERE status IN ('leased','merging') AND in_world=1")[0]["c"]
            if busy:
                return {"wait": True, "reason": "Alle Kacheln sind vergeben, warte auf andere Worker"}
            failed = self.q("SELECT COUNT(*) c FROM tiles WHERE status='failed' AND in_world=1")[0]["c"]
            if failed:
                return {"wait": True, "reason": f"{failed} Kacheln fehlgeschlagen, bitte im Koordinator prüfen"}
            return {"done": True}

    def _pick_verify(self, wid: str) -> str | None:
        for key in self.cfg.verify_tiles:
            rows = self.q("SELECT status FROM tiles WHERE key=? AND in_world=1", key)
            if not rows:
                continue
            copies = self.q("SELECT worker_id FROM verify_copies WHERE tile_key=?", key)
            active = self.q("SELECT worker_id, kind FROM leases WHERE tile_key=? AND status='active'", key)
            involved = {r["worker_id"] for r in copies} | {r["worker_id"] for r in active}
            if wid in involved:
                continue
            # Zweite Generierung erst, wenn die erste läuft oder fertig ist
            if len(involved) == 1:
                return key
        return None

    def _create_lease(self, wid: str, tile_key: str, kind: str, bundle_hash: str) -> dict:
        lid = uuid.uuid4().hex
        t = now()
        self.x(
            "INSERT INTO leases(id, tile_key, worker_id, kind, status, created, expires, updated, phase, bundle_hash) "
            "VALUES(?,?,?,?, 'active', ?,?,?, 'assigned', ?)",
            lid, tile_key, wid, kind, t, t + self._timeout(), t, bundle_hash,
        )
        if kind == "gen":
            self.x("UPDATE tiles SET status='leased', attempts=attempts+1 WHERE key=?", tile_key)
        return self._lease_response(self.q("SELECT * FROM leases WHERE id=?", lid)[0])

    def _lease_response(self, lease: sqlite3.Row) -> dict:
        out = {
            "lease_id": lease["id"],
            "kind": lease["kind"],
            "bundle_hash": lease["bundle_hash"],
            "expires_in": int(lease["expires"] - now()),
            "heartbeat_seconds": 30,
        }
        if lease["kind"] == "template":
            return out
        tile = self.tile(lease["tile_key"])
        out.update({
            "tile": tile.to_dict(),
            "dim_path": self.cfg.dimension(tile.dim).path,
            "template_sha256": self.template_sha(lease["bundle_hash"]),
        })
        return out

    def lease(self, lid: str) -> sqlite3.Row:
        rows = self.q("SELECT * FROM leases WHERE id=?", lid)
        if not rows:
            raise StateError("Unbekannte Lease", 404)
        return rows[0]

    def heartbeat(self, wid: str, lid: str, data: dict) -> dict:
        with self.lock:
            self.touch_worker(wid)
            self.expire_leases()
            lease = self.lease(lid)
            if lease["worker_id"] != wid:
                raise StateError("Lease gehört einem anderen Worker", 403)
            if lease["status"] == "expired":
                # Kurz weg gewesen: wiederbeleben, wenn die Arbeit noch frei ist
                if lease["kind"] == "template":
                    free = not self.template_sha(lease["bundle_hash"]) and not self.q(
                        "SELECT 1 FROM leases WHERE kind='template' AND status='active'")
                else:
                    tile_row = self.q("SELECT status FROM tiles WHERE key=?", lease["tile_key"])[0]
                    free = lease["kind"] == "verify" or tile_row["status"] == "pending"
                if free:
                    self.x("UPDATE leases SET status='active' WHERE id=?", lid)
                    if lease["kind"] == "gen":
                        self.x("UPDATE tiles SET status='leased' WHERE key=?", lease["tile_key"])
                    lease = self.lease(lid)
            if lease["status"] != "active":
                return {"ok": False, "abort": True, "reason": f"Lease ist {lease['status']}"}
            t = now()
            self.x(
                "UPDATE leases SET expires=?, updated=?, phase=?, progress=?, chunks_done=?, cps=?, eta=?, message=? WHERE id=?",
                t + self._timeout(), t,
                str(data.get("phase", ""))[:32],
                float(data.get("progress", 0) or 0),
                int(data.get("chunks_done", 0) or 0),
                float(data.get("cps", 0) or 0),
                data.get("eta"),
                str(data.get("message", ""))[:300],
                lid,
            )
            return {"ok": True, "paused": self.paused}

    def release(self, wid: str, lid: str, reason: str, failed: bool = False) -> None:
        with self.lock:
            lease = self.lease(lid)
            if lease["worker_id"] != wid:
                raise StateError("Lease gehört einem anderen Worker", 403)
            if lease["status"] != "active":
                return
            self.x(
                "UPDATE leases SET status=?, message=?, updated=? WHERE id=?",
                "failed" if failed else "released", reason[:300], now(), lid,
            )
            if lease["kind"] == "gen":
                self.x(
                    "UPDATE tiles SET status='pending', last_error=? WHERE key=? AND status='leased'",
                    reason[:300] if failed else None, lease["tile_key"],
                )
                attempts = self.q("SELECT attempts FROM tiles WHERE key=?", lease["tile_key"])[0]["attempts"]
                if failed and attempts >= 5:
                    self.x("UPDATE tiles SET status='failed' WHERE key=?", lease["tile_key"])

    def finish_template(self, lease: sqlite3.Row, sha: str) -> None:
        with self.lock:
            self.set_template(lease["bundle_hash"], sha)
            self.x("UPDATE leases SET status='completed', phase='done', progress=1, updated=? WHERE id=?",
                   now(), lease["id"])

    def touch_upload(self, lid: str) -> None:
        self.x("UPDATE leases SET expires=?, updated=?, phase='upload' WHERE id=? AND status='active'",
               now() + self._timeout(), now(), lid)

    # Abschluss --------------------------------------------------------------
    def begin_complete(self, wid: str, lid: str) -> tuple[sqlite3.Row, str]:
        """Prüft, ob die Lease abgeschlossen werden darf. Gibt (lease, Aktion) zurück:
        'merge', 'verify' oder 'duplicate' (Kachel schon fertig, Paket verwerfen)."""
        with self.lock:
            lease = self.lease(lid)
            if lease["worker_id"] != wid:
                raise StateError("Lease gehört einem anderen Worker", 403)
            if lease["status"] not in ("active", "expired"):
                raise StateError(f"Lease ist {lease['status']}", 409)
            if lease["kind"] == "verify":
                return lease, "verify"
            if lease["kind"] == "template":
                if self.template_sha(lease["bundle_hash"]):
                    self.x("UPDATE leases SET status='superseded', updated=? WHERE id=?", now(), lid)
                    return lease, "duplicate"
                return lease, "template"
            st = self.q("SELECT status FROM tiles WHERE key=?", lease["tile_key"])[0]["status"]
            if st in ("done", "merging"):
                self.x("UPDATE leases SET status='superseded', updated=? WHERE id=?", now(), lid)
                return lease, "duplicate"
            self.x("UPDATE tiles SET status='merging' WHERE key=?", lease["tile_key"])
            self.x("UPDATE leases SET status='completed', phase='merging', progress=1, updated=? WHERE id=?", now(), lid)
            # Andere aktive Generierungen dieser Kachel abbrechen
            self.x(
                "UPDATE leases SET status='superseded', updated=? WHERE tile_key=? AND id!=? AND status='active' AND kind='gen'",
                now(), lease["tile_key"], lid,
            )
            return lease, "merge"

    def finish_merge(self, lease: sqlite3.Row, size: int, ok: bool, error: str = "") -> None:
        with self.lock:
            if ok:
                tile = self.tile(lease["tile_key"])
                self.x(
                    "UPDATE tiles SET status='done', done_at=?, done_by=?, bundle_hash=?, size=?, last_error=NULL WHERE key=?",
                    now(), lease["worker_id"], lease["bundle_hash"], size, lease["tile_key"],
                )
                self.x(
                    "UPDATE workers SET tiles_done=tiles_done+1, chunks_done=chunks_done+? WHERE id=?",
                    tile.chunk_count, lease["worker_id"],
                )
            else:
                self.x("UPDATE tiles SET status='pending', last_error=? WHERE key=?", error[:300], lease["tile_key"])
                self.x("UPDATE leases SET status='failed', message=? WHERE id=?", error[:300], lease["id"])

    def record_verify_copy(self, lease: sqlite3.Row, path: Path, sha: str) -> int:
        with self.lock:
            self.x(
                "INSERT OR REPLACE INTO verify_copies(tile_key, lease_id, worker_id, path, sha256, created) VALUES(?,?,?,?,?,?)",
                lease["tile_key"], lease["id"], lease["worker_id"], str(path), sha, now(),
            )
            if lease["kind"] == "verify":
                self.x("UPDATE leases SET status='completed', phase='done', progress=1, updated=? WHERE id=?", now(), lease["id"])
            return self.q("SELECT COUNT(*) c FROM verify_copies WHERE tile_key=?", lease["tile_key"])[0]["c"]

    def save_verify_result(self, tile_key: str, result: dict) -> None:
        self.x(
            "INSERT OR REPLACE INTO verify_results(tile_key, result, created) VALUES(?,?,?)",
            tile_key, json.dumps(result), now(),
        )

    # Verwaltung -------------------------------------------------------------
    def reset_tiles(self, keys: list[str] | None) -> int:
        with self.lock:
            if keys is None:
                n = self.q("SELECT COUNT(*) c FROM tiles")[0]["c"]
                self.x("UPDATE tiles SET status='pending', attempts=0, done_at=NULL, done_by=NULL, bundle_hash=NULL, size=NULL, last_error=NULL")
                self.x("UPDATE leases SET status='superseded' WHERE status='active'")
                self.x("DELETE FROM verify_copies")
                self.x("DELETE FROM verify_results")
                return n
            for k in keys:
                self.tile(k)
                self.x("UPDATE tiles SET status='pending', attempts=0, last_error=NULL WHERE key=?", k)
                self.x("UPDATE leases SET status='superseded' WHERE tile_key=? AND status='active'", k)
            return len(keys)

    def done_bundle_hashes(self) -> set[str]:
        return {r["bundle_hash"] for r in self.q("SELECT DISTINCT bundle_hash FROM tiles WHERE status='done'")}

    # Status -----------------------------------------------------------------
    def status(self, bundle_hash: str | None = None) -> dict:
        with self.lock:
            template = {"sha256": self.template_sha(bundle_hash) if bundle_hash else None}
            tl = self.q("SELECT l.*, w.name FROM leases l LEFT JOIN workers w ON w.id=l.worker_id "
                        "WHERE l.kind='template' AND l.status='active'")
            template["running_on"] = tl[0]["name"] if tl else None
            self.expire_leases()
            t = now()
            workers = {r["id"]: r for r in self.q("SELECT * FROM workers")}
            active = self.q("SELECT * FROM leases WHERE status='active'")
            active_by_tile: dict[str, list] = {}
            for l in active:
                active_by_tile.setdefault(l["tile_key"], []).append(l)
            tiles_out = []
            totals = {"chunks_total": 0, "chunks_done": 0, "chunks_running": 0, "tiles": {}}
            for r in self.q("SELECT * FROM tiles WHERE in_world=1"):
                tile = _tile_from_row(r)
                leases = active_by_tile.get(r["key"], [])
                gen = next((l for l in leases if l["kind"] == "gen"), None)
                entry = {
                    "key": r["key"], "dim": r["dim"], "tx": r["tx"], "tz": r["tz"],
                    "rx0": r["rx0"], "rx1": r["rx1"], "rz0": r["rz0"], "rz1": r["rz1"],
                    "status": r["status"], "priority": r["priority"], "attempts": r["attempts"],
                    "error": r["last_error"],
                    "done_by": workers[r["done_by"]]["name"] if r["done_by"] in workers else None,
                    "verify": r["key"] in self.cfg.verify_tiles,
                }
                if gen:
                    entry["worker"] = workers[gen["worker_id"]]["name"] if gen["worker_id"] in workers else "?"
                    entry["progress"] = gen["progress"]
                    entry["phase"] = gen["phase"]
                tiles_out.append(entry)
                totals["chunks_total"] += tile.chunk_count
                totals["tiles"][r["status"]] = totals["tiles"].get(r["status"], 0) + 1
                if r["status"] == "done":
                    totals["chunks_done"] += tile.chunk_count
                elif gen:
                    totals["chunks_running"] += int(tile.chunk_count * (gen["progress"] or 0))
            workers_out = []
            total_cps = 0.0
            for w in workers.values():
                mine = [l for l in active if l["worker_id"] == w["id"]]
                lease = next((l for l in mine if l["phase"] != "upload"), mine[0] if mine else None)
                uploads = sum(1 for l in mine if l["phase"] == "upload")
                online = t - (w["last_seen"] or 0) < 120
                cps = lease["cps"] if lease and online else 0
                total_cps += cps or 0
                info = json.loads(w["info"] or "{}")
                workers_out.append({
                    "id": w["id"], "name": w["name"], "online": online,
                    "last_seen": w["last_seen"], "tiles_done": w["tiles_done"], "chunks_done": w["chunks_done"],
                    "gpu": info.get("gpu"), "driver": info.get("driver"), "os": info.get("os"),
                    "ram_gb": info.get("ram_gb"), "version": info.get("version"),
                    "overrides": info.get("overrides", []),
                    "tile": lease["tile_key"] if lease else None,
                    "kind": lease["kind"] if lease else None,
                    "phase": lease["phase"] if lease else None,
                    "progress": lease["progress"] if lease else None,
                    "cps": cps, "eta": lease["eta"] if lease else None,
                    "message": lease["message"] if lease else None,
                    "uploads": uploads,
                })
            remaining = totals["chunks_total"] - totals["chunks_done"] - totals["chunks_running"]
            totals["cps"] = total_cps
            totals["eta_seconds"] = int(remaining / total_cps) if total_cps > 0 else None
            gpus = {(w["gpu"], w["driver"]) for w in workers_out if w["online"] and w["gpu"]}
            warnings = []
            if len(gpus) > 1:
                warnings.append(
                    "Online-Worker nutzen unterschiedliche GPUs oder Treiber: "
                    + ", ".join(f"{g} ({d})" for g, d in sorted(gpus, key=str))
                    + ". Determinismus-Test beachten."
                )
            return {
                "tile_regions": self.cfg.tile_regions,
                "dimensions": [
                    {"name": d.name, "radius": d.radius, "path": d.path} for d in self.cfg.dimensions
                ],
                "tiles": tiles_out,
                "workers": sorted(workers_out, key=lambda w: (not w["online"], w["name"])),
                "priorities": [dict(r) for r in self.q("SELECT * FROM priorities ORDER BY position, id")],
                "verify": [
                    {"tile": r["tile_key"], **json.loads(r["result"]), "created": r["created"]}
                    for r in self.q("SELECT * FROM verify_results")
                ],
                "verify_copies": [
                    {"tile": r["tile_key"], "worker": workers[r["worker_id"]]["name"] if r["worker_id"] in workers else "?"}
                    for r in self.q("SELECT tile_key, worker_id FROM verify_copies")
                ],
                "totals": totals,
                "template": template,
                "paused": self.paused,
                "warnings": warnings,
                "time": t,
            }


def _tile_from_row(r: sqlite3.Row) -> Tile:
    return Tile(r["dim"], r["tx"], r["tz"], r["rx0"], r["rx1"], r["rz0"], r["rz1"])
