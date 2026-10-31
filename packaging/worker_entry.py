"""Einstiegspunkt für die ausführbare Worker-Datei (PyInstaller)."""
import sys

from bigworld.worker.main import main

if __name__ == "__main__":
    sys.exit(main())
