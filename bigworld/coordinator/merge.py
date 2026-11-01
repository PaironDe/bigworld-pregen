"""Hochgeladene Kachelpakete in die Staging-Welt einsortieren."""

from __future__ import annotations

import logging
import os
import shutil
import tarfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from ..common import package
from ..common.compare import compare_packages
from ..common.hashing import join_safe
from . import bundle
from .config import Config
from .state import State

log = logging.getLogger("bigworld.merge")

_merge_lock = threading.Lock()


def init_staging(cfg: Config, bundle_hash: str) -> None:
    """Vorlage-Welt (level.dat, datapacks, data/) in die Staging-Welt legen."""
    from .template import template_dir

    world = cfg.staging_world
    world.mkdir(parents=True, exist_ok=True)
    src = template_dir(cfg, bundle_hash) / "world"
    if not (src / "level.dat").is_file():
        raise ValueError("Für dieses Bundle gibt es noch keine Vorlage-Welt")
    for p in sorted(src.rglob("*")):
        if p.is_file():
            target = world / p.relative_to(src)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, target)
    (cfg.data_dir / "staging" / "bundle").write_text(bundle_hash + "\n")
    log.info("Staging-Welt mit Template aus Bundle %s angelegt", bundle_hash[:12])


# Sperre für den Deploy: Der Deploy verschiebt die Staging-Welt ins Volume. Solange die Sperrdatei
# existiert, wartet das Zusammenführen; laufende Zusammenführungen melden sich mit einer Markierung.
def _lock_path(cfg: Config) -> Path:
    return cfg.data_dir / "staging.lock"


def _marker_dir(cfg: Config) -> Path:
    return cfg.data_dir / "merging"


def _lock_active(cfg: Config) -> bool:
    lock = _lock_path(cfg)
    try:
        pid = int(lock.read_text().strip() or 0)
    except (OSError, ValueError):
        return lock.exists()
    if pid:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            log.warning("Verwaiste Staging-Sperre (Prozess %d existiert nicht mehr) entfernt", pid)
            lock.unlink(missing_ok=True)
            return False
        except PermissionError:
            pass
    return True


def _enter_merge(cfg: Config, name: str) -> Path:
    d = _marker_dir(cfg)
    d.mkdir(parents=True, exist_ok=True)
    marker = d / name
    waited = False
    while True:
        while _lock_active(cfg):
            if not waited:
                log.info("Deploy läuft, Zusammenführen von %s wartet", name)
                waited = True
            time.sleep(5)
        marker.write_text("")
        if not _lock_active(cfg):
            return marker
        marker.unlink(missing_ok=True)


@contextmanager
def staging_locked(cfg: Config, logf=print, timeout: int = 900):
    """Für den Deploy: neue Zusammenführungen anhalten und laufende abwarten."""
    lock = _lock_path(cfg)
    lock.write_text(str(os.getpid()))
    try:
        end = time.time() + timeout
        d = _marker_dir(cfg)
        while d.is_dir() and any(d.iterdir()):
            if time.time() > end:
                raise TimeoutError("Zusammenführen läuft seit über 15 Minuten, Abbruch")
            logf("   warte auf laufendes Zusammenführen ...")
            time.sleep(3)
        yield
    finally:
        lock.unlink(missing_ok=True)


def _write_into_staging(cfg: Config, lease, tar_path: Path, members) -> None:
    if (not (cfg.staging_world / "level.dat").exists()
            or bundle_for_staging(cfg) != lease["bundle_hash"]):
        # Neue Welt oder neues Bundle: level.dat, data/ und datapacks/ aus dessen Vorlage
        init_staging(cfg, lease["bundle_hash"])
    with tarfile.open(tar_path, "r") as tar:
        for m in members:
            target = join_safe(cfg.staging_world, m.name)
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_name(target.name + ".tmp")
            src = tar.extractfile(m)
            if src is None:
                raise ValueError(f"{m.name} nicht lesbar")
            with open(tmp, "wb") as out:
                shutil.copyfileobj(src, out, 1024 * 1024)
            os.replace(tmp, target)


def merge_package(cfg: Config, state: State, lease, tar_path: Path) -> None:
    """Läuft in einem Hintergrund-Thread."""
    tile = state.tile(lease["tile_key"])
    dim_path = cfg.dimension(tile.dim).path
    size = tar_path.stat().st_size
    try:
        members = package.validate_package(tar_path, tile, dim_path)
        with _merge_lock:
            marker = _enter_merge(cfg, lease["id"])
            try:
                _write_into_staging(cfg, lease, tar_path, members)
                # Noch unter der Markierung: ein Deploy sieht die Kachel entweder gar nicht
                # oder fertig und in der Staging-Welt
                state.finish_merge(lease, size, True)
            finally:
                marker.unlink(missing_ok=True)
        if lease["tile_key"] in cfg.verify_tiles:
            keep_verify_copy(cfg, state, lease, tar_path)
        else:
            tar_path.unlink(missing_ok=True)
        log.info("Kachel %s zusammengeführt (%d Dateien, %.1f MB)", tile.key, len(members), size / 1e6)
    except Exception as e:  # noqa: BLE001 - Fehler landet im Status
        log.exception("Zusammenführen von %s fehlgeschlagen", tile.key)
        state.finish_merge(lease, size, False, f"Zusammenführen fehlgeschlagen: {e}")
        bad = cfg.incoming_dir / "rejected"
        bad.mkdir(parents=True, exist_ok=True)
        if tar_path.exists():
            tar_path.replace(bad / tar_path.name)


def keep_verify_copy(cfg: Config, state: State, lease, tar_path: Path) -> None:
    tile = state.tile(lease["tile_key"])
    if lease["kind"] == "verify":
        # Auch Vergleichskopien müssen zur Kachel passen
        package.validate_package(tar_path, tile, cfg.dimension(tile.dim).path)
    d = cfg.verify_dir / tile.key.replace("|", "_").replace(":", "-")
    d.mkdir(parents=True, exist_ok=True)
    dest = d / f"{lease['worker_id']}.tar"
    tar_path.replace(dest)
    from ..common.hashing import sha256_file

    n = state.record_verify_copy(lease, dest, sha256_file(dest))
    if n >= 2:
        threading.Thread(target=run_compare, args=(cfg, state, tile.key), daemon=True).start()


def run_compare(cfg: Config, state: State, tile_key: str) -> dict:
    rows = state.q("SELECT * FROM verify_copies WHERE tile_key=? ORDER BY created", tile_key)
    if len(rows) < 2:
        raise ValueError("Für den Vergleich werden zwei Kopien gebraucht")
    a, b = rows[0], rows[1]
    names = {r["id"]: r["name"] for r in state.q("SELECT id, name FROM workers")}
    names[STAGING_COPY] = "Bestand (Staging-Welt)"
    log.info("Vergleiche %s: %s gegen %s", tile_key, a["worker_id"], b["worker_id"])
    try:
        rep = compare_packages(Path(a["path"]), Path(b["path"])).to_dict()
    except Exception as e:  # noqa: BLE001
        rep = {"deterministic": False, "error": str(e)}
    rep["worker_a"] = names.get(a["worker_id"], a["worker_id"])
    rep["worker_b"] = names.get(b["worker_id"], b["worker_id"])
    state.save_verify_result(tile_key, rep)
    log.info("Vergleich %s: %s", tile_key, "identisch" if rep.get("deterministic") else "UNTERSCHIEDE")
    return rep


STAGING_COPY = "staging"


def create_recheck(cfg: Config, state: State, tile_key: str, worker_id: str) -> int:
    """Stichprobe anlegen: Bestand der Kachel aus der Staging-Welt als Vergleichskopie ablegen.

    Der Worker, der die Kachel ursprünglich generiert hat, generiert sie mit dem aktuellen
    Bundle noch einmal; danach werden beide Fassungen verglichen. Gibt die Dateianzahl zurück.
    """
    from ..common.hashing import sha256_file
    from .state import now

    tile = state.tile(tile_key)
    dim_path = cfg.dimension(tile.dim).path
    files = package.collect_tile_files(cfg.staging_world, dim_path, tile)
    if not any(rel.split("/")[-2] == "region" for rel, _ in files):
        raise ValueError(f"{tile_key}: keine Regionsdateien in der Staging-Welt")
    d = cfg.verify_dir / tile.key.replace("|", "_").replace(":", "-")
    d.mkdir(parents=True, exist_ok=True)
    for old in d.glob("*.tar"):
        old.unlink()
    dest = d / f"{STAGING_COPY}.tar"
    package.write_package(dest, {"tile": tile.to_dict(), "source": "staging"}, files)
    state.add_recheck(tile_key, worker_id)
    state.x("INSERT OR REPLACE INTO verify_copies(tile_key, lease_id, worker_id, path, sha256, created) "
            "VALUES(?,?,?,?,?,?)", tile_key, "staging", STAGING_COPY, str(dest), sha256_file(dest), now())
    return len(files)


def bundle_for_staging(cfg: Config) -> str | None:
    p = cfg.data_dir / "staging" / "bundle"
    return p.read_text().strip() if p.exists() else None


__all__ = ["init_staging", "merge_package", "keep_verify_copy", "run_compare", "bundle", "bundle_for_staging",
           "create_recheck", "STAGING_COPY", "staging_locked"]
