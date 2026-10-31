"""Passende Java-Runtime (Temurin JRE) automatisch herunterladen."""

from __future__ import annotations

import hashlib
import json
import platform
import shutil
import subprocess
import sys
import tarfile
import urllib.request
import zipfile
from pathlib import Path

JAVA_MAJOR = 25
API = "https://api.adoptium.net/v3/assets/latest/{major}/hotspot?architecture={arch}&image_type=jre&os={os}&vendor=eclipse"


def _os_arch() -> tuple[str, str]:
    os_name = {"win32": "windows", "darwin": "mac"}.get(sys.platform, "linux")
    m = platform.machine().lower()
    arch = "aarch64" if m in ("arm64", "aarch64") else "x64"
    return os_name, arch


def java_major(java: str) -> int | None:
    try:
        out = subprocess.run([java, "-version"], capture_output=True, text=True, timeout=20,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        text = out.stderr + out.stdout
        import re

        m = re.search(r'version "(\d+)', text)
        return int(m.group(1)) if m else None
    except OSError:
        return None


def _find_java(root: Path) -> Path | None:
    exe = "java.exe" if sys.platform == "win32" else "java"
    for p in root.rglob(exe):
        if p.parent.name == "bin":
            return p
    return None


def ensure_java(base: Path, configured: str = "", log=print) -> str:
    if configured:
        major = java_major(configured)
        if major != JAVA_MAJOR:
            raise RuntimeError(f"Eingestelltes Java ({configured}) hat Version {major}, gebraucht wird {JAVA_MAJOR}")
        return configured
    target = base / "runtime" / f"java-{JAVA_MAJOR}"
    existing = _find_java(target) if target.exists() else None
    if existing and java_major(str(existing)) == JAVA_MAJOR:
        return str(existing)

    os_name, arch = _os_arch()
    log(f"Lade Java {JAVA_MAJOR} (Temurin JRE, {os_name}/{arch}) herunter ...")
    url = API.format(major=JAVA_MAJOR, arch=arch, os=os_name)
    req = urllib.request.Request(url, headers={"User-Agent": "bigworld-worker"})
    with urllib.request.urlopen(req, timeout=60) as r:
        assets = json.loads(r.read())
    if not assets:
        raise RuntimeError("Adoptium liefert kein passendes Java")
    pkg = assets[0]["binary"]["package"]
    tmp_dir = base / "runtime" / "download"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    archive = tmp_dir / pkg["name"]
    req = urllib.request.Request(pkg["link"], headers={"User-Agent": "bigworld-worker"})
    h = hashlib.sha256()
    with urllib.request.urlopen(req, timeout=600) as r, open(archive, "wb") as f:
        while True:
            b = r.read(1024 * 1024)
            if not b:
                break
            h.update(b)
            f.write(b)
    if h.hexdigest() != pkg["checksum"]:
        archive.unlink(missing_ok=True)
        raise RuntimeError("Prüfsumme des Java-Downloads stimmt nicht")
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)
    if archive.name.endswith(".zip"):
        with zipfile.ZipFile(archive) as z:
            z.extractall(target)
    else:
        with tarfile.open(archive) as t:
            t.extractall(target, filter="data")
    archive.unlink(missing_ok=True)
    java = _find_java(target)
    if not java:
        raise RuntimeError("java nicht im heruntergeladenen Archiv gefunden")
    if sys.platform != "win32":
        java.chmod(0o755)
    log(f"Java bereit: {java}")
    return str(java)
