# Agent engineering standards

These standards apply to every agent, tool, hook, and agent-related configuration file
in this repository (`src/northwind/agents/**`, `src/northwind/llm/**`,
`config/agents.toml`, and any MCP or skill configuration). Reviewers, human or
automated, should treat a violation as a blocking issue unless the PR explains why it
does not apply.

## 1. Every agent is registered and tiered

- Each agent has an entry in `config/agents.toml` with a `risk_tier` (`low`, `medium`,
  or `high`), an iteration budget (`recursion_limit` for LangGraph, `max_turns` for
  Strands), per-tool timeouts, and a `high_risk_tools` list.
- An agent that can spend money, change records in a system of record, or act outside
  the process is at least `medium`. An agent that does any of those without a human in
  the loop is not allowed.

## 2. Kill switch is required

- Every tool call checks the kill switch first (`KillSwitch.check()`, which
  `run_tool()` does for you). Strands agents also register `KillSwitchHook` so the
  switch is checked before the invocation, each model call, and each tool call.
- When the switch is engaged the agent stops cleanly: no partial writes, a clear
  `halted` status, and a trace event. It must not be caught and ignored.

## 3. Tracing is required

- Every graph node, tool call, and routing or approval decision emits an
  OpenTelemetry span or decision event (`traced()`, `record_decision()`).
- Traces go to the gitignored `traces/` directory. Never log secrets or full
  free-text payloads at INFO or above.

## 4. High-risk tools need approval gates

- Any tool listed in `high_risk_tools` must pass through an approval gate before it
  executes: `ApprovalGateHook` for Strands, `interrupt()` plus `_authorize()` for
  LangGraph.
- Spending limits are enforced before asking for approval and again inside the tool
  (defense in depth). Approvers default to deny (`deny_all`).
- An agent must never approve its own high-risk action.

## 5. Tool output is untrusted input

- Free-text fields from tools (supplier notes, driver notes, site notes) are wrapped
  with `wrap_untrusted()` before a model sees them, and are never concatenated into a
  system prompt.
- Routing, approval, and amount decisions are made from structured fields by
  deterministic code. A model may summarize; it may not decide.

## 6. Exceptions are specific

- No bare `except:` and no blind `except Exception:` / `except BaseException:`.
  Catch the specific errors you can handle (`RecordNotFoundError`, `ToolCallFailed`,
  `KillSwitchEngaged`, `anthropic.APIError`, ...) and let the rest propagate.
- Never swallow an exception with `pass`. If you handle it, log or trace it and return
  an explicit error result.
- Only transient errors (`TransientServiceError`, `TimeoutError`) are retried, with
  exponential backoff and a bounded number of attempts (`run_tool()`).

## 7. Async code never blocks

- No `time.sleep`, `requests`, synchronous file or database I/O, or `input()` directly
  inside `async def`. Use `asyncio.sleep`, an async client, or `asyncio.to_thread`.
- Every coroutine is awaited or scheduled in a `TaskGroup`. Concurrent tool calls use
  `asyncio.TaskGroup`, not fire-and-forget tasks.
- Every external call has a timeout (`asyncio.timeout` via `run_tool()`).

## 8. State is typed and consistent

- LangGraph state is a `TypedDict` in `state.py`. A node may only read keys that some
  upstream node writes, and only write keys declared in the state type.
- Conditional edges declare an explicit path map, and every branch must be reachable.
- Nodes that call `interrupt()` must not have side effects before the interrupt,
  because the node re-runs from the top on resume.

## 9. Secrets and configuration

- Credentials come from the environment (`.env`, gitignored). Never commit a real or
  realistic-looking credential; examples use `DEMO_ONLY_NOT_A_REAL_TOKEN`.
- MCP servers and skills request the narrowest scopes needed: no wildcard filesystem
  access, no general shell execution, and no inline credentials.

## 10. Tests

- Every agent has tests for its happy path, kill switch, approval gate, and iteration
  limit, and they run offline with `MOCK_LLM=1`.
