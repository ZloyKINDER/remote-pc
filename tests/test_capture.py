from __future__ import annotations

import unittest

from remotepc.capture import capture_frame
from remotepc.winapi import Monitor


class CaptureEncodingTests(unittest.TestCase):
    def setUp(self):
        self.monitor = Monitor(
            id=1,
            left=0,
            top=0,
            width=1920,
            height=1080,
            primary=True,
            device=r"\\.\DISPLAY1",
            name="Test",
        )

    def test_demo_jpeg(self):
        frame = capture_frame(self.monitor, 800, 85, demo=True, codec="jpeg")
        self.assertEqual(frame.codec, "jpeg")
        self.assertTrue(frame.data.startswith(b"\xff\xd8"))
        self.assertEqual(frame.width, 800)

    def test_demo_webp(self):
        frame = capture_frame(self.monitor, 800, 80, demo=True, codec="webp")
        self.assertEqual(frame.codec, "webp")
        self.assertEqual(frame.data[:4], b"RIFF")
        self.assertEqual(frame.data[8:12], b"WEBP")


if __name__ == "__main__":
    unittest.main()
