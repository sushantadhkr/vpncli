import threading
import unittest
from unittest import mock

from vpncli import tray


class MenuCallbackTests(unittest.TestCase):
    """Building the menu must have no side effects.

    pystray rebuilds the menu every time the state changes, so a menu entry
    that ran its work while being constructed would repeat it forever.
    """

    def setUp(self):
        self.app = object.__new__(tray.TrayApp)

    def test_uninstall_entries_return_callbacks_without_doing_work(self):
        def explode(self, *args, **kwargs):
            raise AssertionError("a thread was started while building the menu")

        with mock.patch.object(threading.Thread, "start", explode):
            purge_all = self.app.uninstall_action(True)
            keep_data = self.app.uninstall_action(False)
        self.assertTrue(callable(purge_all))
        self.assertTrue(callable(keep_data))
        self.assertIsNot(purge_all, keep_data)

    def test_connect_entries_are_factories_too(self):
        self.app.status = mock.Mock(state="disconnected", profile=None)
        self.app.store = mock.Mock(names=lambda: ["nl-1"])

        def explode(self, *args, **kwargs):
            raise AssertionError("a thread was started while building the menu")

        with mock.patch.object(threading.Thread, "start", explode):
            callback = self.app.connect_action("nl-1")
            checked = self.app.checked_for("nl-1")
        self.assertTrue(callable(callback))
        self.assertTrue(callable(checked))


class WindowsLauncherTests(unittest.TestCase):
    def test_gui_script_launcher_is_preferred_over_python(self):
        with mock.patch.object(tray.sys, "executable", "/env/bin/python.exe"):
            with mock.patch.object(tray.assets, "APP_NAME", "vpncli"):
                with self.fake_folder({"vpncli-tray.exe"}):
                    target, arguments = tray.assets.windows_target()
        self.assertTrue(target.endswith("vpncli-tray.exe"))
        self.assertEqual(arguments, "")

    def test_pythonw_is_used_when_there_is_no_gui_script(self):
        with mock.patch.object(tray.sys, "executable", "/env/bin/python.exe"):
            with self.fake_folder({"pythonw.exe"}):
                target, arguments = tray.assets.windows_target()
        self.assertTrue(target.endswith("pythonw.exe"))
        self.assertEqual(arguments, "-m vpncli tray")

    def test_plain_python_is_the_last_resort(self):
        with mock.patch.object(tray.sys, "executable", "/env/bin/python.exe"):
            with self.fake_folder(set()):
                target, arguments = tray.assets.windows_target()
        self.assertTrue(target.endswith("python.exe"))
        self.assertEqual(arguments, "-m vpncli tray")

    @staticmethod
    def fake_folder(names):
        from pathlib import Path

        return mock.patch.object(Path, "is_file", lambda self: self.name in names)


if __name__ == "__main__":
    unittest.main()