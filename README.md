# agent-review-lab

> [!WARNING]
> **`main` is the reference implementation. The feature branches are not.**
> Every branch other than `main` deliberately contains bugs and insecure patterns
> (blocking async calls, swallowed exceptions, missing kill switches and approval gates,
> prompt-injection paths, over-permissive tool configuration, and more) so that code
> review can be exercised against them. Do not copy code from those branches, merge
> them, or use any of it in production.

A small, self-contained lab for learning two agent frameworks,
[LangGraph](https://langchain-ai.github.io/langgraph/) and
[Strands Agents](https://strandsagents.com/), and for studying how automated code
review handles agent-specific problems: tools called incorrectly, inconsistent graph
state, async mistakes, missing exception handling, and missing safety controls such as
tracing, kill switches, and approval gates.

The business domain is **Northwind Fuel Co.**, a fictional fuel and energy logistics
company. All suppliers, sites, people, and numbers are invented. Every "external
system" (ERP, tank inventory, pricing, ordering) is mocked locally in an in-memory
SQLite database that is seeded at runtime. The only optional network call is to the
Anthropic API when you turn mock mode off.

## The two agents

| Agent | Framework | Risk tier | What it does |
| --- | --- | --- | --- |
| `invoice-reconciler` | LangGraph | medium | Matches a supplier fuel invoice to its purchase order and delivery tickets, flags volume, price-per-gallon, delivery-date, and total discrepancies, then auto-approves, rejects, or pauses for human review. |
| `fuel-order-assistant` | Strands Agents | high | Checks tank inventory, gets a price quote, and places a fuel order. Ordering goes through an approval hook and a spending limit. |

## Quick start (mock mode, no API key)

Requires [uv](https://docs.astral.sh/uv/). uv installs Python 3.12 for you.

```bash
uv sync
```

```bash
uv run demo invoice all --decision approve
```

```bash
uv run demo order "Order 5000 gal diesel for NW-DEPOT-01"
```

`MOCK_LLM` is on by default. In mock mode the LangGraph agent uses a deterministic
summarizer and the Strands agent uses `ScriptedOrderModel`, a `strands.models.Model`
implementation that plans tool calls from the conversation the same way a real model
would emit them. Everything runs offline and gives the same result every time.

### More things to try

```bash
uv run demo invoice INV-1004
```

```bash
uv run demo invoice INV-1005 --decision defer
```

```bash
uv run demo order "Order 9000 gal diesel for NW-DEPOT-02" --yes
```

```bash
uv run demo order "How much diesel does NW-DEPOT-02 need?"
```

```bash
uv run demo kill-switch on
```

| Command | What it shows |
| --- | --- |
| `invoice INV-1004` | High-value invoice: the graph pauses with `interrupt()` and asks you to approve. |
| `invoice INV-1005 --decision defer` | Supplier note containing instruction-like text is fenced, flagged, and ignored for routing. |
| `order "... 9000 gal ..." --yes` | Quote exceeds the $25,000 spending limit, so the approval hook refuses before asking anyone. |
| `order "How much ..."` | No order intent: the agent quotes but never reaches the high-risk tool. |
| `kill-switch on` | Creates `.killswitch`; both agents halt cleanly before their next step. Use `kill-switch off` to release it. |

Seeded invoices: `INV-1001` (clean, auto-approve), `INV-1002` (short delivery),
`INV-1003` (price over PO by more than 5%, reject), `INV-1004` (clean but high value),
`INV-1005` (late delivery plus an injected note), `INV-1006` (unknown PO, reject).
Sites: `NW-DEPOT-01`, `NW-DEPOT-02`, `NW-DEPOT-03`.

## Using a real model

```bash
cp .env.example .env
```

Edit `.env`: set `MOCK_LLM=0` and put your own key in `ANTHROPIC_API_KEY`
(`.env` is gitignored). Optionally set `ANTHROPIC_MODEL_ID`. With a real model the
Strands agent decides its own tool calls; the hooks, spending limit, approval gate,
and kill switch apply exactly as in mock mode. In the LangGraph agent the model only
writes the reviewer summary; routing stays deterministic.

## How the frameworks map to the code

### LangGraph: `src/northwind/agents/invoice_reconciler/`

| Concept | Where |
| --- | --- |
| **State** | `state.py`: `InvoiceState` is a `TypedDict`; every key a node reads or writes is declared there. |
| **Nodes** | `graph.py`: `load_invoice`, `fetch_records`, `check_discrepancies`, `summarize`, `human_review`, `approve`, `reject`. Each is wrapped by `_node()` to add a kill-switch check and a span. |
| **Edges** | `graph.py` `_build()`: fixed edges plus three conditional edges (`route_after_load`, `route_after_summary`, `route_after_review`) with explicit path maps. |
| **Tools** | `tools.py`: `InvoiceTools` methods call the mocked ERP through `run_tool()`. |
| **Routing rules** | `rules.py`: deterministic discrepancy detection and `choose_route()`; the model never decides. |
| **Checkpointer** | `InMemorySaver` passed to `compile()`; each run uses its own `thread_id`. |
| **Interrupt** | `human_review` calls `interrupt(payload)`; the caller resumes with `Command(resume={...})` via `InvoiceReconciler.resume()`. |
| **Recursion limit** | `recursion_limit` from `config/agents.toml`, passed in the run config. |

### Strands Agents: `src/northwind/agents/fuel_order_assistant/`

| Concept | Where |
| --- | --- |
| **Agent** | `agent.py`: `strands.Agent` with a system prompt, tools, hooks, and a `SequentialToolExecutor`. |
| **Tools** | `tools.py`: `@tool` functions `check_tank_inventory`, `get_price_quote`, `place_fuel_order` (built per agent by `build_order_tools()`). Docstrings and type hints become the tool schema. |
| **Model** | `llm/strands_models.py`: `ScriptedOrderModel` (mock) or `strands.models.anthropic.AnthropicModel`. |
| **Hooks** | `hooks.py`: `KillSwitchHook` (before invocation, model call, and tool call), `ApprovalGateHook` (spending limit, then human approval, for `high_risk_tools`), `ToolAuditHook` (records every tool outcome). |
| **Iteration limit** | `invoke_async(..., limits={"turns": max_turns})`; a run that hits it stops with `stop_reason="limit_turns"`. |

### Shared safety controls

| Control | Where |
| --- | --- |
| Risk tier, limits, timeouts, high-risk tools | `config/agents.toml`, loaded by `config.py` |
| Kill switch (`NORTHWIND_KILL_SWITCH=1` or the `.killswitch` file) | `safety.py` `KillSwitch`, checked before every tool call |
| Per-tool timeout, retries with exponential backoff and jitter | `resilience.py` `run_tool()` |
| OpenTelemetry spans for runs, nodes, tools, and decisions, written as JSON lines to `traces/` | `tracing.py` |
| Untrusted free-text fencing and an injection heuristic | `llm/untrusted.py` |
| Engineering standards for reviewers | [`AGENTS.md`](AGENTS.md) |

## Project layout

```text
config/agents.toml               agent registry: risk tiers, limits, timeouts
src/northwind/
  agents/invoice_reconciler/     LangGraph agent
  agents/fuel_order_assistant/   Strands agent
  llm/                           mock and Anthropic model adapters, untrusted-text helpers
  mocks/                         in-memory SQLite seed data and async service clients
  config.py safety.py resilience.py tracing.py cli.py
tests/                           offline pytest suite
```

## Tests and linting

```bash
uv run pytest
```

```bash
uv run ruff check .
```

The tests run offline and cover each agent's happy path, the kill switch (before a run
and mid-run), the approval gates, spending and iteration limits, retries, and timeouts.

## Code review configuration

The repository includes a [`.coderabbit.yaml`](.coderabbit.yaml) that configures
CodeRabbit reviews for agent code. It is organized in four parts:

1. **Review profile.** `assertive`, so smaller issues are reported as well as defects.
2. **Path instructions.** Focused guidance per area: all agent code
   (`src/northwind/agents/**`) is checked for tool-call correctness, kill-switch and
   tracing coverage, approval gates, iteration limits, async correctness, exception
   handling, and prompt injection through tool output. The LangGraph and Strands
   packages get framework-specific instructions (state-key consistency and reachable
   edges; tool schemas and hooks), and there are separate instructions for the shared
   safety modules, model adapters, the agent registry, MCP/skill configuration files,
   and tests.
3. **Tools.** Ruff (which uses the `[tool.ruff]` rules in `pyproject.toml`, including the
   `ASYNC`, `BLE`, `S`, and `TRY` rule sets), secret scanners, YAML and Markdown linters,
   and SkillSpector for `SKILL.md` and MCP configuration files.
4. **Pre-merge checks.** Custom pass/fail checks for agent safety controls: new agents
   need a kill switch, tracing, and an approval gate; iteration limits; no blind or
   swallowed exceptions; no blocking calls in async code; untrusted tool output kept out
   of prompts; LangGraph state-key consistency; tool argument correctness; least
   privilege for MCP and skill configuration; tracing coverage; and no hardcoded
   credentials.

`AGENTS.md` is also registered as a code guideline so reviews are checked against it.

## License

[MIT](LICENSE)
