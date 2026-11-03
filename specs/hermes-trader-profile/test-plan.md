# Test Plan: Hermes Trader Profile

> **Source:** `.sdlc/specs/hermes-trader-profile/master-plan.md`
> **Created:** 2026-11-03

Acceptance-level checklist: what must work when the spec is done. Each item maps to master plan requirements (quoted by short name). "Automated" items run against `FakeBroker` and `FakeReviewer` with `uv run pytest -m "not paper"` and no network. "Paper" items need the user's Alpaca paper keys (`uv run pytest -m paper` or `trader-broker smoke-test`). "Manual" items run on the user's server.

## 1. Buy path

| ID | Behavior to verify | Level | Requirements |
|---|---|---|---|
| A1 | `propose_buy` with a valid proposal and two approving reviewers places one bracket order: limit entry `gtc`, take-profit at target, stop at stop, whole shares, `tb-` client id. It returns `placed` with final qty/stop/target and both reviewers' summaries. | Automated | Bracket order; return shape; cross-model review |
| A2 | The agent cannot choose a share count: `propose_buy` has no qty input, and the placed qty equals the code-sized qty from the sizing table (cap-bound, risk-bound and cash-bound cases). | Automated | Size every buy in code |
| A3 | An existing position or an open buy order in the same symbol reduces the allowed add so the symbol stays at or below 20% of equity. | Automated | Size every buy in code; symbol cap |
| A4 | Each entry rule blocks with its rule id and lists every failing rule: `leverage`, `cash`, `min_price` ($4.99), `min_adv` ($5M), `market_closed`, `too_late` (20 min before close), `not_tradable`, `stop_invalid`, `target_invalid`, `qty_zero`. Each block appears in `get_risk_status` recent blocks. | Automated | Enforce entry caps |
| A5 | A submit that times out after the broker accepted it produces exactly one order and returns `placed`; a timeout with no accepted order never resubmits. | Automated | Client order id idempotency |

## 2. Cross-model review

| ID | Behavior to verify | Level | Requirements |
|---|---|---|---|
| R1 | Verdict combinations: approve+approve → placed; approve+reject(low) → placed; approve+reject(high) → blocked; reject+reject(low) → blocked. | Automated | `decide()` rule |
| R2 | A reviewer that times out or returns invalid JSON twice blocks the buy, and the reason names that reviewer. Sells and stops still work. | Automated | Reviewer retry and error outcome |
| R3 | Reviewer modifications only make the order safer: a raised stop and a 0.5 multiplier give stop 92 and qty 100 (equity 100k, entry 100, stop 90); a raised stop never grows size; a looser stop is ignored. | Automated | `decide()` modifications; final qty never exceeds original |
| R4 | The review packet is built by the server and holds every listed field (proposal, sized order, quote, indicators, 60 closes, ≤10 headlines, account exposure, SPY regime). Packet hash, both raw verdicts, decision and final order are stored and linked to the journal entry. | Automated | `build_packet`; persist reviews |

## 3. Halts and kill switch

| ID | Behavior to verify | Level | Requirements |
|---|---|---|---|
| H1 | Equity −4.1% vs prior close blocks new buys with `daily_halt`; the block stays after equity recovers to −3% the same day and clears the next trading day. Sells still work. | Automated | Daily halt |
| H2 | Equity 20% below peak (peak 120k, equity 95.9k) sets the kill switch; it stays set after equity recovers; only `trader-broker reset-kill-switch` clears it and resets the peak to current equity. | Automated | Kill switch |
| H3 | A missing or corrupt state DB blocks buys with `state_unavailable`; sells, stops and reads still work; `init-state` refuses to overwrite an existing DB without `--force`. | Automated | State fail-closed |

## 4. Exits and stops

| ID | Behavior to verify | Level | Requirements |
|---|---|---|---|
| E1 | `close_position` on a bracket-protected position cancels the legs, sells all shares with a market `day` order, and leaves no open orders for the symbol. | Automated | Sell while legs reserve shares |
| E2 | A partial `sell` leaves the remaining shares protected by a GTC stop at the previous stop price. | Automated | Sell re-protection |
| E3 | Selling more than held is rejected `exceeds_position`; no short position is ever opened. | Automated | Long-only |
| E4 | `tighten_stop` raises a stop below the bid; lowering a stop (90 → 88) or setting it at or above the bid is rejected. | Automated | tighten_stop |
| E5 | `cancel_entry_order` cancels only trader-broker's unfilled entry; stop legs, take-profit legs, partly filled parents and foreign orders are rejected `not_cancellable`. | Automated | cancel_entry_order |
| E6 | Exit tools work while the kill switch, the daily halt, or missing state would block buys. | Automated | Halts never block exits |

## 5. Housekeeping and integrity

| ID | Behavior to verify | Level | Requirements |
|---|---|---|---|
| K1 | An unfilled entry older than 30 minutes is cancelled at the next write tool call or `get_risk_status`. | Automated | Housekeeping stale entries |
| K2 | A position with no stop (manual trade or cancelled leg) gets a GTC stop at the journal stop or 8% below average entry, and appears in `get_risk_status.unprotected`; if the bid is already below that stop, it is market-sold and logged. | Automated | Housekeeping unprotected positions |
| K3 | Foreign orders or positions, an entry whose stored sizing broke a cap, and negative cash each record one integrity violation; price drift past 20% records none. | Automated | Reconciliation |

## 6. Journal and report

| ID | Behavior to verify | Level | Requirements |
|---|---|---|---|
| J1 | A closed trade of +10% while SPY rose 2% settles with alpha +8% and appears in `get_journal(status="pending_lessons")`; `journal_note` stores the lesson and removes it from that list. | Automated | Journal settlement and lessons |
| J2 | `trader-broker report` prints PASS/FAIL for ≥8 weeks, return > SPY, max DD < 25% and 0 integrity violations, plus blocks by reason and per-reviewer stats; `--json` gives the same data. | Automated | Go-live report |

## 7. Server surface, CLI and safety guards

| ID | Behavior to verify | Level | Requirements |
|---|---|---|---|
| S1 | The MCP server exposes exactly the 10 read tools (`readOnlyHint: true`) and 6 write tools (`readOnlyHint: false`), and no tool changes caps, mode, kill switch or credentials. | Automated | Exact MCP tool set |
| S2 | The server starts in paper mode by default; `TRADER_BROKER_MODE=live` without `TRADER_BROKER_LIVE_ACK=I_ACCEPT_REAL_MONEY_RISK` exits non-zero with a clear message; the state DB path contains the mode. | Automated | Paper default and live guard |
| S3 | `trader-broker market-open-gate` prints `{"wakeAgent": true}` as the last line on a trading day (including an early-close day at 15:45), `false` on holidays and weekends, `true` on an API error, and exits 0 every time. | Automated | Market gate |
| S4 | No module outside `brokers/` imports `alpaca`; `FakeBroker` and `AlpacaBroker` satisfy the `Broker` Protocol. | Automated | Broker boundary |
| S5 | Logs are JSON on stderr and in the rotating log file, and contain no secret values. | Automated | Structured logs (SHOULD) |
| S6 | CI runs ruff check, ruff format check, mypy and the non-paper tests, and passes. | Automated | CI (SHOULD) |

## 8. Alpaca paper account

| ID | Behavior to verify | Level | Requirements |
|---|---|---|---|
| P1 | A 1-share bracket buy with a far-below-market limit is accepted with all prices, is found by `get_order_by_client_id`, and is cancelled. | Paper | Paper smoke test |
| P2 | An IEX quote and SIP-history daily bars (≥20 bars) are fetched for a liquid symbol. | Paper | Paper smoke test; ADV from SIP bars |

## 9. Hermes profile

| ID | Behavior to verify | Level | Requirements |
|---|---|---|---|
| F1 | Profile files are valid: `config.yaml` (timezone, anthropic model, no fallback, `trust: full`, `timeout: 300`, disabled terminal/code/file/browser toolsets), `SOUL.md`, five skills with Hermes frontmatter, five cron jobs with the stated times, grace values and gate script, routes mapped to `stock-trader`. | Automated | Hermes profile |
| F2 | `install-profile.sh --dry-run` writes nothing; a real run creates the `stock-trader` profile, never overwrites `.env`, creates each cron job once and paused unless `--enable-jobs`, prints the `profile_routes` snippet, and never edits the default profile. | Automated (stubs) | Installer |
| F3 | On the user's server: the installer runs, `init-state` and `smoke-test` pass, the routes snippet is applied by hand, and a manual run of the `stock-trader` profile can call `mcp_trader_broker_get_risk_status` but has no terminal, code or file tools. | Manual | Hermes end-to-end (README checklist) |
| F4 | On the user's server: a cron job on a market holiday is skipped by the gate without waking the model; a job on a trading day posts its summary to the routed Discord and Telegram trading channel. | Manual | Daily schedule; profile_routes delivery |

## 10. Documentation

| ID | Behavior to verify | Level | Requirements |
|---|---|---|---|
| D1 | `docs/system/architecture.md` describes the built system and cites the confirmed Hermes toolset identifiers with their source; `README.md` holds the setup guide and install checklist; `CLAUDE.md` imports `@AGENTS.md`. | Manual (read) | Docs |
