import os
import tempfile
import unittest
from pathlib import Path

from vpncli.profiles import ProfileError, ProfileStore, slugify

CONFIG = """client
dev tun
remote vpn.example.com 1194 udp
auth-user-pass
ca ca.crt
"""


class ProfileStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        home = Path(self.tmp.name) / "home"
        os.environ["VPNCLI_HOME"] = str(home)
        self.addCleanup(os.environ.pop, "VPNCLI_HOME", None)
        self.source = Path(self.tmp.name) / "source"
        self.source.mkdir()
        (self.source / "nl-1.ovpn").write_text(CONFIG)
        (self.source / "ca.crt").write_text("CERT")
        self.store = ProfileStore(home / "profiles")

    def test_add_copies_config_and_assets(self):
        profile, warnings = self.store.add("nl-1", self.source / "nl-1.ovpn")
        self.assertEqual(warnings, [])
        self.assertTrue(profile.config_path.is_file())
        self.assertTrue((profile.directory / "ca.crt").is_file())
        self.assertEqual(self.store.names(), ["nl-1"])

    def test_missing_asset_warns(self):
        (self.source / "ca.crt").unlink()
        _, warnings = self.store.add("nl-1", self.source / "nl-1.ovpn")
        self.assertTrue(any("ca.crt" in w for w in warnings))

    def test_duplicate_requires_force(self):
        self.store.add("nl-1", self.source / "nl-1.ovpn")
        with self.assertRaises(ProfileError):
            self.store.add("nl-1", self.source / "nl-1.ovpn")
        self.store.add("nl-1", self.source / "nl-1.ovpn", overwrite=True)

    def test_credentials_roundtrip(self):
        profile, _ = self.store.add("nl-1", self.source / "nl-1.ovpn", "alice", "s3cret")
        self.assertTrue(profile.has_credentials)
        self.assertEqual(profile.username(), "alice")
        if os.name == "posix":
            self.assertEqual(profile.auth_path.stat().st_mode & 0o777, 0o600)
        self.assertTrue(self.store.clear_credentials("nl-1"))
        self.assertFalse(profile.has_credentials)

    def test_force_keeps_saved_credentials(self):
        self.store.add("nl-1", self.source / "nl-1.ovpn", "alice", "pw")
        profile, _ = self.store.add("nl-1", self.source / "nl-1.ovpn", overwrite=True)
        self.assertEqual(profile.username(), "alice")

    def test_lookup_by_unique_prefix(self):
        self.store.add("nl-1", self.source / "nl-1.ovpn")
        self.assertEqual(self.store.get("NL").name, "nl-1")
        with self.assertRaises(ProfileError):
            self.store.get("missing")

    def test_rename_and_remove(self):
        self.store.add("nl-1", self.source / "nl-1.ovpn")
        self.store.rename("nl-1", "amsterdam")
        self.assertEqual(self.store.names(), ["amsterdam"])
        self.store.remove("amsterdam")
        self.assertEqual(self.store.names(), [])

    def test_rejects_non_client_config(self):
        bogus = self.source / "bogus.ovpn"
        bogus.write_text("dev tun\n")
        with self.assertRaises(ProfileError):
            self.store.add("bogus", bogus)

    def test_slugify(self):
        self.assertEqual(slugify("My Server (US) #1.ovpn"), "My-Server-US-1.ovpn")
        self.assertEqual(slugify("***"), "profile")


if __name__ == "__main__":
    unittest.main()
