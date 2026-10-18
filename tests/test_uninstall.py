import tempfile
import unittest
from pathlib import Path
from unittest import mock

from vpncli import paths, uninstall


def make_layout(home: Path, venv: Path) -> None:
    (venv / "bin").mkdir(parents=True)
    (venv / "pyvenv.cfg").write_text("home = /usr\n", encoding="utf-8")


class PlanTests(unittest.TestCase):
    def test_detects_standalone_venv_and_link(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            make_layout(home, home / ".local" / "share" / "vpncli" / "venv")
            link = home / ".local" / "bin" / "vpncli"
            link.parent.mkdir(parents=True)
            link.write_text("", encoding="utf-8")
            with mock.patch.object(uninstall, "_installed_in_current_env", return_value=False):
                labels = [label for label, _ in uninstall.plan(home)]
            self.assertTrue(any("installation folder" in label for label in labels))
            self.assertTrue(any("PATH entry" in label for label in labels))

    def test_detects_pipx_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            pipx = home / ".local" / "pipx" / "venvs" / "vpncli"
            pipx.mkdir(parents=True)
            with mock.patch.object(uninstall.shutil, "which", return_value="/usr/bin/pipx"), \
                 mock.patch.object(uninstall, "_installed_in_current_env", return_value=False):
                steps = uninstall.plan(home)
            self.assertEqual(steps[0][1][:2], ["pipx", "uninstall"])

    def test_unknown_layout_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            with mock.patch.object(uninstall, "_installed_in_current_env", return_value=False):
                with self.assertRaises(uninstall.UninstallError):
                    uninstall.plan(home)

    def test_current_environment_is_not_reported_twice(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            make_layout(home, home / ".local" / "share" / "vpncli" / "venv")
            with mock.patch.object(uninstall, "_installed_in_current_env", return_value=True), \
                 mock.patch.object(uninstall, "_prefix", return_value=home / ".local" / "share" / "vpncli" / "venv"):
                labels = [label for label, _ in uninstall.plan(home)]
            self.assertFalse([label for label in labels if label.startswith("python package")])


class RemoveDataTests(unittest.TestCase):
    def test_removes_data_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "vpncli" / "profiles" / "demo"
            target.mkdir(parents=True)
            (target / "auth.txt").write_text("user\npass\n", encoding="utf-8")
            with mock.patch.object(paths, "data_dir", return_value=Path(tmp) / "vpncli"):
                removed = uninstall.remove_data()
            self.assertFalse(removed.exists())

    def test_refuses_shallow_target(self):
        with mock.patch.object(paths, "data_dir", return_value=Path("/")):
            with self.assertRaises(uninstall.UninstallError):
                uninstall.remove_data()


if __name__ == "__main__":
    unittest.main()