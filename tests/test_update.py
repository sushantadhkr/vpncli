import unittest
from pathlib import Path
from unittest import mock

from vpncli import paths, update


class RepoTests(unittest.TestCase):
    def test_default_repo(self):
        with mock.patch.dict("os.environ", {}, clear=False):
            with mock.patch.dict("os.environ", {"VPNCLI_REPO": ""}):
                self.assertEqual(update.owner_repo(), ("sushantadhkr", "vpncli"))

    def test_git_suffix_and_trailing_slash_are_tolerated(self):
        for url in (
            "https://github.com/sushantadhkr/vpncli",
            "https://github.com/sushantadhkr/vpncli.git",
            "https://github.com/sushantadhkr/vpncli/",
            "git@github.com:sushantadhkr/vpncli.git",
        ):
            with mock.patch.dict("os.environ", {"VPNCLI_REPO": url}):
                self.assertEqual(update.owner_repo(), ("sushantadhkr", "vpncli"))

    def test_fork_owner_is_used(self):
        with mock.patch.dict("os.environ", {"VPNCLI_REPO": "https://github.com/me/vpncli.git"}):
            self.assertEqual(update.owner_repo(), ("me", "vpncli"))

    def test_unusable_repo_is_reported(self):
        with mock.patch.dict("os.environ", {"VPNCLI_REPO": "vpncli"}):
            with self.assertRaises(update.UpdateError):
                update.owner_repo()

    def test_installer_url_points_at_main(self):
        expected = update.INSTALLER["win" if paths.WINDOWS else "posix"]
        with mock.patch.dict("os.environ", {}):
            url = update.installer_url()
        self.assertTrue(url.endswith("sushantadhkr/vpncli/main/%s" % expected), url)

    def test_windows_uses_the_powershell_installer(self):
        with mock.patch.dict("os.environ", {}), mock.patch.object(paths, "WINDOWS", True):
            self.assertTrue(update.installer_url().endswith("install.ps1"))


class FetchTests(unittest.TestCase):
    def test_download_failure_is_reported(self):
        import urllib.error

        with mock.patch.object(update.urllib.request, "urlopen", side_effect=urllib.error.URLError("no network")):
            with self.assertRaises(update.UpdateError):
                update.fetch_installer("https://example.invalid/install.sh")

    def test_empty_response_is_rejected(self):
        response = mock.MagicMock()
        response.read.return_value = b""
        response.__enter__.return_value = response
        with mock.patch.object(update.urllib.request, "urlopen", return_value=response):
            with self.assertRaises(update.UpdateError):
                update.fetch_installer("https://example.invalid/install.sh")

    def test_installer_command_matches_platform(self):
        script = Path("C:/install.ps1" if paths.WINDOWS else "/tmp/install.sh")
        self.assertEqual(
            update.installer_command(script)[0],
            "powershell" if paths.WINDOWS else "/bin/sh",
        )
        with mock.patch.object(paths, "WINDOWS", True):
            command = update.installer_command(Path("C:/install.ps1"))
        self.assertEqual(command[0], "powershell")
        self.assertIn("Bypass", command)

    def test_dry_run_changes_nothing(self):
        with mock.patch.object(update, "fetch_installer") as fetch:
            url, output = update.update(dry_run=True)
        self.assertIsNone(output)
        self.assertTrue(url)
        fetch.assert_not_called()


if __name__ == "__main__":
    unittest.main()