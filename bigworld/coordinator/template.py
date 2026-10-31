"""Vorlage-Welt: einmal pro Bundle von einem Worker erzeugt, dann für alle gleich.

Der Template-Job startet einen Server mit level.dat, datapacks/ und den
Weltgen-Einstellungen der bisherigen Welt plus dem aktuellen Mod-Satz. Der
Server initialisiert die Welt (inkl. Datapacks der Mods) und hält an. Hochgeladen
werden nur level.dat, datapacks/ und erlaubte Dateien aus data/.
"""

from __future__ import annotations

import shutil
import tarfile
from pathlib import Path

from ..common.hashing import join_safe, sha256_file
from ..common.template_files import REQUIRED, member_allowed
from .config import Config

MAX_TEMPLATE_BYTES = 512 * 1024 * 1024


def template_dir(cfg: Config, bundle_hash: str) -> Path:
    return cfg.data_dir / "templates" / bundle_hash


def template_tar(cfg: Config, bundle_hash: str) -> Path:
    return template_dir(cfg, bundle_hash) / "template.tar"


def store(cfg: Config, bundle_hash: str, tar_path: Path) -> tuple[str, list[str]]:
    """Prüft das hochgeladene Template und legt es ab. Gibt (SHA256, Dateiliste) zurück."""
    if tar_path.stat().st_size > MAX_TEMPLATE_BYTES:
        raise ValueError("Vorlage ist unplausibel groß")
    names: list[str] = []
    with tarfile.open(tar_path, "r") as tar:
        for m in tar.getmembers():
            if m.name == "tile.json":
                continue
            if not m.isfile():
                raise ValueError(f"Nur normale Dateien erlaubt: {m.name}")
            if not member_allowed(m.name):
                raise ValueError(f"Datei gehört nicht in die Vorlage: {m.name}")
            names.append(m.name)
    for req in REQUIRED:
        if req not in names:
            raise ValueError(f"Vorlage unvollständig, {req} fehlt")
    d = template_dir(cfg, bundle_hash)
    if d.exists():
        shutil.rmtree(d)
    world = d / "world"
    world.mkdir(parents=True)
    with tarfile.open(tar_path, "r") as tar:
        for m in tar.getmembers():
            if m.name in names:
                target = join_safe(world, m.name)
                target.parent.mkdir(parents=True, exist_ok=True)
                src = tar.extractfile(m)
                with open(target, "wb") as out:
                    shutil.copyfileobj(src, out)
    dest = template_tar(cfg, bundle_hash)
    tar_path.replace(dest)
    return sha256_file(dest), sorted(names)
