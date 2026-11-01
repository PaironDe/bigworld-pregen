"""Live-Merge: fertige Kacheln nach dem ersten Deploy in die laufende Welt übernehmen.

Ablauf (nächtlich per systemd-Timer oder per Befehl):
  Ankündigung im Chat -> save-all -> Server stoppen -> nur fehlende Chunks einfügen
  -> Besitzer setzen -> Server starten

Grundsätze:
- Nur das freigegebene Volume, nur bei gestopptem Server.
- Chunks, die es in der Live-Welt schon vollständig gibt, werden nie überschrieben
  (dort kann gebaut worden sein). Ersetzt werden nur fehlende und unfertige Chunks.
- Läuft erst, wenn ein Deploy die Welt eingesetzt hat (Markierungsdatei) und der Seed passt.
"""

from __future__ import annotations

import gzip
import json
import os
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

from ..common import anvil, nbt, package
from ..common.tiles import Tile
from .config import ALLOWED_VOLUME_WORLD, Config
from .state import State

MARKER = "bigworld-deploy.json"
CONFIRM_TEXT = "EINFÜGEN"


class LiveMergeError(Exception):
    pass


# Welt-Prüfungen -------------------------------------------------------------

def world_seed(world: Path) -> int | None:
    p = world / "data" / "minecraft" / "world_gen_settings.dat"
    if not p.is_file():
        return None
    data = p.read_bytes()
    if data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)
    root = nbt.parse(data)

    def find(obj):
        if isinstance(obj, dict):
            for k, v in obj.items():
                if k.lower() == "seed" and isinstance(v, int):
                    return v
                r = find(v)
                if r is not None:
                    return r
        return None

    return find(root)


def write_marker(world: Path, bundle_hash: str | None) -> None:
    info = {"deployed_at": time.time(), "bundle": bundle_hash, "seed": world_seed(world)}
    (world / MARKER).write_text(json.dumps(info, indent=2) + "\n")


def read_marker(world: Path) -> dict | None:
    p = world / MARKER
    try:
        return json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return None


# Chunkweises Zusammenführen ------------------------------------------------

@dataclass
class MergeStats:
    regions_new: int = 0
    regions_merged: int = 0
    chunks_added: int = 0
    chunks_kept: int = 0
    files: list[Path] = field(default_factory=list)


def _is_full(region: anvil.RegionFile, index: int) -> bool:
    """Vorhandener Live-Chunk fertig? Im Zweifel ja: lieber eine Lücke lassen als Gebautes überschreiben
    (z. B. LZ4-komprimierte oder unlesbare Chunks werden nie ersetzt)."""
    try:
        data = region.chunk_bytes(index)
    except Exception:  # noqa: BLE001
        return True
    if data is None:
        return False
    try:
        status = anvil.chunk_status(data)
    except Exception:  # noqa: BLE001
        return True
    return status is None or status in anvil.FULL_STATUSES


def _copy_external(src_dir: Path, dst_dir: Path, rx: int, rz: int, index: int, stats: MergeStats) -> None:
    cx, cz = rx * 32 + (index % 32), rz * 32 + (index // 32)
    name = f"c.{cx}.{cz}.mcc"
    src = src_dir / name
    if not src.is_file():
        raise LiveMergeError(f"Externe Chunkdatei {src} fehlt")
    tmp = dst_dir / (name + ".bwtmp")
    shutil.copyfile(src, tmp)
    os.replace(tmp, dst_dir / name)
    stats.files.append(dst_dir / name)


def merge_region_set(src_dim: Path, dst_dim: Path, rx: int, rz: int, stats: MergeStats) -> None:
    """Eine Region (region/, entities/, poi/) aus der Staging- in die Live-Welt übernehmen."""
    name = f"r.{rx}.{rz}.mca"
    src_region = src_dim / "region" / name
    if not src_region.is_file():
        return
    dst_region = dst_dim / "region" / name
    if not dst_region.is_file() or dst_region.stat().st_size == 0:
        # Region gibt es live noch nicht: alle drei Dateien komplett übernehmen
        for storage in package.STORAGES:
            s = src_dim / storage / name
            if not s.is_file():
                continue
            d = dst_dim / storage
            d.mkdir(parents=True, exist_ok=True)
            reg = anvil.RegionFile(s)
            for ext in reg.external_files():
                idx = anvil.chunk_index(int(ext.name.split(".")[1]), int(ext.name.split(".")[2]))
                _copy_external(s.parent, d, rx, rz, idx, stats)
            tmp = d / (name + ".bwtmp")
            shutil.copyfile(s, tmp)
            os.replace(tmp, d / name)
            stats.files.append(d / name)
        stats.regions_new += 1
        stats.chunks_added += sum(1 for i in range(1024) if anvil.RegionFile(src_region).has_chunk(i))
        return

    # Region existiert live: nur fehlende oder unfertige Chunks übernehmen
    live = anvil.RegionFile(dst_region)
    new = anvil.RegionFile(src_region)
    take = []
    for i in range(1024):
        if not new.has_chunk(i):
            continue
        if live.has_chunk(i) and _is_full(live, i):
            stats.chunks_kept += 1
        else:
            take.append(i)
    if not take:
        return
    for storage in package.STORAGES:
        s = src_dim / storage / name
        d_dir = dst_dim / storage
        d = d_dir / name
        src_reg = anvil.RegionFile(s) if s.is_file() else None
        dst_reg = anvil.RegionFile(d) if d.is_file() and d.stat().st_size > 0 else None
        entries: dict[int, tuple[bytes, int]] = {}
        if dst_reg is not None:
            for i in range(1024):
                b = dst_reg.blob(i)
                if b is not None:
                    entries[i] = (b, dst_reg.timestamp(i))
        changed = False
        for i in take:
            b = src_reg.blob(i) if src_reg is not None else None
            if b is None:
                continue
            if b[4] & anvil.EXTERNAL_FLAG:
                d_dir.mkdir(parents=True, exist_ok=True)
                _copy_external(s.parent, d_dir, rx, rz, i, stats)
            entries[i] = (b, src_reg.timestamp(i))
            changed = True
        if changed:
            d_dir.mkdir(parents=True, exist_ok=True)
            anvil.write_region_blobs(d, entries)
            stats.files.append(d)
    stats.regions_merged += 1
    stats.chunks_added += len(take)


def merge_tile(staging: Path, live: Path, dim_path: str, tile: Tile, stats: MergeStats) -> None:
    src_dim = staging / dim_path if dim_path else staging
    dst_dim = live / dim_path if dim_path else live
    for rx, rz in tile.regions():
        merge_region_set(src_dim, dst_dim, rx, rz, stats)


def remove_tile_from_staging(staging: Path, dim_path: str, tile: Tile) -> None:
    for _, path in package.collect_tile_files(staging, dim_path, tile):
        path.unlink(missing_ok=True)


# Mods abgleichen -----------------------------------------------------------

def compare_mods(cfg: Config, bundle_hash: str, live_mods: Path) -> tuple[list[str], list[str]]:
    """(Fehler, Hinweise): Der Live-Server muss die Mods des Bundles in gleicher Version haben,
    sonst passen später selbst generierte Chunks nicht zu den eingefügten."""
    from . import bundle

    manifest = bundle.load_manifest(cfg, bundle_hash)
    wanted = {m["id"]: m["version"] for m in manifest["mods"]
              if m.get("id") and m["id"] not in bundle.GENERATION_HELPER_IDS}
    have: dict[str, str] = {}
    for jar in sorted(live_mods.glob("*.jar")):
        info = bundle.mod_info(jar)
        if info["id"]:
            have[info["id"]] = info["version"]
    errors, notes = [], []
    for mid, ver in sorted(wanted.items()):
        if mid not in have:
            errors.append(f"{mid} {ver} fehlt auf dem Live-Server")
        elif have[mid] != ver:
            errors.append(f"{mid}: Live {have[mid]}, Bundle {ver}")
    extra = sorted(set(have) - set(wanted) - bundle.GENERATION_HELPER_IDS)
    if extra:
        notes.append("Nur auf dem Live-Server (ändern das Gelände nicht, sonst bitte prüfen): " + ", ".join(extra))
    return errors, notes


@dataclass
class ModSync:
    add: list[Path]       # Jars aus dem Bundle, die live fehlen oder anders sind
    remove: list[Path]    # Live-Jars, die durch eine Bundle-Version ersetzt werden
    keep: list[Path]      # Live-Jars, die bleiben (gleich wie Bundle oder nur live, z. B. LSS)


def plan_mod_sync(cfg: Config, bundle_hash: str, live_mods: Path) -> ModSync:
    """Live-Mods an das Bundle angleichen; Mods, die es nur live gibt (LSS, Lithium ...), bleiben."""
    from . import bundle

    manifest = bundle.load_manifest(cfg, bundle_hash)
    wanted = {m["id"]: (m["version"], bundle.file_path(cfg, bundle_hash, f"mods/{m['file']}"))
              for m in manifest["mods"]
              if m.get("id") and m["id"] not in ("chunky",)}
    live = {}
    for jar in sorted(live_mods.glob("*.jar")) if live_mods.is_dir() else []:
        info = bundle.mod_info(jar)
        live[jar] = info
    add, remove, keep = [], [], []
    have = {}
    for jar, info in live.items():
        mid = info["id"]
        if mid in wanted and wanted[mid][0] != info["version"]:
            remove.append(jar)
        else:
            keep.append(jar)
            have[mid] = info["version"]
    for mid, (ver, src) in sorted(wanted.items()):
        if have.get(mid) != ver:
            add.append(src)
    return ModSync(add, remove, keep)


def apply_mod_sync(plan: ModSync, live_mods: Path, owner: str, log=print) -> Path:
    """Alte Mods komplett nach mods.bak-<Zeit> sichern, dann neue Liste einsetzen."""
    import grp
    import pwd

    backup = live_mods.with_name(f"mods.bak-{time.strftime('%Y%m%d-%H%M%S')}")
    shutil.copytree(live_mods, backup)
    for jar in plan.remove:
        jar.unlink()
        log(f"   entfernt: {jar.name}")
    uid, gid = pwd.getpwnam(owner).pw_uid, grp.getgrnam(owner).gr_gid
    for src in plan.add:
        dest = live_mods / src.name
        shutil.copy2(src, dest)
        os.chown(dest, uid, gid)
        log(f"   neu:      {src.name}")
    for p in [backup, *backup.rglob("*")]:
        os.chown(p, uid, gid)
    return backup


# Ablauf ----------------------------------------------------------------------

def _chown_tree(paths: list[Path], owner: str) -> None:
    import grp
    import pwd

    uid = pwd.getpwnam(owner).pw_uid
    gid = grp.getgrnam(owner).gr_gid
    done: set[Path] = set()
    for p in paths:
        for q in [p, *p.parents]:
            if q in done or not str(q).startswith(ALLOWED_VOLUME_WORLD):
                continue
            try:
                os.chown(q, uid, gid)
            except FileNotFoundError:
                pass
            done.add(q)


def run(cfg: Config, state: State, scheduled: bool, ignore_mods: bool = False, dry_run: bool = False,
        ask=input, log=print) -> int:
    from . import bundle
    from .deploy import Panel

    lm = cfg.live_merge
    if scheduled and not lm.enabled:
        log("Live-Merge ist in der Konfiguration nicht freigegeben ([live_merge] enabled = false). Nichts getan.")
        return 0
    if os.geteuid() != 0 and not dry_run:
        raise LiveMergeError("Live-Merge muss mit sudo laufen (Dateien gehören pterodactyl)")
    vol_world = Path(cfg.deploy.volume_world)
    if str(vol_world) != ALLOWED_VOLUME_WORLD or vol_world.resolve() != Path(ALLOWED_VOLUME_WORLD).resolve():
        raise LiveMergeError(f"Ziel {vol_world} ist nicht das freigegebene Volume")
    marker = read_marker(vol_world)
    if not marker:
        raise LiveMergeError("Die Live-Welt wurde noch nicht per Deploy eingesetzt (bigworld-deploy.json fehlt). "
                             "Erst `bigworld-coord deploy`.")
    staging = cfg.staging_world
    tiles = state.tiles_for_live()
    if not tiles:
        log("Keine neuen fertigen Kacheln. Nichts zu tun.")
        return 0
    if lm.max_tiles > 0:
        tiles = tiles[: lm.max_tiles]
    live_seed, staging_seed = world_seed(vol_world), world_seed(staging)
    if live_seed is None or staging_seed is None or live_seed != staging_seed:
        raise LiveMergeError(f"Seed passt nicht (Live {live_seed}, Staging {staging_seed}). Abbruch.")
    h = bundle.current_hash(cfg)
    errors, notes = compare_mods(cfg, h, vol_world.parent / "mods") if h else (["kein aktives Bundle"], [])
    for n in notes:
        log(f"Hinweis: {n}")
    if errors:
        for e in errors:
            log(f"Mod-Unterschied: {e}")
        if not ignore_mods:
            raise LiveMergeError("Live-Server und Bundle haben unterschiedliche Mods. Erst angleichen "
                                 "(oder bewusst mit --ignore-mods).")

    chunks = sum(t.chunk_count for t in tiles)
    log(f"Live-Merge: {len(tiles)} Kacheln (bis {chunks:,} Chunks) nach {vol_world}")
    for t in tiles:
        log(f"  {t.key}  x {t.x0}..{t.x1 - 1}  z {t.z0}..{t.z1 - 1}")
    if dry_run:
        log("Probelauf, nichts geändert.")
        return 0
    if not scheduled:
        if ask(f'Server stoppen und einfügen? Genau "{CONFIRM_TEXT}" eingeben: ').strip() != CONFIRM_TEXT:
            raise LiveMergeError("Abgebrochen, nichts geändert")

    panel = Panel(cfg.deploy.panel_url, cfg.deploy.api_key, cfg.deploy.server_id)
    was_running = panel.state() != "offline"
    if was_running:
        if lm.announce_minutes > 1:
            panel.command(f"say {lm.message.format(minutes=lm.announce_minutes)}")
            log(f"Ankündigung: noch {lm.announce_minutes} min")
            time.sleep(60 * (lm.announce_minutes - 1))
        if lm.announce_minutes > 0:
            panel.command(f"say {lm.message.format(minutes=1)}")
            log("Ankündigung: noch 1 min")
            time.sleep(60)
        panel.command("save-all flush")
        time.sleep(15)
        panel.power("stop")
        if not panel.wait_for("offline", 300, log):
            raise LiveMergeError("Server ist nach 5 Minuten nicht offline. Nichts geändert.")

    stats = MergeStats()
    merged: list[str] = []
    try:
        for t in tiles:
            merge_tile(staging, vol_world, cfg.dimension(t.dim).path, t, stats)
            merged.append(t.key)
        _chown_tree(stats.files, cfg.deploy.owner)
    finally:
        if merged:
            state.mark_live(merged)
        if was_running:
            panel.power("start")
            log("Server wird gestartet")
            if cfg.deploy.post_start_commands and panel.wait_for("running", 600, log):
                time.sleep(20)
                for cmd in cfg.deploy.post_start_commands:
                    log(f"Konsole: {cmd}")
                    panel.command(cmd)
    for t in tiles:
        if t.key in merged:
            remove_tile_from_staging(staging, cfg.dimension(t.dim).path, t)
    log(f"Fertig: {stats.regions_new} neue Regionen, {stats.regions_merged} ergänzt, "
        f"{stats.chunks_added:,} Chunks eingefügt, {stats.chunks_kept:,} vorhandene Chunks unangetastet")
    return len(merged)
