# Kontext (für Menschen und KI-Assistenten)

Stand: 31.10.2026. Bitte aktuell halten, wenn sich am Setup etwas ändert.

## Ziel

Neue Welt für den Minecraft-Server „Minecraft Big World“ verteilt auf mehreren PCs
vorgenerieren. Mitspieler starten nur ein Programm und klicken „Generieren“.
Spieler sollen später extreme Fernsicht haben: Voxy auf den Clients, LOD Server Support (LSS)
auf dem Server.

## Plattform

- Minecraft **Fabric 26.2**, Java 25, Chunky, C2ME + C2ME-OpenCL-Modul
- Datapacks: JJThunder To The Max, Trek, Towns and Towers (26.2-Versionen, wie bisher)
- Gleicher Seed wie die bisherige Welt (Seed in `data/minecraft/world_gen_settings.dat`)
- Neue Mods mit Weltgenerierung: Tech Reborn 6.1.1, Mystical Agriculture (inoffizieller
  Fabric-Port), Advanced Reborn. Finale Liste kommt als `.mrpack`.
- Größe: Overworld 15.360 Blöcke Radius, Nether und End je 2.000

## Rechner

| Rechner | Rolle | Hardware | Tailscale |
|---|---|---|---|
| serversoeren | Koordinator, optional Worker | Ubuntu 24.04, i7-9700K, 32 GB, RTX 2080 Super | 100.72.176.11 |
| Sörens PC | Worker | RTX 4070, 32 GB, Windows | desktop-k1btrq6 |
| Björns PC | Worker | RTX 4070, 32 GB (Klon), Windows | bjoern-m-1 (anderes Tailscale-Konto) |

## Server-Pfade

- Pterodactyl: `/etc/pterodactyl/config.yml`, Volumes unter `/var/lib/pterodactyl/volumes/`
  (auf der NVMe, Root-Dateisystem, nicht auf separater Platte)
- **Nur dieses Volume**: `/var/lib/pterodactyl/volumes/fd1a91d3-f2fd-441c-b99f-cad2457c8ab3`
- Koordinator: Code `/opt/bigworld`, Daten `/srv/bigworld`, Config `/etc/bigworld/coordinator.toml`
- Minecraft-Port: `192.168.2.116:27017`

## Regeln auf dem Server

- Nur das Volume `fd1a91d3-…` bearbeiten, und das nur im bestätigten Deploy-Schritt
- Andere Volumes, `/opt/smarthome`, `~/immich` und alle Docker-Container nicht anfassen
- ufw-Regeln nur nach Rückfrage ändern
- Wildcards unter `volumes/` immer als `sudo sh -c "…"` (sonst löst die eigene Shell sie auf)
- Server vor Änderungen an Welt oder Mods stoppen
- Docker-Ports umgehen ufw; der Koordinator läuft nicht in Docker, für ihn gilt ufw

## Architektur

Siehe Claude-Doc „Big World Pregen – Auswertung & Architektur“ und den Code:

- `bigworld/common`: Kacheln (4 × 4 Regionen), Regionsdateien, NBT, Paketformat, Vergleich
- `bigworld/coordinator`: FastAPI + SQLite, Leases, Template-Job, Upload, Merge, Deploy
- `bigworld/worker`: Sync, Java, Fabric-Server + Chunky, Prüfung, Upload, lokale Oberfläche
- `bigworld/web`: Karte und Oberflächen (Koordinator und Worker)

## Getrennt davon: Refined-Storage-Port

Eigenes Repo (Fork des offiziellen Refined Storage, Fabric 3.2.1 für 26.1.2 → 26.2),
nur private Nutzung, Build per GitHub Actions. Nicht in die große Welt, bevor ein Test mit
Tech Reborn bestanden ist. Keine weiteren Ports für Mekanism, Immersive Engineering,
Industrial Foregoing, Modern Industrialization, Ad Astra.
