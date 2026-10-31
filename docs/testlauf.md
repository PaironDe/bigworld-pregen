# Testlauf: 2.000 Blöcke Radius, serversoeren (2080 Super) + Sörens PC (4070)

Ziel: Ablauf prüfen und **Determinismus-Test** 2080 Super gegen 4070.
Die Kachel `minecraft:overworld|0|0` wird von beiden generiert und chunkweise verglichen.

## 0. Dimensionspfade prüfen (einmalig)

```bash
V=/var/lib/pterodactyl/volumes/fd1a91d3-f2fd-441c-b99f-cad2457c8ab3
sudo find "$V/world" -maxdepth 4 -type d -name region
```

Die drei Pfade (ohne `/region`) müssen in `/etc/bigworld/coordinator.toml` bei
`path` stehen. Vorbelegt ist `dimensions/minecraft/overworld`, `…/the_nether`, `…/the_end`.
Stimmt das nicht, merkt es der Worker bei der ersten Kachel und nennt den richtigen Pfad.

## 1. Koordinator vorbereiten

Nach [server-installation.md](server-installation.md), mit Radius `2000` für alle drei
Dimensionen (Standard in der Beispielkonfiguration). Ergibt 12 Kacheln à 2.048 × 2.048 Blöcke.

## 2. Worker auf serversoeren (2080 Super)

Der Worker ist im venv schon mitinstalliert. Vorher eine eventuell laufende grafische
Sitzung (Steam/Xorg) beenden, die belegt dieselbe GPU.

```bash
sudo mkdir -p /srv/bigworld-worker && sudo chown soeren: /srv/bigworld-worker
tmux new -s worker
/opt/bigworld/venv/bin/bigworld-worker --headless --data-dir /srv/bigworld-worker \
  --url http://100.72.176.11:8770 --token <worker_token> --name serversoeren --ram 12
# Strg+B, D zum Abkoppeln
```

**GPU-Last senken** (die 2080 Super hing früher mit 16 gleichzeitigen Aufgaben):
Eigene Config-Dateien unter `/srv/bigworld-worker/overrides/config/…` überschreiben die
aus dem Bundle, nur auf diesem Rechner. Der Koordinator zeigt in der Worker-Tabelle an,
welche Overrides aktiv sind. Mods lassen sich so nicht verändern.

Hängt die GPU wieder (`Stage1Cache timed out` im Log `/srv/bigworld-worker/logs/server.log`):
Der Worker bricht nach 15 Minuten ohne Fortschritt ab, gibt die Kachel frei und versucht
es erneut.

## 3. Worker auf dem PC (4070)

`BigWorldWorker-windows.exe` aus den GitHub-Actions-Artefakten starten, Token eintragen,
RAM auf 12–16, **Generieren**.

## 4. Was passieren sollte

1. Der erste Worker bekommt den **Template-Job**: Er startet einen Server mit
   `level.dat`, Datapacks und Weltgen-Einstellungen der alten Welt plus neuem Mod-Satz und
   lädt die initialisierte Vorlage hoch. Dauert beim allerersten Mal länger, weil
   Minecraft und Bibliotheken heruntergeladen werden.
2. Danach holen beide Worker Kacheln. Die Vergleichskachel `overworld 0,0` bekommt der
   zweite Worker zusätzlich als **Vergleichsauftrag**.
3. Weboberfläche → *Determinismus-Test*: **Identisch** oder **Unterschiede** mit Beispielen.

## 5. Auswerten

- **Identisch**: GPU-Generierung ist zwischen 2080 Super und 4070 deterministisch.
- **Unterschiede**: Beispiele ansehen. Weichen nur einzelne Chunks ab, eher OpenCL.
  Dann das OpenCL-Modul aus `bundle-src/mods` entfernen, `bundle build`, `reset --all`,
  erneut testen (CPU).
- **Nähte im Spiel**: Staging-Welt auf den PC holen und im Einzelspieler mit dem Modpack
  an den Kachelgrenzen (Vielfache von 2.048) umsehen:

  ```bash
  sudo tar -cf ~/staging-test.tar -C /srv/bigworld/staging world && sudo chown soeren ~/staging-test.tar
  ```

## 6. Danach: echte Generierung

```bash
sudo nano /etc/bigworld/coordinator.toml     # Overworld radius = 15360, Nether/End 2000
sudo -u bigworld bigworld-coord reset --all
sudo systemctl restart bigworld-coordinator
```

Overworld mit 15.360 Blöcken = 256 Kacheln, Nether und End je 4.
