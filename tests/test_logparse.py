import unittest

from vpncli.runtime import parse_log

CONNECTED = """2026-10-18 10:00:00 OpenVPN 2.6.12 aarch64-apple-darwin
2026-10-18 10:00:01 TCP/UDP: Preserving recently used remote address
2026-10-18 10:00:02 Peer Connection Initiated with [AF_INET]203.0.113.5:1194
2026-10-18 10:00:03 TUN/TAP device utun4 opened
2026-10-18 10:00:03 net_addr_v4_add: 10.8.0.2/24 dev utun4
2026-10-18 10:00:04 Initialization Sequence Completed
"""

AUTH_FAILED = """2026-10-18 10:00:02 Peer Connection Initiated with [AF_INET]203.0.113.5:1194
2026-10-18 10:00:03 AUTH: Received control message: AUTH_FAILED
2026-10-18 10:00:03 SIGTERM[soft,auth-failure] received, process exiting
"""

RECONNECTING = CONNECTED + "2026-10-18 10:05:00 SIGUSR1[soft,ping-restart] received, process restarting\n"


class LogParseTests(unittest.TestCase):
    def test_empty_log_is_connecting(self):
        self.assertEqual(parse_log("").state, "connecting")

    def test_connected_details(self):
        link = parse_log(CONNECTED)
        self.assertEqual(link.state, "connected")
        self.assertEqual(link.server, "203.0.113.5")
        self.assertEqual(link.local_ip, "10.8.0.2")
        self.assertEqual(link.device, "utun4")

    def test_auth_failure_is_not_masked_by_exit(self):
        link = parse_log(AUTH_FAILED)
        self.assertEqual(link.state, "auth_failed")
        self.assertIn("AUTH_FAILED", link.detail)

    def test_restart_after_connect(self):
        self.assertEqual(parse_log(RECONNECTING).state, "reconnecting")

    def test_fatal_error(self):
        link = parse_log("Options error: Unrecognized option\nExiting due to fatal error\n")
        self.assertEqual(link.state, "error")
        self.assertIn("fatal error", link.detail)

    def test_clean_shutdown(self):
        link = parse_log(CONNECTED + "SIGTERM[hard,] received, process exiting\n")
        self.assertEqual(link.state, "disconnected")


if __name__ == "__main__":
    unittest.main()
