from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from remotepc.config import HostConfig, decode_password, encode_password


class ConfigTests(unittest.TestCase):
    def test_password_is_not_plaintext_and_roundtrips(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "remote-pc.json"
            cfg = HostConfig(
                password="secret-12345",
                port=9000,
                max_width=2560,
                fps=60,
                quality=90,
                tls=False,
                public_ip="203.0.113.5",
                start_with_windows=True,
                autostart_server=True,
                run_as_admin=True,
            )
            cfg.save(path)
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("secret-12345", text)
            raw = json.loads(text)
            self.assertTrue(
                raw["password"].startswith("dpapi:") or raw["password"].startswith("plain:")
            )
            loaded = HostConfig.load(path)
            self.assertEqual(loaded.password, "secret-12345")
            self.assertEqual(loaded.port, 9000)
            self.assertEqual(loaded.max_width, 2560)
            self.assertEqual(loaded.fps, 60)
            self.assertEqual(loaded.quality, 90)
            self.assertFalse(loaded.tls)
            self.assertEqual(loaded.public_ip, "203.0.113.5")
            self.assertTrue(loaded.start_with_windows)
            self.assertTrue(loaded.autostart_server)
            self.assertTrue(loaded.run_as_admin)

    def test_missing_and_broken_file_use_defaults(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "missing.json"
            self.assertEqual(HostConfig.load(path).port, 8765)
            path.write_text("{broken", encoding="utf-8")
            self.assertEqual(HostConfig.load(path).port, 8765)

    def test_password_codec(self):
        encoded = encode_password("пароль-123")
        self.assertEqual(decode_password(encoded), "пароль-123")
        self.assertEqual(decode_password(""), "")

    def test_validation(self):
        self.assertIsNotNone(HostConfig(password="123").validate())
        self.assertIsNone(HostConfig(password="123456").validate())
        self.assertIsNotNone(HostConfig(password="123456", public_ip="not-ip").validate())


if __name__ == "__main__":
    unittest.main()
