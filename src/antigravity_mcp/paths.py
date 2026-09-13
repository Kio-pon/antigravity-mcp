"""Where the bridge keeps mutable state.

Job history has to outlive the MCP subprocess: every client session spawns a
fresh process with no shared memory, so without a file on disk a job dispatched
in one session would be unreachable from the next. That file must not live
inside the package directory — an installed package may sit in a read-only
site-packages, and writing user state into an install tree breaks reinstalls
and `pip uninstall`. These helpers resolve a per-user state directory following
each platform's convention, honouring XDG_STATE_HOME where it is set.
"""

import contextlib
import os
import sys
from pathlib import Path

APP_NAME = "antigravity-mcp"


def state_dir() -> Path:
    """Return the per-user directory for this app's mutable state.

    Override with ANTIGRAVITY_STATE_DIR to place state somewhere explicit —
    useful for tests, for sandboxes, and for running several isolated bridges
    side by side.
    """
    override = os.environ.get("ANTIGRAVITY_STATE_DIR")
    if override:
        return Path(override).expanduser()

    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~\\AppData\\Local")
        return Path(base) / APP_NAME

    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME

    base = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    return Path(base) / APP_NAME


def state_file(name: str) -> Path:
    """Resolve a named file inside the state directory, creating the directory.

    Directory creation is best-effort: a read-only or otherwise unwritable home
    should degrade to in-memory-only job history, never crash the server at
    import time.
    """
    directory = state_dir()
    with contextlib.suppress(OSError):
        directory.mkdir(parents=True, exist_ok=True)
    return directory / name
