import os
import tempfile
import unittest
from pathlib import Path

from vpncli import paths


class EnsureSharedTests(unittest.TestCase):
    def test_creates_private_file_owned_by_the_user(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "openvpn.log"
            paths.ensure_shared(target)
            info = target.stat()
            self.assertTrue(target.is_file())
            self.assertEqual(info.st_mode & 0o777, 0o600)
            self.assertEqual(info.st_uid, os.getuid())

    def test_existing_file_is_tightened(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "openvpn.pid"
            target.write_text("123\n", encoding="utf-8")
            os.chmod(str(target), 0o644)
            paths.ensure_shared(target)
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)

    def test_keeps_existing_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "openvpn.log"
            target.write_text("hello\n", encoding="utf-8")
            paths.ensure_shared(target)
            self.assertEqual(target.read_text(encoding="utf-8"), "hello\n")

    def test_missing_directory_is_not_fatal(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths.ensure_shared(Path(tmp) / "nope" / "openvpn.log")


if __name__ == "__main__":
    unittest.main()