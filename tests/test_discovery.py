"""Cross-platform language server discovery.

These tests never invoke a real ss/lsof/netstat/ps/powershell and never need an
Antigravity IDE — every command's output is fed in as a captured string. That
is what lets the Windows and macOS parsers be tested from CI on Linux, which is
the only coverage those paths will realistically ever get.
"""

import subprocess
import unittest
from unittest.mock import patch

from antigravity_mcp import discovery

SS_OUTPUT = """\
State  Recv-Q Send-Q Local Address:Port  Peer Address:Port Process
LISTEN 0      4096       127.0.0.1:46007        0.0.0.0:*     users:(("language_server",pid=53005,fd=25))
LISTEN 0      4096       127.0.0.1:35251        0.0.0.0:*     users:(("language_server",pid=53005,fd=31))
LISTEN 0      511            [::1]:9229              [::]:*     users:(("node",pid=41002,fd=20))
LISTEN 0      4096         0.0.0.0:22             0.0.0.0:*     users:(("sshd",pid=900,fd=3))
LISTEN 0      128        127.0.0.1:63342        0.0.0.0:*
"""

LSOF_OUTPUT = """\
COMMAND     PID  USER   FD   TYPE             DEVICE SIZE/OFF NODE NAME
language_ 53005 user   25u  IPv4 0xab12cd34ef56       0t0  TCP 127.0.0.1:35251 (LISTEN)
language_ 53005 user   31u  IPv6 0xab12cd34ef57       0t0  TCP *:46007 (LISTEN)
Dropbox     812 user   20u  IPv4 0xab12cd34ef58       0t0  TCP 0.0.0.0:17500 (LISTEN)
"""

NETSTAT_OUTPUT = """\
Active Connections

  Proto  Local Address          Foreign Address        State           PID
  TCP    127.0.0.1:35251        0.0.0.0:0              LISTENING       53005
  TCP    [::1]:46007            [::]:0                 LISTENING       53005
  TCP    127.0.0.1:52000        127.0.0.1:35251        ESTABLISHED     7788
  TCP    0.0.0.0:445            0.0.0.0:0              LISTENING       4
"""

PS_LONG_OUTPUT = """\
53005 /Applications/Antigravity.app/language_server_macos_arm64 --csrf_token=bc195a60-a8b0-4748-8552-65fd110aed68 --enable_lsp
  812 /usr/libexec/secd
"""

POWERSHELL_JSON = (
    '[{"ProcessId":53005,"CommandLine":"\\"C:\\\\Program Files\\\\Antigravity\\\\'
    'language_server_windows_x64.exe\\" --csrf_token=BC195A60-A8B0-4748-8552-65FD110AED68"},'
    '{"ProcessId":812,"CommandLine":"C:\\\\Windows\\\\explorer.exe"}]'
)

WMIC_CSV = """\
Node,CommandLine,ProcessId
DESKTOP-1,"C:\\Antigravity\\language_server_windows_x64.exe" --csrf_token=abc-123,53005
DESKTOP-1,C:\\Windows\\explorer.exe,812
"""


class TestParseCsrfToken(unittest.TestCase):
    def test_equals_form(self):
        """The common --csrf_token=<uuid> spelling is extracted."""
        token = discovery.parse_csrf_token("--csrf_token=bc195a60-a8b0-4748-8552-65fd110aed68")
        self.assertEqual(token, "bc195a60-a8b0-4748-8552-65fd110aed68")

    def test_space_separated_form(self):
        """Some builds pass the flag and value as separate argv entries."""
        self.assertEqual(discovery.parse_csrf_token("--csrf_token abc-123"), "abc-123")

    def test_uppercase_hex(self):
        """Windows spellings of the same uuid are uppercase."""
        self.assertEqual(discovery.parse_csrf_token("--csrf_token=DEAD-BEEF"), "DEAD-BEEF")

    def test_extension_server_token_is_rejected(self):
        """Regression: --extension_server_csrf_token guards a DIFFERENT service that
        answers RPC calls with 404. Matching it produces a server that looks
        discovered and then fails every call."""
        self.assertIsNone(
            discovery.parse_csrf_token("--extension_server_csrf_token=dead-beef-0000")
        )

    def test_picks_primary_token_when_both_present(self):
        """A real command line carries both flags; only the primary one is usable."""
        cmdline = (
            "/opt/language_server_linux_x64 --enable_lsp "
            "--extension_server_csrf_token=aaaa-1111 "
            "--csrf_token=bbbb-2222 --extension_server_port=33017"
        )
        self.assertEqual(discovery.parse_csrf_token(cmdline), "bbbb-2222")

    def test_no_token_present(self):
        """A process without the flag is not a candidate."""
        self.assertIsNone(discovery.parse_csrf_token("/opt/language_server_linux_x64 --enable_lsp"))


class TestListeningPortsSs(unittest.TestCase):
    def parse(self, output):
        with patch.object(discovery, "_run_command", return_value=output):
            return discovery._listening_ports_ss()

    def test_loopback_ports_are_grouped_by_pid(self):
        """Both of a pid's loopback listeners are reported against that pid."""
        self.assertEqual(sorted(self.parse(SS_OUTPUT)[53005]), [35251, 46007])

    def test_ipv6_loopback_is_included(self):
        """[::1] is loopback too and must not be dropped."""
        self.assertEqual(self.parse(SS_OUTPUT)[41002], [9229])

    def test_non_loopback_is_ignored(self):
        """A 0.0.0.0 listener is not the IDE's private RPC port."""
        self.assertNotIn(900, self.parse(SS_OUTPUT))

    def test_row_without_pid_is_skipped(self):
        """Rows with no users:(...) field carry no pid to attribute the port to."""
        self.assertNotIn(63342, [p for ports in self.parse(SS_OUTPUT).values() for p in ports])

    def test_duplicate_rows_do_not_duplicate_ports(self):
        """The same socket seen twice must not appear twice."""
        doubled = SS_OUTPUT + SS_OUTPUT.splitlines()[1] + "\n"
        self.assertEqual(sorted(self.parse(doubled)[53005]), [35251, 46007])

    def test_no_output(self):
        """A missing `ss` yields no data rather than an exception."""
        self.assertEqual(self.parse(None), {})


class TestListeningPortsLsof(unittest.TestCase):
    def parse(self, output=LSOF_OUTPUT):
        with patch.object(discovery, "_run_command", return_value=output):
            return discovery._listening_ports_lsof()

    def test_loopback_row(self):
        """A 127.0.0.1 LISTEN row is attributed to its pid."""
        self.assertIn(35251, self.parse()[53005])

    def test_wildcard_bind_is_accepted(self):
        """A *:port bind includes loopback; the caller probes it before trusting it."""
        self.assertIn(46007, self.parse()[53005])

    def test_header_row_is_skipped(self):
        """The COMMAND/PID header must not parse as a process."""
        self.assertNotIn(0, self.parse())

    def test_non_loopback_row_ignored(self):
        """0.0.0.0 listeners belong to unrelated services."""
        self.assertNotIn(812, self.parse())


class TestListeningPortsNetstat(unittest.TestCase):
    def parse(self, output=NETSTAT_OUTPUT):
        with patch.object(discovery, "_run_command", return_value=output):
            return discovery._listening_ports_netstat()

    def test_listening_loopback_rows(self):
        """Both IPv4 and IPv6 loopback LISTENING rows are captured."""
        self.assertEqual(sorted(self.parse()[53005]), [35251, 46007])

    def test_established_connections_ignored(self):
        """Only LISTENING sockets identify a server; ESTABLISHED ones are clients."""
        self.assertNotIn(7788, self.parse())

    def test_wildcard_listener_ignored(self):
        """0.0.0.0 is not loopback."""
        self.assertNotIn(4, self.parse())

    def test_header_lines_skipped(self):
        """'Active Connections' and the column header parse to nothing."""
        self.assertEqual(set(self.parse()), {53005})


class TestPortStrategyFallthrough(unittest.TestCase):
    def test_falls_through_to_the_next_strategy(self):
        """An unavailable primary tool must not end discovery — the next one runs."""
        with (
            patch.object(discovery, "_listening_ports_ss", return_value={}),
            patch.object(discovery, "_listening_ports_lsof", return_value={7: [1234]}),
            patch.object(discovery, "_listening_ports_netstat", return_value={}),
            patch.object(discovery.sys, "platform", "linux"),
        ):
            self.assertEqual(discovery.listening_ports_by_pid(), {7: [1234]})

    def test_all_strategies_empty(self):
        """When nothing is listening, the answer is empty, not an error."""
        with (
            patch.object(discovery, "_listening_ports_ss", return_value={}),
            patch.object(discovery, "_listening_ports_lsof", return_value={}),
            patch.object(discovery, "_listening_ports_netstat", return_value={}),
        ):
            self.assertEqual(discovery.listening_ports_by_pid(), {})


class TestProcessCmdlines(unittest.TestCase):
    def test_ps_long_parsing(self):
        """`ps -axww -o pid=,command=` rows split into pid and full command line."""
        with patch.object(discovery, "_run_command", return_value=PS_LONG_OUTPUT):
            rows = dict(discovery._cmdlines_from_ps_long())
        self.assertIn("language_server_macos_arm64", rows[53005])
        self.assertIn("--csrf_token=", rows[53005])
        self.assertEqual(rows[812], "/usr/libexec/secd")

    def test_powershell_json_list(self):
        """WMI output is parsed as JSON so backslashes and quotes survive intact."""
        with patch.object(discovery, "_run_command", return_value=POWERSHELL_JSON):
            rows = dict(discovery._cmdlines_from_powershell())
        self.assertIn("language_server_windows_x64.exe", rows[53005])

    def test_powershell_single_object(self):
        """One match serializes as an object, not a one-element array."""
        single = '{"ProcessId":5,"CommandLine":"language_server --csrf_token=a-1"}'
        with patch.object(discovery, "_run_command", return_value=single):
            self.assertEqual(
                dict(discovery._cmdlines_from_powershell())[5], "language_server --csrf_token=a-1"
            )

    def test_powershell_invalid_json(self):
        """Garbage from a blocked PowerShell yields nothing instead of raising."""
        with patch.object(discovery, "_run_command", return_value="not json"):
            self.assertEqual(list(discovery._cmdlines_from_powershell()), [])

    def test_wmic_csv(self):
        """The wmic fallback parses its CSV, quoted paths included."""
        with patch.object(discovery, "_run_command", return_value=WMIC_CSV):
            rows = dict(discovery._cmdlines_from_wmic())
        self.assertIn("language_server_windows_x64.exe", rows[53005])

    def test_wmic_unexpected_header(self):
        """A CSV without the expected columns is abandoned, not guessed at."""
        with patch.object(discovery, "_run_command", return_value="Node,Foo\nX,1\n"):
            self.assertEqual(list(discovery._cmdlines_from_wmic()), [])


class TestFindCandidates(unittest.TestCase):
    def find(self, processes, ports):
        with (
            patch.object(discovery, "process_cmdlines", return_value=iter(processes)),
            patch.object(discovery, "listening_ports_by_pid", return_value=ports),
        ):
            return discovery.find_language_server_candidates()

    def test_unrelated_process_excluded(self):
        """A process that is not a language server is never a candidate."""
        self.assertEqual(self.find([(1, "/usr/bin/firefox --csrf_token=a-1")], {1: [100]}), [])

    def test_language_server_without_token_excluded(self):
        """Without a CSRF token there is no way to authenticate to it."""
        self.assertEqual(
            self.find([(1, "/opt/language_server_linux_x64 --enable_lsp")], {1: [100]}), []
        )

    def test_paired_with_every_listening_port(self):
        """The RPC port is not identifiable up front, so every port is a candidate."""
        found = self.find([(1, "/opt/language_server_linux_x64 --csrf_token=a-1")], {1: [100, 200]})
        self.assertEqual(found, [(1, 100, "a-1"), (1, 200, "a-1")])

    def test_ports_deduplicated_in_discovery_order(self):
        """Two processes reported on one port must not yield two candidates."""
        found = self.find(
            [(1, "language_server --csrf_token=a-1"), (2, "language_server --csrf_token=b-2")],
            {1: [100], 2: [100, 300]},
        )
        self.assertEqual(found, [(1, 100, "a-1"), (2, 300, "b-2")])

    def test_windows_executable_name_matches(self):
        """Matching is case-insensitive and covers the .exe spelling."""
        found = self.find(
            [(1, r"C:\A\Language_Server_Windows_x64.exe --csrf_token=a-1")], {1: [100]}
        )
        self.assertEqual(found, [(1, 100, "a-1")])

    def test_process_with_no_ports_is_dropped(self):
        """A language server not yet listening has nothing to connect to."""
        self.assertEqual(self.find([(1, "language_server --csrf_token=a-1")], {}), [])


class TestRunCommand(unittest.TestCase):
    def test_missing_tool(self):
        """A tool that is not installed is an empty result, not a crash."""
        with patch.object(discovery.subprocess, "run", side_effect=FileNotFoundError):
            self.assertIsNone(discovery._run_command(["nope"]))

    def test_timeout(self):
        """A hung command is abandoned at the timeout rather than blocking discovery."""
        with patch.object(
            discovery.subprocess, "run", side_effect=subprocess.TimeoutExpired("ss", 5)
        ):
            self.assertIsNone(discovery._run_command(["ss"]))

    def test_nonzero_exit_still_returns_output(self):
        """Regression: lsof exits 1 whenever it cannot stat some unrelated process —
        which on a normal machine is always — while printing a complete listing.
        Discarding that output breaks macOS discovery entirely."""
        completed = subprocess.CompletedProcess(args=["lsof"], returncode=1, stdout="useful output")
        with patch.object(discovery.subprocess, "run", return_value=completed):
            self.assertEqual(discovery._run_command(["lsof"]), "useful output")

    def test_empty_output_is_none(self):
        """Empty stdout and no stdout are the same absence of information."""
        completed = subprocess.CompletedProcess(args=["ss"], returncode=0, stdout="")
        with patch.object(discovery.subprocess, "run", return_value=completed):
            self.assertIsNone(discovery._run_command(["ss"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
