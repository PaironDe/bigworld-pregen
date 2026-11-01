"""Konfiguration des Koordinators (TOML, Standard: /etc/bigworld/coordinator.toml)."""

from __future__ import annotations

import os
import secrets
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_CONFIG_PATH = "/etc/bigworld/coordinator.toml"

# Das einzige Volume, das der Deploy-Schritt jemals verändern darf.
ALLOWED_VOLUME_WORLD = "/var/lib/pterodactyl/volumes/fd1a91d3-f2fd-441c-b99f-cad2457c8ab3/world"


@dataclass
class Dimension:
    name: str
    radius: int
    path: str


@dataclass
class DeployConfig:
    volume_world: str = ALLOWED_VOLUME_WORLD
    backup_dir: str = ""
    panel_url: str = ""
    api_key: str = ""
    server_id: str = "fd1a91d3"
    owner: str = "pterodactyl"
    lss_config: str = "config/lss-server-config.yaml"
    post_start_commands: list[str] = field(default_factory=list)


@dataclass
class Config:
    host: str
    port: int
    data_dir: Path
    worker_token: str
    admin_token: str
    tile_regions: int
    lease_timeout_minutes: int
    max_upload_gb: float
    max_area_side: int
    mixed_gpus_ok: bool
    dimensions: list[Dimension]
    verify_tiles: list[str]
    deploy: DeployConfig
    path: Path | None = None

    def dimension(self, name: str) -> Dimension:
        for d in self.dimensions:
            if d.name == name:
                return d
        raise KeyError(name)

    # Verzeichnisse ---------------------------------------------------------
    @property
    def db_path(self) -> Path:
        return self.data_dir / "state.sqlite"

    @property
    def bundle_src(self) -> Path:
        return self.data_dir / "bundle-src"

    @property
    def bundles_dir(self) -> Path:
        return self.data_dir / "bundles"

    @property
    def incoming_dir(self) -> Path:
        return self.data_dir / "incoming"

    @property
    def verify_dir(self) -> Path:
        return self.data_dir / "verify"

    @property
    def staging_world(self) -> Path:
        return self.data_dir / "staging" / "world"


def load(path: str | os.PathLike | None = None) -> Config:
    p = Path(path or os.environ.get("BIGWORLD_CONFIG", DEFAULT_CONFIG_PATH))
    with open(p, "rb") as f:
        try:
            raw = tomllib.load(f)
        except tomllib.TOMLDecodeError as e:
            raise SystemExit(f"Konfiguration {p} ist kein gültiges TOML ({e}). "
                             "Neu schreiben: sudo bigworld-coord init --preset bigworld --force") from None
    srv = raw.get("server", {})
    world = raw.get("world", {})
    dims = [
        Dimension(d["name"], int(d.get("radius", 0)), d.get("path", default_dim_path(d["name"])))
        for d in world.get("dimensions", [])
    ]
    if not dims:
        raise ValueError("Keine Dimensionen in [world] konfiguriert")
    dep = raw.get("deploy", {})
    deploy = DeployConfig(
        volume_world=dep.get("volume_world", ALLOWED_VOLUME_WORLD),
        backup_dir=dep.get("backup_dir", ""),
        panel_url=dep.get("panel_url", "").rstrip("/"),
        api_key=dep.get("api_key", ""),
        server_id=dep.get("server_id", "fd1a91d3"),
        owner=dep.get("owner", "pterodactyl"),
        lss_config=dep.get("lss_config", "config/lss-server-config.yaml"),
        post_start_commands=list(dep.get("post_start_commands", [])),
    )
    for name in ("worker_token", "admin_token"):
        if len(srv.get(name, "")) < 16:
            raise ValueError(f"[server].{name} fehlt oder ist zu kurz (mind. 16 Zeichen)")
    return Config(
        host=srv.get("host", "127.0.0.1"),
        port=int(srv.get("port", 8770)),
        data_dir=Path(srv.get("data_dir", "/srv/bigworld")),
        worker_token=srv["worker_token"],
        admin_token=srv["admin_token"],
        tile_regions=int(world.get("tile_regions", 4)),
        lease_timeout_minutes=int(world.get("lease_timeout_minutes", 15)),
        max_upload_gb=float(world.get("max_upload_gb", 20)),
        max_area_side=int(world.get("max_area_side", 65536)),
        mixed_gpus_ok=bool(world.get("mixed_gpus_ok", False)),
        dimensions=dims,
        verify_tiles=list(raw.get("verify", {}).get("tiles", [])),
        deploy=deploy,
        path=p,
    )


def default_dim_path(name: str) -> str:
    ns, _, dim = name.partition(":")
    return f"dimensions/{ns}/{dim}"


EXAMPLE = """\
# Konfiguration des Big-World-Koordinators
# Änderungen werden nach `sudo systemctl restart bigworld-coordinator` aktiv.

[server]
# Nur auf der Tailscale-IP lauschen: aus dem LAN und dem Internet nicht erreichbar.
host = "100.72.176.11"
port = 8770
data_dir = "/srv/bigworld"
# Token für die Worker-Programme (an Mitspieler weitergeben)
worker_token = "{worker_token}"
# Token für Pause usw. in der Weboberfläche (nur für dich). Bereiche darf jeder mit Worker-Token anlegen.
admin_token = "{admin_token}"

[world]
# Kachelgröße in Regionen (1 Region = 512 Blöcke). 4 -> 2.048 x 2.048 Blöcke.
tile_regions = 4
# Ohne Lebenszeichen eines Workers wird seine Kachel nach dieser Zeit neu vergeben.
lease_timeout_minutes = 15
# Obergrenze für ein einzelnes Kachelpaket
max_upload_gb = 20

# Was generiert wird, legt ihr in der Weboberfläche als "Bereiche" fest (Koordinator
# oder Worker-Programm). Hier stehen die Dimensionen und ihre Ordner in der Welt.
# radius (optional): Standardbereich um 0,0 in Blöcken. Wird beim ersten Start und nach
# jedem `reset --all` als Bereich angelegt und lässt sich danach in der Oberfläche
# verschieben oder löschen.
# Größter erlaubter Bereich (Kantenlänge in Blöcken) pro Eintrag in der Oberfläche
max_area_side = 65536
# true = keine Warnung, wenn Worker mit verschiedenen GPUs gleichzeitig laufen
mixed_gpus_ok = false

[[world.dimensions]]
name = "minecraft:overworld"
path = "dimensions/minecraft/overworld"
radius = 4000

[[world.dimensions]]
name = "minecraft:the_nether"
path = "dimensions/minecraft/the_nether"
radius = 4000

[[world.dimensions]]
name = "minecraft:the_end"
path = "dimensions/minecraft/the_end"
radius = 4000

# Dimensionen aus Mods (z. B. Space Reloaded) bei Bedarf ergänzen, dann gibt es dafür
# einen Tab in der Oberfläche und man kann dort Bereiche anlegen:
# [[world.dimensions]]
# name = "spacereloaded:moon"
# path = "dimensions/spacereloaded/moon"

[verify]
# Diese Kacheln werden von zwei verschiedenen Workern generiert und chunkweise
# verglichen (Determinismus-Test). Format: "<dimension>|<tx>|<tz>"
tiles = ["minecraft:overworld|0|0"]

[deploy]
# Fest vorgegeben, andere Pfade verweigert der Deploy-Befehl.
volume_world = "{volume_world}"
# Sicherung der alten Welt als tar vor dem Löschen. Leer = keine Sicherung.
backup_dir = "/mnt/media/bigworld-backups"
# Pterodactyl-Panel und Client-API-Key (Konto -> API-Zugangsdaten), erst zum Deploy nötig
panel_url = ""
api_key = ""
server_id = "fd1a91d3"
owner = "pterodactyl"
# LSS-Config relativ zum Server-Ordner; der Deploy setzt dort generation.enabled: false
lss_config = "config/lss-server-config.yaml"
# Konsolenbefehle nach dem Start (z. B. für einen LSS-Backfill)
post_start_commands = []
"""


BIGWORLD = """\
# Konfiguration des Big-World-Koordinators
# Änderungen werden nach `sudo systemctl restart bigworld-coordinator` aktiv.
# Neu schreiben (Tokens bleiben erhalten): sudo bigworld-coord init --preset bigworld --force

[server]
# Nur auf der Tailscale-IP lauschen: aus dem LAN und dem Internet nicht erreichbar.
host = "100.72.176.11"
port = 8770
data_dir = "/srv/bigworld"
# Token für die Worker-Programme (an Mitspieler weitergeben); erlaubt auch Bereiche anlegen
worker_token = "{worker_token}"
# Token für Pause usw. in der Weboberfläche (nur für dich)
admin_token = "{admin_token}"

[world]
# Kachelgröße in Regionen (1 Region = 512 Blöcke). 4 -> 2.048 x 2.048 Blöcke.
tile_regions = 4
# Ohne Lebenszeichen eines Workers wird seine Kachel nach dieser Zeit neu vergeben.
lease_timeout_minutes = 15
# Obergrenze für ein einzelnes Kachelpaket
max_upload_gb = 20
# Größter Bereich pro Eintrag in der Oberfläche (Kantenlänge in Blöcken)
max_area_side = 65536
# Verschiedene GPUs gemischt: im Test geprüft und akzeptiert, keine Warnung
mixed_gpus_ok = true

# radius = Standardbereich um 0,0 in Blöcken (in jede Richtung). Wird beim ersten Start
# und nach jedem `reset --all` als Bereich angelegt, danach in der Oberfläche frei änderbar.
[[world.dimensions]]
name = "minecraft:overworld"
path = "dimensions/minecraft/overworld"
radius = 4000

[[world.dimensions]]
name = "minecraft:the_nether"
path = "dimensions/minecraft/the_nether"
radius = 4000

[[world.dimensions]]
name = "minecraft:the_end"
path = "dimensions/minecraft/the_end"
radius = 4000

[[world.dimensions]]
name = "spacereloaded:moon"
path = "dimensions/spacereloaded/moon"
radius = 4000

[[world.dimensions]]
name = "spacereloaded:mars"
path = "dimensions/spacereloaded/mars"
radius = 4000

[verify]
# Determinismus-Test erledigt, keine doppelte Kachel mehr
tiles = []

[deploy]
# Fest vorgegeben, andere Pfade verweigert der Deploy-Befehl.
volume_world = "{volume_world}"
# Sicherung der alten Welt als tar vor dem Löschen. Leer = keine Sicherung.
backup_dir = ""
# Pterodactyl-Panel und Client-API-Key (Konto -> API-Zugangsdaten), erst zum Deploy nötig
panel_url = "{panel_url}"
api_key = "{api_key}"
server_id = "fd1a91d3"
owner = "pterodactyl"
# LSS-Config relativ zum Server-Ordner; der Deploy setzt dort generation.enabled: false
lss_config = "config/lss-server-config.yaml"
post_start_commands = []
"""

PRESETS = {"standard": EXAMPLE, "bigworld": BIGWORLD}
_TOKEN_RE = r'^\s*{key}\s*=\s*"([A-Za-z0-9_\-]{{16,}})"'
_TEXT_RE = r'^\s*{key}\s*=\s*"([^"$]*)"'


def existing_values(text: str) -> dict:
    """Tokens und Panel-Daten aus einer (evtl. kaputten) alten Datei herauslesen."""
    import re

    out = {}
    for key in ("worker_token", "admin_token"):
        m = re.search(_TOKEN_RE.format(key=key), text, re.M)
        if m:
            out[key] = m.group(1)
    for key in ("panel_url", "api_key"):
        m = re.search(_TEXT_RE.format(key=key), text, re.M)
        if m:
            out[key] = m.group(1)
    return out


def example_config(preset: str = "standard", values: dict | None = None) -> str:
    v = {"worker_token": secrets.token_urlsafe(24), "admin_token": secrets.token_urlsafe(24),
         "panel_url": "", "api_key": "", "volume_world": ALLOWED_VOLUME_WORLD}
    v.update({k: x for k, x in (values or {}).items() if x is not None})
    return PRESETS[preset].format(**v)
