"""Elevated Task Scheduler entry for starting RemotePC as administrator."""

from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from xml.sax.saxutils import escape

from .autostart import launcher_path, pythonw_path
from .config import default_config_path

TASK_NAME = "RemotePC"
CREATE_NO_WINDOW = 0x08000000


def _config_arg(config_path: Path | None) -> list[str]:
    path = (config_path or default_config_path()).resolve()
    if path == default_config_path().resolve():
        return []
    return ["--config", str(path)]


def task_command(config_path: Path | None = None) -> str:
    args = [str(pythonw_path()), str(launcher_path()), *_config_arg(config_path)]
    return subprocess.list2cmdline(args)


def is_installed() -> bool:
    try:
        result = subprocess.run(
            ["schtasks.exe", "/Query", "/TN", TASK_NAME],
            capture_output=True,
            text=True,
            encoding="oem",
            errors="replace",
            creationflags=CREATE_NO_WINDOW,
            timeout=10,
        )
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _run_schtasks(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["schtasks.exe", *args],
        capture_output=True,
        text=True,
        encoding="oem",
        errors="replace",
        creationflags=CREATE_NO_WINDOW,
        timeout=30,
    )


def _user_id() -> str:
    domain = os.environ.get("USERDOMAIN", "").strip()
    user = os.environ.get("USERNAME", "").strip()
    return f"{domain}\\{user}" if domain else user


def task_xml(config_path: Path | None = None) -> str:
    launcher = str(launcher_path())
    extra = _config_arg(config_path)
    config_args = f' --config "{extra[1]}"' if extra else ""
    arguments = f'"{launcher}"{config_args}'
    user = _user_id()
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Author>{escape(user)}</Author>
    <Description>RemotePC host (elevated autostart)</Description>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <UserId>{escape(user)}</UserId>
    </LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>{escape(user)}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>HighestAvailable</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>7</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{escape(str(pythonw_path()))}</Command>
      <Arguments>{escape(arguments)}</Arguments>
      <WorkingDirectory>{escape(str(default_config_path().parent))}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def install_task(config_path: Path | None = None) -> None:
    fd, name = tempfile.mkstemp(prefix="remotepc-task-", suffix=".xml")
    os.close(fd)
    xml_path = Path(name)
    try:
        xml_path.write_text(task_xml(config_path), encoding="utf-16")
        result = _run_schtasks(["/Create", "/XML", str(xml_path), "/TN", TASK_NAME, "/F"])
    finally:
        try:
            xml_path.unlink(missing_ok=True)
        except OSError:
            pass
    if result.returncode != 0:
        message = (result.stderr or result.stdout or "").strip()
        raise OSError(f"schtasks не создал задачу: {message or result.returncode}")


def remove_task() -> None:
    result = _run_schtasks(["/Delete", "/TN", TASK_NAME, "/F"])
    if result.returncode != 0:
        message = (result.stderr or result.stdout or "").strip()
        raise OSError(f"schtasks не удалил задачу: {message or result.returncode}")


def write_status(path: Path | None, ok: bool, error: str = "") -> None:
    if path is None:
        return
    try:
        path.write_text(
            json.dumps({"ok": ok, "error": error}, ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError:
        pass


def _read_status(path: Path) -> tuple[bool, str] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return bool(data.get("ok")), str(data.get("error", ""))


def _shell_execute_runas(status_path: Path, operation: str, config_path: Path | None) -> tuple[bool, str]:
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    shell32.ShellExecuteW.argtypes = [
        ctypes.c_void_p,
        ctypes.c_wchar_p,
        ctypes.c_wchar_p,
        ctypes.c_wchar_p,
        ctypes.c_wchar_p,
        ctypes.c_int,
    ]
    shell32.ShellExecuteW.restype = ctypes.c_void_p
    config = (config_path or default_config_path()).resolve()
    params = (
        f'-m remotepc --{operation}-admin-task'
        f' --config "{config}" --status "{status_path}"'
    )
    result = shell32.ShellExecuteW(
        None,
        "runas",
        str(pythonw_path()),
        params,
        str(default_config_path().parent),
        0,
    )
    value = int(result or 0)
    if value <= 32:
        if value == 5:
            return False, "Запрос UAC отклонён."
        return False, f"Не удалось запустить помощник UAC (код {value})."
    return True, ""


def run_elevated(operation: str, config_path: Path | None = None) -> tuple[bool, str]:
    """Run install/remove elevated through UAC and wait for the result file."""
    if operation not in ("install", "remove"):
        return False, "Неизвестная операция."
    fd, name = tempfile.mkstemp(prefix="remotepc-task-", suffix=".json")
    os.close(fd)
    status_path = Path(name)
    try:
        status_path.unlink(missing_ok=True)
        ok, error = _shell_execute_runas(status_path, operation, config_path)
        if not ok:
            return False, error
        deadline = time.monotonic() + 60.0
        while time.monotonic() < deadline:
            status = _read_status(status_path)
            if status is not None:
                return status
            time.sleep(0.2)
        return False, "UAC не подтверждён или задача не установлена за 60 секунд."
    finally:
        try:
            status_path.unlink(missing_ok=True)
        except OSError:
            pass
