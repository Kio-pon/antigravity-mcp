"""antigravity-mcp — drive the Antigravity IDE's local agent engine from any MCP client.

Exposes the Antigravity IDE's running language server (and, as a fallback, the
Gemini REST API) as a set of MCP tools, so an orchestrating model in Claude
Code, Codex, Cursor, or any other MCP client can dispatch background subagent
jobs and poll them for results.
"""

__version__ = "1.1.0"

from .executor import AgentExecutor
from .job_manager import Job, JobManager
from .server import MCPServer

__all__ = ["Job", "JobManager", "AgentExecutor", "MCPServer", "__version__"]
