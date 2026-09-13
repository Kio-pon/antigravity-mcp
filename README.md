# antigravity-mcp

**Delegate background subagent jobs to the Antigravity IDE's local agent engine — from Claude Code, Codex, Cursor, or any other MCP client.**

[![CI](https://github.com/Kio-pon/antigravity-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/Kio-pon/antigravity-mcp/actions/workflows/ci.yml)
[![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

If you have the Antigravity IDE open and signed in, you already have a local agent engine with a catalog of frontier models sitting idle. This is an [MCP](https://modelcontextprotocol.io) server that exposes that engine as a handful of tools, so the model you actually drive your work with can hand jobs off to it and keep going.

Your orchestrator stays the architect. The subagents do the legwork.

```
  ┌─────────────────────────────────────────────────┐
  │  Your MCP client                                │
  │  (Claude Code · Codex · Cursor · anything else) │
  │  plans the work, reviews every result           │
  └───────────────────────┬─────────────────────────┘
                          │  MCP · JSON-RPC 2.0 over stdio
                          ▼
  ┌─────────────────────────────────────────────────┐
  │  antigravity-mcp                                │
  │  returns a job_id immediately, never blocks     │
  │  bounded worker pool · job history on disk      │
  └───────────────────────┬─────────────────────────┘
                          │  tried in order
        ┌─────────────────┼─────────────────┐
        ▼                 ▼                 ▼
  ┌───────────────┐ ┌───────────────┐ ┌───────────────┐
  │ Antigravity   │ │ Gemini REST   │ │ Explicit      │
  │ language      │ │ API           │ │ failure       │
  │ server        │ │               │ │               │
  │ no API key    │ │ needs a key   │ │ never faked   │
  └───────────────┘ └───────────────┘ └───────────────┘
```

## Why

Orchestrating models are expensive and serial. Plenty of real work isn't worth their attention: scanning forty files to answer one architectural question, generating the fifth round of near-identical fixtures, or reading a dependency tree you already understand. Hand those to a fast local worker, keep planning while it runs, and collect the result when you need it.

Because jobs run on the IDE's own authenticated session, **no API key is needed** when the IDE is open.

## Install

```bash
pip install git+https://github.com/Kio-pon/antigravity-mcp.git
```

Or clone it — there are no dependencies, so a checkout runs as-is on a bare interpreter:

```bash
git clone https://github.com/Kio-pon/antigravity-mcp.git
```

## Register with your MCP client

<details open>
<summary><b>Claude Code</b></summary>

```bash
claude mcp add antigravity-agents -- antigravity-mcp
```

From a clone instead of an install:

```bash
claude mcp add antigravity-agents -- python3 /path/to/antigravity-mcp/src/antigravity_mcp/__main__.py
```
</details>

<details>
<summary><b>Codex</b></summary>

Add to `~/.codex/config.toml`:

```toml
[mcp_servers.antigravity-agents]
command = "antigravity-mcp"
```
</details>

<details>
<summary><b>Cursor, Windsurf, and other <code>mcp.json</code> clients</b></summary>

```json
{
  "mcpServers": {
    "antigravity-agents": {
      "command": "antigravity-mcp",
      "env": {
        "ANTIGRAVITY_MAX_CONCURRENT_JOBS": "3"
      }
    }
  }
}
```
</details>

Ready-made config files live in [`examples/`](examples/).

Verify it is wired up by asking your client to call `list_available_models`. With the IDE open you should get back the live model catalog.

## Tools

| Tool | Arguments | What it does |
| :--- | :--- | :--- |
| `dispatch_gemini_agent` | `task`, `context_files?`, `model?`, `system_instruction?` | Starts a background job. Returns a `job_id` immediately. |
| `check_agent_job` | `job_id` | Status, elapsed time, progress log, and output. |
| `list_active_jobs` | `limit?` | Recent and in-flight jobs. |
| `cancel_agent_job` | `job_id` | Cooperatively stops a running job. |
| `gemini_code_search` | `query`, `context_files` | Scans many files at once and answers synchronously. |
| `list_available_models` | — | The live model catalog from your IDE session. |

### Picking a model

The catalog is whatever your Antigravity account currently has, and it changes as models ship — so nothing is hardcoded. Call `list_available_models` to see it, then pass either an exact label or a loose description:

```
"Gemini 3.8 Flash (High)"   exact label
"gemini 3.7 flash medium"   version + family + effort tier
"flash"                     best available flash — highest effort, newest version
"opus"                      best available Opus
```

Matching is token-based and strict about what you *do* specify: every word and version number you give has to appear in the model's label. `"gemini-3.5-flash"` will not quietly resolve to a 3.7 model just because both are flash — it raises and lists what actually exists. Leave a dimension out and it is a wildcard, resolved to the highest effort tier and newest version that fits.

## How a session usually goes

```
1. dispatch_gemini_agent  → job_id, returned instantly
2. …carry on with your own work…
3. check_agent_job        → output when it's ready
4. review before applying anything to files
```

Step 4 is the point of the whole design. A subagent's output is a proposal, not a commit.

## Execution backends

Two backends, tried in order:

1. **Local Antigravity language server** — the default. Connects over loopback HTTPS and reuses the IDE's signed-in session. No API key.
2. **Gemini REST API** — used when the IDE is not running and `GEMINI_API_KEY` or `GOOGLE_API_KEY` is set.

If neither is reachable the job fails with an error saying so. There is
deliberately no further tier that returns a stand-in result: a plausible answer
no model actually produced is worse than an error, because nothing downstream
can tell the two apart.

## Configuration

All optional.

| Variable | Default | Purpose |
| :--- | :--- | :--- |
| `ANTIGRAVITY_MAX_CONCURRENT_JOBS` | `3` | Jobs running at once. Every job drives a live session on the *same* language server process, so this is a real resource ceiling, not a formality. Extra jobs queue. |
| `ANTIGRAVITY_JOB_TIMEOUT` | `1800` | Seconds before a job is abandoned. Completion is detected, not waited out — a short single-shot answer returns in seconds — so a generous ceiling only bounds a genuinely stuck agent. |
| `ANTIGRAVITY_MAX_CONCURRENT_REQUESTS` | `16` | Tool calls handled in parallel. |
| `ANTIGRAVITY_STATE_DIR` | platform state dir | Where job history is kept. |
| `ANTIGRAVITY_DISABLE_LOCAL` | unset | Skip the local backend entirely. |
| `ANTIGRAVITY_LOG_LEVEL` | `INFO` | Server log level on stderr. |
| `GEMINI_API_KEY` / `GOOGLE_API_KEY` | unset | Enables the REST fallback. |

Job history is written to `$XDG_STATE_HOME/antigravity-mcp/jobs.json` on Linux, `~/Library/Application Support/antigravity-mcp/` on macOS, and `%LOCALAPPDATA%\antigravity-mcp\` on Windows — never into the install tree. It survives across sessions, which is what makes a `job_id` from yesterday still mean something today.

## The optional skill

[`skills/antigravity-agent/`](skills/antigravity-agent/) is a Claude Code skill that teaches the orchestrator *when* delegating is worth it, so you don't have to say it every time. Install it with:

```bash
cp -r skills/antigravity-agent ~/.claude/skills/
```

## Protocol support

The server implements the current **2026-07-28** specification and the
handshake-based revisions before it, deciding per request which one a client is
speaking. Modern clients declare their version in each request's `_meta`; legacy
clients open with `initialize`. Both work against the same process with no
configuration, and `server/discover` reports every version on offer.

Because stdio has no HTTP status codes to drive fallback, `server/discover` is
also how a client tells a modern server from a legacy one — so it is answered
before anything else.

## Compatibility

> [!IMPORTANT]
> The local backend speaks private IDE IPC — `StartCascade`, `SendUserCascadeMessage`, `GetUserStatus`, `GetCascadeTrajectory` over loopback Connect-RPC. These are internal endpoints with **no cross-version stability guarantee**. An IDE update can change them.

| | Status |
| :--- | :--- |
| MCP specification | **2026-07-28** (current), plus 2025-06-18, 2025-03-26, 2024-11-05 |
| Antigravity IDE | Verified against **v1.107.0** (`linux_x64`). Antigravity 2.0, the separate agent-first app, is **untested** |
| Linux | Verified — process discovery via `/proc` and `ss` |
| macOS | Implemented via `lsof` and `ps -axww`, **not yet verified on real hardware** |
| Windows | Implemented via `netstat` and WMI, **not yet verified on real hardware** |
| Python | 3.9+ |

Running it on macOS or Windows? A quick issue saying whether discovery found your language server would be genuinely useful — that's the one part that cannot be tested without the hardware.

## Development

```bash
git clone https://github.com/Kio-pon/antigravity-mcp.git
cd antigravity-mcp
pip install -e ".[dev]"
pytest
```

The default suite is hermetic — no IDE, no network, no API key — driving the real request path with a stub executor in place of a language server. To also run the live round-trip against a real one, open the IDE and set `ANTIGRAVITY_LIVE_TESTS=1`.

See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

[MIT](LICENSE).

Not affiliated with, endorsed by, or supported by Google or the Antigravity IDE team.
