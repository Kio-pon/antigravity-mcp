"""Local language server executor: discovery, model resolution, trajectory handling.

All tests but the last are offline and mocked. The final class is a live
round-trip against a running Antigravity IDE, and skips itself when none is
reachable — so the suite is green in CI and still proves the real thing works
on a developer machine with the IDE open.
"""

import json
import os
import time
import unittest
from unittest.mock import patch

from antigravity_mcp.antigravity_local import (
    AntigravityLocalExecutor,
    LanguageServerEndpoint,
    ModelInfo,
    discover_language_server,
    list_models,
    resolve_model_id,
)
from antigravity_mcp.executor import AgentExecutor
from antigravity_mcp.job_manager import Job


class TestAntigravityLocalUnit(unittest.TestCase):
    """Unit tests using mocked processes and network calls."""

    @patch("antigravity_mcp.antigravity_local.find_language_server_candidates")
    @patch("antigravity_mcp.antigravity_local._rpc_call")
    def test_discover_language_server(self, mock_rpc, mock_candidates):
        """An unhealthy candidate is skipped and the first one answering Heartbeat wins."""
        mock_candidates.return_value = [(100, 11111, "token-1"), (200, 22222, "token-2")]

        # Candidate 1 fails heartbeat, candidate 2 succeeds.
        def rpc_side_effect(endpoint, method, payload, timeout=2.0):
            if endpoint.port == 11111:
                raise Exception("Connection refused")
            return {"lastExtensionHeartbeat": "2026-08-16T10:00:00Z"}

        mock_rpc.side_effect = rpc_side_effect

        endpoint = discover_language_server(use_cache=False)
        self.assertIsNotNone(endpoint)
        self.assertEqual(endpoint.port, 22222)
        self.assertEqual(endpoint.csrf_token, "token-2")

    @patch("antigravity_mcp.antigravity_local._rpc_call")
    def test_list_models(self, mock_rpc):
        mock_rpc.return_value = {
            "userStatus": {
                "cascadeModelConfigData": {
                    "clientModelConfigs": [
                        {
                            "label": "Gemini 3.7 Flash (High)",
                            "modelOrAlias": {"model": "MODEL_PLACEHOLDER_M298"},
                            "supportsImages": True,
                        },
                        {
                            "label": "Claude Opus 4.6 (Thinking)",
                            "modelOrAlias": {"model": "MODEL_PLACEHOLDER_M26"},
                            "supportsImages": False,
                        },
                    ]
                }
            }
        }
        endpoint = LanguageServerEndpoint(host="127.0.0.1", port=39573, csrf_token="test-token")
        models = list_models(endpoint)
        self.assertEqual(len(models), 2)
        self.assertEqual(models[0].label, "Gemini 3.7 Flash (High)")
        self.assertEqual(models[0].model_id, "MODEL_PLACEHOLDER_M298")
        self.assertTrue(models[0].supports_images)
        self.assertEqual(models[1].label, "Claude Opus 4.6 (Thinking)")
        self.assertEqual(models[1].model_id, "MODEL_PLACEHOLDER_M26")

    def test_resolve_model_id(self):
        models = [
            ModelInfo(label="Gemini 3.7 Flash (High)", model_id="MODEL_PLACEHOLDER_M298"),
            ModelInfo(label="Gemini 3.1 Pro (High)", model_id="MODEL_PLACEHOLDER_M16"),
            ModelInfo(label="Claude Opus 4.6 (Thinking)", model_id="MODEL_PLACEHOLDER_M26"),
            ModelInfo(label="Claude Sonnet 4.6 (Thinking)", model_id="MODEL_PLACEHOLDER_M35"),
        ]

        # Alias resolution
        self.assertEqual(resolve_model_id("gemini-flash", models), "MODEL_PLACEHOLDER_M298")
        self.assertEqual(resolve_model_id("gemini-pro", models), "MODEL_PLACEHOLDER_M16")
        self.assertEqual(resolve_model_id("claude-opus", models), "MODEL_PLACEHOLDER_M26")
        self.assertEqual(resolve_model_id("claude-sonnet", models), "MODEL_PLACEHOLDER_M35")

        # Exact model ID
        self.assertEqual(
            resolve_model_id("MODEL_PLACEHOLDER_M298", models), "MODEL_PLACEHOLDER_M298"
        )

        # Exact label
        self.assertEqual(
            resolve_model_id("Gemini 3.7 Flash (High)", models), "MODEL_PLACEHOLDER_M298"
        )

        # Unknown model raises ValueError with list of available models
        with self.assertRaises(ValueError) as ctx:
            resolve_model_id("non-existent-model-xyz", models)
        self.assertIn("Available Antigravity models", str(ctx.exception))
        self.assertIn("Gemini 3.7 Flash (High)", str(ctx.exception))

    @patch("antigravity_mcp.antigravity_local._rpc_call")
    def test_executor_error_step_handling(self, mock_rpc):
        endpoint = LanguageServerEndpoint(host="127.0.0.1", port=39573, csrf_token="test-token")

        # Setup mock responses for GetUserStatus, StartCascade, SendUserCascadeMessage, GetCascadeTrajectory
        # A fixed response table: this executor only ever makes these four
        # calls, and the test pins the error path they lead to.
        responses = {
            "GetUserStatus": {
                "userStatus": {
                    "cascadeModelConfigData": {
                        "clientModelConfigs": [
                            {
                                "label": "Gemini 3.7 Flash (High)",
                                "modelOrAlias": {"model": "MODEL_PLACEHOLDER_M298"},
                            }
                        ]
                    }
                }
            },
            "StartCascade": {"cascadeId": "test-cascade-id"},
            "SendUserCascadeMessage": {},
            "GetCascadeTrajectory": {
                "trajectory": {
                    "steps": [
                        {
                            "type": "CORTEX_STEP_TYPE_USER_INPUT",
                            "status": "CORTEX_STEP_STATUS_DONE",
                        },
                        {
                            "type": "CORTEX_STEP_TYPE_ERROR_MESSAGE",
                            "status": "CORTEX_STEP_STATUS_DONE",
                            "errorMessage": {
                                "shortError": "Quota exceeded for model",
                                "userVisibleError": "Quota limit reached",
                                "fullError": "stack trace line 1\nstack trace line 2",
                            },
                        },
                    ]
                }
            },
        }

        def rpc_handler(ep, method, payload, timeout=10.0):
            return responses.get(method, {})

        mock_rpc.side_effect = rpc_handler

        executor = AntigravityLocalExecutor(endpoint=endpoint, timeout=5.0)
        job = Job(id="agy-test1", task="test task", model="gemini-flash", status="pending")

        executor.run_job_sync(job)

        self.assertEqual(job.status, "failed")
        self.assertEqual(job.error, "Quota limit reached")
        # Ensure full stack trace is logged in progress logs but not user-facing job.error
        self.assertTrue(any("stack trace line 1" in log for log in job.progress_logs))


class TestExecutorDispatchOrder(unittest.TestCase):
    """Tests priority dispatch and loud failure when no backend exists."""

    def test_no_backend_fails_loudly(self):
        # Ensure no mock mode, no api key, and no local server discoverable
        with patch.dict(
            os.environ, {"ANTIGRAVITY_MOCK": "0", "ANTIGRAVITY_DISABLE_LOCAL": "1"}, clear=True
        ):
            executor = AgentExecutor(api_key=None)
            job = Job(
                id="agy-fail", task="task requiring backend", model="gemini-flash", status="pending"
            )
            executor.run_job_sync(job)

            self.assertEqual(job.status, "failed")
            self.assertIn("No execution backend available", job.error)
            self.assertIsNone(job.result)

    def test_mock_backend_when_explicitly_enabled(self):
        with patch.dict(
            os.environ, {"ANTIGRAVITY_MOCK": "1", "ANTIGRAVITY_DISABLE_LOCAL": "1"}, clear=True
        ):
            executor = AgentExecutor(api_key=None)
            job = Job(id="agy-mock", task="mock task", model="gemini-flash", status="pending")
            executor.run_job_sync(job)

            self.assertEqual(job.status, "completed")
            self.assertIn("Mock subagent response", job.result)


@unittest.skipUnless(
    os.environ.get("ANTIGRAVITY_LIVE_TESTS", "").lower() in ("1", "true"),
    "Live tests are opt-in: set ANTIGRAVITY_LIVE_TESTS=1 with the Antigravity IDE open.",
)
class TestAntigravityLocalIntegration(unittest.TestCase):
    """Live round-trip against a real running Antigravity language server.

    Opt-in rather than automatic. It drives a real model, so it costs real
    quota and real time, and it contends with any other job already running on
    the same language server — neither of which belongs in a default test run.
    Requires no API key: the IDE's own session does the authenticating.
    """

    @classmethod
    def setUpClass(cls):
        cls.endpoint = discover_language_server()

    def test_live_ping_pong_roundtrip(self):
        """End-to-end: dispatch a trivial prompt and get real model output back."""
        if not self.endpoint:
            self.skipTest("No local Antigravity language server currently running.")

        # Explicitly ensure no Gemini API key is present
        with patch.dict(os.environ, {"ANTIGRAVITY_MOCK": "0"}, clear=False):
            if "GEMINI_API_KEY" in os.environ:
                del os.environ["GEMINI_API_KEY"]
            if "GOOGLE_API_KEY" in os.environ:
                del os.environ["GOOGLE_API_KEY"]

            executor = AntigravityLocalExecutor(endpoint=self.endpoint, timeout=180.0)
            job = Job(
                id=f"agy-live-{int(time.time())}",
                task="reply with the single word PONG, do not use any tools.",
                model="gemini-flash",
                status="pending",
            )

            executor.run_job_sync(job)

            self.assertEqual(job.status, "completed", f"Job failed with error: {job.error}")
            self.assertIsNotNone(job.result)
            self.assertIn("PONG", job.result.strip().upper())

            # Verify model usage metadata was captured
            model_usage = job.metadata.get("model_usage", {})
            self.assertTrue(len(model_usage) > 0, "model_usage metadata must be captured")
            print("\n=== Live Integration Test Passed ===")
            print(f"Job Result: {job.result.strip()}")
            print(f"Model Usage: {json.dumps(model_usage, indent=2)}")


if __name__ == "__main__":
    unittest.main(verbosity=2)


def _planner(text, status="CORTEX_STEP_STATUS_DONE"):
    return {
        "type": "CORTEX_STEP_TYPE_PLANNER_RESPONSE",
        "status": status,
        "plannerResponse": {"response": text},
    }


def _step(kind, status="CORTEX_STEP_STATUS_DONE"):
    return {"type": f"CORTEX_STEP_TYPE_{kind}", "status": status}


class TestCompletionDetection(unittest.TestCase):
    """How a finished run is recognised.

    The shapes here are taken from real trajectories captured off a live
    language server, not invented — the CHECKPOINT-tailed one below is the exact
    shape that used to hang until the job timeout.
    """

    def run_with_steps(self, steps, timeout=30.0):
        endpoint = LanguageServerEndpoint(host="127.0.0.1", port=1, csrf_token="t", pid=1)
        responses = {
            "GetUserStatus": {
                "userStatus": {
                    "cascadeModelConfigData": {
                        "clientModelConfigs": [
                            {
                                "label": "Gemini 3.8 Flash (High)",
                                "modelOrAlias": {"model": "M318"},
                            }
                        ]
                    }
                }
            },
            "StartCascade": {"cascadeId": "c1"},
            "SendUserCascadeMessage": {},
            "GetCascadeTrajectory": {"trajectory": {"steps": steps}},
        }

        def rpc(ep, method, payload, timeout=10.0):
            return responses.get(method, {})

        job = Job(id="agy-x", task="t", model="flash", status="pending")
        with patch("antigravity_mcp.antigravity_local._rpc_call", side_effect=rpc):
            AntigravityLocalExecutor(endpoint=endpoint, timeout=timeout).run_job_sync(job)
        return job

    def test_direct_answer_ending_in_checkpoint_completes(self):
        """Regression: an agent that answers without calling tools ends its trajectory
        with a CHECKPOINT after the planner response. Requiring a PLANNER_RESPONSE tail
        never matched, so the bridge polled a finished run until the timeout and threw
        a complete answer away."""
        job = self.run_with_steps(
            [
                _step("USER_INPUT"),
                _step("CONVERSATION_HISTORY"),
                _planner("the complete answer"),
                _step("CHECKPOINT"),
            ]
        )
        self.assertEqual(job.status, "completed", job.error)
        self.assertEqual(job.result, "the complete answer")

    def test_tool_using_run_ending_in_planner_response_completes(self):
        """The shape that always worked must keep working."""
        job = self.run_with_steps(
            [
                _step("USER_INPUT"),
                _planner(""),
                _step("RUN_COMMAND"),
                _step("CHECKPOINT"),
                _planner("final narration"),
            ]
        )
        self.assertEqual(job.status, "completed", job.error)
        self.assertEqual(job.result, "final narration")

    def test_checkpoint_without_any_planner_response_does_not_complete(self):
        """A CHECKPOINT alone is not an answer; with nothing said there is nothing to
        return, so the run must not be declared finished."""
        job = self.run_with_steps([_step("USER_INPUT"), _step("CHECKPOINT")], timeout=2.0)
        self.assertEqual(job.status, "failed")
        self.assertIn("timed out", job.error)

    def test_run_still_generating_does_not_complete(self):
        """A step still generating means the run is live, whatever the tail looks like."""
        job = self.run_with_steps(
            [
                _step("USER_INPUT"),
                _planner("partial", status="CORTEX_STEP_STATUS_GENERATING"),
                _step("CHECKPOINT"),
            ],
            timeout=2.0,
        )
        self.assertEqual(job.status, "failed")
        self.assertIn("timed out", job.error)

    def test_all_planner_chunks_are_joined(self):
        """Narration between tool calls is part of the answer, not noise to drop."""
        job = self.run_with_steps(
            [
                _step("USER_INPUT"),
                _planner("first"),
                _step("RUN_COMMAND"),
                _planner("second"),
                _step("CHECKPOINT"),
            ]
        )
        self.assertEqual(job.result, "first\n\nsecond")
