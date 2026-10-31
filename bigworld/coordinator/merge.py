"""Hochgeladene Kachelpakete in die Staging-Welt einsortieren."""

from __future__ import annotations

import logging
import os
import shutil
import tarfile
import threading
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
    """Template-Dateien (level.dat, datapacks, Weltgen-Einstellungen) in die Staging-Welt legen."""
    world = cfg.staging_world
    world.mkdir(parents=True, exist_ok=True)
    src = cfg.bundles_dir / bundle_hash / "files" / "template"
    for p in sorted(src.rglob("*")):
        if p.is_file():
            target = world / p.relative_to(src)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, target)
    (cfg.data_dir / "staging" / "bundle").write_text(bundle_hash + "\n")
    log.info("Staging-Welt mit Template aus Bundle %s angelegt", bundle_hash[:12])


def merge_package(cfg: Config, state: State, lease, tar_path: Path) -> None:
    """Läuft in einem Hintergrund-Thread."""
    tile = state.tile(lease["tile_key"])
    dim_path = cfg.dimension(tile.dim).path
    size = tar_path.stat().st_size
    try:
        members = package.validate_package(tar_path, tile, dim_path)
        with _merge_lock:
            if not (cfg.staging_world / "level.dat").exists():
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
        if lease["tile_key"] in cfg.verify_tiles:
            keep_verify_copy(cfg, state, lease, tar_path)
        else:
            tar_path.unlink(missing_ok=True)
        state.finish_merge(lease, size, True)
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


def bundle_for_staging(cfg: Config) -> str | None:
    p = cfg.data_dir / "staging" / "bundle"
    return p.read_text().strip() if p.exists() else None


__all__ = ["init_staging", "merge_package", "keep_verify_copy", "run_compare", "bundle", "bundle_for_staging"]
