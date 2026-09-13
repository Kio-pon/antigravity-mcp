"""MCP server exposing the Antigravity IDE's agent engine as dispatchable tools.

Speaks JSON-RPC 2.0 over stdio, the transport every MCP client supports, so the
same process works unmodified under Claude Code, Codex, Cursor, or anything
else that can launch a subprocess.

Requests are handled on a thread pool rather than inline on the read loop. That
matters more than it looks: `gemini_code_search` runs its scan synchronously
before answering, and on a single-threaded loop a two-minute search would
freeze every other tool call behind it — including the `check_agent_job` polls
the orchestrator needs to watch its other jobs. JSON-RPC ids let responses come
back out of order, so concurrent handling is free correctness here.
"""

import json
import logging
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Optional

from . import __version__, protocol
from .antigravity_local import describe_available_models
from .executor import AgentExecutor
from .job_manager import JobManager

logging.basicConfig(
    level=os.environ.get("ANTIGRAVITY_LOG_LEVEL", "INFO").upper(),
    format="[%(asctime)s] %(levelname)s: %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("antigravity-mcp")

SERVER_NAME = "antigravity-mcp"
SERVER_VERSION = __version__

# This server is "dual-era" in the 2026-07-28 spec's terminology: it serves both
# modern clients, which carry the protocol version in each request's `_meta` and
# never shake hands, and legacy clients, which open with `initialize`. Which era
# a request belongs to is read off the request itself, so both can be served on
# the same process — see protocol.py for the rules.
DEFAULT_PROTOCOL_VERSION = protocol.DEFAULT_LEGACY_VERSION

# How long a client may cache our tool catalog. The catalog is a module-level
# constant that only changes when the server binary does, so a long TTL is
# honest. `public` because nothing in it varies per user.
LIST_CACHE_TTL_MS = 3_600_000
LIST_CACHE_SCOPE = "public"

# Every dispatched job drives a Cascade session on the SAME local language
# server process — it is one shared backend, not one per job. Without a cap,
# N concurrent dispatches mean N concurrent live model conversations competing
# for that one process's RAM and CPU. Jobs past the cap queue for a slot.
MAX_CONCURRENT_JOBS = int(os.environ.get("ANTIGRAVITY_MAX_CONCURRENT_JOBS", "3"))

# Request handling is cheap (mostly dict lookups); the ceiling exists only so a
# misbehaving client cannot spawn unbounded threads. It is deliberately larger
# than the job cap so status polls are never stuck behind running work.
MAX_CONCURRENT_REQUESTS = int(os.environ.get("ANTIGRAVITY_MAX_CONCURRENT_REQUESTS", "16"))

SERVER_INSTRUCTIONS = """\
Delegate work to a local Antigravity IDE agent instead of doing it inline when \
the task is large-context, parallelisable, or mechanical.

Typical loop: call list_available_models if you do not already know a live \
model name, dispatch_gemini_agent to start the job (returns immediately with a \
job_id), continue your own work, then check_agent_job to collect the result. \
Always review a subagent's output before applying it to files."""

TOOLS = [
    {
        "name": "dispatch_gemini_agent",
        "description": (
            "Dispatch a coding, analysis, or research task to a background Antigravity "
            "subagent. Returns immediately with a job_id so the orchestrator is never blocked."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "task": {
                    "type": "string",
                    "description": "Detailed task description or prompt for the worker agent.",
                },
                "context_files": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional absolute file paths to include as context for the agent.",
                },
                "model": {
                    "type": "string",
                    "description": (
                        "Which live model to run this job on, e.g. 'Gemini 3.8 Flash (High)'. "
                        "The live catalog (versions, families, and thinking-effort tiers) "
                        "changes over time and depends on the connected Antigravity IDE "
                        "session — call list_available_models rather than assuming a name. "
                        "Defaults to the fastest available flash-tier model."
                    ),
                },
                "system_instruction": {
                    "type": "string",
                    "description": "Custom system instructions for the subagent worker.",
                },
            },
            "required": ["task"],
        },
    },
    {
        "name": "list_available_models",
        "description": (
            "List the live models currently available from the connected Antigravity IDE "
            "session. Call this before dispatch_gemini_agent when you do not already know "
            "an exact, currently-live model name."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "check_agent_job",
        "description": "Check the status, logs, duration, and output of a dispatched agent job.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "job_id": {
                    "type": "string",
                    "description": "The job ID returned from dispatch_gemini_agent (e.g. 'agy-1a2b3c4d').",
                }
            },
            "required": ["job_id"],
        },
    },
    {
        "name": "list_active_jobs",
        "description": "List active, pending, completed, cancelled, or failed subagent jobs.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "default": 10,
                    "description": "Maximum number of recent jobs to return.",
                }
            },
        },
    },
    {
        "name": "cancel_agent_job",
        "description": "Cancel a running background subagent job.",
        "inputSchema": {
            "type": "object",
            "properties": {"job_id": {"type": "string", "description": "The job ID to cancel."}},
            "required": ["job_id"],
        },
    },
    {
        "name": "gemini_code_search",
        "description": (
            "Use a large-context model to scan multiple repository files at once and answer "
            "architectural or code questions about them. Runs synchronously and returns the answer."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Query or pattern to search for and explain across the provided files.",
                },
                "context_files": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Absolute paths of the files to scan.",
                },
            },
            "required": ["query", "context_files"],
        },
    },
]

SEARCH_SYSTEM_INSTRUCTION = (
    "You are a large-scale repository code searcher. Answer the query accurately and "
    "concretely, based strictly on the provided context files. Cite file paths."
)

# JSON-RPC reserved error codes used by this server.
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602


class ToolInputError(ValueError):
    """A tool was called with missing or malformed arguments."""


class MCPServer:
    def __init__(self, executor: Optional[AgentExecutor] = None) -> None:
        self.job_manager = JobManager()
        # Injectable so tests can drive the full request path against a stub
        # instead of a live language server. The server previously shipped a
        # mock backend to make that possible, which meant production code
        # carried a fake-result path purely for the benefit of the test suite.
        self.executor = executor or AgentExecutor()
        self._job_pool = ThreadPoolExecutor(
            max_workers=MAX_CONCURRENT_JOBS, thread_name_prefix="antigravity-job"
        )
        self._request_pool = ThreadPoolExecutor(
            max_workers=MAX_CONCURRENT_REQUESTS, thread_name_prefix="antigravity-req"
        )
        # Concurrent handlers share one stdout; a lock keeps each JSON-RPC
        # response on its own line instead of interleaving mid-message.
        self._write_lock = threading.Lock()
        self._handlers: dict[str, Callable[[dict[str, Any]], str]] = {
            "dispatch_gemini_agent": self._tool_dispatch_agent,
            "list_available_models": self._tool_list_models,
            "check_agent_job": self._tool_check_job,
            "list_active_jobs": self._tool_list_jobs,
            "cancel_agent_job": self._tool_cancel_job,
            "gemini_code_search": self._tool_code_search,
        }

    # ---------------------------------------------------------------- routing

    def handle_request(self, req: dict[str, Any]) -> Optional[dict[str, Any]]:
        if not isinstance(req, dict):
            return self._error(None, INVALID_REQUEST, "Request must be a JSON object.")

        req_id = req.get("id")
        method = req.get("method")
        params = req.get("params") or {}

        # A request without an id is a notification: act on it, answer nothing.
        if req_id is None:
            if method in ("notifications/initialized", "notifications/cancelled"):
                logger.debug("Received notification: %s", method)
            return None

        # Modern requests declare their version and capabilities per request, and
        # one declaring a version we do not implement must be refused here rather
        # than served on a guess. Legacy requests carry no `_meta` and fall
        # through to the handshake path below.
        if protocol.is_modern_request(req):
            try:
                protocol.negotiate_modern_version(req)
            except protocol.ProtocolError as exc:
                return {"jsonrpc": "2.0", "id": req_id, "error": exc.to_error_object()}

        # Servers MUST implement server/discover. On stdio it doubles as the
        # client's era probe: answering it at all is what identifies this server
        # as modern, so it is handled before any era-specific branch.
        if method == "server/discover":
            return self._result(req_id, self._discover_payload())

        if method == "initialize":
            # The handshake exists only for legacy clients; a modern client never
            # sends it. Echoing a modern version here would promise a protocol
            # the client has already shown it is not speaking.
            version = protocol.negotiate_legacy_version(params.get("protocolVersion"))
            return self._result(
                req_id,
                {
                    "protocolVersion": version,
                    "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                    "capabilities": {"tools": {"listChanged": False}},
                    "instructions": SERVER_INSTRUCTIONS,
                },
            )

        if method == "ping":
            # Removed in 2026-07-28; kept for legacy clients that still send it.
            return self._result(req_id, {})

        if method == "tools/list":
            return self._result(
                req_id,
                {"tools": TOOLS, "ttlMs": LIST_CACHE_TTL_MS, "cacheScope": LIST_CACHE_SCOPE},
            )

        if method == "tools/call":
            return self._call_tool(req_id, params.get("name"), params.get("arguments") or {})

        return self._error(req_id, METHOD_NOT_FOUND, f"Method not found: {method}")

    def _discover_payload(self) -> dict[str, Any]:
        """Identity, capabilities, and every protocol version this server speaks.

        Deliberately lists the legacy versions alongside the modern one: a client
        that cannot speak 2026-07-28 can read a version it does support straight
        out of this response instead of having to probe for one.
        """
        return {
            "supportedVersions": list(protocol.SUPPORTED_VERSIONS),
            "capabilities": {"tools": {"listChanged": False}},
            "instructions": SERVER_INSTRUCTIONS,
            "ttlMs": LIST_CACHE_TTL_MS,
            "cacheScope": LIST_CACHE_SCOPE,
        }

    def _call_tool(self, req_id: Any, name: Optional[str], args: dict[str, Any]) -> dict[str, Any]:
        handler = self._handlers.get(name or "")
        if handler is None:
            # An unknown tool is a protocol-level mistake by the client, not a
            # failed execution, so it gets a JSON-RPC error rather than an
            # isError result the model would try to reason about.
            return self._error(req_id, INVALID_PARAMS, f"Unknown tool: {name}")
        try:
            return self._tool_success(req_id, handler(args))
        except ToolInputError as exc:
            return self._error(req_id, INVALID_PARAMS, str(exc))
        except Exception as exc:  # surfaced to the model so it can adapt
            logger.exception("Error executing tool %s", name)
            return self._tool_error(req_id, f"{type(exc).__name__}: {exc}")

    # ------------------------------------------------------------ tool bodies

    @staticmethod
    def _require_str(args: dict[str, Any], key: str) -> str:
        value = args.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ToolInputError(f"Missing or empty required argument: '{key}'.")
        return value

    @staticmethod
    def _optional_paths(args: dict[str, Any], key: str) -> list:
        value = args.get(key) or []
        if not isinstance(value, list) or not all(isinstance(p, str) for p in value):
            raise ToolInputError(f"'{key}' must be an array of file path strings.")
        return value

    def _tool_dispatch_agent(self, args: dict[str, Any]) -> str:
        task = self._require_str(args, "task")
        files = self._optional_paths(args, "context_files")
        model = args.get("model") or "flash"
        system_instruction = args.get("system_instruction")

        job = self.job_manager.create_job(task=task, model=model, metadata={"context_files": files})
        self._job_pool.submit(self.executor.run_job_sync, job, files, system_instruction)

        return (
            f"✅ **Subagent dispatched**\n"
            f"- **Job ID**: `{job.id}`\n"
            f"- **Model**: `{model}`\n"
            f"- **Status**: `{job.status}`\n"
            f"- **Note**: up to {MAX_CONCURRENT_JOBS} jobs run concurrently "
            f"(ANTIGRAVITY_MAX_CONCURRENT_JOBS); beyond that this job queues for a slot.\n\n"
            f"Collect the result with `check_agent_job` using `job_id='{job.id}'`."
        )

    def _tool_list_models(self, args: dict[str, Any]) -> str:
        return describe_available_models()

    def _tool_check_job(self, args: dict[str, Any]) -> str:
        job_id = self._require_str(args, "job_id")
        job = self.job_manager.get_job(job_id)
        if not job:
            raise ToolInputError(f"Job '{job_id}' not found.")

        snapshot = job.to_dict()
        duration = snapshot["duration_seconds"]
        logs = "\n".join(f"  {line}" for line in snapshot["progress_logs"])

        if job.status == "running":
            return (
                f"⏳ **Job `{job.id}` is RUNNING**\n"
                f"- **Model**: `{job.model}`\n"
                f"- **Elapsed**: {duration}s\n"
                f"- **Logs**:\n{logs}\n\n"
                f"*Still processing — check again shortly.*"
            )
        if job.status == "pending":
            return (
                f"🕒 **Job `{job.id}` is QUEUED**\n"
                f"- **Model**: `{job.model}`\n"
                f"- Waiting for one of {MAX_CONCURRENT_JOBS} concurrent slots to free up."
            )
        if job.status == "completed":
            return f"🎉 **Job `{job.id}` COMPLETED** ({duration}s)\n\n### Output:\n{job.result}"
        if job.status == "cancelled":
            return f"🚫 **Job `{job.id}` was CANCELLED**\n\nLogs:\n{logs}"
        return f"❌ **Job `{job.id}` FAILED**\n- **Error**: {job.error}\n- **Logs**:\n{logs}"

    def _tool_list_jobs(self, args: dict[str, Any]) -> str:
        limit = args.get("limit", 10)
        if not isinstance(limit, int) or limit < 1:
            raise ToolInputError("'limit' must be a positive integer.")
        jobs = self.job_manager.list_jobs(limit=limit)
        if not jobs:
            return "No jobs recorded yet."
        lines = [f"**Active & recent jobs ({len(jobs)})**:"]
        for j in jobs:
            task = j["task"].replace("\n", " ")
            summary = task[:70] + ("…" if len(task) > 70 else "")
            lines.append(
                f"- `{j['id']}` [{j['status'].upper()}] {summary} ({j['duration_seconds']}s)"
            )
        return "\n".join(lines)

    def _tool_cancel_job(self, args: dict[str, Any]) -> str:
        job_id = self._require_str(args, "job_id")
        if self.job_manager.cancel_job(job_id):
            return f"Cancellation requested for job `{job_id}`."
        raise ToolInputError(f"Job `{job_id}` not found, or it already finished.")

    def _tool_code_search(self, args: dict[str, Any]) -> str:
        query = self._require_str(args, "query")
        files = self._optional_paths(args, "context_files")
        if not files:
            raise ToolInputError("'context_files' must list at least one file to scan.")

        # Routed through the job pool so a search counts against the same
        # concurrency budget as a dispatched job — they contend for the one
        # language server process either way. This call blocks its own request
        # thread waiting for the answer, but not the stdio read loop.
        job = self.job_manager.create_job(task=query, model="flash", metadata={"search": True})
        future = self._job_pool.submit(
            self.executor.run_job_sync, job, files, SEARCH_SYSTEM_INSTRUCTION
        )
        future.result()

        if job.status == "completed":
            return f"### Code search results\n{job.result}"
        raise RuntimeError(f"Search failed: {job.error}")

    # -------------------------------------------------------------- responses

    @staticmethod
    def _result(req_id: Any, result: dict[str, Any]) -> dict[str, Any]:
        """Wrap a payload as a JSON-RPC result.

        Every result carries `resultType` and the server's identity, which
        2026-07-28 requires. Legacy clients ignore both — earlier revisions tell
        clients to treat an absent `resultType` as "complete" — so a single code
        path serves both eras without branching.
        """
        payload = protocol.complete_result(result, SERVER_NAME, SERVER_VERSION)
        return {"jsonrpc": "2.0", "id": req_id, "result": payload}

    @staticmethod
    def _error(req_id: Any, code: int, message: str) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}

    def _tool_success(self, req_id: Any, text: str) -> dict[str, Any]:
        return self._result(req_id, {"content": [{"type": "text", "text": text}], "isError": False})

    def _tool_error(self, req_id: Any, text: str) -> dict[str, Any]:
        return self._result(req_id, {"content": [{"type": "text", "text": text}], "isError": True})

    # ------------------------------------------------------------------ stdio

    def _write(self, message: dict[str, Any]) -> None:
        payload = json.dumps(message)
        with self._write_lock:
            try:
                sys.stdout.write(payload + "\n")
                sys.stdout.flush()
            except (BrokenPipeError, ValueError):
                # The client closed the pipe. Nothing left to say to it.
                pass

    def _handle_line(self, line: str) -> None:
        try:
            req = json.loads(line)
        except json.JSONDecodeError as exc:
            self._write(self._error(None, PARSE_ERROR, f"Parse error: {exc}"))
            return
        try:
            response = self.handle_request(req)
        except Exception as exc:
            logger.exception("Unhandled error handling request")
            req_id = req.get("id") if isinstance(req, dict) else None
            response = self._error(req_id, INVALID_REQUEST, f"Internal error: {exc}")
        if response is not None:
            self._write(response)

    def run_stdio(self) -> None:
        """Read newline-delimited JSON-RPC from stdin until the client hangs up."""
        logger.info("antigravity-mcp %s listening on stdio", SERVER_VERSION)
        try:
            for raw in sys.stdin:
                line = raw.strip()
                if line:
                    self._request_pool.submit(self._handle_line, line)
        except KeyboardInterrupt:
            pass
        finally:
            self.shutdown()

    def shutdown(self, wait_for_jobs: bool = False) -> None:
        """Drain in-flight requests, then stop accepting new work.

        In-flight requests are allowed to finish so their responses (and, more
        importantly, their persisted job state) are not lost mid-write.

        Running agent jobs are not waited on by default: they are already
        recorded on disk and get marked failed on the next load, which is
        exactly what a killed session is, and a client closing the pipe should
        not block for however long a model takes.

        `wait_for_jobs` makes shutdown fully quiescent instead. Callers that
        tear down the state directory afterwards need it — a job thread still
        running will keep writing `jobs.json` into a directory being deleted
        underneath it, which surfaces as a "directory not empty" race.
        """
        logger.info("Shutting down.")
        self._request_pool.shutdown(wait=True, cancel_futures=False)
        self._job_pool.shutdown(wait=wait_for_jobs, cancel_futures=not wait_for_jobs)


def main() -> None:
    MCPServer().run_stdio()


if __name__ == "__main__":
    main()
