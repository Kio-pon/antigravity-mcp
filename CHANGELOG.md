# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.1.0] — 2026-09-13

Support for the current MCP specification, **2026-07-28**, alongside the
handshake-based revisions this server already spoke.

### Added
- **MCP 2026-07-28 support.** The server is now "dual-era": it serves modern
  clients, which declare their protocol version and capabilities in each
  request's `_meta` and never shake hands, and legacy clients, which open with
  `initialize`. Which era a request belongs to is read off the request itself,
  so a single process serves both with no configuration.
- **`server/discover`**, which the specification makes mandatory. On stdio it is
  also the client's era probe: answering it is what identifies this server as
  modern. It returns every version this server speaks — the legacy ones
  included — so a client that cannot speak 2026-07-28 can read a version it does
  support straight out of the response instead of probing for one.
- `resultType: "complete"` and `_meta['io.modelcontextprotocol/serverInfo']` on
  every result. Legacy clients ignore both, since earlier revisions instruct
  clients to treat an absent `resultType` as complete.
- `ttlMs` and `cacheScope` on `tools/list`, letting clients cache the catalog.
  The catalog is a constant that changes only when the server binary does, so
  the advertised hour-long TTL is honest rather than optimistic.

### Changed
- A request declaring an unsupported protocol version now returns
  `UnsupportedProtocolVersionError` (`-32022`) listing the versions this server
  does support, instead of silently downgrading. Silent downgrade left the
  client believing it had negotiated something it had not.
- A modern request missing its required `protocolVersion` or `clientCapabilities`
  metadata is rejected with `-32602`, as the specification requires.
- `initialize` never echoes a modern protocol version. A client using the
  handshake has already demonstrated it is not speaking the per-request
  protocol, so promising it one would be a lie it could not act on.

### Notes
- `ping` was removed in 2026-07-28 but is still answered here, because legacy
  clients continue to send it.
- Nothing in this server uses the features deprecated by 2026-07-28 — Roots,
  Sampling, Logging, Dynamic Client Registration, or the HTTP+SSE transport — so
  there is nothing to migrate off.

## [1.0.0] — 2026-09-13

First public release.

### Added
- MCP server exposing six tools: `dispatch_gemini_agent`, `check_agent_job`,
  `list_active_jobs`, `cancel_agent_job`, `gemini_code_search`, and
  `list_available_models`.
- Three-tier backend dispatch — local Antigravity language server, Gemini REST
  API, opt-in mock — with an explicit failure when none is reachable, so a job
  never returns fabricated output.
- Cross-platform language server discovery: `/proc` and `ss` on Linux, `lsof`
  and `ps -axww` on macOS, `netstat` and WMI on Windows, each with fallbacks.
- Token-based model resolution against the live catalog: a request must match a
  model's version, family, and effort tier together, and unspecified dimensions
  resolve to the highest effort and newest version available.
- Job history persisted to the per-user state directory, surviving across
  client sessions.
- `pip`-installable package with an `antigravity-mcp` console script, zero
  runtime dependencies, and CI across Linux, macOS, and Windows on Python
  3.9–3.13.
- Optional Claude Code skill describing when delegation is worthwhile.

### Fixed
- Tool calls are handled on a thread pool instead of inline on the stdio read
  loop. A long `gemini_code_search` previously blocked every other tool call
  behind it, including the `check_agent_job` polls needed to watch other jobs.
- `check_agent_job` reported negative elapsed time for running jobs, having
  measured against `created_at` rather than the current time.
- Job state is written to the per-user state directory rather than into the
  package directory, which breaks under a read-only or shared install.
- Unknown tools and malformed arguments now return JSON-RPC error codes instead
  of tool results the model would try to reason about.
- Broken-pipe and malformed-input handling on stdio no longer risks a crash on
  client disconnect.
