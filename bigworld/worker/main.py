"""Startpunkt des Workers: Doppelklick öffnet die Oberfläche im Browser."""

from __future__ import annotations

import argparse
import signal
import sys
import threading
import time
import webbrowser
from pathlib import Path

from .. import __version__
from .engine import Engine
from .settings import default_data_dir


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="bigworld-worker", description=f"Big-World-Worker {__version__}")
    p.add_argument("--data-dir", help=f"Arbeitsordner (Standard: {default_data_dir()})")
    p.add_argument("--headless", action="store_true", help="ohne Browser, sofort generieren (für Server)")
    p.add_argument("--url", help="Koordinator-Adresse")
    p.add_argument("--token", help="Worker-Token")
    p.add_argument("--name", help="Name dieses Rechners")
    p.add_argument("--ram", type=int, help="RAM für Minecraft in GB")
    p.add_argument("--port", type=int, default=8771, help="Port der lokalen Oberfläche")
    p.add_argument("--no-browser", action="store_true", help="Browser nicht automatisch öffnen")
    p.add_argument("--simulate", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--simulate-delay", type=float, default=0.0, help=argparse.SUPPRESS)
    p.add_argument("--simulate-diff", action="store_true", help=argparse.SUPPRESS)
    args = p.parse_args(argv)

    base = Path(args.data_dir) if args.data_dir else default_data_dir()
    simulate = {"delay": args.simulate_delay, "diff": args.simulate_diff} if args.simulate else None
    other = _running_instance(base)
    if other:
        print(f"Der Big-World-Worker läuft auf diesem PC bereits (Prozess {other}).")
        print("Bitte das andere schwarze Fenster benutzen oder dort 'Beenden' klicken und neu starten.")
        if not args.headless:
            try:
                input("Enter drücken zum Schließen ...")
            except EOFError:
                pass
        return 3
    engine = Engine(base, simulate=simulate)
    changes = {k: v for k, v in {"coordinator_url": args.url, "token": args.token, "name": args.name,
                                 "ram_gb": args.ram}.items() if v}
    if changes:
        engine.update_settings(changes)

    print(f"Big-World-Worker {__version__}, Arbeitsordner: {base}", flush=True)
    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, lambda *_: stop.set())

    if args.headless:
        if not engine.settings.token:
            print("Kein Token gesetzt. Mit --token angeben.")
            return 2
        engine.start()
        while not stop.is_set() and (engine.running or engine.state["outbox"]):
            stop.wait(2)
        engine.stop()
        _wait_for_engine(engine)
        return 0 if engine.status["phase"] != "error" else 1

    from .ui import UI

    ui = UI(engine, args.port)
    ui.serve()
    print(f"Oberfläche: {ui.url}", flush=True)
    print("Dieses Fenster offen lassen. Beenden mit Strg+C oder dem Knopf 'Beenden'.", flush=True)
    if not args.no_browser:
        webbrowser.open(ui.url)
    while not stop.is_set() and not ui.quit.is_set():
        stop.wait(1)
    engine.stop()
    _wait_for_engine(engine)
    return 0


def _running_instance(base: Path) -> int | None:
    """Läuft schon ein Worker mit diesem Arbeitsordner? Dann dessen PID, sonst Sperre setzen."""
    import atexit
    import os

    base.mkdir(parents=True, exist_ok=True)
    lock = base / "worker.pid"
    try:
        pid = int(lock.read_text().strip())
    except (OSError, ValueError):
        pid = 0
    if pid and pid != os.getpid() and _pid_alive(pid):
        return pid
    lock.write_text(str(os.getpid()))
    atexit.register(lambda: lock.unlink(missing_ok=True) if lock.exists() and lock.read_text().strip() == str(os.getpid()) else None)
    return None


def _pid_alive(pid: int) -> bool:
    import os

    if sys.platform == "win32":
        import ctypes

        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        code = ctypes.c_ulong()
        ok = k32.GetExitCodeProcess(h, ctypes.byref(code))
        k32.CloseHandle(h)
        return bool(ok) and code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _wait_for_engine(engine: Engine, timeout: float = 200) -> None:
    end = time.time() + timeout
    while engine.thread and engine.thread.is_alive() and time.time() < end:
        time.sleep(0.5)


if __name__ == "__main__":
    sys.exit(main())
