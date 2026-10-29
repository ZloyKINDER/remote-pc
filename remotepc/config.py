"""Persistent host configuration.

The file lives next to ``ffmpeg.exe``/the host package.  The password is
encrypted with Windows DPAPI (current user + machine) when possible; a
``plain:`` prefix is used only on non-Windows systems or if DPAPI is not
available.
"""

from __future__ import annotations

import base64
import ctypes
import json
import os
import sys
from dataclasses import asdict, dataclass
from ctypes import wintypes
from pathlib import Path
from typing import Any

from . import auth

CONFIG_NAME = "remote-pc.json"
CONFIG_VERSION = 1
DPAPI_PREFIX = "dpapi:"
PLAIN_PREFIX = "plain:"

CRYPTPROTECT_UI_FORBIDDEN = 0x1


def default_config_path() -> Path:
    return Path(__file__).resolve().parent.parent / CONFIG_NAME


class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_char)),
    ]


def _blob(data: bytes) -> tuple[_DataBlob, ctypes.Array]:
    buffer = ctypes.create_string_buffer(data)
    return _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char))), buffer


def _dpapi_protect(data: bytes) -> bytes | None:
    if sys.platform != "win32":
        return None
    try:
        crypt32 = ctypes.windll.crypt32
        kernel32 = ctypes.windll.kernel32
        crypt32.CryptProtectData.argtypes = [
            ctypes.POINTER(_DataBlob),
            wintypes.LPCWSTR,
            ctypes.POINTER(_DataBlob),
            ctypes.c_void_p,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(_DataBlob),
        ]
        crypt32.CryptProtectData.restype = wintypes.BOOL
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        kernel32.LocalFree.restype = ctypes.c_void_p
        in_blob, in_buffer = _blob(data)
        out_blob = _DataBlob()
        ok = crypt32.CryptProtectData(
            ctypes.byref(in_blob),
            "RemotePC password",
            None,
            None,
            None,
            CRYPTPROTECT_UI_FORBIDDEN,
            ctypes.byref(out_blob),
        )
        if not ok:
            return None
        try:
            return ctypes.string_at(out_blob.pbData, out_blob.cbData)
        finally:
            kernel32.LocalFree(out_blob.pbData)
    except Exception:
        return None


def _dpapi_unprotect(data: bytes) -> bytes | None:
    if sys.platform != "win32":
        return None
    try:
        crypt32 = ctypes.windll.crypt32
        kernel32 = ctypes.windll.kernel32
        crypt32.CryptUnprotectData.argtypes = [
            ctypes.POINTER(_DataBlob),
            ctypes.POINTER(wintypes.LPWSTR),
            ctypes.POINTER(_DataBlob),
            ctypes.c_void_p,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(_DataBlob),
        ]
        crypt32.CryptUnprotectData.restype = wintypes.BOOL
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        kernel32.LocalFree.restype = ctypes.c_void_p
        in_blob, in_buffer = _blob(data)
        out_blob = _DataBlob()
        ok = crypt32.CryptUnprotectData(
            ctypes.byref(in_blob),
            None,
            None,
            None,
            None,
            CRYPTPROTECT_UI_FORBIDDEN,
            ctypes.byref(out_blob),
        )
        if not ok:
            return None
        try:
            return ctypes.string_at(out_blob.pbData, out_blob.cbData)
        finally:
            kernel32.LocalFree(out_blob.pbData)
    except Exception:
        return None


def encode_password(password: str) -> str:
    if not password:
        return ""
    protected = _dpapi_protect(password.encode("utf-8"))
    if protected is not None:
        return DPAPI_PREFIX + base64.b64encode(protected).decode("ascii")
    return PLAIN_PREFIX + base64.b64encode(password.encode("utf-8")).decode("ascii")


def decode_password(value: str) -> str:
    if not value:
        return ""
    try:
        if value.startswith(DPAPI_PREFIX):
            raw = base64.b64decode(value[len(DPAPI_PREFIX) :])
            plain = _dpapi_unprotect(raw)
            return plain.decode("utf-8") if plain is not None else ""
        if value.startswith(PLAIN_PREFIX):
            return base64.b64decode(value[len(PLAIN_PREFIX) :]).decode("utf-8")
        # Unknown/legacy value: treat it as a literal password.
        return value
    except Exception:
        return ""


def _bounded(value: Any, low: int, high: int, default: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


@dataclass
class HostConfig:
    password: str = ""
    port: int = 8765
    bind: str = "0.0.0.0"
    max_width: int = 3840
    fps: int = 60
    quality: int = 85
    tls: bool = True
    demo: bool = False
    public_ip: str = ""
    cert_dir: str = ""
    start_with_windows: bool = False
    autostart_server: bool = False
    run_as_admin: bool = False

    def normalized(self) -> "HostConfig":
        return HostConfig(
            password=self.password,
            port=_bounded(self.port, 1, 65535, 8765),
            bind=(self.bind or "0.0.0.0").strip(),
            max_width=_bounded(self.max_width, 640, 7680, 3840),
            fps=_bounded(self.fps, 5, 60, 60),
            quality=_bounded(self.quality, 30, 95, 85),
            tls=bool(self.tls),
            demo=bool(self.demo),
            public_ip=(self.public_ip or "").strip(),
            cert_dir=(self.cert_dir or "").strip(),
            start_with_windows=bool(self.start_with_windows),
            autostart_server=bool(self.autostart_server),
            run_as_admin=bool(self.run_as_admin),
        )

    @classmethod
    def load(cls, path: Path | None = None) -> "HostConfig":
        path = path or default_config_path()
        if not path.is_file():
            return cls()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cls()
        if not isinstance(raw, dict):
            return cls()
        return cls(
            password=decode_password(str(raw.get("password", ""))),
            port=raw.get("port", 8765),
            bind=raw.get("bind", "0.0.0.0"),
            max_width=raw.get("max_width", 3840),
            fps=raw.get("fps", 60),
            quality=raw.get("quality", 85),
            tls=raw.get("tls", True),
            demo=raw.get("demo", False),
            public_ip=raw.get("public_ip", ""),
            cert_dir=raw.get("cert_dir", ""),
            start_with_windows=raw.get("start_with_windows", False),
            autostart_server=raw.get("autostart_server", False),
            run_as_admin=raw.get("run_as_admin", False),
        ).normalized()

    def save(self, path: Path | None = None) -> None:
        path = path or default_config_path()
        data = asdict(self.normalized())
        data["password"] = encode_password(self.password)
        data["version"] = CONFIG_VERSION
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(tmp, path)

    def validate(self) -> str | None:
        if not self.password or len(self.password) < 6:
            return "Пароль должен быть длиной от 6 до 256 символов."
        if len(self.password) > 256:
            return "Пароль слишком длинный."
        if not (1 <= int(self.port) <= 65535):
            return "Порт должен быть от 1 до 65535."
        if self.public_ip:
            import ipaddress

            try:
                ipaddress.ip_address(self.public_ip)
            except ValueError:
                return "Публичный IP должен быть корректным IP-адресом."
        return None

    def new_password(self) -> None:
        self.password = auth.generate_password(20)

    @property
    def config_path(self) -> Path:
        return default_config_path()
