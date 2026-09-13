"""
Job manager module for asynchronous Antigravity/Gemini agent tasks.
Provides thread-safe registration, tracking, cancellation, and retrieval of jobs.
Job state is mirrored to a JSON file so history survives across MCP subprocess
restarts (each client session launches a fresh subprocess with no shared
memory, so without this every job would vanish the moment the session ended).
The file lives in the per-user state directory, never in the install tree —
see paths.py for why.
"""

import json
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from .paths import state_file

STATE_FILE_NAME = "jobs.json"


def default_state_path() -> str:
    """Resolved lazily, not at import: creating directories as a side effect of
    importing a module makes the package hostile to tooling that merely imports
    it (docs builders, linters, `--help`)."""
    return str(state_file(STATE_FILE_NAME))


@dataclass
class Job:
    id: str
    task: str
    model: str
    status: str  # 'pending', 'running', 'completed', 'failed', 'cancelled'
    created_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    progress_logs: list[str] = field(default_factory=list)
    result: Optional[str] = None
    error: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict)
    _cancel_event: threading.Event = field(default_factory=threading.Event, repr=False)
    # Invoked after every mutating call below. Not part of persisted state.
    _on_change: Optional[Callable[[], None]] = field(default=None, repr=False, compare=False)

    def to_dict(self) -> dict[str, Any]:
        duration = round(
            (self.finished_at or time.time()) - (self.started_at or self.created_at), 2
        )
        return {
            "id": self.id,
            "task": self.task,
            "model": self.model,
            "status": self.status,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_seconds": duration,
            "progress_logs": list(self.progress_logs),
            "result": self.result,
            "error": self.error,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Job":
        job = cls(
            id=data["id"],
            task=data.get("task", ""),
            model=data.get("model", "flash"),
            status=data.get("status", "failed"),
            created_at=data.get("created_at", time.time()),
            started_at=data.get("started_at"),
            finished_at=data.get("finished_at"),
            progress_logs=list(data.get("progress_logs", [])),
            result=data.get("result"),
            error=data.get("error"),
            metadata=dict(data.get("metadata", {})),
        )
        # A job that was still pending/running when the process exited had its
        # background thread and RPC connection killed with it — it can never
        # actually finish, so reopening it as still-running would hang forever.
        if job.status in ("pending", "running"):
            job.status = "failed"
            job.error = "The MCP session ended before this job finished."
            job.finished_at = job.finished_at or time.time()
            job.log("Marked failed: session ended while job was in flight.")
        return job

    def log(self, message: str) -> None:
        timestamp = time.strftime("%H:%M:%S")
        self.progress_logs.append(f"[{timestamp}] {message}")
        if self._on_change:
            self._on_change()

    def cancel(self) -> None:
        self._cancel_event.set()
        self.status = "cancelled"
        self.finished_at = time.time()
        self.log("Job was cancelled by orchestrator.")

    def is_cancelled(self) -> bool:
        return self._cancel_event.is_set()


class JobManager:
    """Thread-safe registry of agent tasks, mirrored to a JSON file on disk."""

    def __init__(self, max_history: int = 100, state_path: Optional[str] = None):
        self._jobs: dict[str, Job] = {}
        # RLock: cancel_job() holds the lock while calling job.cancel(), which
        # logs and triggers _save_to_disk() -> re-acquires the lock. A plain
        # Lock would deadlock on that reentrant path.
        self._lock = threading.RLock()
        self._max_history = max_history
        self._state_path = state_path or default_state_path()
        self._load_from_disk()

    def _load_from_disk(self) -> None:
        if not os.path.exists(self._state_path):
            return
        try:
            with open(self._state_path, encoding="utf-8") as f:
                raw = json.load(f)
            for entry in raw.get("jobs", []):
                job = Job.from_dict(entry)
                job._on_change = self._save_to_disk
                self._jobs[job.id] = job
        except Exception:
            # Corrupt or unreadable state file — start fresh rather than crash.
            pass

    def _save_to_disk(self) -> None:
        with self._lock:
            snapshot = {"jobs": [j.to_dict() for j in self._jobs.values()]}
        tmp_path = f"{self._state_path}.tmp"
        try:
            os.makedirs(os.path.dirname(self._state_path), exist_ok=True)
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(snapshot, f)
            os.replace(tmp_path, self._state_path)
        except Exception:
            # Persistence is best-effort; never let a disk error break the job.
            pass

    def create_job(
        self, task: str, model: str = "flash", metadata: Optional[dict[str, Any]] = None
    ) -> Job:
        job_id = f"agy-{uuid.uuid4().hex[:8]}"
        job = Job(id=job_id, task=task, model=model, status="pending", metadata=metadata or {})
        job._on_change = self._save_to_disk
        with self._lock:
            if len(self._jobs) >= self._max_history:
                # Evict oldest finished jobs
                finished = [
                    j
                    for j in self._jobs.values()
                    if j.status in ("completed", "failed", "cancelled")
                ]
                if finished:
                    oldest = min(finished, key=lambda j: j.created_at)
                    del self._jobs[oldest.id]
            self._jobs[job_id] = job
        self._save_to_disk()
        return job

    def get_job(self, job_id: str) -> Optional[Job]:
        with self._lock:
            return self._jobs.get(job_id)

    def list_jobs(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            sorted_jobs = sorted(self._jobs.values(), key=lambda j: j.created_at, reverse=True)
            return [j.to_dict() for j in sorted_jobs[:limit]]

    def cancel_job(self, job_id: str) -> bool:
        with self._lock:
            job = self._jobs.get(job_id)
            if job and job.status in ("pending", "running"):
                job.cancel()
                return True
            return False
