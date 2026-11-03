# Test Spec: hermes-trader-profile

> **Source:** `.sdlc/specs/hermes-trader-profile/test-plan.md`, `task-spec.md`, `master-plan.md`
> **Created:** 2026-11-03
> **Last Updated:** 2026-11-03

## Approach

The unit suite written with the code drives `TraderService`, `risk`, `review` and the CLI one function at a time. These tests go one level up: they drive the **MCP server over its tool interface** (`build_server(...).call_tool`, the same path Hermes uses), wired to the real `TraderService`, risk gate, review decision, `StateStore` (a temp SQLite file), journal and report, with only the outside world faked: `FakeBroker` for Alpaca and `FakeReviewer` (or a real `OpenAICompatReviewer` over an `httpx.MockTransport`) for the two reviewer APIs. Each test is a user workflow across several tool calls: propose, fill, sell, settle, report. The operator CLI is driven through `main(argv)` against the same state file the server writes. Adversarial cases (unexpected arguments, absurd prices, racing buys, state corrupted mid-session, repeated housekeeping) are folded into the scenarios.

Integration level (per `.sdlc/workspace.md`, integration tests are ⚠️ paper-only): the real `trader-broker serve` process is started over stdio and spoken to by an MCP client, and `scripts/install-profile.sh` runs against stub `hermes`/`uv` binaries. The Alpaca paper checks (P1, P2) and the on-server Hermes checks (F3, F4, D1) cannot run here and are deferred to the user with exact commands.

Test-plan items already pinned end to end by existing tests are listed under **Covered by existing tests** rather than duplicated.

## Component Tests

- [x] **CT-1** — A1: an approved `propose_buy` places exactly one GTC bracket (limit entry, take-profit at target, stop at stop, whole shares, `tb-` client id) and returns `placed` with final qty/stop/target and both reviewers' summaries; symbol input is normalised.
  - File: `tests/component/test_buy_tools.py`
  - Modules exercised: server, service, risk, review, state, journal, FakeBroker
- [x] **CT-2** — A2: `propose_buy` has no quantity input (schema, and a `qty` argument is refused); the placed qty equals the code-sized qty for cap-bound, risk-bound and cash-bound cases.
  - File: `tests/component/test_buy_tools.py`
  - Modules exercised: server, service, risk, FakeBroker
- [x] **CT-3** — A3: an existing position, or an open entry from an earlier `propose_buy`, shrinks the next buy so the symbol stays at or below 20% of equity; two buys racing through the server never exceed the cap together.
  - File: `tests/component/test_buy_tools.py`
  - Modules exercised: server (call lock), service, risk, FakeBroker
- [x] **CT-4** — A4: each entry rule (`leverage`, `cash`, `min_price` 4.99, `min_adv` $5M, `market_closed`, `too_late` 20 min before close, `not_tradable`, `stop_invalid`, `target_invalid`, `qty_zero`) blocks with its rule id and shows in `get_risk_status.recent_blocks`; a proposal breaking several rules lists them all; a stale account read that would lever the account blocks `leverage`; malformed or absurd prices never reach the broker. (`cash` cannot be reached through the tool, because sizing already bounds qty by available cash; it stays covered by `tests/unit/test_risk.py::test_buy_over_available_cash_fails_cash`, and cash-starved buys block `qty_zero`.)
  - File: `tests/component/test_buy_tools.py`
  - Modules exercised: server, service, risk, state, FakeBroker
- [x] **CT-5** — A5: a bracket submit that times out after the broker accepted it yields one order and `placed`; a submit that times out with nothing accepted is blocked and not resubmitted.
  - File: `tests/component/test_buy_tools.py`
  - Modules exercised: server, service, state, FakeBroker
- [x] **CT-6** — R1: verdict combinations through `propose_buy`: approve+approve and approve+reject(low) place; approve+reject(high) and reject+reject(low) block; each review is stored with its decision.
  - File: `tests/component/test_review_in_buy.py`
  - Modules exercised: server, service, review, state, FakeReviewer
- [x] **CT-7** — R2: a reviewer that times out, or a real `OpenAICompatReviewer` whose API returns invalid JSON or times out on both tries, blocks the buy with a reason naming that reviewer (exactly two requests sent); a reply asking for a size multiplier above 1 is an invalid verdict and blocks the same way; `sell` and `tighten_stop` still work.
  - File: `tests/component/test_review_in_buy.py`
  - Modules exercised: server, service, review (`OpenAICompatReviewer` over httpx MockTransport), FakeBroker
- [x] **CT-8** — R3: reviewer changes only make the order safer: stop raised to 92 with a 0.5 multiplier gives stop 92 qty 100; a raised stop never grows the size; a looser stop is ignored.
  - File: `tests/component/test_review_in_buy.py`
  - Modules exercised: server, service, review, risk, FakeBroker
- [x] **CT-9** — R4: the packet reviewers receive is server-built and holds every listed field (proposal, sized order, quote, indicators, 60 closes, ≤10 latest headlines, account exposure, SPY regime); its hash, both raw verdicts, the decision and the final order are stored and linked from the journal entry.
  - File: `tests/component/test_review_in_buy.py`
  - Modules exercised: server, service, review, state, journal
- [x] **CT-10** — H1: equity −4.1% vs prior close blocks buys with `daily_halt`; still blocked after recovery to −3% the same day; clears the next trading day; sells work while halted.
  - File: `tests/component/test_halts_and_state.py`
  - Modules exercised: server, service, risk, state, FakeBroker
- [x] **CT-11** — H2: peak 120k → equity 95.9k sets the kill switch through a tool call; it survives recovery and a server restart on the same state file; only `trader-broker reset-kill-switch` (CLI `main`) clears it and resets the peak, after which a buy is placed.
  - File: `tests/component/test_halts_and_state.py`
  - Modules exercised: server, service, state, cli
- [x] **CT-12** — H3: with no state database, or one corrupted while the server runs, `propose_buy` blocks `state_unavailable` while reads, `sell` and `tighten_stop` work; `init-state` refuses to replace the corrupt file without `--force` and replaces it with it; `status` reports the state as unavailable.
  - File: `tests/component/test_halts_and_state.py`
  - Modules exercised: server, service, state, cli
- [x] **CT-13** — E1: `close_position` on a filled bracket cancels both legs, sells every share with a market `day` order and leaves no open order for the symbol.
  - File: `tests/component/test_exit_tools.py`
  - Modules exercised: server, service, FakeBroker
- [x] **CT-14** — E2: a partial `sell` leaves the rest protected by one GTC stop at the previous stop, including a stop raised earlier with `tighten_stop`.
  - File: `tests/component/test_exit_tools.py`
  - Modules exercised: server, service, FakeBroker, state
- [x] **CT-15** — E3: selling more than held, a symbol not held, zero or negative shares is rejected; no order is sent and no short opens.
  - File: `tests/component/test_exit_tools.py`
  - Modules exercised: server, service, FakeBroker
- [x] **CT-16** — E4: `tighten_stop` raises 90 → 93 below the bid; lowering 90 → 88, equal, at the bid, above the bid and non-numeric values are rejected and the stop is unchanged.
  - File: `tests/component/test_exit_tools.py`
  - Modules exercised: server, service, FakeBroker
- [x] **CT-17** — E5: `cancel_entry_order` cancels this server's unfilled entry with its legs; a stop leg, a take-profit leg, a partly filled entry, a foreign order and an unknown id are `not_cancellable` and left open.
  - File: `tests/component/test_exit_tools.py`
  - Modules exercised: server, service, FakeBroker
- [x] **CT-18** — E6: with the kill switch and daily halt both set, and again with no state, `tighten_stop`, partial `sell`, `close_position` and `cancel_entry_order` all succeed.
  - File: `tests/component/test_exit_tools.py`
  - Modules exercised: server, service, risk, state, FakeBroker
- [x] **CT-19** — K1: an unfilled entry is kept at 29 minutes and cancelled (with its legs) once older than 30 minutes, at the next write tool or `get_risk_status`.
  - File: `tests/component/test_housekeeping_and_journal.py`
  - Modules exercised: server, service, FakeBroker
- [x] **CT-20** — K2: a manually bought position gets a GTC stop 8% below average entry and is listed in `unprotected`; a filled bracket whose stop leg was cancelled gets a stop at the journal's stop; a second pass places nothing new; a bid already below the stop market-sells the shares and logs it. The stop-leg-only cancellation case (orphaned take-profit reserving every share) was a product bug; fixed in 6325800 and now a passing regression test.
  - File: `tests/component/test_housekeeping_and_journal.py`
  - Modules exercised: server, service, state, journal, FakeBroker
- [x] **CT-21** — K3: a foreign order, a foreign position, an entry whose stored sizing broke a cap and negative cash each record exactly one integrity violation across repeated passes; a bought position drifting past 20% records none.
  - File: `tests/component/test_housekeeping_and_journal.py`
  - Modules exercised: server, service, state, FakeBroker
- [x] **CT-22** — J1: full trade lifecycle over tools: buy, fill, `close_position` at +10% (the journal entry closes at the next housekeeping pass), settlement after SPY's +2% close is published gives alpha +8% in `get_journal(status="pending_lessons")`; `journal_note` stores the lesson and removes it from that list.
  - File: `tests/component/test_housekeeping_and_journal.py`
  - Modules exercised: server, service, journal, state, FakeBroker
- [x] **CT-23** — S1: tool parameters expose nothing that sets caps, mode, kill switch or credentials, and an extra argument naming one is refused by every write tool.
  - File: `tests/component/test_tool_surface.py`
  - Modules exercised: server, service

- [x] **CT-24** — J2: `trader-broker report` and `report --json` (CLI `main`) over a state file written by the server's own tool calls (a review block, a `min_price` block, a buy placed, sold at +10% and settled against SPY at +2%) give the same PASS/FAIL per criterion, blocks by reason and per-reviewer verdict counts and hit rates; `report` without a state database exits 1 and creates nothing.
  - File: `tests/component/test_operator_report.py`
  - Modules exercised: server, service, review, state, journal, report, cli

## Integration Tests

- [x] **IT-1** — S1/S2/S5: `trader-broker serve` started as a real process over stdio lists exactly the 16 tools with their read-only hints, runs in paper mode by default with its log under `<state_dir>/paper/`, and writes the same JSON log lines to stderr and the log file, none containing a configured secret (one is passed in a tool argument); no state database is created.
  - File: `tests/integration/test_stdio_server.py`
  - Infrastructure: subprocess + FastMCP stdio client; dummy keys, no network call made
- [x] **IT-2** — S2: `serve` with `TRADER_BROKER_MODE=live` and no or a wrong `TRADER_BROKER_LIVE_ACK` exits non-zero with a message naming the variable and creates no state or log file.
  - File: `tests/integration/test_stdio_server.py`
  - Infrastructure: subprocess
- [x] **IT-3** — F2: after a real install, `--dry-run` against the installed profile with a locally edited file reports the replacement, changes no file and creates no job; every run prints the `profile_routes` snippet verbatim.
  - File: `tests/profile/test_profile_files.py` (beside the existing installer tests and their stubs)
  - Infrastructure: bash + stub `hermes`/`uv` on PATH, temp HOME
- [x] **IT-4** — P1 — DEFERRED TO USER: a 1-share far-below-market bracket buy is accepted with all prices, found by client order id and cancelled. **Not runnable here: requires Alpaca paper keys.** Run: `ALPACA_API_KEY=... ALPACA_SECRET_KEY=... uv run pytest -m paper` and `uv run trader-broker smoke-test`
  - File: `tests/smoke/test_alpaca_paper.py` (existing)
  - Infrastructure: Alpaca paper account
- [x] **IT-5** — P2 — DEFERRED TO USER: IEX quote and ≥20 SIP-history daily bars for a liquid symbol. **Not runnable here: requires Alpaca paper keys.** Same commands as IT-4.
  - File: `tests/smoke/test_alpaca_paper.py` (existing)
  - Infrastructure: Alpaca paper account
- [x] **IT-6** — F3 — DEFERRED TO USER: on the server, run the installer, `init-state`, `smoke-test`, apply the routes snippet, and check a manual `stock-trader` run can call `mcp_trader_broker_get_risk_status` but has no terminal, code or file tools. **Not runnable here: requires a Hermes v2026.9.24 install and the user's keys.** Follow the README install checklist: `scripts/install-profile.sh --model <claude-model-id>`, then `set -a; . ~/.hermes/profiles/stock-trader/.env; set +a; uv run trader-broker init-state && uv run trader-broker smoke-test`, then work through the README "Install checklist" (`hermes -p stock-trader chat`, ask for the risk status, then ask it to run a shell command or read a file; it must have no tool for either). Note: the README names the tool `mcp__trader_broker__get_risk_status`; the test plan writes `mcp_trader_broker_get_risk_status`; confirm which Hermes shows.
  - Infrastructure: user's Hermes server
- [x] **IT-7** — F4 — DEFERRED TO USER: a gated job on a market holiday is skipped without waking the model; on a trading day a job posts to the routed Discord and Telegram channel. **Not runnable here: requires Hermes cron and the user's chat routes.** On a market holiday or weekend run `~/.hermes/profiles/stock-trader/scripts/market-open-gate.sh` (last line must be `{"wakeAgent": false}`) and check in `hermes -p stock-trader cron list` that a resumed gated job did not run; on a trading day resume one job (`hermes -p stock-trader cron resume <job id>`) and check its summary arrives in the routed Discord channel and Telegram group.
  - Infrastructure: user's Hermes server, Discord and Telegram
- [x] **IT-8** — D1 — DEFERRED TO USER, manual read: `docs/system/architecture.md` describes the built system and cites the confirmed Hermes toolset identifiers with their source; `README.md` holds the setup guide and install checklist; `CLAUDE.md` imports `@AGENTS.md`. Read the three files. Spot check here: `CLAUDE.md` is `@AGENTS.md`; `architecture.md` cites `website/docs/reference/toolsets-reference.md` at tag v2026.9.24; README has the "Install checklist".
  - Infrastructure: none (reading)

## Covered by existing tests

- **S3** (market gate: trading day 09:00, holiday, weekend, early close at 15:45, API error, exit 0): `tests/unit/test_cli.py::test_market_open_gate_*` drive `main(["market-open-gate"])` for exactly these cases; `tests/profile/test_profile_files.py::test_gate_*` run the gate script.
- **S4** (broker boundary): `tests/unit/test_boundaries.py` (AST import scan, runtime Protocol check for `FakeBroker`); `AlpacaBroker` is checked statically by `uv run mypy src`.
- **S6** (CI): `.github/workflows/ci.yml` runs exactly the gate commands; all four were run here (see Validation).
- **F1** (profile files) and the rest of **F2** (first install, paused/enabled jobs, `.env` kept, no duplicate jobs, default profile never written): `tests/profile/test_profile_files.py`.

## How to Run

```bash
# Component tests
uv run pytest tests/component

# Integration tests (local: real server process; installer with stubs)
uv run pytest tests/integration tests/profile

# Alpaca paper integration (needs ALPACA_API_KEY, ALPACA_SECRET_KEY; deferred to user)
uv run pytest -m paper

# All tests, as CI runs them
uv run pytest -m "not paper"
```

## Validation

- [x] All component tests pass
- [x] All integration tests pass (or N/A if not supported) — local ones pass; IT-4..IT-8 deferred to the user (paper keys / Hermes server)
- [x] All existing unit tests still pass (no regressions)
- [x] Build passes — `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy src` clean; `uv run pytest -m "not paper"`: 550 passed, 1 deselected (paper) after fix commit 6325800
