# Entscheidungen

Neueste oben. Format: Datum · Entscheidung · Begründung.

## 01.11.2026

- **Mischbetrieb verschiedener GPUs freigegeben.** Vergleich 2080 Super (Linux) gegen 4070
  (Windows) mit C2ME-OpenCL: Biome identisch, aber ~0,8 % der Blöcke verschieden (Erze,
  Deepslate-Übergänge, Dripstone) und vereinzelt andere Strukturstarts. An den Kachelgrenzen
  im Test aber kaum sichtbar (Stufen im Mittel wie natürliches Gelände, eine Stelle bis 15
  Blöcke). Nach Sichtprüfung im Spiel als akzeptabel bewertet.
- **Generierungsbereiche statt fester Radius.** Jeder mit Worker-Token legt in der Oberfläche
  Bereiche an; sie werden der Reihe nach abgearbeitet. `reset --all` löscht auch die Bereiche.
- **Do a Barrel Roll nicht im Generier-Bundle.** Bringt eine alte fabric-permissions-api mit,
  die auf 26.2 abstürzt, sobald Chunky einen Konsolenbefehl prüft.

## 31.10.2026

- **Fabric 26.2 bleibt.** Umstellung auf NeoForge 1.21.1 verworfen.
- **Kacheln 4 × 4 Regionen (2.048 Blöcke).** Feinere Prioritätsbereiche, weniger Verlust bei
  Abbruch. Overworld 15.360 Blöcke = 256 Kacheln (Randkacheln halb so breit).
- **Template-Job.** Erster Worker startet mit level.dat, datapacks/ und Weltgen-Einstellungen
  der alten Welt plus neuem Mod-Satz und lädt die initialisierte Vorlage hoch. Alle anderen
  starten davon. Laufzeitdaten (Wetter, Zufallssequenzen, `lss-timestamps.bin` usw.) bleiben draußen.
- **Bundle = späterer Server-Mod-Satz ohne LSS/DH/Voxy**, feste Versionen und SHA256,
  inkl. `config/`. Worker mit abweichendem Bundle bekommen keine Kacheln.
- **Determinismus-Test Pflicht**: Kachel `overworld 0,0` auf 2080 Super und 4070,
  NBT-Vergleich ohne `LastUpdate`/`InhabitedTime` bzw. Entity-UUIDs. Bei Abweichung: CPU.
- **Nur vollständige Regionen** werden hochgeladen; der Worker prüft jeden Chunk auf
  Status `minecraft:full` und generiert Fehlendes nach. Chunky generiert 32 Blöcke über
  den Kachelrand hinaus, die Nachbarregionen werden verworfen.
- **Kein Egg-Wechsel.** Pterodactyl bleibt Fabric 26.2.
- **Deploy nur per Befehl** mit getippter Bestätigung, Zielpfad fest im Code.
  Setzt in der LSS-Config `generation.enabled: false`.
- **LSS-Backfill-Fortschritt im Status: offen.** Unklar, welcher LSS-Befehl oder welche
  Log-Zeile den Fortschritt liefert. Erst klären, dann einbauen.
- **Worker ohne Abhängigkeiten** (nur Python-Standardbibliothek), eine Datei per PyInstaller,
  Oberfläche im Browser auf 127.0.0.1 mit Sitzungsschlüssel.
- **Koordinator lauscht nur auf der Tailscale-IP**, zusätzlich Worker- und Admin-Token.
