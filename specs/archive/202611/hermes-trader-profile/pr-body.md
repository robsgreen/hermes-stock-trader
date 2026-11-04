## feat: add trader-broker MCP server and Hermes stock-trader profile

### Why
We want a Hermes Agent profile that swing-trades US stocks aggressively. Neither Hermes nor Alpaca's MCP server limits order size or daily losses, and the live record for LLM traders is poor: the losses came mostly from aggressive, leveraged setups. This PR gives the agent a single path to the broker, and that path enforces the caps in code. The model decides *what* to buy and *why*. Code decides *how much*, and when to stop.

### What
- **`trader-broker`**: a Python 3.12 FastMCP server with 16 tools.
  - A `Broker` adapter interface. Alpaca comes first; Robinhood or another broker can be added later.
  - The risk gate:
    - long-only, no leverage
    - at most 3% of equity at risk per trade, measured at the stop
    - at most 20% per symbol, counting pending buys
    - $5 minimum price and $20M ADV floors
    - a 4% daily halt that stays latched for the day
    - a 20% drawdown kill switch that only the CLI can reset
  - Bracket orders on every buy.
  - GPT and Grok review every buy. A trade proceeds if either reviewer approves and neither rejects with high confidence. Reviewer changes can only tighten it.
  - Housekeeping keeps every held share under a stop.
  - A SQLite state store that fails closed, plus idempotency by client order id.
  - A journal scored against SPY, and a go-live report.
- **Hermes `stock-trader` profile**:
  - Config with terminal, code, file and browser tools disabled.
  - A persona and 5 trading skills.
  - 5 weekday cron jobs, with a market-day gate script.
  - An idempotent installer.
  - A `profile_routes` snippet for delivering reports through the existing Discord and Telegram bots.
- **Docs**:
  - `README.md` (setup on the server)
  - `docs/system/architecture.md`
  - `AGENTS.md`
  - the retro, `docs/specs/202611/hermes-trader-profile.md`

### How
- The agent calls `propose_buy(symbol, stop, target, thesis, …)` and never picks a share count. The server sizes the order and checks every cap. It then runs both reviewers in parallel, re-checks the order after their changes, and places a GTC bracket.
- Sells, closes and raised stops are never blocked by halts.
- A cross-process account lock serializes everything that writes.
- Paper trading is the default. Live trading needs `TRADER_BROKER_MODE=live` plus an explicit acknowledgement phrase.

### Testing
- `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy src` (strict) and `uv run pytest -m "not paper"` all pass: **600 tests**. The suite covers unit tests, component tests over the in-process MCP server, and integration tests over the stdio server and the installer (run with a Hermes stub that behaves like v2026.9.24).
- Two reviewers ran three review rounds:
  - Round 1: 0 critical / 3 high / 20 medium / 15 low.
  - Round 2: 5 medium / 1 low, introduced by the round-1 fixes.
  - Round 3: clean.

  Every logic fix has a regression test that failed before the fix.
- **Not yet run (needs your keys and server):**
  - `uv run pytest -m paper` and `trader-broker smoke-test` with Alpaca paper keys
  - the Hermes end-to-end checklist in README step 7

### Review observations (not addressed)
4 low observations are recorded in `.sdlc` `review.md`:
- Alpaca's order listing may return bracket legs twice.
- Order history is read in a 500-order window.
- Housekeeping has an orders-then-positions read race.
- `settle_trades` has an efficiency note.

There are no pre-existing findings.

### Before the 8-week paper period
1. Run the paper smoke tests.
2. After the first fill, check that `get_orders` lists each bracket leg once.
3. Confirm the tools appear as `mcp__trader_broker__<tool>`.
4. Route only a private channel, and keep the default bot's allowlist narrow.

🤖 Generated with [Claude Code](https://claude.com/claude-code)

https://claude.ai/code/session_01JA8ywTbX4hkn3XQ4wqwWro
