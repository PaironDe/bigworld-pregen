"""Bestätigter, separater Schritt: Staging-Welt ins Pterodactyl-Volume einsetzen.

Läuft nur auf Befehl (`sudo bigworld-coord deploy`), nie automatisch.
"""

from __future__ import annotations

import grp
import json
import os
import pwd
import shutil
import tarfile
import time
import urllib.error
import urllib.request
from pathlib import Path

from .config import ALLOWED_VOLUME_WORLD, Config
from .state import State

CONFIRM_TEXT = "WELT ERSETZEN"


class DeployError(Exception):
    pass


class Panel:
    def __init__(self, url: str, key: str, server_id: str):
        if not url or not key:
            raise DeployError("[deploy] panel_url und api_key müssen gesetzt sein")
        self.base = f"{url}/api/client/servers/{server_id}"
        self.key = key

    def _req(self, method: str, path: str, body: dict | None = None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            self.base + path, data=data, method=method,
            headers={
                "Authorization": f"Bearer {self.key}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                raw = r.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            raise DeployError(f"Panel-API {method} {path}: HTTP {e.code} {e.read()[:200]!r}") from e
        except urllib.error.URLError as e:
            raise DeployError(f"Panel nicht erreichbar: {e}") from e

    def state(self) -> str:
        return self._req("GET", "/resources")["attributes"]["current_state"]

    def power(self, signal: str) -> None:
        self._req("POST", "/power", {"signal": signal})

    def command(self, cmd: str) -> None:
        self._req("POST", "/command", {"command": cmd})

    def wait_for(self, wanted: str, timeout: int, log) -> bool:
        end = time.time() + timeout
        while time.time() < end:
            s = self.state()
            if s == wanted:
                return True
            log(f"  Serverstatus: {s} (warte auf {wanted})")
            time.sleep(5)
        return False


def patch_lss_generation(path: Path) -> bool:
    """Setzt in der LSS-YAML 'generation: enabled: false'. Ändert sonst nichts.

    Absichtlich ohne YAML-Bibliothek: Zeilenweise innerhalb des Blocks
    'generation:', damit Kommentare und Formatierung erhalten bleiben.
    """
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    in_block = False
    block_indent = 0
    changed = False
    for i, line in enumerate(lines):
        stripped = line.strip()
        indent = len(line) - len(line.lstrip(" "))
        if not stripped or stripped.startswith("#"):
            continue
        if in_block and indent <= block_indent:
            in_block = False
        if stripped.rstrip() == "generation:" or stripped.startswith("generation:") and stripped.endswith(":"):
            in_block = True
            block_indent = indent
            continue
        if in_block and stripped.startswith("enabled:"):
            prefix = line[: line.index("enabled:")]
            comment = ""
            if "#" in stripped:
                comment = "  #" + stripped.split("#", 1)[1]
            newline = "\n" if line.endswith("\n") else ""
            lines[i] = f"{prefix}enabled: false{comment}{newline}"
            changed = True
            in_block = False
    if changed:
        path.write_text("".join(lines), encoding="utf-8")
    return changed


def _du(path: Path) -> int:
    total = 0
    for dirpath, _, files in os.walk(path):
        for f in files:
            try:
                total += os.lstat(os.path.join(dirpath, f)).st_size
            except OSError:
                pass
    return total


def _gb(n: int) -> str:
    return f"{n / 1024**3:.1f} GB"


def run(cfg: Config, state: State, allow_incomplete: bool, ask=input, log=print) -> None:
    if os.geteuid() != 0:
        raise DeployError("Deploy muss mit sudo laufen (Dateien gehören pterodactyl)")
    vol_world = Path(cfg.deploy.volume_world)
    if str(vol_world) != ALLOWED_VOLUME_WORLD or vol_world.resolve() != Path(ALLOWED_VOLUME_WORLD).resolve():
        raise DeployError(f"Ziel {vol_world} ist nicht das freigegebene Volume {ALLOWED_VOLUME_WORLD}")
    staging = cfg.staging_world
    if not (staging / "level.dat").is_file():
        raise DeployError(f"{staging} enthält keine Welt")

    st = state.status()
    counts = st["totals"]["tiles"]
    open_tiles = sum(v for k, v in counts.items() if k != "done")
    if open_tiles and not allow_incomplete:
        raise DeployError(f"{open_tiles} Kacheln sind nicht fertig ({counts}). Abbruch. "
                          "Mit --allow-incomplete trotzdem einsetzen.")
    if any(w["tile"] for w in st["workers"]):
        raise DeployError("Es laufen noch Kacheln auf Workern. Erst pausieren und warten.")

    panel = Panel(cfg.deploy.panel_url, cfg.deploy.api_key, cfg.deploy.server_id)
    server_state = panel.state()

    staging_size = _du(staging)
    old_size = _du(vol_world) if vol_world.exists() else 0
    same_fs = vol_world.parent.stat().st_dev == staging.stat().st_dev
    backup_dir = Path(cfg.deploy.backup_dir) if cfg.deploy.backup_dir else None

    log("")
    log("=== Deploy: neue Welt einsetzen ===")
    log(f"Neue Welt (Staging):  {staging}  ({_gb(staging_size)})")
    log(f"Ziel (Live-Welt):     {vol_world}  ({_gb(old_size)})")
    log(f"Kacheln:              {counts}")
    log(f"Server-Status:        {server_state}")
    log(f"Gleiches Dateisystem: {'ja, Verschieben dauert Sekunden' if same_fs else 'NEIN, es wird kopiert'}")
    if backup_dir:
        backup_dir.mkdir(parents=True, exist_ok=True)
        free = shutil.disk_usage(backup_dir).free
        log(f"Sicherung alte Welt:  {backup_dir} (frei: {_gb(free)})")
        if old_size and free < old_size * 1.05:
            raise DeployError("Nicht genug Platz für die Sicherung")
    else:
        log("Sicherung alte Welt:  KEINE (backup_dir ist leer)")
    if not same_fs:
        free = shutil.disk_usage(vol_world.parent).free
        if free < staging_size * 1.05:
            raise DeployError("Nicht genug Platz im Volume für die Kopie")
    log("")
    log("Ablauf: Server stoppen -> alte Welt sichern -> alte Welt löschen -> neue Welt einsetzen")
    log("        -> Besitzer auf pterodactyl setzen -> Server starten")
    log("Das Löschen der alten Welt ist nicht rückgängig zu machen (außer über die Sicherung).")
    answer = ask(f'Zum Fortfahren genau "{CONFIRM_TEXT}" eingeben: ')
    if answer.strip() != CONFIRM_TEXT:
        raise DeployError("Abgebrochen, nichts geändert")

    state.set_setting("paused", "1")

    log("1/6 Server stoppen ...")
    if server_state != "offline":
        panel.power("stop")
        if not panel.wait_for("offline", 300, log):
            raise DeployError("Server ist nach 5 Minuten nicht offline. Nichts geändert.")

    if vol_world.exists() and backup_dir:
        dest = backup_dir / f"world-{time.strftime('%Y%m%d-%H%M%S')}.tar"
        log(f"2/6 Alte Welt sichern nach {dest} ...")
        with tarfile.open(dest, "w") as tar:
            tar.add(str(vol_world), arcname="world")
    else:
        log("2/6 Sicherung übersprungen")

    if vol_world.exists():
        log("3/6 Alte Welt löschen ...")
        shutil.rmtree(vol_world)

    log("4/6 Neue Welt einsetzen ...")
    if same_fs:
        os.rename(staging, vol_world)
    else:
        shutil.copytree(staging, vol_world)
        shutil.rmtree(staging)

    log("5/6 Besitzer auf pterodactyl setzen ...")
    uid = pwd.getpwnam(cfg.deploy.owner).pw_uid
    gid = grp.getgrnam(cfg.deploy.owner).gr_gid
    for dirpath, dirs, files in os.walk(vol_world):
        os.chown(dirpath, uid, gid)
        for n in files:
            os.chown(os.path.join(dirpath, n), uid, gid)

    if cfg.deploy.lss_config:
        lss = vol_world.parent / cfg.deploy.lss_config
        if lss.is_file():
            if patch_lss_generation(lss):
                log(f"   {cfg.deploy.lss_config}: generation.enabled auf false gesetzt")
            else:
                log(f"   WARNUNG: generation.enabled in {lss} nicht gefunden, bitte von Hand prüfen")
        else:
            log(f"   WARNUNG: {lss} existiert nicht, LSS-Config nicht angepasst")

    log("6/6 Server starten ...")
    panel.power("start")
    if panel.wait_for("running", 600, log):
        time.sleep(20)
        for cmd in cfg.deploy.post_start_commands:
            log(f"  Konsole: {cmd}")
            panel.command(cmd)
    else:
        log("  Server ist noch nicht 'running', bitte im Panel nachsehen.")
    log("Fertig. Die neue Welt ist eingesetzt.")
