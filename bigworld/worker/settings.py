"""Einstellungen und Ablageorte des Workers."""

from __future__ import annotations

import json
import os
import platform
import socket
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

DEFAULT_URL = "http://100.72.176.11:8770"


def default_data_dir() -> Path:
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "BigWorldWorker"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "BigWorldWorker"
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "bigworld-worker"


def total_ram_gb() -> float:
    try:
        if sys.platform == "win32":
            import ctypes

            class MEMSTAT(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("sullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            st = MEMSTAT()
            st.dwLength = ctypes.sizeof(MEMSTAT)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st))
            return st.ullTotalPhys / 1024**3
        if Path("/proc/meminfo").exists():
            for line in Path("/proc/meminfo").read_text().splitlines():
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) / 1024**2
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 1024**3
    except Exception:  # noqa: BLE001
        return 0.0


def default_ram_gb() -> int:
    total = total_ram_gb()
    if total <= 0:
        return 8
    return int(max(4, min(16, total * 0.5)))


@dataclass
class Settings:
    coordinator_url: str = DEFAULT_URL
    token: str = ""
    admin_token: str = ""
    name: str = field(default_factory=lambda: socket.gethostname()[:40])
    ram_gb: int = field(default_factory=default_ram_gb)
    java_path: str = ""          # leer = automatisch Java 25 herunterladen
    extra_jvm_args: str = ""
    keep_going: bool = True      # nach einer Kachel automatisch die nächste holen

    @classmethod
    def load(cls, path: Path) -> "Settings":
        s = cls()
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                for k, v in data.items():
                    if hasattr(s, k):
                        setattr(s, k, v)
            except (OSError, json.JSONDecodeError):
                pass
        return s

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")
        tmp.replace(path)

    def public(self) -> dict:
        d = asdict(self)
        d["token"] = "gesetzt" if self.token else ""
        d["admin_token"] = "gesetzt" if self.admin_token else ""
        return d


def system_info() -> dict:
    info = {
        "os": f"{platform.system()} {platform.release()}",
        "arch": platform.machine(),
        "cpus": os.cpu_count(),
        "ram_gb": round(total_ram_gb(), 1),
        "gpu": None,
        "driver": None,
    }
    try:
        import subprocess

        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,driver_version", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).stdout.strip().splitlines()
        if out:
            name, _, driver = out[0].partition(",")
            info["gpu"] = name.strip()
            info["driver"] = driver.strip()
    except (OSError, ValueError, Exception):  # noqa: BLE001
        pass
    return info
