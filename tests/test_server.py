"""MCP protocol surface: handshake, tool registration, validation, job lifecycle.

Every test here runs against the mock backend with local discovery disabled, so
the suite is hermetic: no Antigravity IDE, no network, no API key. State is
redirected to a temp directory so running tests never touches real job history.
"""

import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest

from antigravity_mcp import __version__, protocol
from antigravity_mcp.job_manager import JobManager
from antigravity_mcp.server import INVALID_PARAMS, METHOD_NOT_FOUND, MCPServer

JOB_ID_PATTERN = re.compile(r"`(agy-[a-f0-9]+)`")

HERMETIC_ENV = {
    "ANTIGRAVITY_MOCK": "1",
    "ANTIGRAVITY_DISABLE_LOCAL": "1",
}


class HermeticServerTest(unittest.TestCase):
    """Base class giving each test a throwaway state dir and the mock backend."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._saved = {k: os.environ.get(k) for k in (*HERMETIC_ENV, "ANTIGRAVITY_STATE_DIR")}
        os.environ.update(HERMETIC_ENV)
        os.environ["ANTIGRAVITY_STATE_DIR"] = self._tmp.name
        self.server = MCPServer()

    def tearDown(self):
        self.server.shutdown()
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self._tmp.cleanup()

    def call(self, name, arguments=None, req_id=1):
        return self.server.handle_request(
            {
                "jsonrpc": "2.0",
                "id": req_id,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments or {}},
            }
        )

    def text_of(self, response):
        return response["result"]["content"][0]["text"]

    def dispatch(self, task="Refactor the auth middleware", **kwargs):
        response = self.call("dispatch_gemini_agent", {"task": task, **kwargs})
        match = JOB_ID_PATTERN.search(self.text_of(response))
        self.assertIsNotNone(match, "dispatch response did not contain a job id")
        return match.group(1)

    def wait_for_terminal(self, job_id, timeout=5.0):
        """Poll until the job leaves pending/running, so tests never race the pool."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            job = self.server.job_manager.get_job(job_id)
            if job and job.status not in ("pending", "running"):
                return job
            time.sleep(0.05)
        self.fail(f"job {job_id} did not finish within {timeout}s")


class TestHandshake(HermeticServerTest):
    def test_initialize_reports_identity(self):
        """The handshake advertises this server's real name and version."""
        res = self.server.handle_request(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
        )
        info = res["result"]["serverInfo"]
        self.assertEqual(info["name"], "antigravity-mcp")
        self.assertEqual(info["version"], __version__)
        self.assertIn("tools", res["result"]["capabilities"])

    def test_initialize_echoes_supported_protocol_version(self):
        """A client asking for a version we implement gets that version back."""
        res = self.server.handle_request(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2025-06-18"},
            }
        )
        self.assertEqual(res["result"]["protocolVersion"], "2025-06-18")

    def test_initialize_falls_back_on_unknown_protocol_version(self):
        """An unrecognised version negotiates down to our default, not an error."""
        res = self.server.handle_request(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "1999-01-01"},
            }
        )
        self.assertEqual(res["result"]["protocolVersion"], "2024-11-05")

    def test_notification_gets_no_response(self):
        """Notifications have no id and must never be answered."""
        res = self.server.handle_request({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.assertIsNone(res)

    def test_ping(self):
        """ping carries no payload of its own, but is still a well-formed result.

        Removed in 2026-07-28 and kept only because legacy clients still send it."""
        res = self.server.handle_request({"jsonrpc": "2.0", "id": 9, "method": "ping"})
        self.assertEqual(res["result"]["resultType"], "complete")
        self.assertNotIn("error", res)

    def test_unknown_method(self):
        """An unsupported method returns JSON-RPC -32601."""
        res = self.server.handle_request({"jsonrpc": "2.0", "id": 9, "method": "tools/nope"})
        self.assertEqual(res["error"]["code"], METHOD_NOT_FOUND)


class TestToolRegistration(HermeticServerTest):
    def test_tools_list_is_complete_and_well_formed(self):
        """Every advertised tool carries a description and a valid input schema."""
        tools = self.server.handle_request({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})[
            "result"
        ]["tools"]
        names = {t["name"] for t in tools}
        self.assertEqual(
            names,
            {
                "dispatch_gemini_agent",
                "list_available_models",
                "check_agent_job",
                "list_active_jobs",
                "cancel_agent_job",
                "gemini_code_search",
            },
        )
        for tool in tools:
            self.assertTrue(tool["description"].strip(), f"{tool['name']} has no description")
            self.assertEqual(tool["inputSchema"]["type"], "object")

    def test_schemas_are_json_serialisable(self):
        """Schemas must survive the wire; a stray non-JSON value breaks every client."""
        json.dumps(self.server.handle_request({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}))


class TestArgumentValidation(HermeticServerTest):
    def test_unknown_tool_is_a_protocol_error(self):
        """Calling a tool that does not exist is the client's bug, not a tool failure."""
        self.assertEqual(self.call("does_not_exist")["error"]["code"], INVALID_PARAMS)

    def test_missing_required_argument(self):
        """dispatch without a task is rejected before a job is ever created."""
        res = self.call("dispatch_gemini_agent", {})
        self.assertEqual(res["error"]["code"], INVALID_PARAMS)
        self.assertEqual(self.server.job_manager.list_jobs(), [])

    def test_blank_required_argument(self):
        """Whitespace is not a task."""
        self.assertEqual(
            self.call("dispatch_gemini_agent", {"task": "   "})["error"]["code"], INVALID_PARAMS
        )

    def test_context_files_must_be_strings(self):
        """A malformed context_files array is reported, not silently coerced."""
        res = self.call("dispatch_gemini_agent", {"task": "x", "context_files": [1, 2]})
        self.assertEqual(res["error"]["code"], INVALID_PARAMS)

    def test_search_requires_at_least_one_file(self):
        """An empty file list would search nothing and mislead the caller."""
        res = self.call("gemini_code_search", {"query": "q", "context_files": []})
        self.assertEqual(res["error"]["code"], INVALID_PARAMS)

    def test_unknown_job_id(self):
        """Checking a job that does not exist is an input error, not a crash."""
        self.assertEqual(
            self.call("check_agent_job", {"job_id": "agy-000"})["error"]["code"], INVALID_PARAMS
        )


class TestJobLifecycle(HermeticServerTest):
    def test_dispatch_then_collect(self):
        """A dispatched job runs on the pool and its output is retrievable by id."""
        job_id = self.dispatch()
        self.wait_for_terminal(job_id)
        text = self.text_of(self.call("check_agent_job", {"job_id": job_id}))
        self.assertIn("COMPLETED", text)

    def test_elapsed_time_of_a_running_job_is_never_negative(self):
        """Regression: elapsed was measured against created_at, so running jobs showed -0.1s."""
        job = self.server.job_manager.create_job(task="t", model="flash")
        job.status = "running"
        job.started_at = time.time()
        time.sleep(0.02)
        text = self.text_of(self.call("check_agent_job", {"job_id": job.id}))
        elapsed = float(re.search(r"\*\*Elapsed\*\*: ([\d.]+)s", text).group(1))
        self.assertGreaterEqual(elapsed, 0.0)

    def test_list_and_cancel(self):
        """A dispatched job appears in listings and can be cancelled by id."""
        job_id = self.dispatch(task="Long running simulation")
        self.assertIn(job_id, self.text_of(self.call("list_active_jobs")))
        job = self.server.job_manager.get_job(job_id)
        job.status = "running"
        self.assertIn(
            "Cancellation requested",
            self.text_of(self.call("cancel_agent_job", {"job_id": job_id})),
        )
        self.assertEqual(job.status, "cancelled")

    def test_cancel_finished_job_is_rejected(self):
        """Cancelling something already done is reported rather than faked."""
        job_id = self.dispatch()
        self.wait_for_terminal(job_id)
        self.assertEqual(
            self.call("cancel_agent_job", {"job_id": job_id})["error"]["code"], INVALID_PARAMS
        )

    def test_list_jobs_rejects_bad_limit(self):
        """A nonsensical limit is an input error."""
        self.assertEqual(
            self.call("list_active_jobs", {"limit": 0})["error"]["code"], INVALID_PARAMS
        )

    def test_code_search_returns_an_answer(self):
        """Code search scans the given files and answers synchronously."""
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as handle:
            handle.write("def add(a, b):\n    return a + b\n")
            path = handle.name
        try:
            text = self.text_of(
                self.call(
                    "gemini_code_search", {"query": "what does add do?", "context_files": [path]}
                )
            )
            self.assertIn("Code search results", text)
        finally:
            os.unlink(path)


class TestJobPersistence(unittest.TestCase):
    def test_history_survives_a_restart(self):
        """Job history is mirrored to disk so a new session can still read it."""
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "jobs.json")
            first = JobManager(state_path=path)
            job = first.create_job(task="persisted task", model="flash")
            job.status = "completed"
            job.result = "done"
            job.log("finished")

            reloaded = JobManager(state_path=path).get_job(job.id)
            self.assertIsNotNone(reloaded)
            self.assertEqual(reloaded.result, "done")

    def test_in_flight_jobs_are_marked_failed_on_reload(self):
        """A job still running when the process died can never finish; reopening it as
        running would hang the orchestrator forever, so it loads as failed."""
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "jobs.json")
            manager = JobManager(state_path=path)
            job = manager.create_job(task="interrupted", model="flash")
            job.status = "running"
            job.log("started")

            reloaded = JobManager(state_path=path).get_job(job.id)
            self.assertEqual(reloaded.status, "failed")
            self.assertIn("session ended", reloaded.error)

    def test_corrupt_state_file_does_not_crash(self):
        """A truncated or garbled state file starts fresh instead of taking the server down."""
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "jobs.json")
            with open(path, "w") as handle:
                handle.write("{not valid json")
            self.assertEqual(JobManager(state_path=path).list_jobs(), [])


class TestStdioSubprocess(unittest.TestCase):
    def test_real_subprocess_handshake(self):
        """The published entry point works as an actual stdio subprocess, which is
        the only way any MCP client ever launches it."""
        env = {
            **os.environ,
            **HERMETIC_ENV,
            "PYTHONPATH": os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"
            ),
        }
        with tempfile.TemporaryDirectory() as tmp:
            env["ANTIGRAVITY_STATE_DIR"] = tmp
            proc = subprocess.Popen(
                [sys.executable, "-m", "antigravity_mcp"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
            )
            request = json.dumps(
                {"jsonrpc": "2.0", "id": 100, "method": "initialize", "params": {}}
            )
            stdout, _ = proc.communicate(input=request + "\n", timeout=30)

        lines = [line for line in stdout.strip().splitlines() if line.strip()]
        self.assertTrue(lines, "server produced no response on stdout")
        response = json.loads(lines[0])
        self.assertEqual(response["id"], 100)
        self.assertEqual(response["result"]["serverInfo"]["name"], "antigravity-mcp")


if __name__ == "__main__":
    unittest.main(verbosity=2)


MODERN_META = {
    protocol.META_PROTOCOL_VERSION: "2026-07-28",
    protocol.META_CLIENT_CAPABILITIES: {},
    protocol.META_CLIENT_INFO: {"name": "TestClient", "version": "1.0.0"},
}


class TestModernProtocol(HermeticServerTest):
    """The 2026-07-28 era: per-request metadata, no handshake."""

    def modern(self, method, params=None, req_id=1, meta=None):
        body = dict(params or {})
        body["_meta"] = MODERN_META if meta is None else meta
        return self.server.handle_request(
            {"jsonrpc": "2.0", "id": req_id, "method": method, "params": body}
        )

    def test_server_discover_is_implemented(self):
        """Servers MUST implement server/discover; on stdio it is also the era probe."""
        result = self.modern("server/discover")["result"]
        self.assertEqual(result["resultType"], "complete")
        self.assertIn("2026-07-28", result["supportedVersions"])
        self.assertIn("tools", result["capabilities"])

    def test_discover_advertises_legacy_versions_too(self):
        """A client that cannot speak the modern version reads a usable one from here."""
        supported = self.modern("server/discover")["result"]["supportedVersions"]
        self.assertIn("2024-11-05", supported)

    def test_discover_reports_server_identity(self):
        """serverInfo moves into result _meta under the reserved key."""
        meta = self.modern("server/discover")["result"]["_meta"]
        self.assertEqual(meta[protocol.META_SERVER_INFO]["name"], "antigravity-mcp")

    def test_tools_call_without_a_handshake(self):
        """The whole point of the revision: a tool call works with no initialize."""
        result = self.modern("tools/call", {"name": "list_active_jobs", "arguments": {}})["result"]
        self.assertEqual(result["resultType"], "complete")
        self.assertFalse(result["isError"])

    def test_unsupported_version_is_rejected_with_32022(self):
        """Silently downgrading left the client believing it had negotiated something
        it had not; the spec requires naming the versions actually supported."""
        error = self.modern(
            "tools/list",
            meta={
                protocol.META_PROTOCOL_VERSION: "1900-01-01",
                protocol.META_CLIENT_CAPABILITIES: {},
            },
        )["error"]
        self.assertEqual(error["code"], protocol.UNSUPPORTED_PROTOCOL_VERSION)
        self.assertEqual(error["data"]["requested"], "1900-01-01")
        self.assertIn("2026-07-28", error["data"]["supported"])

    def test_missing_client_capabilities_is_invalid_params(self):
        """clientCapabilities is required on every modern request."""
        error = self.modern("tools/list", meta={protocol.META_PROTOCOL_VERSION: "2026-07-28"})[
            "error"
        ]
        self.assertEqual(error["code"], INVALID_PARAMS)

    def test_tools_list_is_cacheable(self):
        """ttlMs and cacheScope let clients cache a catalog that rarely changes."""
        result = self.modern("tools/list")["result"]
        self.assertGreater(result["ttlMs"], 0)
        self.assertEqual(result["cacheScope"], "public")

    def test_tool_catalog_order_is_deterministic(self):
        """Stable ordering is what makes client-side and prompt caching work."""
        first = [t["name"] for t in self.modern("tools/list")["result"]["tools"]]
        second = [t["name"] for t in self.modern("tools/list")["result"]["tools"]]
        self.assertEqual(first, second)


class TestDualEra(HermeticServerTest):
    """Both eras served by one process, decided per request."""

    def test_legacy_handshake_still_works(self):
        """Existing clients must not break on upgrade."""
        res = self.server.handle_request(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2025-06-18"},
            }
        )
        self.assertEqual(res["result"]["protocolVersion"], "2025-06-18")

    def test_initialize_never_promises_a_modern_version(self):
        """A handshake client has shown it is not speaking the per-request protocol,
        so echoing 2026-07-28 back would promise something it cannot act on."""
        res = self.server.handle_request(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2026-07-28"},
            }
        )
        self.assertEqual(res["result"]["protocolVersion"], protocol.DEFAULT_LEGACY_VERSION)

    def test_legacy_results_also_carry_result_type(self):
        """One code path serves both eras: earlier revisions tell clients to treat an
        absent resultType as complete, so including it is harmless for them."""
        res = self.server.handle_request({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        self.assertEqual(res["result"]["resultType"], "complete")

    def test_discover_answered_without_meta(self):
        """A probe that omits _meta is answered rather than refused; being strict here
        would fail the very handshake the probe exists to establish."""
        res = self.server.handle_request({"jsonrpc": "2.0", "id": 1, "method": "server/discover"})
        self.assertIn("supportedVersions", res["result"])
