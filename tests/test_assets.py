import os
import shutil
import sys
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

class MacosHandoverTests(unittest.TestCase):
    def make_bundle(self, root: Path, script: str, executable: bool = True) -> Path:
        bundle = root / "Applications" / "vpncli.app"
        launcher = bundle / "Contents" / "MacOS" / "vpncli-tray"
        launcher.parent.mkdir(parents=True)
        launcher.write_text(script, encoding="utf-8")
        launcher.chmod(0o755 if executable else 0o644)
        return bundle

    def test_missing_bundle_is_not_handed_over(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            with mock.patch.object(assets, "MACOS_APPLICATIONS", (home / "Applications",)), \
                 mock.patch.object(assets.sys, "platform", "darwin"):
                self.assertIsNone(assets.macos_app())

    def test_bundle_with_live_interpreter_is_used(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            interpreter = home / "venv" / "bin" / "python"
            interpreter.parent.mkdir(parents=True)
            interpreter.write_text("", encoding="utf-8")
            bundle = self.make_bundle(home, '#!/bin/sh\nexec "%s" -m vpncli tray\n' % interpreter)
            with mock.patch.object(assets, "MACOS_APPLICATIONS", (home / "Applications",)), \
                 mock.patch.object(assets.sys, "platform", "darwin"):
                self.assertEqual(assets.macos_app(), bundle)

    def test_bundle_pointing_at_a_dead_interpreter_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            bundle = self.make_bundle(home, '#!/bin/sh\nexec "/gone/python" -m vpncli tray\n')
            with mock.patch.object(assets, "MACOS_APPLICATIONS", (home / "Applications",)), \
                 mock.patch.object(assets.sys, "platform", "darwin"):
                self.assertIsNone(assets.macos_app())
            self.assertTrue(bundle.is_dir())


class LauncherMarkerTests(unittest.TestCase):
    """The bundle launcher must always carry the hand-over marker.

    A launcher left over from a version that predates the marker starts a
    child that hands straight back to the bundle, so the tray exits at once
    and nothing is left running.
    """

    def test_launcher_always_sets_the_marker(self):
        script = assets.macos_launcher_text()
        self.assertIn("VPNCLI_TRAY_LAUNCHED=1", script)
        self.assertIn("-m vpncli tray", script)

    def test_stale_launcher_is_rewritten_on_handover(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            applications = home / "Applications"
            bundle = applications / "vpncli.app"
            launcher = bundle / "Contents" / "MacOS" / "vpncli-tray"
            launcher.parent.mkdir(parents=True)
            launcher.write_text(
                '#!/bin/sh\nexec "%s" -m vpncli tray\n' % sys.executable, encoding="utf-8"
            )
            launcher.chmod(0o755)
            with mock.patch.object(assets, "MACOS_APPLICATIONS", (applications,)), \
                 mock.patch.object(assets.sys, "platform", "darwin"), \
                 mock.patch.object(assets, "_macos_target", return_value=bundle), \
                 mock.patch("subprocess.Popen"):
                assets.launch_macos_app()
            script = bundle / "Contents" / "MacOS" / "vpncli-tray.sh"
            target = script if script.is_file() else launcher
            self.assertIn("VPNCLI_TRAY_LAUNCHED=1", target.read_text(encoding="utf-8"))

    def test_current_launcher_is_left_alone(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            applications = home / "Applications"
            bundle = applications / "vpncli.app"
            binary = assets.launcher_binary()
            if binary is None:
                self.skipTest("launcher binary not shipped")
            launcher = bundle / "Contents" / "MacOS" / "vpncli-tray"
            script = bundle / "Contents" / "MacOS" / "vpncli-tray.sh"
            launcher.parent.mkdir(parents=True)
            shutil.copy2(str(binary), str(launcher))
            script.write_text(assets.macos_launcher_text(), encoding="utf-8")
            launcher.chmod(0o644)
            script.chmod(0o644)
            with mock.patch.object(assets, "MACOS_APPLICATIONS", (applications,)), \
                 mock.patch.object(assets.sys, "platform", "darwin"), \
                 mock.patch.object(assets, "_macos_target", return_value=bundle), \
                 mock.patch("subprocess.Popen"):
                assets.launch_macos_app()
            # A launcher that is current keeps whatever mode it had.
            if os.name == "posix":
                self.assertEqual(launcher.stat().st_mode & 0o777, 0o644)
