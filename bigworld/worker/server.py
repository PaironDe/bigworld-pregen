"""Fabric-Dedicated-Server im Hintergrund starten und Chunky über die Konsole steuern."""

from __future__ import annotations

import collections
import queue
import re
import socket
import subprocess
import threading
import time
from pathlib import Path

from . import procutil

RE_DONE = re.compile(r"Done \([\d.,]+s\)!")
RE_SAVED = re.compile(r"Saved the game|Saved the world")
RE_PROGRESS = re.compile(
    r"Task running for (\S+?)\.? Processed: (\d+) chunks \(([\d.,]+)%\)"
    r"(?:, ETA: ([\d:]+))?(?:, Rate: ([\d.,]+) cps)?"
)
RE_FINISHED = re.compile(r"Task finished for (\S+?)\.? Processed: (\d+) chunks")
RE_CHUNKY_PROBLEM = re.compile(r"\[Chunky\].*(No task|not found|Invalid|Unknown|cancel)", re.I)
RE_FATAL = re.compile(r"(Exception in server tick loop|Encountered an unexpected exception|"
                      r"Failed to start the minecraft server|OutOfMemoryError|Incompatible mod set|"
                      r"Incompatible mods found|Mod resolution failed|Failed to bind to port)")


class ServerError(Exception):
    def __init__(self, message: str, tail: list[str] | None = None):
        super().__init__(message)
        self.tail = tail or []


class Aborted(Exception):
    pass


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _num(s: str | None) -> float:
    return float(s.replace(",", ".")) if s else 0.0


def _hms(s: str | None) -> int | None:
    if not s:
        return None
    parts = [int(x) for x in s.split(":")]
    sec = 0
    for p in parts:
        sec = sec * 60 + p
    return sec


class MinecraftServer:
    def __init__(self, server_dir: Path, java: str, ram_gb: int, extra_args: list[str], log_path: Path,
                 on_line=None):
        self.server_dir = server_dir
        self.java = java
        self.ram_gb = ram_gb
        self.extra_args = extra_args
        self.log_path = log_path
        self.on_line = on_line
        self.proc: subprocess.Popen | None = None
        self.lines: queue.Queue[str] = queue.Queue()
        self.tail: collections.deque[str] = collections.deque(maxlen=60)
        self._reader: threading.Thread | None = None

    @property
    def pid(self) -> int | None:
        return self.proc.pid if self.proc else None

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self, launcher: str) -> None:
        cmd = [
            self.java,
            f"-Xmx{self.ram_gb}G",
            f"-Xms{min(2, self.ram_gb)}G",
            "--enable-native-access=ALL-UNNAMED",
            "-Dfile.encoding=UTF-8",
            *self.extra_args,
            "-jar", launcher, "nogui",
        ]
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.proc = subprocess.Popen(
            cmd, cwd=self.server_dir, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1, **procutil.popen_kwargs(),
        )
        procutil.attach(self.proc)
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()

    def _read(self) -> None:
        assert self.proc and self.proc.stdout
        with open(self.log_path, "a", encoding="utf-8") as log:
            for line in self.proc.stdout:
                line = line.rstrip("\r\n")
                log.write(line + "\n")
                log.flush()
                self.tail.append(line)
                self.lines.put(line)
                if self.on_line:
                    self.on_line(line)
        self.lines.put("\0EXIT")

    def send(self, command: str) -> None:
        if not self.alive():
            raise ServerError("Server läuft nicht", list(self.tail))
        assert self.proc and self.proc.stdin
        self.proc.stdin.write(command + "\n")
        self.proc.stdin.flush()

    def wait_for(self, pattern: re.Pattern, timeout: float, abort: threading.Event | None = None,
                 on_line=None) -> re.Match:
        end = time.time() + timeout
        while True:
            if abort is not None and abort.is_set():
                raise Aborted()
            left = end - time.time()
            if left <= 0:
                raise ServerError(f"Zeitüberschreitung beim Warten auf '{pattern.pattern}'", list(self.tail))
            try:
                line = self.lines.get(timeout=min(1.0, left))
            except queue.Empty:
                continue
            if line == "\0EXIT":
                raise ServerError(f"Server hat sich beendet (Code {self.proc.poll() if self.proc else '?'})",
                                  list(self.tail))
            if RE_FATAL.search(line):
                # Stacktrace folgt in den nächsten Zeilen: kurz mitlesen, damit die Ursache im Protokoll steht
                trace = [line]
                stop_at = time.time() + 3
                while time.time() < stop_at and len(trace) < 60:
                    try:
                        nxt = self.lines.get(timeout=0.5)
                    except queue.Empty:
                        continue
                    if nxt == "\0EXIT":
                        break
                    trace.append(nxt)
                causes = [t.strip() for t in trace if t.lstrip().startswith("Caused by:")]
                detail = causes[-1] if causes else (trace[1].strip() if len(trace) > 1 else "")
                raise ServerError(f"Server meldet einen Fehler: {line.strip()[:160]} | {detail[:300]} "
                                  f"(vollständig in {self.log_path})", trace)
            if on_line:
                on_line(line)
            m = pattern.search(line)
            if m:
                return m

    def drain(self) -> None:
        while True:
            try:
                self.lines.get_nowait()
            except queue.Empty:
                return

    def stop(self, timeout: float = 180) -> None:
        if not self.alive():
            return
        try:
            self.send("stop")
        except (ServerError, OSError):
            pass
        assert self.proc
        try:
            self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.kill()

    def kill(self) -> None:
        if self.proc and self.proc.poll() is None:
            procutil.kill(self.proc.pid)
            try:
                self.proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                pass


class MinecraftGenerator:
    """Erzeugt Vorlage und Kacheln mit einem echten Fabric-Server + Chunky."""

    START_TIMEOUT = 20 * 60  # erster Start lädt Minecraft und Bibliotheken herunter
    STALL_TIMEOUT = 15 * 60  # so lange ohne Chunky-Fortschritt gilt als hängend

    def __init__(self, server_dir: Path, java: str, ram_gb: int, extra_args: list[str], launcher: str,
                 log_path: Path, log=print, on_pid=None):
        self.server_dir = server_dir
        self.java = java
        self.ram_gb = ram_gb
        self.extra_args = extra_args
        self.launcher = launcher
        self.log_path = log_path
        self.log = log
        self.on_pid = on_pid
        self.server: MinecraftServer | None = None

    def _start(self, abort: threading.Event) -> MinecraftServer:
        srv = MinecraftServer(self.server_dir, self.java, self.ram_gb, self.extra_args, self.log_path)
        self.server = srv
        srv.start(self.launcher)
        if self.on_pid:
            self.on_pid(srv.pid)
        self.log("Server startet ...")
        srv.wait_for(RE_DONE, self.START_TIMEOUT, abort)
        self.log("Server bereit")
        return srv

    def _save_and_stop(self, srv: MinecraftServer) -> None:
        srv.drain()
        srv.send("save-all flush")
        try:
            srv.wait_for(RE_SAVED, 600)
        except ServerError:
            self.log("Keine Speicherbestätigung erhalten, stoppe trotzdem")
        srv.stop()
        if self.on_pid:
            self.on_pid(None)

    def shutdown(self) -> None:
        if self.server and self.server.alive():
            self.server.stop(60)
        if self.on_pid:
            self.on_pid(None)

    def make_template(self, abort: threading.Event) -> None:
        srv = self._start(abort)
        self._save_and_stop(srv)

    def generate(self, tile, dim_path: str, abort: threading.Event, progress) -> None:
        dim = tile.dim
        chunky = tile.chunky_params()
        srv = self._start(abort)
        try:
            srv.drain()
            for cmd in (
                f"chunky world {dim}",
                "chunky shape rectangle",
                f"chunky center {chunky['center_x']} {chunky['center_z']}",
                f"chunky radius {chunky['radius_x']} {chunky['radius_z']}",
                "chunky start",
            ):
                srv.send(cmd)
                time.sleep(0.3)
            self.log(f"Chunky gestartet: {dim}, Mitte {chunky['center_x']} {chunky['center_z']}, "
                     f"Radius {chunky['radius_x']} x {chunky['radius_z']}")

            def on_line(line: str) -> None:
                m = RE_PROGRESS.search(line)
                if m and m.group(1).rstrip(".") == dim:
                    progress(
                        chunks=int(m.group(2)),
                        percent=_num(m.group(3)),
                        eta=_hms(m.group(4)),
                        cps=_num(m.group(5)),
                    )
                elif RE_CHUNKY_PROBLEM.search(line):
                    self.log(f"Chunky meldet: {line.strip()[-160:]}")

            while True:
                m = srv.wait_for(RE_FINISHED, self.STALL_TIMEOUT, abort, on_line=on_line)
                if m.group(1).rstrip(".") == dim:
                    break
            progress(chunks=int(m.group(2)), percent=100.0, eta=0, cps=0)
            self._save_and_stop(srv)
        except BaseException:
            srv.kill()
            if self.on_pid:
                self.on_pid(None)
            raise


def tail_text(lines: list[str], n: int = 15) -> str:
    return "\n".join(lines[-n:])
