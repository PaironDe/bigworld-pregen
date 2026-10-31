"""Format der Kachelpakete, die Worker hochladen.

Ein Paket ist ein unkomprimiertes tar (Regionsdateien sind bereits komprimiert).
Erstes Mitglied ist 'tile.json' mit Metadaten, danach Dateien relativ zum
Weltordner, z. B. 'dimensions/minecraft/overworld/region/r.0.0.mca'.

Erlaubt sind nur Dateien der Kachel aus region/, entities/ und poi/ des
Dimensionsordners: r.X.Z.mca mit (X, Z) in der Kachel sowie externe
Chunkdateien c.X.Z.mcc, deren Chunk in der Kachel liegt.
"""

from __future__ import annotations

import io
import json
import tarfile
from pathlib import Path

from . import anvil
from .hashing import safe_relpath
from .tiles import Tile

STORAGES = ("region", "entities", "poi")
META_NAME = "tile.json"


def member_allowed(rel: str, tile: Tile, dim_path: str) -> bool:
    if not safe_relpath(rel):
        return False
    prefix = dim_path.strip("/") + "/" if dim_path.strip("/") else ""
    if not rel.startswith(prefix):
        return False
    rest = rel[len(prefix):]
    parts = rest.split("/")
    if len(parts) != 2 or parts[0] not in STORAGES:
        return False
    parsed = anvil.parse_name(parts[1])
    if not parsed:
        return False
    kind, x, z, _ = parsed
    if kind == "r":
        return tile.contains_region(x, z)
    return tile.contains_region(x >> 5, z >> 5)


def collect_tile_files(world: Path, dim_path: str, tile: Tile) -> list[tuple[str, Path]]:
    """Alle Dateien der Kachel (relativer Pfad, absoluter Pfad)."""
    out: list[tuple[str, Path]] = []
    dim_dir = world / dim_path if dim_path else world
    for storage in STORAGES:
        d = dim_dir / storage
        if not d.is_dir():
            continue
        for rx, rz in tile.regions():
            p = d / f"r.{rx}.{rz}.mca"
            if p.is_file() and p.stat().st_size > 0:
                out.append((_rel(dim_path, storage, p.name), p))
                try:
                    externals = anvil.RegionFile(p).external_files()
                except anvil.RegionError:
                    externals = []
                for ext in externals:
                    if ext.is_file():
                        out.append((_rel(dim_path, storage, ext.name), ext))
    return out


def _rel(dim_path: str, storage: str, name: str) -> str:
    base = dim_path.strip("/")
    return f"{base}/{storage}/{name}" if base else f"{storage}/{name}"


def write_package(dest: Path, meta: dict, files: list[tuple[str, Path]]) -> None:
    with tarfile.open(dest, "w", format=tarfile.PAX_FORMAT) as tar:
        data = json.dumps(meta, indent=2).encode()
        info = tarfile.TarInfo(META_NAME)
        info.size = len(data)
        info.mode = 0o644
        tar.addfile(info, io.BytesIO(data))
        for rel, path in files:
            info = tar.gettarinfo(str(path), arcname=rel)
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            info.mode = 0o644
            with open(path, "rb") as f:
                tar.addfile(info, f)


def read_meta(tar_path: Path) -> dict:
    with tarfile.open(tar_path, "r") as tar:
        m = tar.getmember(META_NAME)
        f = tar.extractfile(m)
        if f is None:
            raise ValueError("tile.json fehlt")
        return json.loads(f.read())


def validate_package(tar_path: Path, tile: Tile, dim_path: str) -> list[tarfile.TarInfo]:
    """Gibt die Dateimitglieder zurück oder wirft ValueError mit Begründung."""
    members = []
    with tarfile.open(tar_path, "r") as tar:
        for m in tar.getmembers():
            if m.name == META_NAME:
                continue
            if not m.isfile():
                raise ValueError(f"Nur normale Dateien erlaubt: {m.name}")
            if not member_allowed(m.name, tile, dim_path):
                raise ValueError(f"Datei gehört nicht zur Kachel: {m.name}")
            members.append(m)
    if not any(m.name.split("/")[-2] == "region" for m in members):
        raise ValueError("Paket enthält keine Regionsdateien")
    return members
