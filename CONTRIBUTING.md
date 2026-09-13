# Contributing

Thanks for taking a look. Issues and pull requests are both welcome.

## Getting set up

```bash
git clone https://github.com/Kio-pon/antigravity-mcp.git
cd antigravity-mcp
pip install -e ".[dev]"
pytest
```

The default suite is hermetic — no Antigravity IDE, no network, no API key —
and it needs to stay that way, because CI runs on three operating systems where
none of those exist. Tests that need a backend inject a stub executor into
`MCPServer`; the package itself must never grow a fake-result path, since one
flag set by accident would then let it return invented output in production. Anything requiring a real language server belongs behind
the `ANTIGRAVITY_LIVE_TESTS=1` opt-in, alongside the existing round-trip test.

Before pushing:

```bash
ruff check . && ruff format --check . && pytest
```

## The most useful thing you can contribute

**Confirming discovery on macOS or Windows.** Those code paths are written
against documented tool output but have never run on real hardware. If you have
the IDE open on either platform:

```bash
python -c "from antigravity_mcp.discovery import find_language_server_candidates as f; print(f())"
```

An issue reporting what that prints — even an empty list — is genuinely
valuable, and please include your OS version and IDE version.

## Working on discovery

Add platform strategies as small private functions returning an empty result on
any failure, then register them in `_port_strategies` or `_cmdline_strategies`.
Two rules keep this code trustworthy:

- **Never raise.** A missing tool, a permission error, or a timeout must degrade
  to an empty result. Discovery failing loudly at import time would take the
  whole server down over a diagnostic detail.
- **Test against captured real output.** Paste genuine command output into the
  test constants. Hand-written approximations of `netstat` output have a way of
  agreeing with the parser and disagreeing with reality.

## Touching the local backend

`antigravity_local.py` talks to private, undocumented IDE endpoints. Two places
have subtle behaviour that looks removable and is not — the completion-detection
loop and the CSRF token regex. Both carry comments explaining the bug that
produced them. Please read those before simplifying either.

## Pull requests

Small and focused beats large and sweeping. Explain the behaviour change, add a
test that would have failed before it, and note anything you could not verify
on your platform.
