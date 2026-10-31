"""Bundle vom Koordinator spiegeln: nur geänderte Dateien laden, Fremdes entfernen."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

from ..common.hashing import join_safe, sha256_file
from .client import Client

# Bereiche des Server-Ordners, die vollständig dem Bundle gehören
STRICT_DIRS = ("mods",)


class SyncError(Exception):
    pass


def _index_path(bundle_dir: Path) -> Path:
    return bundle_dir / ".index.json"


def _load_index(bundle_dir: Path) -> dict:
    try:
        return json.loads(_index_path(bundle_dir).read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _cached_sha(path: Path, index: dict, rel: str) -> str | None:
    if not path.is_file():
        return None
    st = path.stat()
    entry = index.get(rel)
    if entry and entry[0] == st.st_size and entry[1] == st.st_mtime_ns:
        return entry[2]
    sha = sha256_file(path)
    index[rel] = [st.st_size, st.st_mtime_ns, sha]
    return sha


def sync_bundle(client: Client, manifest: dict, bundle_dir: Path, log=print) -> int:
    """Lädt fehlende/geänderte Dateien nach bundle_dir. Gibt die Anzahl geladener Dateien zurück."""
    bundle_dir.mkdir(parents=True, exist_ok=True)
    index = _load_index(bundle_dir)
    wanted = {f["path"]: f for f in manifest["files"]}
    loaded = 0
    for rel, f in wanted.items():
        dest = join_safe(bundle_dir, rel)
        if _cached_sha(dest, index, rel) == f["sha256"]:
            continue
        log(f"Lade {rel} ({f['size'] / 1e6:.1f} MB)")
        client.download(f"/api/v1/bundle/{manifest['hash']}/files/{rel}", dest, f["sha256"])
        st = dest.stat()
        index[rel] = [st.st_size, st.st_mtime_ns, f["sha256"]]
        loaded += 1
    # Dateien entfernen, die nicht mehr zum Bundle gehören
    for p in sorted(bundle_dir.rglob("*"), reverse=True):
        if p.is_file() and p.name != ".index.json":
            rel = p.relative_to(bundle_dir).as_posix()
            if rel not in wanted:
                p.unlink()
                index.pop(rel, None)
        elif p.is_dir() and not any(p.iterdir()):
            p.rmdir()
    _index_path(bundle_dir).write_text(json.dumps(index))
    # Abschließende Prüfung aller Dateien gegen das Manifest
    for rel, f in wanted.items():
        if _cached_sha(join_safe(bundle_dir, rel), index, rel) != f["sha256"]:
            raise SyncError(f"{rel} stimmt nach dem Laden nicht mit dem Koordinator überein")
    return loaded


def install_into_server(manifest: dict, bundle_dir: Path, server_dir: Path, overrides_dir: Path) -> list[str]:
    """Kopiert Launcher, Mods und Configs in den Server-Ordner. Gibt angewendete Overrides zurück."""
    server_dir.mkdir(parents=True, exist_ok=True)
    wanted = [f["path"] for f in manifest["files"] if not f["path"].startswith("template/")]
    for d in STRICT_DIRS:
        target = server_dir / d
        if target.exists():
            keep = {p for p in wanted if p.startswith(d + "/")}
            for p in target.rglob("*"):
                if p.is_file() and p.relative_to(server_dir).as_posix() not in keep:
                    p.unlink()
    for rel in wanted:
        src = bundle_dir / rel
        dst = server_dir / rel
        if dst.is_file() and dst.stat().st_size == src.stat().st_size and sha256_file(dst) == sha256_file(src):
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
    applied = []
    if overrides_dir.is_dir():
        for p in sorted(overrides_dir.rglob("*")):
            if p.is_file():
                rel = p.relative_to(overrides_dir).as_posix()
                if rel.startswith("mods/"):
                    continue  # Mods dürfen nicht lokal abweichen
                dst = server_dir / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(p, dst)
                applied.append(rel)
    return applied


def prepare_world(server_dir: Path, source_dir: Path) -> Path:
    """Frischer Weltordner aus Vorlage-Dateien (Template-Ordner oder entpackte Vorlage)."""
    world = server_dir / "world"
    if world.exists():
        shutil.rmtree(world)
    shutil.copytree(source_dir, world)
    return world


def write_server_files(server_dir: Path, props: dict, port: int) -> None:
    lines = ["# vom Big-World-Worker geschrieben, Änderungen werden überschrieben"]
    merged = dict(props)
    merged["server-port"] = str(port)
    merged["server-ip"] = "127.0.0.1"
    for k in sorted(merged):
        lines.append(f"{k}={merged[k]}")
    (server_dir / "server.properties").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (server_dir / "eula.txt").write_text("eula=true\n", encoding="utf-8")
    tasks = server_dir / "config" / "chunky" / "tasks"
    if tasks.is_dir():
        shutil.rmtree(tasks)


def os_name() -> str:
    return os.name
