"""
Unified Executor engine for running Antigravity & Gemini Flash worker agents.
Supports priority dispatch:
1. Local Antigravity IDE language server (active authenticated session, no API key).
2. Google Gemini REST API (requires GEMINI_API_KEY or GOOGLE_API_KEY).
If neither is available, the job fails loudly with an explicit error rather
than inventing a result.
"""

import json
import os
import time
import urllib.error
import urllib.request
from typing import Optional

from .antigravity_local import (
    AntigravityLocalExecutor,
    collect_file_contexts,
    discover_language_server,
)
from .job_manager import Job

# How long a single agent job may run before it is abandoned.
#
# The old default was 300s, which is shorter than plenty of legitimate work: a
# `pip install` on a slow link, a multi-file refactor, or anything that waits on
# a subprocess. A real run was observed finishing at 17:55:0x after the bridge
# had already given up at 17:54:57 — the agent had done the work correctly and
# the result was thrown away three seconds short.
#
# A generous ceiling costs nothing when jobs finish early (completion is
# detected, not waited out); it only bounds a genuinely stuck agent. Override
# with ANTIGRAVITY_JOB_TIMEOUT (seconds).
DEFAULT_JOB_TIMEOUT = float(os.environ.get("ANTIGRAVITY_JOB_TIMEOUT", "1800"))


class AgentExecutor:
    """
    Unified executor dispatching agent jobs in priority order:
    local Antigravity language server, then the Gemini REST API.
    """

    def __init__(self, api_key: Optional[str] = None, timeout: Optional[float] = None):
        self.api_key = (
            api_key or os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        )
        self.timeout = DEFAULT_JOB_TIMEOUT if timeout is None else timeout

    def run_job_sync(
        self,
        job: Job,
        context_files: Optional[list[str]] = None,
        system_instruction: Optional[str] = None,
    ) -> None:
        # Priority 1: Local Antigravity Language Server
        if os.environ.get("ANTIGRAVITY_DISABLE_LOCAL", "").lower() not in ("1", "true"):
            endpoint = discover_language_server()
            if endpoint:
                local_exec = AntigravityLocalExecutor(endpoint=endpoint, timeout=self.timeout)
                local_exec.run_job_sync(
                    job, context_files=context_files, system_instruction=system_instruction
                )
                return

        # Priority 2: Gemini REST API (if GEMINI_API_KEY is set)
        if self.api_key:
            self._run_gemini_rest(
                job, context_files=context_files, system_instruction=system_instruction
            )
            return

        # No backend available -> fail loudly
        job.started_at = time.time()
        job.status = "failed"
        job.finished_at = time.time()
        job.error = (
            "No execution backend available. "
            "Ensure the Antigravity IDE is running (with its language server active), "
            "or set GEMINI_API_KEY in the environment for cloud execution."
        )
        job.log("Execution failed: No available backend.")

    def _run_gemini_rest(
        self,
        job: Job,
        context_files: Optional[list[str]] = None,
        system_instruction: Optional[str] = None,
    ) -> None:
        job.started_at = time.time()
        job.status = "running"
        job.log(f"Started job on Gemini REST API with model {job.model}")

        context_str = ""
        if context_files:
            job.log(f"Reading {len(context_files)} context files...")
            context_str = collect_file_contexts(context_files)
            job.log(f"Context gathered ({len(context_str)} characters).")

        if job.is_cancelled():
            return

        try:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{job.model}:generateContent?key={self.api_key}"

            system_prompt = system_instruction or (
                "You are an expert AI software engineer and background subagent worker assisting a lead architect. "
                "Provide direct, high-precision, actionable code and analysis."
            )

            parts = []
            if context_str:
                parts.append({"text": f"Context files:\n{context_str}\n\n"})
            parts.append({"text": f"Task:\n{job.task}"})

            payload = {
                "systemInstruction": {"parts": [{"text": system_prompt}]},
                "contents": [{"role": "user", "parts": parts}],
                "generationConfig": {"temperature": 0.2, "maxOutputTokens": 8192},
            }

            req = urllib.request.Request(
                url,
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )

            job.log("Dispatching request to Gemini REST endpoint...")
            with urllib.request.urlopen(req, timeout=120) as response:
                if job.is_cancelled():
                    return
                resp_data = json.loads(response.read().decode("utf-8"))
                candidates = resp_data.get("candidates", [])
                if candidates:
                    first_cand = candidates[0]
                    content = first_cand.get("content", {})
                    resp_parts = content.get("parts", [])
                    job.result = "".join(p.get("text", "") for p in resp_parts)
                    job.status = "completed"
                    job.finished_at = time.time()
                    job.log("Response received and processed.")
                else:
                    job.error = f"No candidate in response: {resp_data}"
                    job.status = "failed"
                    job.finished_at = time.time()
                    job.log("Failed: No candidate returned.")

        except Exception as e:
            job.error = str(e)
            job.status = "failed"
            job.finished_at = time.time()
            job.log(f"Error during execution: {e}")


# Backwards compatibility alias
GeminiExecutor = AgentExecutor
