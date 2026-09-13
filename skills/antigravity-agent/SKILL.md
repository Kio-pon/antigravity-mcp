---
name: antigravity-agent
description: Delegate background subagent work to the local Antigravity IDE agent engine via the antigravity-mcp server. Use when a task is large-context (scanning many files to answer one question), parallelisable (independent work that can run while you keep planning), or mechanical (boilerplate, fixtures, type definitions) — and when you need to check on, collect, or cancel jobs you already dispatched.
---

# Delegating to Antigravity subagents

You have a local agent engine available through the `antigravity-mcp` server. It
runs on the Antigravity IDE's own authenticated session, so jobs are fast and
cost you nothing on your own context budget.

You stay the architect. Subagents do legwork; you decide what their output means.

## Delegate when

- **The answer is buried in many files.** `gemini_code_search` reads forty files
  at once and answers a specific question. Reading them yourself spends your
  context on material you will use one sentence of.
- **The work is independent and you have other work.** Dispatch, keep planning,
  collect later. This is the only way to genuinely do two things at once.
- **The work is mechanical.** Fixtures, mock data, repetitive test scaffolding,
  type definitions transcribed from a schema.

## Do not delegate when

- The task needs judgment you already hold — architectural decisions, anything
  where being subtly wrong is expensive and hard to notice on review.
- The work is small. A round trip costs seconds of latency; reading one file
  yourself is faster than describing which file you wanted.
- You would not be able to check the result. Delegating work you cannot verify
  just moves the risk somewhere you can see it less well.

## How

**Pick a model.** The catalog is whatever the user's Antigravity account has and
it changes over time, so do not assume a name. Call `list_available_models` when
you do not already know a live one. You can pass an exact label
(`"Gemini 3.8 Flash (High)"`), a partial description (`"gemini 3.7 flash medium"`),
or just a family (`"flash"` → best available flash). Omitting the `model`
argument gets you the fastest flash-tier model, which is the right default for
mechanical work.

**Dispatch and keep working.** `dispatch_gemini_agent` returns a `job_id`
immediately — it does not block. Note the id, then carry on. Coming back to poll
instantly wastes the entire advantage.

**Write the task as a brief, not a hint.** The subagent has no memory of your
conversation and sees only what you send. Say what output you want and in what
form. Pass absolute paths in `context_files` rather than describing where things
are. Use `system_instruction` to constrain the shape of the response — "return
only code in one fenced block, no commentary" is worth the sentence every time.

**Collect.** `check_agent_job` returns status, elapsed time, progress logs, and
output. A job still running says so; check again rather than assuming it stalled.
`list_active_jobs` shows everything in flight, and `cancel_agent_job` stops work
you no longer need.

Jobs run up to 3 at a time by default and queue past that — they all share one
language server process. Dispatching ten at once does not make ten run.

## Review before you apply

A subagent's output is a proposal. Read it before it touches a file, and hold it
to the standard you would hold your own work to: it does not know the project's
conventions, it has not read the code you did not send it, and it will produce
something plausible-looking when it is out of its depth. Say plainly when its
result was wrong or unusable rather than quietly patching around it.
