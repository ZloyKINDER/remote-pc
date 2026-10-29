"""Small, dependency-free Windows layer: monitors, DPI and input injection.

The host is intentionally Windows-only.  Browsers are used as clients, so all
platform specific work happens here.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import socket
import sys
from dataclasses import dataclass
from typing import Iterable

if sys.platform != "win32":  # pragma: no cover - the app is Windows-only
    raise RuntimeError("RemotePC host requires Windows")

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

ULONG_PTR = ctypes.c_size_t
LONG = wintypes.LONG
DWORD = wintypes.DWORD
WORD = wintypes.WORD
BYTE = wintypes.BYTE
BOOL = wintypes.BOOL

SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79
MONITORINFOF_PRIMARY = 1


@dataclass(frozen=True)
class Monitor:
    id: int
    left: int
    top: int
    width: int
    height: int
    primary: bool
    device: str
    name: str

    @property
    def box(self) -> dict[str, int]:
        return {"left": self.left, "top": self.top, "width": self.width, "height": self.height}

    def as_public(self) -> dict:
        return {
            "id": self.id,
            "width": self.width,
            "height": self.height,
            "left": self.left,
            "top": self.top,
            "primary": self.primary,
            "name": self.name,
        }


class RECT(ctypes.Structure):
    _fields_ = [
        ("left", LONG),
        ("top", LONG),
        ("right", LONG),
        ("bottom", LONG),
    ]


class MONITORINFOEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", DWORD),
        ("rcMonitor", RECT),
        ("rcWork", RECT),
        ("dwFlags", DWORD),
        ("szDevice", wintypes.WCHAR * 32),
    ]


class DISPLAY_DEVICEW(ctypes.Structure):
    _fields_ = [
        ("cb", DWORD),
        ("DeviceName", wintypes.WCHAR * 32),
        ("DeviceString", wintypes.WCHAR * 128),
        ("StateFlags", DWORD),
        ("DeviceID", wintypes.WCHAR * 128),
        ("DeviceKey", wintypes.WCHAR * 128),
    ]


MONITORENUMPROC = ctypes.WINFUNCTYPE(
    BOOL, wintypes.HMONITOR, wintypes.HDC, ctypes.POINTER(RECT), wintypes.LPARAM
)

user32.EnumDisplayMonitors.argtypes = [
    wintypes.HDC,
    ctypes.POINTER(RECT),
    MONITORENUMPROC,
    wintypes.LPARAM,
]
user32.EnumDisplayMonitors.restype = BOOL
user32.GetMonitorInfoW.argtypes = [wintypes.HMONITOR, ctypes.POINTER(MONITORINFOEXW)]
user32.GetMonitorInfoW.restype = BOOL
user32.GetSystemMetrics.argtypes = [ctypes.c_int]
user32.GetSystemMetrics.restype = ctypes.c_int
user32.EnumDisplayDevicesW.argtypes = [
    wintypes.LPCWSTR,
    DWORD,
    ctypes.POINTER(DISPLAY_DEVICEW),
    DWORD,
]
user32.EnumDisplayDevicesW.restype = BOOL


def enable_dpi_awareness() -> None:
    """Make GDI coordinates match physical pixels on scaled monitors."""

    # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
    try:
        user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        return
    except Exception:
        pass
    try:
        shcore = ctypes.WinDLL("shcore", use_last_error=True)
        shcore.SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE
        return
    except Exception:
        pass
    try:
        user32.SetProcessDPIAware()
    except Exception:
        pass


def _friendly_device_name(device: str) -> str:
    dd = DISPLAY_DEVICEW()
    dd.cb = ctypes.sizeof(DISPLAY_DEVICEW)
    if user32.EnumDisplayDevicesW(device, 0, ctypes.byref(dd), 0):
        name = (dd.DeviceString or "").strip()
        if name:
            return name
    return ""


def get_monitors() -> list[Monitor]:
    """Return physical monitors, primary first, with stable ids 1..N."""

    found: list[tuple[bool, int, int, int, int, str]] = []

    @MONITORENUMPROC
    def callback(hmonitor, _hdc, _rect, _data):  # noqa: ANN001
        info = MONITORINFOEXW()
        info.cbSize = ctypes.sizeof(MONITORINFOEXW)
        if not user32.GetMonitorInfoW(hmonitor, ctypes.byref(info)):
            return True
        rc = info.rcMonitor
        primary = bool(info.dwFlags & MONITORINFOF_PRIMARY)
        device = info.szDevice
        found.append(
            (primary, int(rc.left), int(rc.top), int(rc.right - rc.left), int(rc.bottom - rc.top), device)
        )
        return True

    if not user32.EnumDisplayMonitors(None, None, callback, 0):
        raise OSError(ctypes.get_last_error(), "EnumDisplayMonitors failed")

    if not found:
        width = int(user32.GetSystemMetrics(0))
        height = int(user32.GetSystemMetrics(1))
        found = [(True, 0, 0, width, height, r"\\.\DISPLAY1")]

    # Primary first, then left-to-right / top-to-bottom.  This is also the order
    # shown in the viewer's monitor picker.
    found.sort(key=lambda item: (not item[0], item[1], item[2]))

    monitors: list[Monitor] = []
    for index, (primary, left, top, width, height, device) in enumerate(found, start=1):
        friendly = _friendly_device_name(device)
        monitors.append(
            Monitor(
                id=index,
                left=left,
                top=top,
                width=width,
                height=height,
                primary=primary,
                device=device,
                name=friendly or device,
            )
        )
    return monitors


@dataclass(frozen=True)
class VirtualScreen:
    left: int
    top: int
    width: int
    height: int


def get_virtual_screen() -> VirtualScreen:
    return VirtualScreen(
        left=int(user32.GetSystemMetrics(SM_XVIRTUALSCREEN)),
        top=int(user32.GetSystemMetrics(SM_YVIRTUALSCREEN)),
        width=max(1, int(user32.GetSystemMetrics(SM_CXVIRTUALSCREEN))),
        height=max(1, int(user32.GetSystemMetrics(SM_CYVIRTUALSCREEN))),
    )


def normalized_to_absolute(
    nx: float,
    ny: float,
    monitor: Monitor,
    virtual: VirtualScreen,
) -> tuple[int, int]:
    """Map a 0..1 point inside a monitor to the Windows virtual desktop."""

    nx = 0.0 if nx < 0 else (1.0 if nx > 1 else nx)
    ny = 0.0 if ny < 0 else (1.0 if ny > 1 else ny)
    x = monitor.left + nx * max(0, monitor.width - 1)
    y = monitor.top + ny * max(0, monitor.height - 1)
    return (int(round(x)), int(round(y)))


def to_virtual_absolute(x: int, y: int, virtual: VirtualScreen) -> tuple[int, int]:
    """Convert virtual-desktop pixels to the 0..65535 range used by SendInput."""

    vw = max(1, virtual.width - 1)
    vh = max(1, virtual.height - 1)
    ax = int(round((x - virtual.left) * 65535 / vw))
    ay = int(round((y - virtual.top) * 65535 / vh))
    return (max(0, min(65535, ax)), max(0, min(65535, ay)))


# ---------------------------------------------------------------------------
# SendInput structures

INPUT_MOUSE = 0
INPUT_KEYBOARD = 1

KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
KEYEVENTF_SCANCODE = 0x0008

MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
MOUSEEVENTF_XDOWN = 0x0080
MOUSEEVENTF_XUP = 0x0100
MOUSEEVENTF_WHEEL = 0x0800
MOUSEEVENTF_HWHEEL = 0x1000
MOUSEEVENTF_ABSOLUTE = 0x8000
MOUSEEVENTF_VIRTUALDESK = 0x4000

XBUTTON1 = 0x0001
XBUTTON2 = 0x0002
WHEEL_DELTA = 120


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", LONG),
        ("dy", LONG),
        ("mouseData", DWORD),
        ("dwFlags", DWORD),
        ("time", DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", WORD),
        ("wScan", WORD),
        ("dwFlags", DWORD),
        ("time", DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", DWORD),
        ("wParamL", WORD),
        ("wParamH", WORD),
    ]


class _INPUTUNION(ctypes.Union):
    _fields_ = [
        ("mi", MOUSEINPUT),
        ("ki", KEYBDINPUT),
        ("hi", HARDWAREINPUT),
    ]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [
        ("type", DWORD),
        ("u", _INPUTUNION),
    ]


user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
user32.SendInput.restype = wintypes.UINT


def _mouse_input(dx: int, dy: int, data: int, flags: int) -> INPUT:
    return INPUT(
        type=INPUT_MOUSE,
        mi=MOUSEINPUT(
            dx=dx,
            dy=dy,
            mouseData=data & 0xFFFFFFFF,
            dwFlags=flags,
            time=0,
            dwExtraInfo=0,
        ),
    )


def _key_input(scan: int = 0, vk: int = 0, flags: int = 0) -> INPUT:
    return INPUT(
        type=INPUT_KEYBOARD,
        ki=KEYBDINPUT(wVk=vk, wScan=scan, dwFlags=flags, time=0, dwExtraInfo=0),
    )


def _send(inputs: Iterable[INPUT]) -> None:
    items = list(inputs)
    if not items:
        return
    array = (INPUT * len(items))(*items)
    sent = int(user32.SendInput(len(items), array, ctypes.sizeof(INPUT)))
    if sent != len(items):
        error = ctypes.get_last_error()
        raise OSError(error, f"SendInput inserted {sent} of {len(items)} events")


# Browser KeyboardEvent.code -> (scan code, extended)
SCAN_CODES: dict[str, tuple[int, bool]] = {
    "Escape": (0x01, False),
    "Digit1": (0x02, False),
    "Digit2": (0x03, False),
    "Digit3": (0x04, False),
    "Digit4": (0x05, False),
    "Digit5": (0x06, False),
    "Digit6": (0x07, False),
    "Digit7": (0x08, False),
    "Digit8": (0x09, False),
    "Digit9": (0x0A, False),
    "Digit0": (0x0B, False),
    "Minus": (0x0C, False),
    "Equal": (0x0D, False),
    "Backspace": (0x0E, False),
    "Tab": (0x0F, False),
    "KeyQ": (0x10, False),
    "KeyW": (0x11, False),
    "KeyE": (0x12, False),
    "KeyR": (0x13, False),
    "KeyT": (0x14, False),
    "KeyY": (0x15, False),
    "KeyU": (0x16, False),
    "KeyI": (0x17, False),
    "KeyO": (0x18, False),
    "KeyP": (0x19, False),
    "BracketLeft": (0x1A, False),
    "BracketRight": (0x1B, False),
    "Enter": (0x1C, False),
    "ControlLeft": (0x1D, False),
    "KeyA": (0x1E, False),
    "KeyS": (0x1F, False),
    "KeyD": (0x20, False),
    "KeyF": (0x21, False),
    "KeyG": (0x22, False),
    "KeyH": (0x23, False),
    "KeyJ": (0x24, False),
    "KeyK": (0x25, False),
    "KeyL": (0x26, False),
    "Semicolon": (0x27, False),
    "Quote": (0x28, False),
    "Backquote": (0x29, False),
    "ShiftLeft": (0x2A, False),
    "Backslash": (0x2B, False),
    "KeyZ": (0x2C, False),
    "KeyX": (0x2D, False),
    "KeyC": (0x2E, False),
    "KeyV": (0x2F, False),
    "KeyB": (0x30, False),
    "KeyN": (0x31, False),
    "KeyM": (0x32, False),
    "Comma": (0x33, False),
    "Period": (0x34, False),
    "Slash": (0x35, False),
    "ShiftRight": (0x36, False),
    "NumpadMultiply": (0x37, False),
    "AltLeft": (0x38, False),
    "Space": (0x39, False),
    "CapsLock": (0x3A, False),
    "F1": (0x3B, False),
    "F2": (0x3C, False),
    "F3": (0x3D, False),
    "F4": (0x3E, False),
    "F5": (0x3F, False),
    "F6": (0x40, False),
    "F7": (0x41, False),
    "F8": (0x42, False),
    "F9": (0x43, False),
    "F10": (0x44, False),
    "NumLock": (0x45, False),
    "ScrollLock": (0x46, False),
    "Numpad7": (0x47, False),
    "Numpad8": (0x48, False),
    "Numpad9": (0x49, False),
    "NumpadSubtract": (0x4A, False),
    "Numpad4": (0x4B, False),
    "Numpad5": (0x4C, False),
    "Numpad6": (0x4D, False),
    "NumpadAdd": (0x4E, False),
    "Numpad1": (0x4F, False),
    "Numpad2": (0x50, False),
    "Numpad3": (0x51, False),
    "Numpad0": (0x52, False),
    "NumpadDecimal": (0x53, False),
    "IntlBackslash": (0x56, False),
    "F11": (0x57, False),
    "F12": (0x58, False),
    "F13": (0x64, False),
    "F14": (0x65, False),
    "F15": (0x66, False),
    "F16": (0x67, False),
    "F17": (0x68, False),
    "F18": (0x69, False),
    "F19": (0x6A, False),
    "F20": (0x6B, False),
    "F21": (0x6C, False),
    "F22": (0x6D, False),
    "F23": (0x6E, False),
    "F24": (0x76, False),
    "NumpadEnter": (0x1C, True),
    "ControlRight": (0x1D, True),
    "NumpadDivide": (0x35, True),
    "PrintScreen": (0x37, True),
    "AltRight": (0x38, True),
    "Home": (0x47, True),
    "ArrowUp": (0x48, True),
    "PageUp": (0x49, True),
    "ArrowLeft": (0x4B, True),
    "ArrowRight": (0x4D, True),
    "End": (0x4F, True),
    "ArrowDown": (0x50, True),
    "PageDown": (0x51, True),
    "Insert": (0x52, True),
    "Delete": (0x53, True),
    "MetaLeft": (0x5B, True),
    "MetaRight": (0x5C, True),
    "ContextMenu": (0x5D, True),
    "NumpadEqual": (0x59, False),
    "IntlRo": (0x73, False),
    "IntlYen": (0x7D, False),
}

# A few keys have no useful scan code for synthetic input.
VK_CODES: dict[str, int] = {
    "Pause": 0x13,
    "PrintScreen": 0x2C,
}

BUTTON_FLAGS = {
    "left": (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP),
    "right": (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP),
    "middle": (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP),
    "back": (MOUSEEVENTF_XDOWN, MOUSEEVENTF_XUP),
    "forward": (MOUSEEVENTF_XDOWN, MOUSEEVENTF_XUP),
}


class InputController:
    """Translate the viewer's normalized events into real Windows input."""

    def __init__(self, virtual: VirtualScreen):
        self.virtual = virtual
        self._pressed_keys: set[tuple[int, bool]] = set()
        self._pressed_vk: set[int] = set()
        self._pressed_buttons: set[str] = set()

    # -- mouse ---------------------------------------------------------
    def move_to_pixels(self, x: int, y: int) -> None:
        x = max(self.virtual.left, min(self.virtual.left + self.virtual.width - 1, int(x)))
        y = max(self.virtual.top, min(self.virtual.top + self.virtual.height - 1, int(y)))
        ax, ay = to_virtual_absolute(x, y, self.virtual)
        _send([_mouse_input(ax, ay, 0, MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK)])

    def mouse_button(self, button: str, down: bool) -> None:
        flags = BUTTON_FLAGS.get(button)
        if not flags:
            return
        down_flag, up_flag = flags
        data = 0
        if button == "back":
            data = XBUTTON1
        elif button == "forward":
            data = XBUTTON2
        _send([_mouse_input(0, 0, data, down_flag if down else up_flag)])
        if down:
            self._pressed_buttons.add(button)
        else:
            self._pressed_buttons.discard(button)

    def wheel(self, dx: int, dy: int) -> None:
        events: list[INPUT] = []
        for _ in range(min(10, abs(int(dy)))):
            events.append(_mouse_input(0, 0, WHEEL_DELTA if dy > 0 else -WHEEL_DELTA, MOUSEEVENTF_WHEEL))
        for _ in range(min(10, abs(int(dx)))):
            events.append(_mouse_input(0, 0, WHEEL_DELTA if dx > 0 else -WHEEL_DELTA, MOUSEEVENTF_HWHEEL))
        _send(events)

    # -- keyboard ------------------------------------------------------
    def key(self, code: str, down: bool) -> bool:
        if code in VK_CODES:
            vk = VK_CODES[code]
            flags = KEYEVENTF_KEYUP if down is False else 0
            _send([_key_input(vk=vk, flags=flags)])
            if down:
                self._pressed_vk.add(vk)
            else:
                self._pressed_vk.discard(vk)
            return True
        scan_info = SCAN_CODES.get(code)
        if not scan_info:
            return False
        scan, extended = scan_info
        flags = KEYEVENTF_SCANCODE
        if extended:
            flags |= KEYEVENTF_EXTENDEDKEY
        if not down:
            flags |= KEYEVENTF_KEYUP
        _send([_key_input(scan=scan, flags=flags)])
        if down:
            self._pressed_keys.add((scan, extended))
        else:
            self._pressed_keys.discard((scan, extended))
        return True

    def text(self, value: str) -> None:
        if not value:
            return
        events: list[INPUT] = []
        # SendInput's wScan is 16-bit: split astral characters into UTF-16 units.
        encoded = value.encode("utf-16-le", errors="replace")
        for offset in range(0, len(encoded), 2):
            unit = int.from_bytes(encoded[offset : offset + 2], "little")
            if unit == 0:
                continue
            events.append(_key_input(scan=unit, flags=KEYEVENTF_UNICODE))
            events.append(_key_input(scan=unit, flags=KEYEVENTF_UNICODE | KEYEVENTF_KEYUP))
            if len(events) >= 1024:
                _send(events)
                events.clear()
        _send(events)

    def _combo(self, parts: list[tuple[str, str]]) -> None:
        """parts are (kind, value) where kind is 'key' or 'text'."""

        events: list[INPUT] = []

        def add_key(code: str, down: bool) -> None:
            scan_info = SCAN_CODES.get(code)
            if not scan_info:
                vk = VK_CODES.get(code)
                if vk:
                    events.append(_key_input(vk=vk, flags=KEYEVENTF_KEYUP if not down else 0))
                return
            scan, extended = scan_info
            flags = KEYEVENTF_SCANCODE
            if extended:
                flags |= KEYEVENTF_EXTENDEDKEY
            if not down:
                flags |= KEYEVENTF_KEYUP
            events.append(_key_input(scan=scan, flags=flags))

        for kind, value in parts:
            if kind == "key":
                add_key(value, True)
        for kind, value in reversed(parts):
            if kind == "key":
                add_key(value, False)
        _send(events)

    def shortcut(self, name: str) -> tuple[bool, str]:
        combos = {
            "alt_tab": [("key", "AltLeft"), ("key", "Tab")],
            "alt_shift_tab": [("key", "AltLeft"), ("key", "ShiftLeft"), ("key", "Tab")],
            "win": [("key", "MetaLeft")],
            "win_d": [("key", "MetaLeft"), ("key", "KeyD")],
            "win_e": [("key", "MetaLeft"), ("key", "KeyE")],
            "ctrl_esc": [("key", "ControlLeft"), ("key", "Escape")],
            "alt_f4": [("key", "AltLeft"), ("key", "F4")],
            "ctrl_shift_esc": [("key", "ControlLeft"), ("key", "ShiftLeft"), ("key", "Escape")],
            "ctrl_alt_del": [],
        }
        if name not in combos:
            return False, "Неизвестная комбинация"
        if name == "ctrl_alt_del":
            return self.send_sas()
        self._combo(combos[name])
        return True, ""

    def send_sas(self) -> tuple[bool, str]:
        # SendSAS is allowed only for services / when the SAS generation policy
        # permits it.  If Windows rejects it, be explicit in the UI.
        try:
            sas = ctypes.WinDLL("sas.dll", use_last_error=True)
            sas.SendSAS.argtypes = [BOOL]
            sas.SendSAS.restype = None
            sas.SendSAS(False)
            return True, ""
        except Exception:
            return (
                False,
                "Windows не разрешает отправлять Ctrl+Alt+Del из обычного приложения. "
                "Используйте экран блокировки/службу или физическую клавиатуру.",
            )

    def release_all(self) -> None:
        events: list[INPUT] = []
        for scan, extended in list(self._pressed_keys):
            flags = KEYEVENTF_SCANCODE | KEYEVENTF_KEYUP
            if extended:
                flags |= KEYEVENTF_EXTENDEDKEY
            events.append(_key_input(scan=scan, flags=flags))
        self._pressed_keys.clear()
        for vk in list(self._pressed_vk):
            events.append(_key_input(vk=vk, flags=KEYEVENTF_KEYUP))
        self._pressed_vk.clear()
        for button in list(self._pressed_buttons):
            _send([_mouse_input(0, 0, 0, BUTTON_FLAGS[button][1])])
        self._pressed_buttons.clear()
        _send(events)


def get_cursor_pos() -> tuple[int, int]:
    point = wintypes.POINT()
    if not user32.GetCursorPos(ctypes.byref(point)):
        raise OSError(ctypes.get_last_error(), "GetCursorPos failed")
    return int(point.x), int(point.y)


def local_ipv4_addresses() -> list[str]:
    addresses: set[str] = set()
    try:
        for item in socket.gethostbyname_ex(socket.gethostname())[2]:
            if item and not item.startswith("127.") and not item.startswith("169.254."):
                addresses.add(item)
    except OSError:
        pass
    # The UDP connect trick discovers the interface used for the default route
    # without sending a packet.  Put it first: that is the address a router's
    # port-forward should normally point to.
    default_route: str | None = None
    sock = None
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.connect(("8.8.8.8", 80))
        default_route = sock.getsockname()[0]
        addresses.add(default_route)
    except OSError:
        pass
    finally:
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass
    ordered = sorted(addresses)
    if default_route in ordered:
        ordered.remove(default_route)
        ordered.insert(0, default_route)
    return ordered
