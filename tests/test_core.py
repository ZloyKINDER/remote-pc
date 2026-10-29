from __future__ import annotations

import unittest

from remotepc import auth
from remotepc.protocol import (
    FRAME_HEADER_SIZE,
    pack_audio_frame,
    pack_frame,
    pack_h264_frame,
    unpack_header,
)
from remotepc.winapi import (
    SCAN_CODES,
    Monitor,
    VirtualScreen,
    normalized_to_absolute,
    to_virtual_absolute,
)


class AuthTests(unittest.TestCase):
    def test_proof_roundtrip_and_rejection(self):
        salt = auth.new_salt()
        key = auth.derive_key("correct horse battery", salt)
        server_nonce = auth.new_nonce()
        client_nonce = auth.new_nonce()
        proof = auth.proof(key, server_nonce, client_nonce)

        self.assertEqual(len(proof), 32)
        self.assertTrue(auth.verify(key, server_nonce, client_nonce, proof))
        self.assertFalse(auth.verify(key, server_nonce, auth.new_nonce(), proof))

        wrong_key = auth.derive_key("wrong password", salt)
        self.assertFalse(auth.verify(wrong_key, server_nonce, client_nonce, proof))

    def test_generated_password_is_not_trivial(self):
        password = auth.generate_password()
        self.assertEqual(len(password), 10)
        self.assertNotIn("0", password)
        self.assertNotIn("O", password)
        self.assertNotEqual(password, auth.generate_password())

    def test_unicode_password(self):
        salt = auth.new_salt()
        key = auth.derive_key("пароль-с-кириллицей", salt)
        nonce_a = auth.new_nonce()
        nonce_b = auth.new_nonce()
        self.assertTrue(auth.verify(key, nonce_a, nonce_b, auth.proof(key, nonce_a, nonce_b)))


class CoordinateTests(unittest.TestCase):
    def setUp(self):
        self.monitor = Monitor(
            id=1,
            left=100,
            top=50,
            width=1920,
            height=1080,
            primary=True,
            device=r"\\.\DISPLAY1",
            name="Test",
        )
        self.virtual = VirtualScreen(left=0, top=0, width=3840, height=1080)

    def test_monitor_corners(self):
        self.assertEqual(normalized_to_absolute(0, 0, self.monitor, self.virtual), (100, 50))
        right, bottom = normalized_to_absolute(1, 1, self.monitor, self.virtual)
        self.assertEqual(right, 100 + 1919)
        self.assertEqual(bottom, 50 + 1079)

    def test_normalized_is_clamped(self):
        self.assertEqual(normalized_to_absolute(-1, -1, self.monitor, self.virtual), (100, 50))
        self.assertEqual(
            normalized_to_absolute(2, 2, self.monitor, self.virtual),
            (100 + 1919, 50 + 1079),
        )

    def test_virtual_desktop_range(self):
        self.assertEqual(to_virtual_absolute(0, 0, self.virtual), (0, 0))
        self.assertEqual(to_virtual_absolute(3839, 1079, self.virtual), (65535, 65535))


class ProtocolTests(unittest.TestCase):
    def test_frame_roundtrip(self):
        payload = b"\xff\xd8jpegdata\xff\xd9"
        frame = pack_frame(42, 2, 1600, 900, payload)
        self.assertEqual(len(frame), FRAME_HEADER_SIZE + len(payload))
        header = unpack_header(frame)
        self.assertIsNotNone(header)
        assert header is not None
        self.assertEqual(header["seq"], 42)
        self.assertEqual(header["monitor"], 2)
        self.assertEqual(header["width"], 1600)
        self.assertEqual(header["height"], 900)
        self.assertEqual(frame[FRAME_HEADER_SIZE:], payload)

    def test_bad_magic(self):
        self.assertIsNone(unpack_header(b"XX" + b"\x00" * 20))

    def test_webp_flag(self):
        frame = pack_frame(1, 1, 800, 600, b"webpdata", "webp")
        header = unpack_header(frame)
        assert header is not None
        self.assertEqual(header["codec"], "webp")
        self.assertEqual(header["flags"], 2)
        self.assertEqual(frame[FRAME_HEADER_SIZE:], b"webpdata")

    def test_h264_flags(self):
        key = pack_h264_frame(1, 1, 2560, 1072, b"idr", True)
        delta = pack_h264_frame(2, 1, 2560, 1072, b"p", False)
        self.assertEqual(unpack_header(key)["codec"], "h264")
        self.assertEqual(unpack_header(key)["flags"], 4)
        self.assertEqual(unpack_header(delta)["flags"], 3)

    def test_audio_frame(self):
        frame = pack_audio_frame(7, 48000, 2, b"\x00\x01" * 4)
        header = unpack_header(frame)
        assert header is not None
        self.assertEqual(header["codec"], "audio")
        self.assertEqual(header["width"], 48000)
        self.assertEqual(header["height"], 2)
        self.assertEqual(frame[FRAME_HEADER_SIZE:], b"\x00\x01" * 4)

    def test_header_layout(self):
        frame = pack_frame(0x01020304, 2, 640, 480, b"x")
        self.assertEqual(FRAME_HEADER_SIZE, 12)
        self.assertEqual(frame[:2], b"RP")
        self.assertEqual(frame[2], 1)  # JPEG flag
        self.assertEqual(frame[3:7], b"\x04\x03\x02\x01")
        self.assertEqual(frame[7], 2)
        self.assertEqual(int.from_bytes(frame[8:10], "little"), 640)
        self.assertEqual(int.from_bytes(frame[10:12], "little"), 480)


class KeyboardMapTests(unittest.TestCase):
    def test_expected_keys_are_mapped(self):
        for code in (
            "KeyA",
            "Digit1",
            "Enter",
            "Backspace",
            "ControlLeft",
            "ShiftRight",
            "AltLeft",
            "MetaLeft",
            "ArrowUp",
            "Delete",
            "F12",
            "NumpadEnter",
        ):
            self.assertIn(code, SCAN_CODES)
        self.assertEqual(SCAN_CODES["KeyA"], (0x1E, False))
        self.assertEqual(SCAN_CODES["NumpadEnter"], (0x1C, True))


if __name__ == "__main__":
    unittest.main()
