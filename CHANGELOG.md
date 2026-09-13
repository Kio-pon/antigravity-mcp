# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.1.0] — 2026-09-13

First public release.

### Tools and execution
- MCP server exposing six tools: `dispatch_gemini_agent`, `check_agent_job`,
  `list_active_jobs`, `cancel_agent_job`, `gemini_code_search`, and
  `list_available_models`.
- Two backends, tried in order: the local Antigravity language server, then the
  Gemini REST API. If neither is reachable the job fails with an explicit error.
  There is deliberately no further tier that returns a stand-in result — a
  plausible answer no model produced is worse than an error, because nothing
  downstream can tell the two apart.
- Tool calls are handled on a thread pool rather than inline on the stdio read
  loop, so a long `gemini_code_search` cannot block the `check_agent_job` polls
  used to watch other jobs.
- Job history persists to the per-user state directory, surviving across client
  sessions. A job still in flight when the process died loads back as failed,
  since it can never now finish.

### Protocol
- Implements the current **2026-07-28** specification alongside the older
  handshake-based revisions. The server is dual-era: modern clients declare
  their protocol version and capabilities in each request's `_meta` and never
  shake hands, legacy clients open with `initialize`, and the era is read off
  each request, so one process serves both with no configuration.
- `server/discover`, which the specification makes mandatory. On stdio it is
  also the client's era probe — answering it is what identifies this server as
  modern. It advertises the legacy versions too, so a client that cannot speak
  2026-07-28 can read a usable version straight out of the response.
- `resultType` and `_meta['io.modelcontextprotocol/serverInfo']` on every
  result; `ttlMs` and `cacheScope` on `tools/list` for client-side caching.
- An unsupported protocol version returns `UnsupportedProtocolVersionError`
  (`-32022`) naming the versions actually supported, rather than silently
  downgrading and leaving the client believing it negotiated something it had
  not. Missing required request metadata returns `-32602`.
- `ping` was removed in 2026-07-28 but is still answered, because legacy clients
  continue to send it.

### Model selection
- Model resolution runs against the live catalog rather than a hardcoded list.
  A request must match a model's version, family, and effort tier together, so
  `gemini-3.5-flash` never quietly resolves to a 3.7 model; unspecified
  dimensions resolve to the highest effort tier and newest version available.

### Discovery
- Cross-platform language server discovery: `/proc` and `ss` on Linux, `lsof`
  and `ps -axww` on macOS, `netstat` and WMI on Windows, each falling through to
  the others so unusual environments still work. Every strategy degrades to an
  empty result instead of raising.
- Only Linux is verified on real hardware, against Antigravity IDE v1.107.0.

### Packaging
- `pip`-installable with an `antigravity-mcp` console script, zero runtime
  dependencies, and CI across Linux, macOS, and Windows on Python 3.9 to 3.13.
- Optional skill describing when delegation is worthwhile.

### Fixed during development
- **Finished runs were polled until the job timeout.** An agent that answers
  directly, without calling tools, produces a trajectory ending in a
  `CHECKPOINT` after its final planner response. Completion required the last
  step to be a `PLANNER_RESPONSE`, so that shape never matched: the run was
  already over, the answer was sitting in the trajectory, and the bridge kept
  polling until it timed out and discarded the result. Observed on three real
  runs that produced complete answers of 7181, 9326, and 15822 characters and
  returned none of them. Runs ending on a planner response were unaffected,
  which made the failure look size-dependent when the opposite was true — the
  short direct answer is the shape that broke.
- `check_agent_job` reported negative elapsed time for running jobs, having
  measured against `created_at` rather than the current time.
- Job state moved out of the package directory into the per-user state
  directory, which a read-only or shared install would otherwise break.
- Unknown tools and malformed arguments return JSON-RPC error codes instead of
  tool results a model would try to reason about.
- `shutdown()` gained `wait_for_jobs` for callers that tear down the state
  directory afterwards; without it a job thread could still be writing
  `jobs.json` into a directory being deleted.
