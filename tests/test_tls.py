from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from remotepc.tls import ensure_cert


class TlsTests(unittest.TestCase):
    def test_certificate_is_created_and_reused(self):
        with tempfile.TemporaryDirectory() as tmp:
            cert_dir = Path(tmp)
            cert, key, fingerprint = ensure_cert(cert_dir, ["203.0.113.10"])
            self.assertTrue(cert.exists())
            self.assertTrue(key.exists())
            self.assertEqual(len(fingerprint.split(":")), 32)
            self.assertEqual(fingerprint, fingerprint.upper())

            cert2, key2, fingerprint2 = ensure_cert(cert_dir, ["203.0.113.10"])
            self.assertEqual(cert, cert2)
            self.assertEqual(key, key2)
            self.assertEqual(fingerprint, fingerprint2)


if __name__ == "__main__":
    unittest.main()
