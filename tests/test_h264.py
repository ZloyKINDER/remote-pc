from __future__ import annotations

import unittest

from remotepc.h264 import AnnexBParser, ffmpeg_args, stream_size
from remotepc.winapi import Monitor


def nal(header: int, payload: bytes = b"\x00") -> bytes:
    return b"\x00\x00\x00\x01" + bytes([header]) + payload


class H264ParserTests(unittest.TestCase):
    def test_access_units_and_keyframes(self):
        stream = (
            nal(0x67, b"\x64\x00\x34")
            + nal(0x68, b"\x01")
            + nal(0x65, b"idr")
            + nal(0x41, b"p1")
            + nal(0x41, b"p2")
        )
        parser = AnnexBParser()
        units = []
        for offset in range(0, len(stream), 7):
            units.extend(parser.feed(stream[offset : offset + 7]))
        units.extend(parser.flush())
        self.assertEqual(len(units), 3)
        self.assertTrue(units[0][1])
        self.assertFalse(units[1][1])
        self.assertFalse(units[2][1])
        self.assertIn(b"idr", units[0][0])
        self.assertIn(b"p2", units[2][0])

    def test_parameter_sets_go_with_idr(self):
        stream = nal(0x41, b"p") + nal(0x67, b"\x64\x00\x34") + nal(0x68) + nal(0x65, b"i")
        parser = AnnexBParser()
        units = parser.feed(stream) + parser.flush()
        self.assertEqual(len(units), 2)
        self.assertTrue(units[1][1])
        self.assertTrue(units[1][0].startswith(b"\x00\x00\x00\x01\x67"))


class H264ArgsTests(unittest.TestCase):
    def monitor(self):
        return Monitor(1, 0, 0, 3440, 1440, True, r"\\.\DISPLAY1", "Test")

    def test_stream_size(self):
        self.assertEqual(stream_size(self.monitor(), 0), (3440, 1440))
        self.assertEqual(stream_size(self.monitor(), 2560), (2560, 1072))

    def test_args_use_dxgi_and_nvenc(self):
        args = ffmpeg_args("ffmpeg.exe", 0, 2560, 1072, 25_000_000, 60)
        text = " ".join(args)
        self.assertIn("ddagrab=output_idx=0:framerate=60", text)
        self.assertIn("draw_mouse=0", text)
        self.assertIn("h264_nvenc", text)
        self.assertIn("scale=2560:1072", text)
        self.assertIn("pipe:1", text)

    def test_args_follow_requested_fps(self):
        args = ffmpeg_args("ffmpeg.exe", 0, 0, 0, 8_000_000, 15)
        text = " ".join(args)
        self.assertIn("framerate=15", text)
        self.assertIn("-g 3", text)
        self.assertNotIn("framerate=60", text)


if __name__ == "__main__":
    unittest.main()
