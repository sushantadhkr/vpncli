import tempfile
import unittest
from pathlib import Path
from unittest import mock

from vpncli import assets


class ArtworkTests(unittest.TestCase):
    def test_artwork_is_shipped(self):
        self.assertTrue(assets.has_artwork())
        self.assertIsNotNone(assets.icon_png())
        self.assertIsNotNone(assets.icon_ico())
        self.assertIsNotNone(assets.icon_icns())

    def test_missing_artwork_is_reported(self):
        with mock.patch.object(assets, "ASSETS_DIR", Path("/nonexistent")):
            self.assertFalse(assets.has_artwork())
            self.assertIsNone(assets.icon_png())


class PlistTests(unittest.TestCase):
    def test_macos_plist_is_valid_xml_with_needed_keys(self):
        import plistlib

        data = plistlib.loads(assets._macos_plist().encode("utf-8"))
        self.assertEqual(data["CFBundleIdentifier"], assets.BUNDLE_ID)
        self.assertEqual(data["CFBundleExecutable"], "vpncli-tray")
        self.assertEqual(data["CFBundleIconFile"], "vpncli")
        # Menu bar only: no Dock icon and no window.
        self.assertTrue(data["LSUIElement"])


class LinuxDesktopTests(unittest.TestCase):
    def test_desktop_entry_is_valid(self):
        text = assets._linux_desktop()
        self.assertTrue(text.startswith("[Desktop Entry]"))
        self.assertIn("Type=Application", text)
        self.assertIn("Icon=vpncli", text)
        self.assertIn("-m vpncli tray", text)
        self.assertIn("Terminal=false", text)


class InstallTests(unittest.TestCase):
    def test_linux_writes_desktop_file_and_icons(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            icons = home / ".local" / "share" / "icons" / "hicolor"
            apps = home / ".local" / "share" / "applications"
            with mock.patch.object(assets, "LINUX_ICONS", icons), \
                 mock.patch.object(assets, "LINUX_APPLICATIONS", apps), \
                 mock.patch.object(assets, "_refresh_desktop_database"):
                entry = assets.install_linux()
            self.assertEqual(entry, apps / "vpncli-tray.desktop")
            self.assertTrue(entry.is_file())
            self.assertTrue((icons / "256x256" / "apps" / "vpncli.png").is_file())

    def test_stamp_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            stamp = Path(tmp) / "registered.json"
            with mock.patch.object(assets, "_stamp_file", return_value=stamp), \
                 mock.patch.object(assets.paths, "write_private", side_effect=lambda p, t: stamp.write_text(t, encoding="utf-8")):
                assets.write_stamp()
                self.assertTrue(assets.is_registered())
                self.assertEqual(assets.read_stamp()["platform"], __import__("sys").platform)


if __name__ == "__main__":
    unittest.main()