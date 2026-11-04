# Hermes Trader Profile

Completed: 2026-11-04

This spec built the whole repository: `trader-broker` (`src/trader_broker/`), a Python MCP server and operator CLI, and `profile/`, the Hermes `stock-trader` profile that uses the server to swing-trade US stocks on Alpaca paper. For how the system works, see `docs/system/architecture.md`. For setup, see `README.md`.

## Problem

The goal was an LLM agent on the existing Hermes server that trades US stocks as aggressively as hard limits allow. The danger is that an LLM with direct broker access can lose real money in ways a prompt cannot stop:

- **Nothing caps the agent.** Neither Hermes nor Alpaca's own MCP server limits order size or daily loss. Alpaca's server lets the model choose any quantity and exposes `close_all_positions` and `cancel_all_orders` with no confirmation.
- **No human can approve cron runs.** Hermes approval prompts fail closed in unattended cron runs, so a person cannot sign off on scheduled trades.
- **The record of LLM trading is poor.** Most models in Alpha Arena season 1.5 lost money. FINSABER found that LLM strategies turn too aggressive in falling markets. StockBench measured agents making arithmetic errors on exactly the sizing step.

The two failure cases to rule out were the oversized bet, where the model picks a share count that puts most of the account in one name, and the prompt-only limit, which holds until the model miscounts.

## Alternatives Considered

| Approach | Pros | Cons | Why Rejected |
|----------|------|------|--------------|
| Hermes `pre_tool_call` plugin guard on Alpaca's MCP server | Less code; runs in cron | The model still picks share counts, and the guard re-derives the math; tied to Hermes plugin internals; untestable without Hermes; Alpaca's full tool surface stays visible | Enforcement belongs where the order is built |
| Server plus plugin guard | Defense in depth | The server never loads Alpaca's raw order tools, so a second lock adds little and doubles the maintenance | Not worth the cost |
| Prompt-only rules | Zero code | The failure cases above | Unsafe |
| Single-model bull/bear debate (TradingAgents style) | One vendor, simple | One model arguing with itself | The user proposed cross-model review, which adds model diversity for a few cents per trade |
| Both reviewers must approve | Safer on paper | LLM committees drift timid, and timidity in rising markets is a documented LLM failure (FINSABER) | The user chose the aggressive rule |
| Alpaca's official MCP server behind our server | Maintained by Alpaca | Two processes | `alpaca-py` directly behind a `Broker` protocol |

## Decisions & Trade-offs

- **Decision:** Models reason, software computes. `trader-broker` is the agent's only path to the broker. The agent proposes a symbol, stop, target and thesis, and code sets every number: share count, caps, halts and the bracket order.
  **Trade-off:** We had to write a custom server, and the agent cannot express its conviction through position size.
  **Rationale:** Code doesn't miscount. No tool changes the caps, the mode, the kill switch or the credentials, and the profile disables the terminal, code, file and browser tools, so the agent cannot edit its limits or reach Alpaca directly.

- **Decision:** Every buy is a GTC bracket order. Sells and stop raises are never blocked by a halt, the kill switch or missing state. Housekeeping runs before each write tool and repairs missing stops.
  **Trade-off:** A gap through the stop overnight still loses up to the 20% per-symbol cap. Housekeeping has no timer, so it runs only when some tool or `tb status` runs.
  **Rationale:** Alpaca holds the stop on its servers from the moment the entry fills, and protecting the account always comes first.

- **Decision:** Cross-model review uses the aggressive rule. Claude proposes and an OpenAI and an xAI model review. A buy goes through if at least one reviewer approves or modifies it and none rejects it with high confidence. If either reviewer errors, the buy is blocked. Reviewers can raise a stop or shrink the size, never loosen anything.
  **Trade-off:** A reviewer outage blocks buys for that run. The proposer has no fallback model, so a Claude outage skips the run.
  **Rationale:** Models from different vendors fail differently. The server builds the review packet itself, so the proposer cannot frame the facts. The journal records each verdict, so each reviewer's value can be judged after the paper period.

- **Decision:** The state database fails closed and orders are idempotent by client id. A `tb-<uuid4>` id is recorded before each submit. A buy whose outcome is unknown is never resubmitted.
  **Trade-off:** A lost or corrupt database blocks buys until the operator runs `tb init-state --force`.
  **Rationale:** A lost file must never silently clear the kill switch or allow a duplicate buy.

- **Decision:** Tool annotations describe what the tool does, not what the plan wanted it to be. `get_risk_status` is destructive because its housekeeping can cancel orders, place stops and market-sell. `get_journal` is a write tool because it settles trades. (Review N1.)
  **Trade-off:** The plan's simple read/write split no longer holds.
  **Rationale:** A client that trusts a "read-only" hint would be misled. Nothing in this setup relies on the hint, because the profile uses `trust: full`.

- **Decision:** A cross-process account lock (`flock` beside the state database) serializes housekeeping, buys and exits across `serve` processes and CLI `status`. `serve` waits `TRADER_ACCOUNT_LOCK_WAIT_S` (default 60 s) for the lock. (Review N2, P43.)
  **Trade-off:** Under contention the agent gets `busy` and retries later. Nothing checks that the lock wait plus the longest tool run stays under the profile's MCP `timeout: 300`.
  **Rationale:** With a 300 s wait, equal to the Hermes timeout, a queued `propose_buy` could place an order after the agent had been told the call timed out. The config comments and `.env.example` say to raise both values together.

- **Decision:** The agent may supply a `limit_price`, but the server bounds it by the ask (`limit_invalid`). (Review P4.)
  **Trade-off:** This adds a rule id the plan did not list.
  **Rationale:** Without the bound, a high limit could get a stock under $5 past `min_price` and could game the stop band.

- **Decision:** `get_orders("open")` doesn't rely on Alpaca's open listing alone. It merges Alpaca's `status=open` read with the newest orders of every status. (Review P1, P42.)
  **Trade-off:** Each call makes two reads. A duplicate leg is possible if Alpaca lists a filled bracket's legs at the top level (O1).
  **Rationale:** Alpaca's documentation doesn't say whether the working legs of a filled entry appear under `status=open`, and nothing could be checked without paper keys. The merged read sees both the legs and old GTC stops.

- **Decision:** The scheduled jobs are Hermes cron jobs, and a gate script skips non-trading days without waking the model. The installer creates the jobs paused, and when re-run it updates existing jobs with `hermes cron edit`.
  **Trade-off:** The installer can overwrite a job prompt the user edited by hand. Per-job time zone and grace period were dropped, because Hermes v2026.9.24 has neither flag.
  **Rationale:** Otherwise, prompt changes, including their safety lines, would never reach installed jobs (P22).

- **Decision:** Reports reach the user through the default profile's bots via `profile_routes`, with no new bot tokens. The README asks for a private trading channel and group and a narrow default-profile allowlist.
  **Trade-off:** A route picks a profile; it does not authorize anyone. Anyone the default bot accepts in a routed chat can command the trader.
  **Rationale:** Hermes cron delivery needs a location-only route (no `user_id`), and that route matches every sender in the chat. A `user_id` route therefore doesn't lock a chat down, and it can't carry cron delivery. Restricting who can post in the chat, and who the bot accepts, is the control that works. See `docs/system/architecture.md` "`profile_routes` delivery".

## What Changed

- `src/trader_broker/` adds the `Broker` protocol with `AlpacaBroker` and `FakeBroker`, the risk gate and sizing, the SQLite state store, cross-model review, the journal and SPY settlement, the go-live report, the account lock, the 16-tool MCP server, and the operator CLI. See `docs/system/architecture.md` "Components" through "Observability".
- `profile/` adds `config.yaml`, `SOUL.md`, `.env.example`, five trading skills, five cron jobs and the routes example. See "The Hermes profile".
- `scripts/install-profile.sh` and `scripts/market-open-gate.sh` add the idempotent installer and the market-calendar gate. See "Installer" and "Cron schedule and the gate script".
- `tests/` adds unit, component, integration (stdio server, installer against stub `hermes`/`uv`), profile and opt-in paper smoke tests: 600 offline tests in all.
- `README.md` (setup guide), `AGENTS.md`, `CLAUDE.md` and `.github/workflows/ci.yml` are new.

## Lessons Learned

- **Decide whether a tool has side effects before calling it a read.** The plan made `get_risk_status` run housekeeping, and that choice made its own annotation table wrong.
- **Check CLI flags against the installed version when planning.** `--timezone` and `--grace-minutes` came from docs prose, and the task spec had to be corrected during implementation.
- **Fixes to stop-protection logic need adversarial re-review.** The round-1 fixes to protection (P5, P8) caused most of round 2's money-safety defects (P39–P41). Code that pairs stops with shares needs scenario tests that interleave fills, cancels and partial states. `FakeBroker.delay_cancels(reads=N)` exists for that.
- **List broker behaviour that only paper trading can show as an assumption to verify.** P1, P3, P6 and P8 all rest on Alpaca semantics that could not be run here. Their fixes were designed so they don't depend on those semantics.
- **Test stubs can hide real behaviour.** The installer stub did not seed `.env` the way `hermes profile create` does, which hid that the template was never installed (P2).

## Post-Mortem Findings

- **Finding:** The live Alpaca paths have never run. No bracket, client-id lookup or SIP-bar fetch has hit Alpaca, because no paper keys were available.
  **Impact:** Several fixes (P1, P6, P8, P42) depend on Alpaca behaviour that only the 600 `FakeBroker` tests exercise.
  **Resolution:** Before the paper period, run `uv run pytest -m paper` and `trader-broker smoke-test`. After the first fill, compare `get_orders` with the Alpaca dashboard for duplicate legs (O1).
- **Finding:** Both history reads are capped at the newest 500 orders (`_ORDER_HISTORY_LIMIT`, `_OPEN_ORDER_SCAN`). Housekeeping reads orders before positions.
  **Impact:** A `tb-` stop older than the window would get a duplicate repair stop, which the broker refuses (O2). A take-profit fill between the two reads can briefly uncover shares until the next pass (O3).
  **Resolution:** Accepted for now. O2 is weeks away at a few orders a day.
- **Finding:** `service.py` is 1,724 lines, and housekeeping, exits and buys share the protection helpers.
  **Impact:** Changes in this area carry high regression risk, as round 2 showed.
  **Resolution:** None yet. A candidate for splitting before the next change to protection logic.
- **Finding:** The earnings blackout is enforced only by prompts (`SOUL.md`, the `trade-analysis` skill), because Alpaca has no earnings calendar.
  **Impact:** Code cannot stop a buy just before earnings.
  **Resolution:** Accepted in the plan.
- **Finding:** The skills and cron prompts hardcode the tool prefix `mcp__trader_broker__`.
  **Impact:** If Hermes exposes the tools under another name, every skill breaks.
  **Resolution:** Confirm the names in a manual `hermes -p stock-trader chat`.

## Review Highlights

- **Three rounds, one past the cap.** Round 1 raised 38 findings plus two adjudicated decisions (N1, N2). Round 2 found five medium money-safety defects in the round-1 fixes (P39–P43), three of them reproduced. The user chose one more fix round, and round 3 closed P39–P44 with no new findings, each fix backed by a regression test that failed before it.
- **Coverage arithmetic is the recurring risk.** Inactive legs of a partly filled entry, stops in `pending_cancel`, fills on a sibling take-profit, and repair stops left behind after a leg sold the whole position each produced a coverage miscount at some point. See `docs/system/architecture.md` "Housekeeping" for the current rules.
- **The Hermes timeout is part of the design.** The MCP `timeout: 300` bounds the review time (two reviewers, 60 s each plus one retry) and the account-lock wait together.
- **Don't price liquidity from IEX volume.** IEX volume is a small slice of consolidated volume, so liquidity is decided only by the server's SIP-based `min_adv` rule (P3).
