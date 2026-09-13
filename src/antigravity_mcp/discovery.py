"""Locate the running Antigravity IDE language server, on any OS.

Newer Antigravity builds no longer put the language server's HTTPS RPC port on
the command line — it is bound dynamically — so finding it means correlating
two separate questions: which processes look like a language server (and carry
a usable CSRF token), and which loopback ports each process is listening on.
Neither question has a portable stdlib answer, so each platform gets its own
strategy built on a tool that ships with the OS.

Every strategy degrades to an empty result instead of raising, and each
platform falls through to the others when its primary returns nothing. An
unusual environment (a container without iproute2, a locked-down PowerShell)
then still has a chance of working rather than failing at the first hurdle.
"""

import csv
import io
import json
import os
import re
import subprocess
import sys
from collections.abc import Iterator
from typing import Any, Optional

COMMAND_TIMEOUT_SECONDS = 5


def _run_command(args: list[str]) -> Optional[str]:
    """Run a discovery command, returning its stdout or None.

    Output is kept even on a non-zero exit. `lsof` in particular exits 1
    whenever it could not stat some unrelated process's descriptors — which on
    a normal multi-user machine is always — while still printing a complete and
    correct listing of everything it could see. Discarding that would break
    macOS discovery outright.
    """
    kwargs: dict[str, Any] = {
        "stdout": subprocess.PIPE,
        "stderr": subprocess.DEVNULL,
        "text": True,
        "timeout": COMMAND_TIMEOUT_SECONDS,
    }
    # Windows-only flag; keeps a console window from flashing when the host
    # application is a GUI process rather than a terminal.
    if hasattr(subprocess, "CREATE_NO_WINDOW"):
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    try:
        result = subprocess.run(args, **kwargs)
    except (OSError, subprocess.SubprocessError):
        # OSError covers the tool not being installed; SubprocessError covers
        # TimeoutExpired and friends. Either way there is nothing to report.
        return None
    return result.stdout or None


def parse_csrf_token(cmdline: str) -> Optional[str]:
    """Extract the language server's primary --csrf_token from a command line.

    This token guards the Connect-RPC endpoint used for Cascade sessions. It is
    deliberately NOT the same as --extension_server_csrf_token, which guards an
    unrelated plain-HTTP service and answers RPC calls with 404 — hence the
    negative lookbehind, which is the whole point of this function.
    """
    match = re.search(r"(?<!extension_server_)--csrf_token[=\s]+([A-Fa-f0-9-]+)", cmdline)
    return match.group(1) if match else None


# --------------------------------------------------------------- listen ports


# Linux: `ss -ltnp` reads the kernel socket table directly and attributes each
# socket to its owning pid without needing root. A row looks like:
#   LISTEN 0 4096 127.0.0.1:46007 0.0.0.0:* users:(("language_server",pid=53005,fd=25))
def _listening_ports_ss() -> dict[int, list[int]]:
    stdout = _run_command(["ss", "-ltnp"])
    if not stdout:
        return {}
    result: dict[int, list[int]] = {}
    for line in stdout.splitlines():
        port_match = re.search(r"(?:127\.0\.0\.1|\[::1\]):(\d+)\s", line)
        if not port_match:
            continue
        port = int(port_match.group(1))
        for pid_match in re.finditer(r"pid=(\d+)", line):
            ports = result.setdefault(int(pid_match.group(1)), [])
            if port not in ports:
                ports.append(port)
    return result


# macOS: there is no /proc and no `ss`, so `lsof` walking open descriptors is
# the standard answer. `-nP` skips DNS and /etc/services lookups, which
# otherwise stall for seconds on a misconfigured network. A row looks like:
#   language_ 53005 user 21u IPv4 0x... 0t0 TCP 127.0.0.1:35251 (LISTEN)
def _listening_ports_lsof() -> dict[int, list[int]]:
    stdout = _run_command(["lsof", "-nP", "-iTCP", "-sTCP:LISTEN"])
    if not stdout:
        return {}
    result: dict[int, list[int]] = {}
    for line in stdout.splitlines():
        parts = line.split()
        if len(parts) < 2 or not parts[1].isdigit():
            continue
        # A wildcard bind (*:port) is accepted too: it includes loopback, and
        # the caller validates every candidate with a real RPC call anyway.
        match = re.search(r"(?:127\.0\.0\.1|\[::1\]|\*):(\d+)", line)
        if not match:
            continue
        ports = result.setdefault(int(parts[1]), [])
        port = int(match.group(1))
        if port not in ports:
            ports.append(port)
    return result


# Windows: no ss, no lsof. `netstat -ano` ships with every release, needs no
# elevation, and puts the owning pid in the last column. A row looks like:
#   TCP    127.0.0.1:54321    0.0.0.0:0    LISTENING    1234
def _listening_ports_netstat() -> dict[int, list[int]]:
    stdout = _run_command(["netstat", "-ano", "-p", "TCP"])
    if not stdout:
        return {}
    result: dict[int, list[int]] = {}
    for line in stdout.splitlines():
        parts = line.split()
        if len(parts) < 5 or parts[0].upper() != "TCP":
            continue
        if parts[3].upper() != "LISTENING" or not parts[4].isdigit():
            continue
        local = parts[1]
        if not (local.startswith("127.0.0.1:") or local.startswith("[::1]:")):
            continue
        try:
            port = int(local.rsplit(":", 1)[1])
        except (ValueError, IndexError):
            continue
        ports = result.setdefault(int(parts[4]), [])
        if port not in ports:
            ports.append(port)
    return result


def _port_strategies():
    if sys.platform.startswith("linux"):
        return (_listening_ports_ss, _listening_ports_lsof, _listening_ports_netstat)
    if sys.platform == "darwin":
        return (_listening_ports_lsof, _listening_ports_ss, _listening_ports_netstat)
    if sys.platform.startswith("win"):
        return (_listening_ports_netstat, _listening_ports_lsof, _listening_ports_ss)
    return (_listening_ports_lsof, _listening_ports_ss, _listening_ports_netstat)


def listening_ports_by_pid() -> dict[int, list[int]]:
    """Map pid -> loopback TCP ports that pid is listening on."""
    for strategy in _port_strategies():
        ports = strategy()
        if ports:
            return ports
    return {}


# ----------------------------------------------------------- process cmdlines


# Linux: reading /proc/<pid>/cmdline is plain file I/O, with none of the fork
# cost of shelling out, and its NUL-delimited arguments need no unquoting.
def _cmdlines_from_proc() -> Iterator[tuple[int, str]]:
    if not os.path.isdir("/proc"):
        return
    try:
        entries = os.listdir("/proc")
    except OSError:
        return
    for entry in entries:
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/cmdline", "rb") as handle:
                raw = handle.read().decode("utf-8", errors="ignore")
        except OSError:
            # The process exited between listdir and open, or is another
            # user's. Either way it is not ours to inspect.
            continue
        cmdline = " ".join(raw.split("\0")).strip()
        if cmdline:
            yield int(entry), cmdline


# macOS/BSD: no /proc. Plain `ps` truncates arguments to the terminal width,
# which would clip the CSRF token off the end of a long command line — `-ww`
# forces unlimited width. `-o pid=,command=` drops the header row.
def _cmdlines_from_ps_long() -> Iterator[tuple[int, str]]:
    stdout = _run_command(["ps", "-axww", "-o", "pid=,command="])
    if not stdout:
        return
    for line in stdout.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) == 2 and parts[0].isdigit():
            yield int(parts[0]), parts[1]


# Windows: WMI via PowerShell returns the command line as structured JSON, so
# paths full of backslashes, spaces, and quotes survive without regex guesswork.
def _cmdlines_from_powershell() -> Iterator[tuple[int, str]]:
    stdout = _run_command(
        [
            "powershell",
            "-NoProfile",
            "-Command",
            "Get-CimInstance Win32_Process | Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress",
        ]
    )
    if not stdout:
        return
    try:
        data = json.loads(stdout)
    except ValueError:
        return
    # A single match serializes as an object rather than a one-element array.
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        return
    for item in data:
        if not isinstance(item, dict):
            continue
        pid, cmdline = item.get("ProcessId"), item.get("CommandLine")
        if isinstance(pid, int) and isinstance(cmdline, str) and cmdline.strip():
            yield pid, cmdline.strip()


# Windows fallback: PowerShell can be disabled by execution policy or Group
# Policy. The deprecated `wmic` is still present on most installs and reaches
# the same WMI data; CSV output keeps quoted fields intact.
def _cmdlines_from_wmic() -> Iterator[tuple[int, str]]:
    stdout = _run_command(["wmic", "process", "get", "ProcessId,CommandLine", "/format:csv"])
    if not stdout:
        return
    reader = csv.reader(io.StringIO(stdout))
    pid_idx: Optional[int] = None
    cmd_idx: Optional[int] = None
    for row in reader:
        if not row or not any(row):
            continue
        if pid_idx is None:
            header = [col.strip().lower() for col in row]
            if "processid" not in header or "commandline" not in header:
                return
            pid_idx, cmd_idx = header.index("processid"), header.index("commandline")
            continue
        if len(row) <= max(pid_idx, cmd_idx):
            continue
        pid_str, cmdline = row[pid_idx].strip(), row[cmd_idx].strip()
        if pid_str.isdigit() and cmdline:
            yield int(pid_str), cmdline


# Last resort anywhere POSIX: `ps aux` has 10 fixed columns before COMMAND.
def _cmdlines_from_ps_aux() -> Iterator[tuple[int, str]]:
    stdout = _run_command(["ps", "aux"])
    if not stdout:
        return
    for line in stdout.splitlines():
        parts = line.split(None, 10)
        if len(parts) > 10 and parts[1].isdigit():
            cmdline = parts[10].strip()
            if cmdline:
                yield int(parts[1]), cmdline


def _cmdline_strategies():
    if sys.platform.startswith("linux"):
        return (_cmdlines_from_proc, _cmdlines_from_ps_long, _cmdlines_from_ps_aux)
    if sys.platform == "darwin":
        return (_cmdlines_from_ps_long, _cmdlines_from_ps_aux)
    if sys.platform.startswith("win"):
        return (_cmdlines_from_powershell, _cmdlines_from_wmic)
    return (_cmdlines_from_proc, _cmdlines_from_ps_long, _cmdlines_from_ps_aux)


def process_cmdlines() -> Iterator[tuple[int, str]]:
    """Yield (pid, command line) for every process this user can see."""
    for strategy in _cmdline_strategies():
        try:
            results = list(strategy())
        except Exception:
            continue
        if results:
            yield from results
            return


# ----------------------------------------------------------------- candidates

# Matches language_server_linux_x64, language_server_macos_arm64, and
# language_server_windows_x64.exe alike.
_LANGUAGE_SERVER_MARKER = "language_server"


def find_language_server_candidates() -> list[tuple[int, int, str]]:
    """Return (pid, port, csrf_token) for every plausible language server endpoint.

    A process qualifies when its command line names a language server and
    carries a usable CSRF token; it is then paired with each loopback port it
    listens on. Ports are deduplicated in discovery order. These are candidates
    only — the caller is expected to probe each one before trusting it.
    """
    ports_by_pid = listening_ports_by_pid()
    seen_ports: set[int] = set()
    candidates: list[tuple[int, int, str]] = []
    for pid, cmdline in process_cmdlines():
        if _LANGUAGE_SERVER_MARKER not in cmdline.lower():
            continue
        token = parse_csrf_token(cmdline)
        if not token:
            continue
        for port in ports_by_pid.get(pid, []):
            if port not in seen_ports:
                seen_ports.add(port)
                candidates.append((pid, port, token))
    return candidates
