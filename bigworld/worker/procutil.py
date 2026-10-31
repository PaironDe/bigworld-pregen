"""Kindprozesse sauber an den Worker binden: stirbt der Worker, stirbt auch Java."""

from __future__ import annotations

import os
import signal
import subprocess
import sys


def popen_kwargs() -> dict:
    if sys.platform == "win32":
        return {"creationflags": subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP}
    if sys.platform.startswith("linux"):
        return {"preexec_fn": _linux_pdeathsig}
    return {"start_new_session": True}


def _linux_pdeathsig() -> None:  # läuft im Kindprozess vor exec
    try:
        import ctypes

        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        PR_SET_PDEATHSIG = 1
        libc.prctl(PR_SET_PDEATHSIG, signal.SIGTERM)
    except Exception:  # noqa: BLE001
        pass


_job = None


def attach(proc: subprocess.Popen) -> None:
    """Unter Windows: Prozess in ein Job-Objekt, das beim Schließen alle Kinder beendet."""
    global _job
    if sys.platform != "win32":
        return
    try:
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        if _job is None:
            _job = k32.CreateJobObjectW(None, None)

            class BASIC(ctypes.Structure):
                _fields_ = [
                    ("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD),
                ]

            class IOC(ctypes.Structure):
                _fields_ = [(n, ctypes.c_uint64) for n in (
                    "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                    "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

            class EXT(ctypes.Structure):
                _fields_ = [
                    ("BasicLimitInformation", BASIC), ("IoInfo", IOC),
                    ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t),
                ]

            info = EXT()
            info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            k32.SetInformationJobObject(_job, 9, ctypes.byref(info), ctypes.sizeof(info))
        handle = k32.OpenProcess(0x1F0FFF, False, proc.pid)
        if handle:
            k32.AssignProcessToJobObject(_job, handle)
            k32.CloseHandle(handle)
    except Exception:  # noqa: BLE001
        pass


def pid_alive_with(pid: int, needle: str) -> bool:
    """Läuft Prozess pid noch und enthält seine Kommandozeile needle?"""
    if pid <= 0:
        return False
    try:
        if sys.platform.startswith("linux"):
            cmd = open(f"/proc/{pid}/cmdline", "rb").read().decode(errors="replace")
            return needle in cmd
        if sys.platform == "win32":
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 f"(Get-CimInstance Win32_Process -Filter 'ProcessId={int(pid)}').CommandLine"],
                capture_output=True, text=True, timeout=20, creationflags=subprocess.CREATE_NO_WINDOW,
            ).stdout
            return needle in out
        os.kill(pid, 0)
        return False  # ohne Kommandozeile lieber nichts beenden
    except (OSError, subprocess.SubprocessError):
        return False


def kill(pid: int) -> None:
    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True,
                           creationflags=subprocess.CREATE_NO_WINDOW)
        else:
            os.kill(pid, signal.SIGKILL)
    except OSError:
        pass
