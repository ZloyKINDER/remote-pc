from __future__ import annotations

import unittest
from types import SimpleNamespace

from remotepc.server import ServerSettings, ViewerSession
from remotepc.winapi import Monitor, VirtualScreen


class FakeInput:
    def __init__(self):
        self.calls = []

    def move_to_pixels(self, x, y):
        self.calls.append(("move", x, y))

    def mouse_button(self, button, down):
        self.calls.append(("button", button, down))

    def wheel(self, dx, dy):
        self.calls.append(("wheel", dx, dy))

    def key(self, code, down):
        self.calls.append(("key", code, down))
        return True

    def text(self, value):
        self.calls.append(("text", value))

    def shortcut(self, name):
        self.calls.append(("shortcut", name))
        return True, ""

    def release_all(self):
        self.calls.append(("release_all",))


class InputDispatchTests(unittest.TestCase):
    def setUp(self):
        monitor = Monitor(
            id=1,
            left=100,
            top=50,
            width=1000,
            height=500,
            primary=True,
            device=r"\\.\DISPLAY1",
            name="Test",
        )
        fake_server = SimpleNamespace(
            monitors=[monitor],
            monitors_by_id={1: monitor},
            virtual=VirtualScreen(left=0, top=0, width=1000, height=500),
            settings=ServerSettings(demo=True, tls=False),
        )
        self.session = ViewerSession(fake_server, None, "test")
        self.fake = FakeInput()
        self.session.input = self.fake

    def test_mouse_maps_monitor_coordinates(self):
        self.session._on_mouse({"action": "down", "x": 0.0, "y": 0.0, "button": "left"})
        self.assertEqual(self.fake.calls[0], ("move", 100, 50))
        self.assertEqual(self.fake.calls[1], ("button", "left", True))

        self.session._on_mouse({"action": "up", "x": 1.0, "y": 1.0, "button": "left"})
        self.assertEqual(self.fake.calls[2], ("move", 100 + 999, 50 + 499))
        self.assertEqual(self.fake.calls[3], ("button", "left", False))

    def test_mouse_up_without_coordinates_still_releases(self):
        self.session._on_mouse({"action": "up", "button": "right"})
        self.assertEqual(self.fake.calls, [("button", "right", False)])

    def test_wheel_clamped(self):
        self.session._on_mouse({"action": "wheel", "dx": -100, "dy": 100})
        self.assertEqual(self.fake.calls, [("wheel", -10, 10)])

    def test_keyboard_and_text(self):
        self.session._on_key({"code": "KeyA", "down": True})
        self.session._on_key({"code": "KeyA", "down": "yes"})  # ignored
        self.session._on_text_input({"text": "Привет"})
        self.assertEqual(self.fake.calls, [("key", "KeyA", True), ("text", "Привет")])

    def test_text_is_limited(self):
        self.session._on_text_input({"text": "x" * 1000})
        self.assertEqual(self.fake.calls, [("text", "x" * 512)])

    def test_shortcut_is_forwarded(self):
        self.session._on_shortcut({"name": "alt_tab"})
        self.assertEqual(self.fake.calls, [("shortcut", "alt_tab")])


if __name__ == "__main__":
    unittest.main()
