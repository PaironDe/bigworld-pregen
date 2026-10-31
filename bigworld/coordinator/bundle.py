"""Bundle: alles, was ein Worker zum Generieren braucht, mit SHA256 je Datei.

bundle-src/ (von Hand oder per `bundle prepare` befüllt):
    fabric-server-launch.jar      Fabric-Server-Launcher
    mods/*.jar                    Server-Mods inkl. Chunky (+ C2ME)
    config/**                     Mod-Konfigurationen
    template/level.dat            aus der bisherigen Welt (gleicher Seed)
    template/datapacks/**
    template/data/minecraft/world_gen_settings.dat   Seed + Dimensionen (seit 26.x hier)
    template/data/minecraft/game_rules.dat
    server.properties             optional, wird über die Standardwerte gelegt

`bundle build` kopiert das unveränderlich nach bundles/<hash>/ und setzt es aktiv.
Der Hash über alle Dateien und Server-Einstellungen ist die "Generier-Version":
Worker mit anderem Hash bekommen keine Kacheln.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import shutil
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

from ..common.hashing import sha256_file
from .config import Config

LAUNCHER = "fabric-server-launch.jar"
TEMPLATE_FILES = [
    "level.dat",
    "data/minecraft/world_gen_settings.dat",
    "data/minecraft/game_rules.dat",
]
REQUIRED = [LAUNCHER, "template/level.dat", "template/data/minecraft/world_gen_settings.dat"]

# Mods, die auf den Generier-PCs nichts verloren haben
EXCLUDED_MOD_IDS = {"lod-server-support", "lodserversupport", "lss", "distanthorizons", "voxy"}
EXCLUDED_MOD_GLOBS = ["lod-server-support*", "DistantHorizons*", "voxy*"]

DEFAULT_PROPERTIES = {
    "online-mode": "false",
    "server-ip": "127.0.0.1",
    "level-name": "world",
    "region-file-compression": "deflate",
    "sync-chunk-writes": "false",
    "view-distance": "3",
    "simulation-distance": "3",
    "spawn-protection": "0",
    "max-players": "1",
    "white-list": "true",
    "enforce-whitelist": "true",
    "enable-query": "false",
    "enable-rcon": "false",
    "enable-status": "false",
    "motd": "bigworld worker",
}

# Diese Schlüssel setzt immer der Worker bzw. sind für die Generierung irrelevant
PROPERTY_BLOCKLIST = {"server-port", "server-ip", "rcon.password", "rcon.port", "query.port",
                      "online-mode", "level-name", "region-file-compression", "management-server-port",
                      "management-server-secret", "management-server-enabled"}


class BundleError(Exception):
    pass


def mod_info(jar: Path) -> dict:
    try:
        with zipfile.ZipFile(jar) as z:
            with z.open("fabric.mod.json") as f:
                data = json.loads(f.read().decode("utf-8", errors="replace"), strict=False)
        return {"id": data.get("id", ""), "version": str(data.get("version", "")), "name": data.get("name", "")}
    except (KeyError, zipfile.BadZipFile, json.JSONDecodeError, OSError):
        return {"id": "", "version": "", "name": ""}


def _excluded(jar: Path, info: dict) -> bool:
    if info["id"].lower() in EXCLUDED_MOD_IDS:
        return True
    return any(fnmatch.fnmatch(jar.name, g) for g in EXCLUDED_MOD_GLOBS)


def read_properties(path: Path) -> dict:
    props = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        props[k.strip()] = v.strip()
    return props


def _list_files(root: Path) -> list[str]:
    out = []
    for dirpath, _, files in os.walk(root):
        for name in files:
            p = Path(dirpath) / name
            out.append(p.relative_to(root).as_posix())
    return sorted(out)


# ---------------------------------------------------------------------------
# prepare
# ---------------------------------------------------------------------------


def prepare(cfg: Config, world: Path, mods_from: list[Path], config_from: Path | None,
            launcher: Path | None, log=print) -> list[str]:
    """Befüllt bundle-src/ aus vorhandenen Ordnern. Liest nur, schreibt nur in bundle-src."""
    src = cfg.bundle_src
    if src.exists():
        backup = src.with_name("bundle-src.old")
        if backup.exists():
            shutil.rmtree(backup)
        src.rename(backup)
        log(f"Altes bundle-src nach {backup} verschoben")
    (src / "mods").mkdir(parents=True)
    warnings: list[str] = []

    # Template-Welt ------------------------------------------------------------
    for rel in TEMPLATE_FILES:
        p = world / rel
        if p.is_file():
            dest = src / "template" / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, dest)
            log(f"Template: {rel}")
        elif rel != "data/minecraft/game_rules.dat":
            raise BundleError(f"{p} fehlt")
    if (world / "datapacks").is_dir():
        shutil.copytree(world / "datapacks", src / "template" / "datapacks")
        for dp in sorted((world / "datapacks").iterdir()):
            log(f"Datapack: {dp.name}")
    else:
        warnings.append("Kein datapacks/-Ordner in der Welt gefunden")

    # Mods ----------------------------------------------------------------------
    by_id: dict[str, Path] = {}
    for d in mods_from:
        for jar in sorted(d.glob("*.jar")):
            info = mod_info(jar)
            if _excluded(jar, info):
                log(f"Mod ausgelassen (nicht für Generier-PCs): {jar.name}")
                continue
            key = info["id"] or jar.name
            if key in by_id and by_id[key].name != jar.name:
                log(f"Mod {key}: {by_id[key].name} ersetzt durch {jar.name} (aus {d})")
            by_id[key] = jar
    for key, jar in sorted(by_id.items()):
        shutil.copy2(jar, src / "mods" / jar.name)
        log(f"Mod: {jar.name}")
    if "chunky" not in by_id:
        warnings.append("Chunky fehlt. Nachladen mit: bigworld-coord bundle fetch-modrinth chunky --mc <Version>")
    if not any(k.startswith("c2me") for k in by_id):
        warnings.append("C2ME fehlt: Generierung läuft ohne Beschleunigung")
    elif not any("opencl" in j.name.lower() for j in by_id.values()):
        warnings.append("Kein C2ME-OpenCL-Modul gefunden: Generierung nur per CPU")

    # Configs -------------------------------------------------------------------
    if config_from and config_from.is_dir():
        shutil.copytree(config_from, src / "config")
        log(f"Configs aus {config_from}")
    else:
        warnings.append("Keine Mod-Configs übernommen")

    # Launcher ------------------------------------------------------------------
    if launcher:
        shutil.copy2(launcher, src / LAUNCHER)
        log(f"Launcher: {launcher}")
    else:
        warnings.append(f"{LAUNCHER} fehlt. Mit --launcher angeben oder per fetch-launcher holen.")

    _fix_owner(cfg, src)
    return warnings


def _fix_owner(cfg: Config, path: Path) -> None:
    """Wenn als root ausgeführt: Dateien dem Besitzer des Datenordners geben."""
    if os.name != "posix" or os.geteuid() != 0:
        return
    st = cfg.data_dir.stat()
    for dirpath, dirs, files in os.walk(path):
        os.chown(dirpath, st.st_uid, st.st_gid)
        for n in files:
            os.chown(os.path.join(dirpath, n), st.st_uid, st.st_gid)


# ---------------------------------------------------------------------------
# Downloads (Modrinth, Fabric-Meta)
# ---------------------------------------------------------------------------


def _get_json(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": "PaironDe/bigworld-pregen"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def _download(url: str, dest: Path) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": "PaironDe/bigworld-pregen"})
    tmp = dest.with_suffix(dest.suffix + ".part")
    with urllib.request.urlopen(req, timeout=120) as r, open(tmp, "wb") as f:
        shutil.copyfileobj(r, f)
    tmp.replace(dest)


def fetch_modrinth(cfg: Config, slug: str, mc: str, log=print, with_deps: bool = True,
                   _seen: set | None = None) -> Path | None:
    """Lädt die neueste Fabric-Version eines Modrinth-Projekts (und Pflicht-Abhängigkeiten)."""
    seen = _seen if _seen is not None else set()
    if slug in seen:
        return None
    seen.add(slug)
    q = urllib.parse.urlencode({"loaders": json.dumps(["fabric"]), "game_versions": json.dumps([mc])})
    versions = _get_json(f"https://api.modrinth.com/v2/project/{urllib.parse.quote(slug)}/version?{q}")
    if not versions:
        raise BundleError(f"Keine Fabric-Version von {slug} für Minecraft {mc} auf Modrinth")
    releases = [v for v in versions if v.get("version_type") == "release"] or versions
    v = releases[0]
    seen.add(v["project_id"])
    f = next((x for x in v["files"] if x.get("primary")), v["files"][0])
    dest_dir = cfg.bundle_src / "mods"
    dest_dir.mkdir(parents=True, exist_ok=True)
    existing = {mod_info(j)["id"]: j for j in dest_dir.glob("*.jar")}
    dest = dest_dir / f["filename"]
    _download(f["url"], dest)
    sha512 = hashlib.sha512(dest.read_bytes()).hexdigest()
    if sha512 != f["hashes"].get("sha512"):
        dest.unlink()
        raise BundleError(f"Prüfsumme von {f['filename']} stimmt nicht")
    mid = mod_info(dest)["id"]
    if mid and mid in existing and existing[mid].name != dest.name:
        log(f"{slug}: {existing[mid].name} ersetzt durch {dest.name}")
        existing[mid].unlink()
    log(f"{slug} {v['version_number']} ({v.get('version_type')}) -> {dest.name}")
    if with_deps:
        for dep in v.get("dependencies", []):
            if dep.get("dependency_type") != "required":
                continue
            if not dep.get("project_id") and dep.get("version_id"):
                # Manche Mods geben nur die Versions-ID an
                dep["project_id"] = _get_json(f"https://api.modrinth.com/v2/version/{dep['version_id']}")["project_id"]
            if not dep.get("project_id"):
                log(f"  Abhängigkeit ohne Projekt-ID übersprungen: {dep}")
                continue
            if dep["project_id"] in seen:
                continue
            project = _get_json(f"https://api.modrinth.com/v2/project/{dep['project_id']}")
            dep_slug = project["slug"]
            have = {mod_info(j)["id"] for j in dest_dir.glob("*.jar")}
            if dep_slug in have or dep_slug.replace("-", "_") in have:
                log(f"  Abhängigkeit {dep_slug} schon vorhanden")
                seen.add(dep["project_id"])
                continue
            log(f"  Abhängigkeit {dep_slug} wird geladen")
            fetch_modrinth(cfg, dep_slug, mc, log, True, seen)
    _fix_owner(cfg, dest_dir)
    return dest


def fetch_launcher(cfg: Config, mc: str, loader: str, log=print) -> Path:
    installers = _get_json("https://meta.fabricmc.net/v2/versions/installer")
    installer = next(i["version"] for i in installers if i.get("stable", True))
    url = f"https://meta.fabricmc.net/v2/versions/loader/{mc}/{loader}/{installer}/server/jar"
    dest = cfg.bundle_src / LAUNCHER
    dest.parent.mkdir(parents=True, exist_ok=True)
    _download(url, dest)
    _fix_owner(cfg, dest)
    log(f"Fabric-Launcher (MC {mc}, Loader {loader}, Installer {installer}) -> {dest}")
    return dest


def detect_loader_version(server_root: Path) -> str | None:
    base = server_root / "libraries" / "net" / "fabricmc" / "fabric-loader"
    if base.is_dir():
        versions = sorted(p.name for p in base.iterdir() if p.is_dir())
        return versions[-1] if versions else None
    return None


# ---------------------------------------------------------------------------
# build
# ---------------------------------------------------------------------------


def build(cfg: Config, log=print) -> dict:
    src = cfg.bundle_src
    missing = [r for r in REQUIRED if not (src / r).is_file()]
    if missing:
        raise BundleError("In bundle-src fehlt: " + ", ".join(missing))
    jars = sorted((src / "mods").glob("*.jar")) if (src / "mods").is_dir() else []
    infos = {j.name: mod_info(j) for j in jars}
    if not any(i["id"] == "chunky" for i in infos.values()):
        raise BundleError("Chunky fehlt in bundle-src/mods")
    ids: dict[str, str] = {}
    for name, info in infos.items():
        if info["id"] and info["id"] in ids:
            raise BundleError(f"Mod {info['id']} doppelt: {ids[info['id']]} und {name}")
        if info["id"]:
            ids[info["id"]] = name

    props = dict(DEFAULT_PROPERTIES)
    if (src / "server.properties").is_file():
        for k, v in read_properties(src / "server.properties").items():
            if k not in PROPERTY_BLOCKLIST:
                props[k] = v

    files = []
    for rel in _list_files(src):
        if rel == "server.properties":
            continue
        p = src / rel
        files.append({"path": rel, "size": p.stat().st_size, "sha256": sha256_file(p)})

    h = hashlib.sha256()
    for f in files:
        h.update(f"{f['path']}\n{f['sha256']}\n".encode())
    h.update(json.dumps(props, sort_keys=True).encode())
    bundle_hash = h.hexdigest()

    dest = cfg.bundles_dir / bundle_hash
    if not dest.exists():
        tmp = cfg.bundles_dir / (bundle_hash + ".tmp")
        if tmp.exists():
            shutil.rmtree(tmp)
        tmp.mkdir(parents=True)
        for f in files:
            target = tmp / "files" / f["path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src / f["path"], target)
        manifest = {
            "hash": bundle_hash,
            "files": files,
            "properties": props,
            "mods": [{"file": n, **infos[n]} for n in sorted(infos)],
            "launcher": LAUNCHER,
        }
        (tmp / "manifest.json").write_text(json.dumps(manifest, indent=2))
        tmp.rename(dest)
        _fix_owner(cfg, dest)
    manifest = json.loads((dest / "manifest.json").read_text())
    log(f"Bundle {bundle_hash[:12]}: {len(files)} Dateien, {len(jars)} Mods")
    return manifest


def set_current(cfg: Config, bundle_hash: str) -> None:
    p = cfg.data_dir / "current_bundle"
    p.write_text(bundle_hash + "\n")


def current_hash(cfg: Config) -> str | None:
    p = cfg.data_dir / "current_bundle"
    if not p.exists():
        return None
    h = p.read_text().strip()
    return h if (cfg.bundles_dir / h / "manifest.json").exists() else None


def load_manifest(cfg: Config, bundle_hash: str) -> dict:
    return json.loads((cfg.bundles_dir / bundle_hash / "manifest.json").read_text())


def file_path(cfg: Config, bundle_hash: str, rel: str) -> Path:
    from ..common.hashing import join_safe

    return join_safe(cfg.bundles_dir / bundle_hash / "files", rel)
