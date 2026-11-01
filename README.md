# Big World · Weltgenerierung

Mit diesem Programm hilft dein PC mit, die Welt für unseren Minecraft-Server
**Minecraft Big World** vorzugenerieren. Jeder PC bekommt Stücke der Welt
(Kacheln) zugeteilt, generiert sie und schickt sie zurück an den Server.

## So geht's

1. **Tailscale** muss laufen und du musst im Tailnet sein (Sören fragen).
2. Lade die passende Datei herunter
   (Repo → **Actions** → neuester grüner Lauf → *Artifacts*, oder unter **Releases**):
   - Windows: `BigWorldWorker-windows.exe`
   - Linux: `BigWorldWorker-linux` (danach `chmod +x BigWorldWorker-linux`)
3. Starten (Doppelklick). Es öffnet sich ein Fenster mit Text **und** eine Seite im Browser.
4. Beim ersten Mal rechts unter **Einstellungen**:
   - **Token** eintragen (bekommst du von Sören)
   - **Arbeitsspeicher**: etwa die Hälfte deines RAMs, z. B. `12` bei 32 GB
   - **Speichern**
5. **Generieren** klicken. Fertig.

Alles andere passiert automatisch: Java herunterladen, Mods und Einstellungen
abgleichen, Minecraft-Server im Hintergrund starten, generieren, prüfen, hochladen,
nächste Kachel holen.

## Bereiche festlegen

Was generiert wird, legt ihr selbst fest: Auf der Worker-Seite unter **Bereiche** einen
Bereich eintragen (Mitte + Radius oder Rechteck, oder auf der Karte aufziehen) und
**Hinzufügen**. Bereiche werden der Reihe nach abgearbeitet, jeweils von der Mitte nach außen.
**Hinzufügen und vorziehen** setzt einen Bereich an den Anfang der Warteschlange.
Unter der Eingabe steht, wie viele neue Chunks das kostet und wie lange es etwa dauert.

## Gut zu wissen

- **Das schwarze Fenster offen lassen**, solange generiert wird. Schließen beendet den Worker.
- **Stoppen** gibt die aktuelle Kachel frei, ein anderer PC übernimmt sie. Fertige Kacheln,
  die noch hochgeladen werden, gehen beim nächsten Start automatisch weiter.
- Stromausfall oder Neustart sind kein Problem: beim nächsten Start einfach wieder **Generieren**.
- **Windows-Warnung „Der Computer wurde durch Windows geschützt“**: Die Datei ist nicht
  signiert. *Weitere Informationen* → *Trotzdem ausführen*. Manche Virenscanner schlagen bei
  solchen Python-Programmen fälschlich an; der Quellcode liegt komplett in diesem Repo.
- **Platz**: etwa 5 GB frei auf dem Laufwerk mit deinem Benutzerordner.
- **Daten**: liegen unter `%LOCALAPPDATA%\BigWorldWorker` (Windows) bzw.
  `~/.local/share/bigworld-worker` (Linux). Kann nach dem Projekt gelöscht werden.
- Während der Generierung ist der PC stark ausgelastet. Spielen nebenbei geht, wird aber zäh.
- Mit **Generieren** akzeptierst du die [Minecraft-EULA](https://www.minecraft.net/eula)
  für den lokalen Hintergrund-Server.

## Für Sören / Admins

- [docs/server-installation.md](docs/server-installation.md): Koordinator auf serversoeren einrichten
- [docs/testlauf.md](docs/testlauf.md): Testlauf mit Determinismus-Test
- [docs/kontext.md](docs/kontext.md): Setup und Regeln (auch für KI-Assistenten)
- [docs/entscheidungen.md](docs/entscheidungen.md): getroffene Entscheidungen
