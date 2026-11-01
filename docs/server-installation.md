# Koordinator auf serversoeren einrichten

Der Koordinator läuft als eigener systemd-Dienst unter dem Benutzer `bigworld`,
**nicht** in Pterodactyl und **nicht** in Docker. Er schreibt nur nach `/srv/bigworld`
und lauscht nur auf der Tailscale-IP `100.72.176.11:8770`.

Alle Befehle als `soeren` per SSH. `V` ist das Minecraft-Volume:

```bash
V=/var/lib/pterodactyl/volumes/fd1a91d3-f2fd-441c-b99f-cad2457c8ab3
```

## 1. Installieren

```bash
sudo apt install -y python3-venv git
sudo useradd --system --home /srv/bigworld --shell /usr/sbin/nologin bigworld
sudo mkdir -p /srv/bigworld /opt/bigworld /etc/bigworld
sudo chown bigworld:bigworld /srv/bigworld

sudo git clone https://github.com/PaironDe/bigworld-pregen /opt/bigworld/src
sudo python3 -m venv /opt/bigworld/venv
sudo /opt/bigworld/venv/bin/pip install "/opt/bigworld/src[coordinator]"
sudo ln -sf /opt/bigworld/venv/bin/bigworld-coord /usr/local/bin/bigworld-coord
```

Aktualisieren später:

```bash
cd /opt/bigworld/src && sudo git pull
sudo /opt/bigworld/venv/bin/pip install "/opt/bigworld/src[coordinator]"
sudo systemctl restart bigworld-coordinator
```

## 2. Konfiguration

```bash
sudo bigworld-coord init                       # legt /etc/bigworld/coordinator.toml mit Tokens an
sudo chown root:bigworld /etc/bigworld/coordinator.toml
sudo chmod 640 /etc/bigworld/coordinator.toml
sudo nano /etc/bigworld/coordinator.toml
```

Wichtig in der Datei:

- `worker_token`: an die Mitspieler weitergeben
- `admin_token`: nur für dich (Prioritäten, Pause in der Weboberfläche)
- `[[world.dimensions]]`: Radius und `path`. Die Pfade sind für 26.x vorbelegt
  (`dimensions/minecraft/overworld` usw.). **Bitte prüfen**, siehe [Testlauf, Schritt 0](testlauf.md).
- `[deploy]`: `panel_url` und `api_key` erst für den Deploy nötig

## 3. Bundle bauen (Mods, Configs, Vorlage)

Liest nur aus dem Volume, schreibt nur nach `/srv/bigworld/bundle-src`:

```bash
sudo bigworld-coord bundle prepare --world "$V/world" --mods-from "$V/mods" --config-from "$V/config"
sudo bigworld-coord bundle fetch-launcher --mc 26.2 --server-root "$V"
```

**Mod-Liste aus dem Modpack übernehmen** (Ordner oder `mods.zip`):

```bash
sudo bigworld-coord bundle mods ~/mods.zip
sudo bigworld-coord bundle build
```

- Weggelassen werden reine Client-Mods (Sodium, Mod Menu), LOD-Mods (Voxy, LSS,
  Distant Horizons) und Do a Barrel Roll (stürzt mit Chunky ab).
- Chunky, C2ME und das C2ME-OpenCL-Modul bleiben als Generier-Helfer erhalten, auch wenn
  sie nicht in der Liste stehen. Fehlt Chunky ganz: `bundle fetch-modrinth chunky --mc 26.2`.
- Die Ausgabe markiert Mods mit eigener Weltgenerierung und warnt bei fehlenden
  Abhängigkeiten oder alten Intermediary-Namen.
- `build` zeigt die Mod-Liste mit Versionen. Das ist der Satz, mit dem generiert wird.
  Wurden schon Kacheln mit einem anderen Bundle generiert, verweigert `build` das,
  außer nach `reset --all`.

## 4. Dienst starten

```bash
sudo cp /opt/bigworld/src/packaging/bigworld-coordinator.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now bigworld-coordinator
journalctl -u bigworld-coordinator -f
```

Weboberfläche: **http://100.72.176.11:8770** (nur im Tailnet), mit dem Admin-Token anmelden.

## 5. Erreichbarkeit über Tailscale prüfen

Von deinem PC (nicht vom Server):

```powershell
curl http://100.72.176.11:8770/api/v1/info
```

Kommt eine Antwort mit `"service": "bigworld-coordinator"`, ist alles gut.
Kommt keine Antwort, blockiert vermutlich ufw. **Vorschlag** (nur nach deinem OK, sonst
werden keine ufw-Regeln geändert), genau eine Regel nur für das Tailscale-Interface:

```bash
sudo ufw allow in on tailscale0 to any port 8770 proto tcp comment 'bigworld-coordinator'
```

## Nützliche Befehle

```bash
sudo -u bigworld bigworld-coord status                 # Fortschritt im Terminal
sudo -u bigworld bigworld-coord compare "minecraft:overworld|0|0"   # Vergleich erneut
sudo -u bigworld bigworld-coord reset "minecraft:overworld|3|-2"    # eine Kachel neu
sudo -u bigworld bigworld-coord reset --all            # alles neu (löscht Staging)
sudo bigworld-coord seed "$V/world/data/minecraft/world_gen_settings.dat"
```

## Deploy (erst wenn alles fertig ist)

```bash
sudo bigworld-coord deploy
```

Zeigt vorher Größen, Ziel, Sicherung und Server-Status und verlangt die Eingabe
`WELT ERSETZEN`. Ablauf: Server über die Panel-API stoppen → alte Welt als tar nach
`backup_dir` sichern → alte Welt löschen → Staging-Welt einsetzen → Besitzer
`pterodactyl` → in `config/lss-server-config.yaml` `generation.enabled: false` setzen →
Server starten. Verändert wird ausschließlich
`/var/lib/pterodactyl/volumes/fd1a91d3-…/world` (fest im Code hinterlegt) und die LSS-Config.
