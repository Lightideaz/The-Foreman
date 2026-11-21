"""Process helpers: Windows-safe liveness checks, process-tree kill, logged runs, IDE detection.

Liveness never uses the kill-with-signal-0 idiom: on Windows signal 0 equals CTRL_C_EVENT and any
other value terminates the process. Windows uses OpenProcess/GetExitCodeProcess instead.
"""
from __future__ import annotations

import csv
import io
import os
import subprocess
import time
from pathlib import Path

IS_WINDOWS = os.name == "nt"

if IS_WINDOWS:
    import ctypes
    from ctypes import wintypes

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _k32.OpenProcess.restype = wintypes.HANDLE
    _k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    _k32.GetExitCodeProcess.restype = wintypes.BOOL
    _k32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                ctypes.POINTER(wintypes.DWORD)]
    _k32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    _k32.CloseHandle.argtypes = [wintypes.HANDLE]
    _k32.CloseHandle.restype = wintypes.BOOL

_QUERY_LIMITED = 0x1000
_STILL_ACTIVE = 259


def image_of(pid: int | None) -> str | None:
    """Full image path of a running process, "" if running but unreadable, None if not running."""
    if not pid or pid <= 0:
        return None
    if not IS_WINDOWS:
        if not Path(f"/proc/{pid}").exists():
            return None
        try:
            return str(Path(f"/proc/{pid}/exe").resolve())
        except OSError:
            return ""
    handle = _k32.OpenProcess(_QUERY_LIMITED, False, pid)
    if not handle:
        return None
    try:
        code = wintypes.DWORD()
        if not _k32.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value != _STILL_ACTIVE:
            return None
        buf = ctypes.create_unicode_buffer(32768)
        size = wintypes.DWORD(len(buf))
        if not _k32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return ""
        return buf.value
    finally:
        _k32.CloseHandle(handle)


def is_alive(pid: int | None, image: str | None = None) -> bool:
    """True if pid is running and, when image is given, its executable has the same file name."""
    found = image_of(pid)
    if found is None:
        return False
    if image and found:
        return os.path.normcase(Path(found).name) == os.path.normcase(Path(image).name)
    return True


def kill_tree(pid: int | None) -> bool:
    if not pid or pid <= 0:
        return False
    if IS_WINDOWS:
        r = subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, timeout=60)
    else:
        r = subprocess.run(["kill", "-9", f"-{pid}"], capture_output=True, timeout=60)
    return r.returncode == 0


def popen_kwargs() -> dict:
    """Start children in their own process group so a tree kill reaches everything they spawn."""
    if IS_WINDOWS:
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def run_logged(cmd: str | list[str], cwd: Path, timeout_s: float, log_path: Path,
               shell: bool = False, env: dict | None = None) -> dict:
    start = time.monotonic()
    with open(log_path, "wb") as log:
        p = subprocess.Popen(cmd, cwd=cwd, shell=shell, stdout=log, stderr=subprocess.STDOUT,
                             stdin=subprocess.DEVNULL, env=env, **popen_kwargs())
        try:
            code, timed_out = p.wait(timeout=timeout_s), False
        except subprocess.TimeoutExpired:
            kill_tree(p.pid)
            code, timed_out = p.wait(), True
    return {"exit_code": code, "timed_out": timed_out, "duration_s": round(time.monotonic() - start, 1)}


def running_image_names(names: tuple[str, ...]) -> list[str]:
    if not IS_WINDOWS:
        return []
    try:
        out = subprocess.run(["tasklist", "/FO", "CSV", "/NH"], capture_output=True, timeout=30).stdout
    except (OSError, subprocess.TimeoutExpired):
        return []
    text = out.decode("utf-8", errors="replace")
    wanted = {n.lower() for n in names}
    return [row[0] for row in csv.reader(io.StringIO(text)) if row and row[0].lower() in wanted]


def antigravity_ide_running() -> bool:
    """The IDE's language server runs only while the IDE is open; agy's sidecars do not count."""
    return bool(running_image_names(("language_server.exe",)))
