"""Current-user Windows autostart.

Primary mechanism is the HKCU ``Run`` key.  If registry access is restricted
by policy, a small UTF-16 VBS launcher in the user's Startup folder is used
instead.  Both start ``pythonw.exe`` through the .pyw launcher, so no console
window appears.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from .config import default_config_path

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "RemotePC"
LAUNCHER_NAME = "remote-pc-gui.pyw"
STARTUP_NAME = "RemotePC.vbs"

LAUNCHER_TEXT = """\"\"\"Windows startup launcher for RemotePC.\"\"\"

import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
os.chdir(ROOT)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from remotepc.__main__ import main

raise SystemExit(main(["--gui", "--autostart", *sys.argv[1:]]))
"""


def launcher_path() -> Path:
    return default_config_path().parent / LAUNCHER_NAME


def startup_dir() -> Path:
    appdata = os.environ.get("APPDATA")
    if not appdata:
        raise OSError("APPDATA не задан")
    return (
        Path(appdata)
        / "Microsoft"
        / "Windows"
        / "Start Menu"
        / "Programs"
        / "Startup"
    )


def startup_script_path() -> Path:
    return startup_dir() / STARTUP_NAME


def ensure_launcher() -> Path:
    path = launcher_path()
    try:
        if not path.is_file() or path.read_text(encoding="utf-8") != LAUNCHER_TEXT:
            path.write_text(LAUNCHER_TEXT, encoding="utf-8")
    except OSError as exc:
        raise OSError(f"Не удалось создать {path}: {exc}") from exc
    return path


def pythonw_path() -> Path:
    exe = Path(sys.executable)
    candidate = exe.with_name("pythonw.exe")
    return candidate if candidate.is_file() else exe


def startup_command(config_path: Path | None = None) -> str:
    command = f'"{pythonw_path()}" "{launcher_path()}"'
    if config_path is not None and config_path.resolve() != default_config_path().resolve():
        command += f' --config "{config_path.resolve()}"'
    return command


def _vbs_quote(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _registry_enable(config_path: Path | None) -> None:
    import winreg

    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
        winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, startup_command(config_path))


def _registry_disable() -> None:
    import winreg

    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE
        ) as key:
            winreg.DeleteValue(key, VALUE_NAME)
    except FileNotFoundError:
        pass


def _startup_enable(config_path: Path | None) -> None:
    script = startup_script_path()
    script.parent.mkdir(parents=True, exist_ok=True)
    command = startup_command(config_path)
    content = "\n".join(
        [
            "' RemotePC autostart",
            "Set sh = CreateObject(\"WScript.Shell\")",
            f"sh.CurrentDirectory = {_vbs_quote(str(launcher_path().parent))}",
            f"sh.Run {_vbs_quote(command)}, 0, False",
            "",
        ]
    )
    # WSH reads UTF-16LE scripts with BOM reliably even when the host folder
    # contains Cyrillic characters.
    script.write_text(content, encoding="utf-16")


def _startup_disable() -> None:
    try:
        startup_script_path().unlink()
    except FileNotFoundError:
        pass


def is_start_with_windows() -> bool:
    if startup_script_path().is_file():
        return True
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            value, _kind = winreg.QueryValueEx(key, VALUE_NAME)
            return bool(value)
    except OSError:
        return False


def set_start_with_windows(enabled: bool, config_path: Path | None = None) -> None:
    if enabled:
        ensure_launcher()
        errors: list[str] = []
        try:
            _registry_enable(config_path)
            # Avoid a duplicate startup entry if an older Startup-folder
            # registration exists.
            try:
                _startup_disable()
            except OSError:
                pass
            return
        except OSError as exc:
            errors.append(f"реестр: {exc}")
        try:
            _startup_enable(config_path)
            return
        except OSError as exc:
            errors.append(f"папка Startup: {exc}")
        raise OSError("; ".join(errors))

    # Disabling is best effort: a locked policy key must not block saving
    # the rest of the config or stopping the app.
    try:
        _registry_disable()
    except OSError:
        pass
    try:
        _startup_disable()
    except OSError:
        pass
