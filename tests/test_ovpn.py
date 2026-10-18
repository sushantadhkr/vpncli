import unittest

from vpncli.ovpn import parse

SAMPLE = """
client
dev tun
proto udp
remote vpn.example.com 1194
remote backup.example.com 443 tcp
auth-user-pass
ca ca.crt
tls-auth ta.key 1
up /etc/openvpn/update-resolv-conf
<cert>
remote ignored.example.com 9999
</cert>
"""


class ParseTests(unittest.TestCase):
    def test_remotes_inherit_defaults(self):
        info = parse(SAMPLE)
        self.assertEqual(len(info.remotes), 2)
        self.assertEqual(info.remotes[0].host, "vpn.example.com")
        self.assertEqual(info.remotes[0].proto, "udp")
        self.assertEqual(info.remotes[1].port, 443)
        self.assertTrue(info.remotes[1].is_tcp)

    def test_inline_blocks_are_skipped(self):
        info = parse(SAMPLE)
        hosts = [r.host for r in info.remotes]
        self.assertNotIn("ignored.example.com", hosts)
        self.assertIn("cert", info.inline)

    def test_auth_detection(self):
        self.assertTrue(parse(SAMPLE).needs_auth)
        self.assertFalse(parse("remote a.b 1194\nauth-user-pass creds.txt").needs_auth)
        self.assertEqual(parse("remote a.b\nauth-user-pass creds.txt").auth_file, "creds.txt")

    def test_assets_and_scripts(self):
        info = parse(SAMPLE)
        self.assertEqual(info.assets, ["ca.crt", "ta.key"])
        self.assertEqual(info.scripts, ["up"])

    def test_default_port_and_proto(self):
        info = parse("remote host.example")
        self.assertEqual(info.remotes[0].port, 1194)
        self.assertEqual(info.remotes[0].proto, "udp")

    def test_global_port_and_proto(self):
        info = parse("proto tcp-client\nport 443\nremote host.example")
        self.assertEqual(info.remotes[0].port, 443)
        self.assertEqual(info.remotes[0].proto, "tcp-client")

    def test_comments_ignored(self):
        info = parse("# remote no.example 1\n; remote nope.example 2\nremote ok.example 1194 # trailing")
        self.assertEqual([r.host for r in info.remotes], ["ok.example"])


if __name__ == "__main__":
    unittest.main()
