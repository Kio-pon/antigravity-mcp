"""Drive the Antigravity IDE's local agent engine over its language server IPC.

Talks Connect-RPC to the loopback HTTPS endpoint the IDE already runs,
authenticating with the CSRF token from the live process. That reuses the
IDE's own signed-in session, which is why no API key is needed when the IDE is
open.

These are private IDE IPC endpoints with no stability guarantees across
versions — see the compatibility note in the README.
"""

import json
import os
import ssl
import threading
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

from .discovery import find_language_server_candidates
from .job_manager import Job

# A burst of concurrent dispatch_gemini_agent calls used to each independently
# scan /proc and shell out to `ss -ltnp` to relocate the language server, on
# top of the RAM/CPU already spent running that many live Cascade sessions.
# A short cache removes the redundant discovery cost for jobs starting within
# the same window; it does not affect the per-job model conversation itself.
_ENDPOINT_CACHE_TTL_SECONDS = 5.0
_endpoint_cache_lock = threading.Lock()
_endpoint_cache: dict[str, Any] = {"endpoint": None, "cached_at": 0.0}


@dataclass
class LanguageServerEndpoint:
    host: str
    port: int
    csrf_token: str
    pid: Optional[int] = None

    @property
    def base_url(self) -> str:
        return f"https://{self.host}:{self.port}/exa.language_server_pb.LanguageServerService"


@dataclass
class ModelInfo:
    label: str
    model_id: str
    supports_images: bool = False
    raw: dict[str, Any] = field(default_factory=dict)


def _create_ssl_context() -> ssl.SSLContext:
    """Create an SSL context bypassing local self-signed cert verification."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _rpc_call(
    endpoint: LanguageServerEndpoint, method: str, payload: dict[str, Any], timeout: float = 10.0
) -> dict[str, Any]:
    """Make a unary Connect-RPC call over HTTPS to the language server."""
    url = f"{endpoint.base_url}/{method}"
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json", "x-codeium-csrf-token": endpoint.csrf_token},
    )
    ctx = _create_ssl_context()
    with urllib.request.urlopen(req, context=ctx, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def discover_language_server(use_cache: bool = True) -> Optional[LanguageServerEndpoint]:
    """
    Dynamically discover the active Antigravity language server process.
    Validates candidates via Heartbeat RPC and returns the first healthy endpoint.

    Cached for _ENDPOINT_CACHE_TTL_SECONDS so a burst of concurrent jobs
    doesn't each pay the /proc + `ss -ltnp` + per-candidate Heartbeat cost.
    If the IDE restarts mid-window, the stale endpoint's next RPC call fails
    with a connection error, which callers already handle as a failed job —
    the cache trades a small chance of that against real discovery cost, so
    it is intentionally short-lived rather than disabled outright.
    """
    if use_cache:
        with _endpoint_cache_lock:
            cached = _endpoint_cache["endpoint"]
            age = time.time() - _endpoint_cache["cached_at"]
            if cached is not None and age < _ENDPOINT_CACHE_TTL_SECONDS:
                return cached

    unique_candidates = [
        LanguageServerEndpoint(host="127.0.0.1", port=port, csrf_token=token, pid=pid)
        for pid, port, token in find_language_server_candidates()
    ]

    for candidate in unique_candidates:
        try:
            resp = _rpc_call(candidate, "Heartbeat", {}, timeout=2.0)
            if "lastExtensionHeartbeat" in resp or resp == {} or "code" not in resp:
                with _endpoint_cache_lock:
                    _endpoint_cache["endpoint"] = candidate
                    _endpoint_cache["cached_at"] = time.time()
                return candidate
        except Exception:
            continue

    with _endpoint_cache_lock:
        _endpoint_cache["endpoint"] = None
        _endpoint_cache["cached_at"] = time.time()
    return None


def list_models(endpoint: LanguageServerEndpoint) -> list[ModelInfo]:
    """
    Fetch the list of valid, active model configurations for the user from GetUserStatus.
    """
    resp = _rpc_call(endpoint, "GetUserStatus", {}, timeout=5.0)
    user_status = resp.get("userStatus", {})
    cascade_data = user_status.get("cascadeModelConfigData", {})
    client_configs = cascade_data.get("clientModelConfigs", [])

    models: list[ModelInfo] = []
    for c in client_configs:
        label = c.get("label", "")
        model_or_alias = c.get("modelOrAlias", {})
        model_id = model_or_alias.get("model") or model_or_alias.get("alias") or ""
        if label and model_id:
            models.append(
                ModelInfo(
                    label=label,
                    model_id=model_id,
                    supports_images=c.get("supportsImages", False),
                    raw=c,
                )
            )
    return models


_EFFORT_WORDS = ("high", "medium", "med", "low", "thinking")
_EFFORT_NORMALIZE = {"med": "medium"}
_FAMILY_WORDS = ("gemini", "flash", "pro", "claude", "opus", "sonnet", "gpt", "oss")


def _tokenize(text: str) -> set:
    """Split a model name/label into lowercase word and version-number tokens."""
    text = text.lower().replace("_", " ").replace("-", " ").replace("(", " ").replace(")", " ")
    tokens = set()
    for word in text.split():
        word = word.strip(".")
        if not word:
            continue
        tokens.add(_EFFORT_NORMALIZE.get(word, word))
    return tokens


def describe_available_models() -> str:
    """
    Discover the running Antigravity language server and return a human-readable
    listing of every live model label (version, family, and thinking-effort
    tier) that dispatch_gemini_agent's `model` argument can actually resolve to.
    Raises RuntimeError with a clear message if no IDE session is reachable.
    """
    endpoint = discover_language_server()
    if not endpoint:
        raise RuntimeError(
            "Antigravity language server is not running or unreachable. "
            "Open the Antigravity IDE with an active Cascade session and retry."
        )
    models = list_models(endpoint)
    if not models:
        raise RuntimeError("Antigravity language server returned no available models.")

    lines = [f"{len(models)} models available:"]
    for m in sorted(models, key=lambda x: x.label):
        images = " (supports images)" if m.supports_images else ""
        lines.append(f'  - "{m.label}"{images}')
    lines.append(
        "\nPass the exact label text (or a subset of its words/version, e.g. "
        '"gemini 3.7 flash high") as the `model` argument to dispatch_gemini_agent.'
    )
    return "\n".join(lines)


_EFFORT_RANK = {"high": 3, "thinking": 3, "medium": 2, "low": 1}


def _model_preference(model: "ModelInfo") -> tuple:
    """Rank a model for tie-breaking: higher effort first, then newer version."""
    tokens = _tokenize(model.label)
    effort = max((_EFFORT_RANK.get(t, 0) for t in tokens), default=0)
    version = 0.0
    for token in tokens:
        try:
            version = max(version, float(token))
        except ValueError:
            continue
    return (effort, version)


def resolve_model_id(requested: str, available_models: list[ModelInfo]) -> str:
    """
    Match a requested model name/alias to a valid live model ID.

    Matching is token-based: a version number (e.g. "3.7"), family
    ("flash"/"pro"/"opus"/"sonnet"/"gpt-oss"), and thinking-effort tier
    ("high"/"medium"/"low"/"thinking") are all extracted from the request and
    must each match a live model's label — so "gemini-3.5-flash" can never
    silently resolve to a 3.7 model just because both are "flash". If no
    model satisfies every token the caller supplied, this raises rather than
    guessing, and the error lists the real available labels.
    """
    if not available_models:
        raise ValueError("No models currently available from Antigravity language server.")

    req = requested.strip()

    # 1. Exact match on model_id
    for m in available_models:
        if m.model_id == req:
            return m.model_id

    # 2. Exact match on label (case-insensitive)
    for m in available_models:
        if m.label.lower() == req.lower():
            return m.model_id

    # 3. Token-based match: every token in the request (version number,
    # family word, effort word) must appear in the label's own tokens.
    req_tokens = _tokenize(req)
    if req_tokens:
        candidates = []
        for m in available_models:
            label_tokens = _tokenize(m.label)
            if req_tokens.issubset(label_tokens):
                candidates.append(m)

        if len(candidates) == 1:
            return candidates[0].model_id

        if len(candidates) > 1:
            # Ambiguous — the request named a family but no version or effort
            # tier (e.g. just "flash"). Rather than picking arbitrarily, prefer
            # the highest thinking-effort tier, then the newest version, so a
            # bare "flash" means "the best flash you have" and keeps meaning
            # that as new versions appear in the catalog.
            candidates.sort(key=_model_preference, reverse=True)
            return candidates[0].model_id

    # If nothing matched, construct helpful error listing all valid models
    available_desc = "\n".join(f"  - '{m.label}' (id: {m.model_id})" for m in available_models)
    raise ValueError(
        f"Unknown model '{requested}'. It doesn't match any live model's name, version, "
        f"family, and effort tier together. Call list_available_models for the current "
        f"catalog. Available Antigravity models:\n{available_desc}"
    )


def collect_file_contexts(file_paths: list[str], max_total_chars: int = 500_000) -> str:
    """Read files safely and construct context blocks."""
    blocks = []
    total_chars = 0
    for path in file_paths:
        if not os.path.exists(path):
            blocks.append(f"\n--- File: {path} (NOT FOUND) ---\n")
            continue
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                content = f.read()
                if total_chars + len(content) > max_total_chars:
                    content = (
                        content[: max_total_chars - total_chars]
                        + "\n... [TRUNCATED DUE TO SIZE LIMIT]"
                    )
                blocks.append(f"\n--- File: {path} ---\n{content}\n")
                total_chars += len(content)
                if total_chars >= max_total_chars:
                    break
        except Exception as e:
            blocks.append(f"\n--- File: {path} (ERROR: {e}) ---\n")
    return "\n".join(blocks)


# Step statuses that mean the engine is still working on that step. A
# trajectory containing any of these is not finished, whatever its last step
# looks like.
_NON_TERMINAL_STEP_STATUSES = frozenset(
    {
        "CORTEX_STEP_STATUS_GENERATING",
        "CORTEX_STEP_STATUS_PENDING",
        "CORTEX_STEP_STATUS_RUNNING",
        "CORTEX_STEP_STATUS_IN_PROGRESS",
    }
)

# Step types a finished trajectory may end on.
#
# DO NOT narrow this back to PLANNER_RESPONSE alone. That was a second, worse
# version of the completion bug: when an agent answers directly without calling
# any tools, the engine appends a CHECKPOINT after the final planner response,
# so the trajectory ends:
#
#   [0] USER_INPUT           DONE
#   [1] CONVERSATION_HISTORY DONE
#   [2] PLANNER_RESPONSE     DONE   <- the complete answer, ~15k chars
#   [3] CHECKPOINT           DONE   <- last step
#
# Requiring a PLANNER_RESPONSE tail never matched that shape, so the bridge
# polled an already-finished trajectory until it hit the job timeout and threw
# a correct answer away. Runs that happened to end on a planner response (many
# tool calls, planner narration last) worked, which is why this looked like
# "big jobs succeed, small jobs hang" — exactly backwards from the real cause.
_TERMINAL_TAIL_TYPES = frozenset(
    {
        "CORTEX_STEP_TYPE_PLANNER_RESPONSE",
        "CORTEX_STEP_TYPE_CHECKPOINT",
    }
)

# How long the step list must stop changing before a run counts as finished.
# Steps arrive in bursts roughly 2s apart, and between bursts the trajectory
# briefly looks complete — a settle window is what stops us reporting success
# in the middle of a run. Raise it if agents get cut off early; lower it only
# if you have measured the real inter-step gap and it is smaller.
_SETTLE_SECONDS = 4.0

# CHECKPOINT also appears mid-run, between a tool call and the planner response
# that follows it, so a CHECKPOINT tail is weaker evidence of completion than a
# planner response is. It gets a longer settle window rather than being excluded
# outright — the cost of waiting a few extra seconds is nothing next to the cost
# of discarding a finished answer.
_CHECKPOINT_SETTLE_SECONDS = 12.0


class AntigravityLocalExecutor:
    """
    Executes agent jobs against the running local Antigravity Language Server.
    Uses StartCascade -> SendUserCascadeMessage -> poll GetCascadeTrajectory.
    """

    # 1800s to match executor.DEFAULT_JOB_TIMEOUT. Callers normally pass an
    # explicit timeout; this default only applies when one is constructed
    # directly (tests, probes), and 300s was short enough to abandon real work.
    def __init__(self, endpoint: Optional[LanguageServerEndpoint] = None, timeout: float = 1800.0):
        self.endpoint = endpoint
        self.timeout = timeout

    def run_job_sync(
        self,
        job: Job,
        context_files: Optional[list[str]] = None,
        system_instruction: Optional[str] = None,
    ) -> None:
        job.started_at = time.time()
        job.status = "running"

        # 1. Discover or validate endpoint
        endpoint = self.endpoint or discover_language_server()
        if not endpoint:
            job.error = "Antigravity language server is not running or unreachable."
            job.status = "failed"
            job.finished_at = time.time()
            job.log("Failed: No active Antigravity IDE language server discovered.")
            return

        job.log(
            f"Connected to Antigravity Language Server on port {endpoint.port} (PID: {endpoint.pid})."
        )

        # 2. Fetch and resolve model
        try:
            available = list_models(endpoint)
            model_id = resolve_model_id(job.model, available)
            resolved_label = next((m.label for m in available if m.model_id == model_id), model_id)
            job.log(f"Resolved model '{job.model}' -> '{resolved_label}' ({model_id})")
        except Exception as e:
            job.error = str(e)
            job.status = "failed"
            job.finished_at = time.time()
            job.log(f"Model resolution failed: {e}")
            return

        # 3. Gather context files
        prompt_parts = []
        if system_instruction:
            prompt_parts.append(f"System Instructions:\n{system_instruction}\n")

        if context_files:
            job.log(f"Reading {len(context_files)} context files...")
            ctx_text = collect_file_contexts(context_files)
            if ctx_text:
                prompt_parts.append(f"Context Files:\n{ctx_text}\n")
            job.log(f"Context gathered ({len(ctx_text)} characters).")

        prompt_parts.append(f"Task:\n{job.task}")
        full_prompt = "\n\n".join(prompt_parts)

        if job.is_cancelled():
            return

        cascade_id = str(uuid.uuid4())

        # 4. StartCascade
        try:
            start_payload = {
                "cascadeId": cascade_id,
                "source": "CORTEX_TRAJECTORY_SOURCE_CASCADE_CLIENT",
                "trajectoryType": "CORTEX_TRAJECTORY_TYPE_CASCADE",
                "requestedModel": model_id,
            }
            job.log(f"Starting Cascade session `{cascade_id}`...")
            _rpc_call(endpoint, "StartCascade", start_payload, timeout=10.0)
        except Exception as e:
            job.error = f"StartCascade RPC failed: {e}"
            job.status = "failed"
            job.finished_at = time.time()
            job.log(f"Failed in StartCascade: {e}")
            return

        if job.is_cancelled():
            return

        # 5. SendUserCascadeMessage
        try:
            msg_payload = {
                "cascadeId": cascade_id,
                "items": [{"text": full_prompt}],
                "cascadeConfig": {"plannerConfig": {"requestedModel": {"model": model_id}}},
            }
            job.log("Submitting prompt to local agent...")
            _rpc_call(endpoint, "SendUserCascadeMessage", msg_payload, timeout=10.0)
        except Exception as e:
            job.error = f"SendUserCascadeMessage RPC failed: {e}"
            job.status = "failed"
            job.finished_at = time.time()
            job.log(f"Failed in SendUserCascadeMessage: {e}")
            return

        # 6. Poll GetCascadeTrajectory with exponential backoff
        poll_interval = 0.5
        max_poll_interval = 2.0
        start_poll_time = time.time()

        # Completion-detection state — see the long comment in the loop below.
        last_signature = None
        last_change_at = time.time()
        warned_error_steps = set()

        job.log("Awaiting agent response from local engine...")

        while True:
            if job.is_cancelled():
                return

            if time.time() - start_poll_time > self.timeout:
                job.error = f"Antigravity agent execution timed out after {self.timeout}s"
                job.status = "failed"
                job.finished_at = time.time()
                job.log("Timed out waiting for response.")
                return

            time.sleep(poll_interval)
            poll_interval = min(poll_interval * 1.3, max_poll_interval)

            try:
                traj_resp = _rpc_call(
                    endpoint, "GetCascadeTrajectory", {"cascadeId": cascade_id}, timeout=10.0
                )
            except urllib.error.URLError as e:
                # Connection refused or socket error indicates IDE was closed
                job.error = f"Antigravity IDE session ended (connection refused: {e})"
                job.status = "failed"
                job.finished_at = time.time()
                job.log(f"Connection lost to language server: {e}")
                return
            except Exception as e:
                job.log(f"Trajectory poll warning: {e}")
                continue

            steps = traj_resp.get("trajectory", {}).get("steps", [])

            # A hard engine error ends the run immediately, wherever it appears.
            finished = False
            for step in steps:
                if step.get("type") == "CORTEX_STEP_TYPE_ERROR_MESSAGE":
                    err_msg = step.get("errorMessage", {})
                    short_err = (
                        err_msg.get("userVisibleError")
                        or err_msg.get("shortError")
                        or "Agent execution failed."
                    )
                    full_err = err_msg.get("fullError", "")

                    if full_err:
                        job.log(f"Internal engine stack trace:\n{full_err}")

                    job.error = short_err
                    job.status = "failed"
                    job.finished_at = time.time()
                    job.log(f"Agent execution error: {short_err}")
                    finished = True
                    break

            if finished:
                break

            # Surface failing tool steps as warnings. They do not end the run —
            # the agent usually recovers by trying another tool — but silently
            # swallowing them made real failures impossible to diagnose.
            for n, step in enumerate(steps):
                if step.get("status") == "CORTEX_STEP_STATUS_ERROR" and n not in warned_error_steps:
                    warned_error_steps.add(n)
                    job.log(
                        f"Tool step {n} ({step.get('type')}) reported an error; agent may retry."
                    )

            # Completion detection.
            #
            # DO NOT go back to "first PLANNER_RESPONSE with status DONE". That
            # was the original bug: a model emits an EMPTY planner response
            # immediately before its first tool call, so the old loop reported
            # success at ~2s with an empty result while the agent kept working
            # for seconds or minutes. Observed trajectory for a one-file read:
            #
            #   step2 PLANNER_RESPONSE/DONE ''        <- old code stopped here
            #   step3 VIEW_FILE/ERROR
            #   step5 PLANNER_RESPONSE/DONE ''
            #   step6 RUN_COMMAND/DONE
            #   step7 PLANNER_RESPONSE/DONE 'the real answer'
            #
            # There is no trajectory-level "finished" flag to consult (the only
            # keys are trajectoryId / cascadeId / trajectoryType / source /
            # metadata), so the run is treated as over only when all three hold:
            #   1. no step is still in a non-terminal status,
            #   2. the LAST step is a completed planner response, and
            #   3. the step list has stopped changing for _SETTLE_SECONDS.
            # (3) matters because steps arrive in bursts a second or two apart,
            # so (1) and (2) both hold briefly in the middle of a run.
            signature = tuple((s.get("type"), s.get("status")) for s in steps)
            if signature != last_signature:
                last_signature = signature
                last_change_at = time.time()

            busy = any(s.get("status") in _NON_TERMINAL_STEP_STATUSES for s in steps)
            last_step = steps[-1] if steps else None
            last_type = last_step.get("type") if last_step else None
            # Something must have been said, or there is nothing to return and
            # the run cannot be over however settled the step list looks.
            has_response = any(s.get("type") == "CORTEX_STEP_TYPE_PLANNER_RESPONSE" for s in steps)
            ended = (
                not busy
                and has_response
                and last_type in _TERMINAL_TAIL_TYPES
                and last_step.get("status") == "CORTEX_STEP_STATUS_DONE"
            )

            settle_needed = (
                _CHECKPOINT_SETTLE_SECONDS
                if last_type == "CORTEX_STEP_TYPE_CHECKPOINT"
                else _SETTLE_SECONDS
            )
            if not (ended and time.time() - last_change_at >= settle_needed):
                continue

            # Join every non-empty planner response in order. Taking only the
            # last one would drop the narration an agent emits between tool
            # calls, which is often where it explains what it did.
            chunks = []
            total_in = 0
            total_out = 0
            for step in steps:
                if step.get("type") != "CORTEX_STEP_TYPE_PLANNER_RESPONSE":
                    continue
                planner = step.get("plannerResponse", {})
                text = planner.get("modifiedResponse") or planner.get("response") or ""
                if text.strip():
                    chunks.append(text.strip())

                usage = step.get("metadata", {}).get("modelUsage", {})
                if usage:
                    job.metadata["model_usage"] = usage
                    try:
                        total_in += int(usage.get("inputTokens", 0) or 0)
                        total_out += int(usage.get("outputTokens", 0) or 0)
                    except (TypeError, ValueError):
                        pass

            job.result = "\n\n".join(chunks)
            job.status = "completed"
            job.finished_at = time.time()
            if total_in or total_out:
                job.log(f"Model usage: inTokens={total_in}, outTokens={total_out}")
            job.log(
                f"Agent response completed ({len(steps)} steps, "
                f"{len(chunks)} response chunk(s), {len(job.result)} chars)."
            )
            if not job.result:
                job.log(
                    "WARNING: agent produced no prose. It may still have "
                    "completed its work via tool calls — check the workspace."
                )
            break
