"""Helpers for launching and probing the monitor server process."""

from __future__ import annotations

import os
import platform
import subprocess
import sys
import tempfile
from pathlib import Path, PureWindowsPath

from PySide6.QtNetwork import QLocalSocket

from wave_monitor.constants import PIPE_NAME


def can_connect_to_server(name: str = PIPE_NAME, timeout_ms: int = 100) -> bool:
    """Return whether a local socket server is accepting connections."""
    sock = QLocalSocket()
    try:
        sock.connectToServer(name)
        ok = bool(sock.waitForConnected(timeout_ms))
        if ok:
            sock.disconnectFromServer()
            if sock.state() == QLocalSocket.ConnectedState:
                sock.waitForDisconnected(timeout_ms)
        return ok
    finally:
        sock.close()


def start_wave_monitor() -> None:
    """Start the monitor window as a detached Python subprocess."""
    executable = sys.executable
    if platform.system() == "Windows":
        executable = _windows_gui_executable(executable)
        _start_windows_detached([executable, "-m", "wave_monitor.window"])
    else:
        _start_posix_detached([executable, "-m", "wave_monitor.window"])


def _start_posix_detached(cmd: list[str]) -> None:
    _POSIX_DETACHED_LAUNCHER = (
        "import subprocess, sys\n"
        "subprocess.Popen(\n"
        "    sys.argv[1:],\n"
        "    stdin=subprocess.DEVNULL,\n"
        "    stdout=subprocess.DEVNULL,\n"
        "    stderr=subprocess.DEVNULL,\n"
        "    close_fds=True,\n"
        "    start_new_session=True,\n"
        ")\n"
    )
    subprocess.Popen(
        [sys.executable, "-c", _POSIX_DETACHED_LAUNCHER, *cmd],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        start_new_session=True,
    )


def _windows_gui_executable(executable: str) -> str:
    """Prefer pythonw.exe so Windows GUI launches do not get a console window."""
    path = PureWindowsPath(executable) if "\\" in executable else Path(executable)
    if path.name.lower() != "python.exe":
        return executable

    candidate = path.with_name("pythonw.exe")
    if os.path.exists(candidate):
        return str(candidate)
    return executable


def _start_windows_detached(cmd: list[str]) -> None:
    """Ask Explorer to launch the GUI outside notebook/kernel job objects."""
    script_path = _write_windows_cmd_launcher(cmd)
    if _windows_script_host_enabled():
        script_path = _write_windows_vbs_launcher(cmd)

    try:
        _open_with_explorer(script_path)
    except OSError:
        _open_with_explorer(_write_windows_cmd_launcher(cmd))


def _open_with_explorer(path: Path) -> None:
    creationflags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    creationflags |= getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0)
    subprocess.Popen(
        ["explorer.exe", str(path)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        creationflags=creationflags,
    )


def _write_windows_vbs_launcher(cmd: list[str]) -> Path:
    """Write a tiny VBS launcher that starts cmd without waiting.

    Windows notebooks and VS Code interactive kernels can run inside a Job
    Object. Direct child processes may remain in that job and get terminated
    on kernel restart, even with CREATE_BREAKAWAY_FROM_JOB. Opening this
    script through Explorer lets the regular shell broker launch the monitor
    from outside the kernel's job.
    """
    command = subprocess.list2cmdline(cmd)
    escaped_command = command.replace('"', '""')
    script = (
        'Set shell = CreateObject("WScript.Shell")\n'
        f'shell.Run "{escaped_command}", 0, False\n'
    )
    path = Path(tempfile.gettempdir()) / "wave_monitor_launch.vbs"
    path.write_text(script, encoding="utf-8")
    return path


def _write_windows_cmd_launcher(cmd: list[str]) -> Path:
    """Write a no-WSH fallback launcher.

    This can flash a console briefly, but it does not depend on Windows Script
    Host and still lets Explorer broker the final process outside the kernel.
    """
    command = subprocess.list2cmdline(cmd)
    script = f'@echo off\nstart "" /B {command}\n'
    path = Path(tempfile.gettempdir()) / "wave_monitor_launch.cmd"
    path.write_text(script, encoding="utf-8")
    return path


def _windows_script_host_enabled() -> bool:
    try:
        import winreg
    except ImportError:
        return True

    subkey = r"Software\Microsoft\Windows Script Host\Settings"
    for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(root, subkey) as key:
                value, _ = winreg.QueryValueEx(key, "Enabled")
        except FileNotFoundError:
            continue
        except OSError:
            continue
        if int(value) == 0:
            return False
    return True
