# Entscheidungen

Neueste oben. Format: Datum · Entscheidung · Begründung.

## 01.11.2026

- **Einzelne Worker pausierbar** (Knopf im Koordinator bzw. `worker pause <name>`): Der Server
  soll beim Spielen nicht mitrechnen, nur verteilen, zusammenführen und LODs bauen.
- **Deploy ohne globale Pause.** Eine Sperrdatei hält neue Zusammenführungen kurz an, laufende
  werden abgewartet; danach geht es in einer neuen Staging-Welt weiter.
- **Ad Astra (privater 26.2-Port, Stand port.2) kommt ins Bundle**, zusammen mit
  Towns and Towers (+ CristelLib) und Trek. Getestet auf einem separaten Testserver und im
  Spiel (Rakete, Mond, Seite an Seite mit Space Reloaded). port.2 definiert die 11
  Tageszeit-Uhren selbst und läuft mit Fabric Loader 0.19.5, kein Loader-Wechsel.
- **Neu generiert werden Oberwelt, Nether und End.** Ad Astra fügt in bestehende Dimensionen nur
  die Ölquelle (Ozeane der Oberwelt) ein, T&T nur Oberwelt-Strukturen, Trek aber auch
  Nether-Strukturen und überarbeitete End-Städte. Mond/Mars von Space Reloaded dürfen bleiben,
  wenn die Stichprobe (`recheck`: gleiche Kachel, gleicher Worker, neues Bundle) identisch ist.
- **Alle fünf Ad-Astra-Planeten ±4000 vorgenerieren** (Mond, Mars, Venus, Merkur, Glacio).
  Die Orbits sind leer und werden nicht vorgeneriert.
- **Live-Merge mit Dauerfreigabe** (Sören): Nach dem ersten Deploy werden fertige Kacheln
  nachts (Timer 04:32) eingefügt: Ankündigung im Chat, `save-all`, Server über das Panel
  stoppen, **nur fehlende oder unfertige Chunks** einfügen (vorhandene fertige Chunks, also alles,
  wo jemand war, bleibt unangetastet), Besitzer setzen, starten. Nicht im laufenden Betrieb, weil
  Minecraft/C2ME Regionsdateien offen halten und die Regel „Server vor Änderungen an der Welt
  stoppen" gilt. Läuft nur, wenn die Welt per Deploy eingesetzt wurde (Markierungsdatei
  `bigworld-deploy.json`), der Seed passt und der Live-Server dieselben Mods wie das Bundle hat.
- **Deploy prüft die Live-Mods.** Fehlen Bundle-Mods auf dem Live-Server, bricht der Deploy ab
  (Blöcke aus fehlenden Mods würden beim Laden gelöscht). `deploy --sync-mods` gleicht im
  bestätigten Deploy-Schritt an: alter `mods/`-Ordner wird gesichert, Bundle-Mods kommen dazu,
  reine Live-Mods (LSS, Lithium, ScalableLux, Do a Barrel Roll …) bleiben.
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
