# Task Spec: Hermes Trader Profile

> **Source:** `.sdlc/specs/hermes-trader-profile/master-plan.md`
> **Created:** 2026-11-03
> **Last Updated:** 2026-11-03
> **Progress:** 0 / 102 sub-tasks complete

**Commands used in every Validation section** (the repo has no toolchain yet; Task 1.1 creates it, and the orchestrator re-runs workspace setup after Task 1.1 so `.sdlc/workspace.md` records them):

| Purpose | Command |
|---|---|
| Install | `uv sync` |
| Lint | `uv run ruff check .` |
| Format | `uv run ruff format --check .` |
| Type-check | `uv run mypy src` |
| Unit tests | `uv run pytest -m "not paper"` (no network) |
| Paper tests | `uv run pytest -m paper` (needs the user's Alpaca paper keys; not run by the orchestrator) |

"Gate commands" below means all five commands from Install to Unit tests, each exiting 0.

---

## Phase 1: Toolchain and Server Core

### Task 1.1: Toolchain, settings and CI

Create the Python 3.12 + uv project, the `Settings` model with the paper/live guard, and the CI workflow. Every later task builds on this.

**Modules:** `.`
**Depends on:** none

**Files:**

```
pyproject.toml                    (create)
uv.lock                           (create)
.python-version                   (create)
.gitignore                        (create)
src/trader_broker/__init__.py     (create)
src/trader_broker/config.py       (create)
tests/conftest.py                 (create)
tests/unit/test_config.py         (create)
.github/workflows/ci.yml          (create)
```

**Sub-tasks:**

- [ ] **1.1.1** — Write `pyproject.toml`: uv project with a `src/` layout, `requires-python = ">=3.12"`, runtime deps `fastmcp>=3.4,<4`, `alpaca-py`, `openai`, `pydantic>=2`, `httpx`; dev dependency group `pytest`, `ruff`, `mypy`, `pyyaml`, `types-PyYAML`; script entry `trader-broker = "trader_broker.cli:main"`; pytest config that registers the `paper` marker with `--strict-markers`; ruff config; mypy config (strict for `src/`, missing-import overrides only for third-party packages without type stubs). Run `uv sync` to create `uv.lock`.
- [ ] **1.1.2** — Write `.python-version` (`3.12`) and `.gitignore` (`.venv`, `__pycache__`, `*.db`, `.env`, `state/`).
- [ ] **1.1.3** — In `config.py`, define `Settings` (pydantic) loaded from environment variables. Fields and defaults: `TRADER_BROKER_MODE` (`paper`), `TRADER_BROKER_LIVE_ACK`, `TRADER_BROKER_STATE_DIR` (`~/.local/share/trader-broker`), `TRADER_RISK_PCT` (0.03), `TRADER_SYMBOL_CAP_PCT` (0.20), `TRADER_DAILY_LOSS_PCT` (0.04), `TRADER_KILL_DRAWDOWN_PCT` (0.20), `TRADER_MIN_PRICE` (5), `TRADER_MIN_ADV_USD` (20000000), `TRADER_ENTRY_TTL_MINUTES` (30), `TRADER_ENTRY_SLIPPAGE_PCT` (0.005), `REVIEWER_TIMEOUT_S` (60), `ALPACA_API_KEY`, `ALPACA_SECRET_KEY`. Add the three configurable limits the plan names without env names, using the `TRADER_` prefix: no-entry-before-close minutes (30), minimum stop distance (0.005 of entry), maximum stop distance (0.25 of entry). Expose the state DB path `<state_dir>/<mode>/state.db` and the log path `<state_dir>/<mode>/logs/trader-broker.log`.
- [ ] **1.1.4** — Add reviewer settings for `REVIEWER_1_*` and `REVIEWER_2_*` (`PROVIDER`, `BASE_URL`, `MODEL`, `API_KEY_ENV`). Defaults: reviewer 1 provider `openai`, base URL `https://api.openai.com/v1`, key env `OPENAI_API_KEY`; reviewer 2 provider `xai`, base URL `https://api.x.ai/v1`, key env `XAI_API_KEY`. `MODEL` has no default; a check used by `serve` fails when a model id or the named API key is missing.
- [ ] **1.1.5** — Add the live guard: mode must be `paper` or `live`; `live` loads only when `TRADER_BROKER_LIVE_ACK` equals `I_ACCEPT_REAL_MONEY_RISK`; otherwise raise a configuration error whose message names both variables. Validate that all `*_PCT` values are in (0, 1).
- [ ] **1.1.6** — Create `tests/conftest.py` with an autouse fixture that removes every `TRADER_*`, `REVIEWER_*`, `ALPACA_*`, `OPENAI_API_KEY`, `XAI_API_KEY` variable from the test environment. Write `tests/unit/test_config.py`: defaults load in paper mode; live without ACK fails; live with a wrong ACK fails; live with the exact ACK loads; unknown mode fails; state DB path contains the mode; env overrides parse; missing reviewer model fails the `serve` check.
- [ ] **1.1.7** — Write `.github/workflows/ci.yml`: on push and pull request, install uv, run `uv sync`, `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy src`, `uv run pytest -m "not paper"`.

**Validation:**

- [ ] **Build:** Gate commands exit 0.
- [ ] **Tests:** `tests/unit/test_config.py` passes and covers defaults, all three live-guard cases, unknown mode, mode in state path, env overrides and the missing reviewer model check.
- [ ] **Requirements:**
    - [ ] "Default `TRADER_BROKER_MODE=paper`. Live mode requires `TRADER_BROKER_MODE=live` AND `TRADER_BROKER_LIVE_ACK=I_ACCEPT_REAL_MONEY_RISK`; otherwise the server refuses to start with a clear error."
    - [ ] "State DB path includes the mode (`<state_dir>/<mode>/state.db`)."
    - [ ] Reviewers configured by env with default providers/base URLs; "model ids required from env".
    - [ ] SHOULD: "CI workflow runs `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy src`, `uv run pytest -m "not paper"`."
- [ ] **Verify:** With `TRADER_BROKER_MODE=live` and no ACK set, a one-line `uv run python -c` command that loads `Settings` raises an error naming `TRADER_BROKER_LIVE_ACK`. Then re-run workspace setup so `.sdlc/workspace.md` records the uv commands.

---

### Task 1.2: Models, Broker Protocol, FakeBroker and indicators

Define the shared data types, the `Broker` Protocol, the scriptable in-memory `FakeBroker` used by every unit test, and the pure indicator functions.

**Modules:** `.`
**Depends on:** 1.1

**Files:**

```
src/trader_broker/models.py           (create)
src/trader_broker/brokers/__init__.py (create)
src/trader_broker/brokers/base.py     (create)
src/trader_broker/brokers/fake.py     (create)
src/trader_broker/indicators.py       (create)
tests/conftest.py                     (modify)
tests/unit/test_indicators.py         (create)
tests/unit/test_boundaries.py         (create)
```

**Sub-tasks:**

- [ ] **1.2.1** — In `models.py`, define `Account`, `Position`, `Order`, `Quote`, `Bar`, `NewsItem`, `Clock`, `BracketOrderRequest`, `Proposal`, `SizedOrder`, `RiskDecision`, `Modifications`, `ReviewVerdict`, `ReviewOutcome`, `JournalEntry`, `Asset`, `Movers`, `EquityPoint`, `Fill`, `TradingDay`. Use `Decimal` for prices. Match the master plan Interfaces exactly for `BracketOrderRequest` (frozen, `time_in_force="gtc"`), `Proposal` (thesis 1..2000 chars, invalidation 1..500 chars, horizon_days 1..60, optional `limit_price`), `ReviewVerdict` (reasons 1..5), `Modifications` (`stop_price`, `size_multiplier` in (0, 1], advisory `target_price`) and `RiskDecision` (failure rule ids `long_only`, `leverage`, `cash`, `symbol_cap`, `risk_size`, `min_price`, `min_adv`, `market_closed`, `too_late`, `daily_halt`, `kill_switch`, `stop_invalid`, `target_invalid`, `qty_zero`, `not_tradable`, `state_unavailable`). `Order` carries id, client order id, symbol, side, type, order class, status, qty, filled qty, limit/stop prices, time in force, parent id or legs, and created time.
- [ ] **1.2.2** — In `brokers/base.py`, define the runtime-checkable `Broker` Protocol with exactly the methods and signatures in the master plan Interfaces. Define broker error types the service needs: a general broker error, a rate-limit error, and an unknown-result error for a write whose outcome is not known (timeout).
- [ ] **1.2.3** — In `brokers/fake.py`, implement `FakeBroker`: in memory, deterministic, scriptable. Tests can set account, positions, assets, quotes, daily and intraday bars, news, movers, clock, calendar, portfolio history and fills, and inject the current time. `submit_bracket_buy` creates a parent entry plus a stop leg and a take-profit leg. Provide test helpers to fill an entry (fully or partly), trigger a stop, and move prices.
- [ ] **1.2.4** — Add scripted failures to `FakeBroker`: raise a broker error on the next call to a named method, raise a rate-limit error, and "accept then time out" (store the order, then raise the unknown-result error). Enforce Alpaca's reserved-quantity rule: reject a sell that exceeds position qty minus qty reserved by open sell orders.
- [ ] **1.2.5** — In `indicators.py`, write pure functions: SMA(n), RSI14, ATR14, percent from 52-week high, and average dollar volume over N daily bars (close × volume).
- [ ] **1.2.6** — Add `conftest.py` fixtures: a `FakeBroker` preset with a $100k paper-style account, a liquid symbol `XYZ` priced above $5 with ADV above $20M, an open market clock, and a fixed "now".
- [ ] **1.2.7** — Write `test_indicators.py` with hand-computed values for each function, including short-input behavior. Write `test_boundaries.py`: `FakeBroker()` is an instance of `Broker` at runtime; an AST scan of every `.py` file under `src/trader_broker/` outside `brokers/` finds no import of the `alpaca` package.

**Validation:**

- [ ] **Build:** Gate commands exit 0.
- [ ] **Tests:** `test_indicators.py` covers every indicator with known values; `test_boundaries.py` covers the FakeBroker runtime Protocol check and the `alpaca` import scan.
- [ ] **Requirements:**
    - [ ] "Implement the `Broker` Protocol and keep all broker-specific code inside `brokers/`."
    - [ ] "`risk.py`, `review.py`, `service.py`, `journal.py`, `report.py` import nothing from `alpaca`; a test greps imports to enforce it."
    - [ ] "`FakeBroker` enforces Alpaca's reserved-quantity rule: a sell exceeding `qty - qty reserved by open sell orders` is rejected."
- [ ] **Verify:** `grep -rn "alpaca" src/trader_broker --include=*.py | grep -v "src/trader_broker/brokers/"` prints no import lines.

---

### Task 1.3: Risk gate and sizing

Implement `risk.py` as pure functions: share sizing, entry checks, and halt checks. No I/O.

**Modules:** `.`
**Depends on:** 1.2

**Files:**

```
src/trader_broker/risk.py      (create)
tests/unit/test_sizing.py      (create)
tests/unit/test_risk.py        (create)
tests/unit/test_halts.py       (create)
```

**Sub-tasks:**

- [ ] **1.3.1** — Define `Caps` built from `Settings` (risk %, symbol cap %, daily loss %, kill drawdown %, min price, min ADV, entry slippage %, no-entry-before-close minutes, min and max stop distance).
- [ ] **1.3.2** — Write the entry price rule: the proposal's limit price when given, else ask × (1 + entry_slippage_pct). Write one price-rounding helper that rounds to $0.01 for prices ≥ $1; use it for entry, stop and target.
- [ ] **1.3.3** — Write `size_position()`: `qty = floor(min(risk_pct*equity/(entry-stop), (symbol_cap_pct*equity - existing_symbol_value)/entry, available_cash/entry))`, with `existing_symbol_value` = position market value + notional of open buy orders for that symbol and `available_cash` = cash − open buy order notional (all symbols). Reject `stop >= entry` (`stop_invalid`), `target <= entry + 0.01` (`target_invalid`), stop distance below the minimum or above the maximum (`stop_invalid`, reason text names the distance rule), and `qty < 1` (`qty_zero`). Return a `SizedOrder` with qty, entry, stop, target, notional, % equity, $ risk, % equity risk, and which limit bound the size.
- [ ] **1.3.4** — Write `check_entry()` returning a `RiskDecision` that lists every failing rule, not only the first: `leverage` (positions market value + open buy notional + new notional > equity), `cash` (new notional > available cash), `symbol_cap`, `risk_size`, `min_price`, `min_adv` (from SIP daily bars ending at the previous close, passed in), `market_closed` (clock not open), `too_late` (less than `no_entry_before_close_minutes` before `next_close`), `not_tradable` (asset missing, inactive, not tradable or not a US equity).
- [ ] **1.3.5** — Write `check_halts()`: given account equity, broker `last_equity`, the stored peak, kill switch, daily halt date and today's trading date, return the new peak (`max(peak, equity)`), whether to set `kill_switch` (`equity <= peak*(1-kill_drawdown_pct)`), whether to set `daily_halt_date = today` (`(equity-last_equity)/last_equity <= -daily_loss_pct`), and the blocking failures (`daily_halt` while `daily_halt_date == today`, `kill_switch` while set). It never clears the kill switch.
- [ ] **1.3.6** — Write `test_sizing.py` with the master plan rows: 20% cap binds over 3% risk (equity 100k, entry 100, stop 90 → 200); tight stop (100/99 → 200, risk $200); wide stop (50/40 → 300); cash binds (cash 5k, 100/95 → 50); existing $15k position (→ max add 50); open $15k entry order on the same symbol (→ max add 50); open buy orders on other symbols reduce available cash; `stop_invalid`, `target_invalid` (entry 100, target 100), stop distance 0.4% and 26%, `qty_zero`.
- [ ] **1.3.7** — Write `test_risk.py`: hand-built `SizedOrder` with positions 95k + pending 3k + new 5k → `leverage`; price 4.99 → `min_price`; ADV $5M → `min_adv`; 20 minutes before close → `too_late`; closed clock → `market_closed`; inactive asset → `not_tradable`; a case with three failing rules names all three. Write `test_halts.py`: last_equity 100k, equity 95.9k → daily halt set; halt date equal to today still blocks at −3%; next trading date clears it; peak 120k, equity 95.9k → kill switch set; peak rises with equity; kill switch stays set when equity recovers.

**Validation:**

- [ ] **Build:** Gate commands exit 0.
- [ ] **Tests:** `test_sizing.py`, `test_risk.py`, `test_halts.py` pass with every scenario in sub-tasks 1.3.6 and 1.3.7.
- [ ] **Requirements:**
    - [ ] "Size every buy in code" with the exact formula; defaults `risk_pct=0.03`, `symbol_cap_pct=0.20`; whole shares only.
    - [ ] "Enforce entry caps in `risk.check_entry` and return a typed `RiskDecision` with every failing rule named."
    - [ ] "Enforce halts in `risk.check_halts`" (daily halt latch, kill switch latch).
    - [ ] Prices "rounded to $0.01 (≥$1)".
- [ ] **Verify:** `grep -nE "^(import|from) " src/trader_broker/risk.py` shows only standard library, pydantic and `trader_broker` imports (no I/O modules such as `sqlite3`, `httpx`, `openai`).

---

### Task 1.4: State store

Implement the SQLite store in `state.py`. It fails closed: a missing or corrupt file is reported as unavailable and is never re-created silently.

**Modules:** `.`
**Depends on:** 1.2

**Files:**

```
src/trader_broker/state.py     (create)
tests/conftest.py              (modify)
tests/unit/test_state.py       (create)
```

**Sub-tasks:**

- [ ] **1.4.1** — Define the schema at `<state_dir>/<mode>/state.db`: a single-row settings table (`equity_peak`, `kill_switch`, `daily_halt_date`, `paper_start_date`, schema version); `orders` (`client_order_id` primary key, broker order id, symbol, side, role such as entry/stop/take-profit/sell/repair, status, at-placement sizing snapshot, journal entry link, created time); `journal`; `reviews` (packet hash, both raw verdicts, decision, final order, journal link); `blocks` (time, tool, symbol, reasons) for the report and `get_risk_status`; `integrity_violations` (time, kind, subject, detail).
- [ ] **1.4.2** — Write open semantics: opening a missing, unreadable or schema-invalid file raises a state-unavailable error and creates nothing. Write `init_state(path, equity, force=False)`: refuses when a DB exists unless `force`; sets `paper_start_date` to today and `equity_peak` to the given equity.
- [ ] **1.4.3** — Write store methods: record an order intent (client id + sizing) before submission; update broker id and status; look up by client id; read and write peak, kill switch and daily halt date; `reset_kill_switch(current_equity)` clears the switch and sets the peak to current equity; insert and query journal rows; insert reviews; insert and list recent blocks; insert integrity violations with de-duplication on (kind, subject) and count them.
- [ ] **1.4.4** — Add a `conftest.py` fixture that creates an initialized temp state DB, and one that points at a missing DB.
- [ ] **1.4.5** — Write `test_state.py`: missing file → unavailable and no file created; corrupt file → unavailable; `init_state` refuses an existing DB, succeeds with `force`; peak, kill switch and halt date survive close and reopen; `reset_kill_switch` clears and resets the peak; an order intent exists before its status update; the same integrity violation recorded twice counts once.

**Validation:**

- [ ] **Build:** Gate commands exit 0.
- [ ] **Tests:** `test_state.py` passes with every case in 1.4.5.
- [ ] **Requirements:**
    - [ ] "`state.py` — SQLite store: equity_peak, kill_switch, daily_halt_date, paper_start_date, orders (client_order_id idempotency), journal, reviews".
    - [ ] "State DB missing/corrupt → Fail closed"; "`trader-broker init-state` (refuses if a DB exists unless `--force`)".
    - [ ] "`reset-kill-switch` … clears it and resets the peak to current equity."
- [ ] **Verify:** Run the test suite, then confirm no `state.db` file was created outside pytest temp directories (`find . -name "*.db" -not -path "./.venv/*"` prints nothing).

---

## Phase 2: Alpaca Adapter

### Task 2.1: AlpacaBroker

Implement the `Broker` Protocol with `alpaca-py`. This is the only place that imports `alpaca`.

**Modules:** `.`
**Depends on:** 1.2

**Files:**

```
src/trader_broker/brokers/alpaca.py   (create)
tests/unit/test_alpaca_adapter.py     (create)
```

**Sub-tasks:**

- [ ] **2.1.1** — Build `AlpacaBroker` from `Settings`: `TradingClient` (paper flag from mode), `StockHistoricalDataClient`, `NewsClient`, `ScreenerClient`.
- [ ] **2.1.2** — Implement the read methods and map SDK objects to `models.py` types: account (`equity`, `last_equity`, `cash`, `buying_power`), positions, orders including bracket legs, `get_order_by_client_id` (not found → `None`), asset, quote (IEX feed), `get_daily_bars` (SIP feed with the end at the previous close when `sip_history=True`), `get_bars`, news, movers, clock, calendar, portfolio history, fills.
- [ ] **2.1.3** — Implement the write methods: `submit_bracket_buy` (order class bracket, limit entry, `gtc`, take-profit limit, stop-loss stop, whole-share qty, client order id), `submit_sell` (market, `day`), `submit_stop` (`gtc`), `replace_stop`, `cancel_order`. Send prices rounded to $0.01 in the format Alpaca accepts (strings, per `POST /v2/orders`).
- [ ] **2.1.4** — Map SDK and HTTP errors to the broker error types from Task 1.2. A write timeout raises the unknown-result error. Reads retry on HTTP 429 three times with 1 s, 2 s, 4 s backoff; writes never retry.
- [ ] **2.1.5** — Add a static check that `AlpacaBroker` satisfies `Broker`, so `uv run mypy src` fails if a signature drifts.
- [ ] **2.1.6** — Write `test_alpaca_adapter.py` with stubbed SDK client objects (no network): bracket request fields and price strings; a 429 on a read retries with patched sleep and then succeeds; a 429 on a write is not retried; a write timeout raises unknown-result; client id not found → `None`.

**Validation:**

- [ ] **Build:** Gate commands exit 0 (mypy covers the Protocol conformance check).
- [ ] **Tests:** `test_alpaca_adapter.py` passes with every case in 2.1.6; `test_boundaries.py` still passes.
- [ ] **Requirements:**
    - [ ] "`FakeBroker` and `AlpacaBroker` both satisfy the Protocol (mypy check + runtime test for FakeBroker)."
    - [ ] "Place every buy as a bracket order: entry limit … with `time_in_force=gtc`, take-profit limit at target, stop-loss stop at stop."
    - [ ] "Prices rounded to $0.01 (≥$1) and sent in the broker's accepted format."
    - [ ] "Alpaca 429 rate limit → Backoff retry ×3 (1s,2s,4s) for reads; writes go through client-id lookup path."
- [ ] **Verify:** `grep -rln "^from alpaca\|^import alpaca" src/` lists only `src/trader_broker/brokers/alpaca.py`.

---

### Task 2.2: Paper smoke test

Add the opt-in paper test and the `smoke-test` CLI subcommand that run the same checks against the user's Alpaca paper account.

**Modules:** `.`
**Depends on:** 2.1

**Files:**

```
src/trader_broker/cli.py              (create)
tests/smoke/test_alpaca_paper.py      (create)
```

**Sub-tasks:**

- [ ] **2.2.1** — Create `cli.py` with `main()` and an argparse subcommand structure. Add `smoke-test [--symbol SYMBOL]` (default a liquid symbol priced ≥ $5): it refuses to run unless mode is `paper`, then runs the smoke steps and prints PASS/FAIL per step; exit code 0 only when all pass.
- [ ] **2.2.2** — Write the smoke steps against the `Broker` Protocol: submit a 1-share bracket buy with a limit far below market (will not fill) and valid stop and target; confirm Alpaca accepted all prices; confirm `get_order_by_client_id` finds it; cancel it and confirm the cancel; fetch an IEX quote; fetch SIP-history daily bars (at least 20 bars).
- [ ] **2.2.3** — Write `tests/smoke/test_alpaca_paper.py` marked `@pytest.mark.paper`, skipped unless `ALPACA_API_KEY` and `ALPACA_SECRET_KEY` are set and mode is `paper`; it calls the same smoke steps and asserts each one.
- [ ] **2.2.4** — Make the smoke steps always cancel the test order in a `finally` path, even when a later assertion fails.

**Validation:**

- [ ] **Build:** Gate commands exit 0.
- [ ] **Tests:** `uv run pytest -m "not paper"` passes and does not run the paper test; `uv run pytest -m paper` with no keys reports the test as skipped.
- [ ] **Requirements:**
    - [ ] "`tests/smoke/test_alpaca_paper.py` places a bracket buy of 1 share of a liquid ≥$5 symbol with a far-below-market limit (won't fill), verifies acceptance of all prices, verifies `get_order_by_client_id`, then cancels it; also verifies IEX quote and SIP-history daily bars fetch."
    - [ ] "@pytest.mark.paper; skipped unless ALPACA_API_KEY/SECRET set and mode=paper".
- [ ] **Verify:** `uv run pytest -m paper -rs` with the Alpaca variables unset prints a skip reason naming the missing keys; `TRADER_BROKER_MODE=live TRADER_BROKER_LIVE_ACK=I_ACCEPT_REAL_MONEY_RISK uv run trader-broker smoke-test` exits non-zero without contacting Alpaca.

---

## Phase 3: Trader Service

### Task 3.1: Housekeeping, equity refresh and risk status

Create `TraderService` with the equity refresh that runs on every tool call and the housekeeping pass that runs before every write tool and `get_risk_status`.

**Modules:** `.`
**Depends on:** 1.3, 1.4

**Files:**

```
src/trader_broker/service.py        (create)
tests/conftest.py                   (modify)
tests/unit/test_housekeeping.py     (create)
```

**Sub-tasks:**

- [ ] **3.1.1** — Create `TraderService` over a `Broker`, the state store (or a state-unavailable marker), `Caps`/`Settings`, a list of reviewers and an injectable time source. Add a `conftest.py` fixture that builds it over `FakeBroker` and a temp state DB.
- [ ] **3.1.2** — Write the equity refresh, run on every tool call (read and write): fetch the account, call `check_halts`, persist the new peak, set `kill_switch` and `daily_halt_date` when triggered. When state is unavailable, skip persistence and do not fail the call.
- [ ] **3.1.3** — Housekeeping step 1: cancel trader-broker entry orders (client id prefix `tb-`) that are not fully filled and are older than `entry_ttl_minutes`. A partly filled entry keeps its legs for the filled qty (Alpaca behavior); only the unfilled remainder is cancelled.
- [ ] **3.1.4** — Housekeeping step 2: for each position, find the qty not covered by an open stop order. For uncovered qty, choose the stop price: the open journal entry's recorded stop, else 8% below average entry. If the current bid is at or below that stop, market-sell the uncovered qty and log it; otherwise submit a GTC stop with a `tb-` client id recorded in state first. Collect these positions for the `unprotected` list.
- [ ] **3.1.5** — Housekeeping step 3: reconcile state orders with broker orders by client order id and update statuses. Record an `integrity_violation` only for: an order or position trader-broker did not place; an entry whose stored at-placement sizing broke a cap; negative cash. Price drift that pushes a position past 20% of equity is not a violation. Skip state writes when state is unavailable, but still run steps 1 and 2.
- [ ] **3.1.6** — Write `get_risk_status()`: run housekeeping, then return mode, equity, last_equity, day P&L %, peak, drawdown %, kill_switch, daily_halt, exposure %, cash, per-symbol % of equity, unprotected positions, open entry orders and recent blocks.
- [ ] **3.1.7** — Write `test_housekeeping.py`: entry 31 minutes old cancelled, 29 minutes old kept; partly filled entry past TTL has its remainder cancelled; unprotected position gets a GTC stop at the journal stop; position without a journal stop gets a stop 8% below average entry; bid below the recorded stop → market sell, logged; position grown to 25% of equity → no violation; foreign order → one violation after two housekeeping runs; negative cash → violation; equity refresh raises the peak; daily halt stays set when equity recovers from −4.1% to −3% the same day; kill switch stays set after equity recovers; state unavailable → stops still placed and no exception.

**Validation:**

- [ ] **Build:** Gate commands exit 0.
- [ ] **Tests:** `test_housekeeping.py` passes with every case in 3.1.7.
- [ ] **Requirements:**
    - [ ] "Run housekeeping at the start of every write tool and `get_risk_status`" — cancel stale entries, give unprotected positions a stop, reconcile.
    - [ ] "record an `integrity_violation` row only for: an order or position trader-broker did not place; an entry whose at-placement sizing … broke a cap; negative cash (margin use). Price drift that pushes a position past 20% is not a violation (test)."
    - [ ] "on every tool call update `equity_peak = max(equity_peak, equity)`"; daily halt and kill switch persist.
    - [ ] SHOULD: `get_risk_status` returns the listed fields.
- [ ] **Verify:** Read `service.py` and confirm housekeeping order is: cancel stale entries, repair unprotected positions, reconcile; and that the repair path writes the client id to state before calling `submit_stop`.

---

### Task 3.2: Exit tools

Implement `sell`, `close_position`, `tighten_stop` and `cancel_entry_order`. Halts, the kill switch and missing state never block them.

**Modules:** `.`
**Depends on:** 3.1

**Files:**

```
src/trader_broker/service.py        (modify)
tests/unit/test_service_sell.py     (create)
tests/unit/test_tighten_stop.py     (create)
tests/unit/test_cancel_entry.py     (create)
```

**Sub-tasks:**

- [ ] **3.2.1** — Write `sell(symbol, qty)`: reject `qty < 1` and `qty` greater than held qty with `exceeds_position`. Record the current stop price. Cancel the symbol's open stop and take-profit legs and wait for cancel confirmation (bounded wait; on timeout return an error and submit nothing). Submit a market `day` sell with a `tb-` client id recorded first. For remaining shares, place a GTC stop at the previous stop price and record it; when there was no previous stop, use the housekeeping stop rule from 3.1.4.
- [ ] **3.2.2** — Write `close_position(symbol)` as a sell of the full held qty.
- [ ] **3.2.3** — Write `tighten_stop(symbol, new_stop)`: reject with a reason when the symbol has no active stop, when `new_stop` is not higher than the current stop, or when `new_stop` is at or above the current bid; otherwise replace each active stop for the symbol and update the journal stop.
- [ ] **3.2.4** — Write `cancel_entry_order(order_id)`: cancel only an open buy entry with `filled_qty == 0` and client id prefix `tb-`; reject stop legs, take-profit legs, partly filled parents and foreign orders with `not_cancellable`.
- [ ] **3.2.5** — Write `test_service_sell.py`: full close; partial sell leaves remaining shares re-protected at the same stop; sell with no legs; `exceeds_position`; selling without first cancelling legs is rejected by `FakeBroker` (reserved-qty rule) so the service path must cancel first; sell succeeds while kill switch is set, daily halt is set, and state is missing.
- [ ] **3.2.6** — Write `test_tighten_stop.py`: 90 → 88 rejected; new stop ≥ bid rejected; no stop rejected; 90 → 95 accepted and journal stop updated; accepted while kill switch is set. Write `test_cancel_entry.py`: own unfilled entry cancelled; stop leg, take-profit leg, partly filled parent and foreign order each rejected `not_cancellable`.

**Validation:**

- [ ] **Build:** Gate commands exit 0.
- [ ] **Tests:** `test_service_sell.py`, `test_tighten_stop.py`, `test_cancel_entry.py` pass with every case in 3.2.5 and 3.2.6.
- [ ] **Requirements:**
    - [ ] "Make `sell(symbol, qty)` and `close_position(symbol)` work while bracket legs reserve the shares" — cancel legs, market `day` sell, re-protect remaining shares at the previous stop.
    - [ ] "Reject `qty` > held qty with `exceeds_position` (no shorts)."
    - [ ] "`cancel_entry_order(order_id)` cancels only an open, unfilled (filled_qty == 0) buy entry order placed by trader-broker … reject … with `not_cancellable`."
    - [ ] "`tighten_stop(symbol, new_stop)` only raises an existing stop and keeps it below the current bid."
    - [ ] "Halts never block `sell`, `close_position`, `tighten_stop`, `cancel_entry_order`."
- [ ] **Verify:** `grep -n "kill_switch\|daily_halt\|state_unavailable" src/trader_broker/service.py` shows no check inside the four exit methods.

---

### Task 3.3: Cross-model review module

Implement `review.py`: the server-built review packet, the OpenAI-compatible reviewers, and the `decide()` rule.

**Modules:** `.`
**Depends on:** 1.3

**Files:**

```
src/trader_broker/review.py            (create)
tests/conftest.py                      (modify)
tests/unit/test_review_packet.py       (create)
tests/unit/test_review_decision.py     (create)
```

**Sub-tasks:**

- [ ] **3.3.1** — Write `build_packet()` from data the service passes in: proposal (symbol, stop, target, thesis, invalidation, horizon_days); sized order (qty, notional, % equity, $ risk, % equity risk); last quote; indicators from about 260 daily bars (close, SMA20, SMA50, SMA200, RSI14, ATR14, % from 52-week high, 20-day ADV) and the last 60 closes; up to 10 latest headlines with timestamps; account (equity, cash, exposure %, positions with % equity); regime snapshot (SPY close vs SMA50 and SMA200). Serialize it deterministically and compute a SHA-256 packet hash.
- [ ] **3.3.2** — Define the `Reviewer` Protocol (name, `review(packet) -> ReviewOutcome`, where the outcome is a validated `ReviewVerdict` or an error with a message).
- [ ] **3.3.3** — Write `OpenAICompatReviewer` with the `openai` SDK: base URL and model from settings, API key read from the env var named by `REVIEWER_n_API_KEY_ENV`, chat completions with JSON output, a system prompt that states the reviewer role and the verdict schema. Validate the reply with pydantic. Timeout `reviewer_timeout_s`; one retry on error or invalid JSON; a second failure gives an `error` outcome. A returned target change is kept as advisory only.
- [ ] **3.3.4** — Write a function that runs both reviewers concurrently and returns both outcomes.
- [ ] **3.3.5** — Write `decide()`: any `error` → block; any `reject` with `high` confidence → block; zero `approve`/`modify` → block; else `new_stop = max(proposal stop, modified stops)` (a modified stop ≥ entry is ignored), `mult = min(size multipliers, 1.0)`, `final_qty = floor(min(original_qty, resize(new_stop)) * mult)`, where `resize` calls `size_position` with the new stop. Block reasons name the reviewer. The final qty never exceeds the original qty.
- [ ] **3.3.6** — Add a scriptable `FakeReviewer` fixture to `conftest.py` (fixed verdict, raise, invalid JSON, delay).
- [ ] **3.3.7** — Write `test_review_decision.py`: approve+approve → proceed; approve+reject(low) → proceed; approve+reject(high) → block; reject+reject(low) → block; one error → block naming the reviewer; equity 100k, entry 100, stop 90→92, mult 0.5 → stop 92, qty 100; entry 50, stop 40→45, no mult → qty stays 300; modified stop 85 vs 90 → keeps 90; modified stop ≥ entry ignored.
- [ ] **3.3.8** — Write `test_review_packet.py`: every packet field is present; 60 closes; at most 10 headlines; regime snapshot present; same input → same hash. Test `OpenAICompatReviewer` with a stubbed `openai` client: valid reply parsed; invalid JSON then valid → success after one retry; invalid twice → `error`; out-of-range `size_multiplier` → treated as invalid.

**Validation:**

- [ ] **Build:** Gate commands exit 0.
- [ ] **Tests:** `test_review_packet.py` and `test_review_decision.py` pass with every case in 3.3.7 and 3.3.8, with no network.
- [ ] **Requirements:**
    - [ ] "`build_packet` includes" every listed field; "The server builds the review packet itself, so the proposer cannot frame the facts."
    - [ ] "Two reviewers configured by env … both via the `openai` SDK's OpenAI-compatible chat completions with JSON output".
    - [ ] "Verdict schema validated with pydantic … Target changes, if returned, are recorded as advisory only."
    - [ ] "Call both reviewers concurrently. Each call: timeout `reviewer_timeout_s` (default 60), one retry on error/invalid JSON; on second failure the outcome is `error`."
    - [ ] "`decide()`" rule exactly as specified; "The final qty never exceeds the original qty."
- [ ] **Verify:** Read `decide()` and confirm a modified stop can only raise the stop and a multiplier can only shrink the size (no code path increases qty above the original).

---

### Task 3.4: `propose_buy` end to end

Wire the risk gate, cross-model review, bracket submission with client-id idempotency, the reviews record, and the journal entry into `propose_buy`.

**Modules:** `.`
**Depends on:** 3.1, 3.3

**Files:**

```
src/trader_broker/service.py        (modify)
src/trader_broker/journal.py        (create)
tests/unit/test_service_buy.py      (create)
tests/unit/test_idempotency.py      (create)
```

**Sub-tasks:**

- [ ] **3.4.1** — Gate stage: run housekeeping; if state is unavailable return `blocked` with `state_unavailable`; apply `check_halts` failures; fetch asset, quote, SIP daily bars ending at the previous close (for ADV), account, positions and open orders; a broker error here returns `blocked` with `broker_error`; call `size_position` and `check_entry`. Every block is written to the `blocks` table.
- [ ] **3.4.2** — Review stage: build the packet (fetching ~260 daily bars, news and SPY bars), run both reviewers, call `decide()`, apply the safer modifications, and re-run `check_entry` on the final order. A review block returns `blocked` with reasons naming the reviewer.
- [ ] **3.4.3** — Submission stage: generate `tb-<uuid4>`, record the order intent with its at-placement sizing in state, then call `submit_bracket_buy` (entry limit `gtc`, take-profit at target, stop at final stop). On an unknown-result error, look up the order by client id: found → treat as placed; not found → `blocked` with `broker_error`. Never submit twice for one proposal.
- [ ] **3.4.4** — In `journal.py`, write `record_entry()` storing thesis, invalidation, horizon, stop, target, entry and review id. In the service, persist the `reviews` row (packet hash, both raw verdicts, decision, final order) and link it to the journal entry.
- [ ] **3.4.5** — Return `{"status": "placed"|"blocked", "reasons": [...], "order": {...}|None, "reviews": [{"reviewer","verdict","confidence","reasons"}]}` with final qty, stop and target in `order`.
- [ ] **3.4.6** — Write `test_service_buy.py`: approve+approve places one bracket with `gtc`, correct prices and a `tb-` client id; journal and reviews rows written and linked; each gate failure returns `blocked` with its rule id and writes a `blocks` row; state missing → `state_unavailable` (and a sell still works); daily halt → `daily_halt` (sell allowed); kill switch → `kill_switch`; reviewer error → blocked and no order submitted; modified stop and multiplier applied; broker error before submit → `broker_error`.
- [ ] **3.4.7** — Write `test_idempotency.py`: broker accepts then times out → exactly one order exists and the result is `placed`; broker times out without accepting → `blocked` and no second submit call; the client id is in state before the submit call (assert via a FakeBroker hook).

**Validation:**

- [ ] **Build:** Gate commands exit 0.
- [ ] **Tests:** `test_service_buy.py` and `test_idempotency.py` pass with every case in 3.4.6 and 3.4.7.
- [ ] **Requirements:**
    - [ ] "`propose_buy` is the only tool that opens a position. … It never supplies a share count."
    - [ ] "Implement cross-model review inside `propose_buy`, after the risk gate passes and before order submission."
    - [ ] "Every order carries a unique `client_order_id` (`tb-<uuid4>`) recorded in state **before** submission; on timeout/unknown result the service looks up by client id before any retry; never submits twice for one proposal."
    - [ ] "Persist packet hash, both raw verdicts, decision and final order in `reviews` table linked to the journal entry."
    - [ ] "`propose_buy` returns: status (`placed` | `blocked`), reasons, final qty/stop/target, both reviewers' verdict summaries."
    - [ ] "Journal every placed buy with thesis, invalidation, horizon, stop, target, entry, review id".
- [ ] **Verify:** Read `propose_buy` and confirm the order of stages is housekeeping → gate → review → re-check → record intent → submit, and that no code path calls `submit_bracket_buy` twice.

---

## Phase 4: Journal, Report, MCP Server and CLI

### Task 4.1: Journal settlement and lessons

Detect closed trades during housekeeping, settle them against SPY, and add the `get_journal` and `journal_note` service methods.

**Modules:** `.`
**Depends on:** 3.4

**Files:**

```
src/trader_broker/journal.py       (modify)
src/trader_broker/service.py       (modify)
tests/unit/test_journal.py         (create)
```

**Sub-tasks:**

- [ ] **4.1.1** — In `journal.py`, detect closes from broker fills and positions: an open journal entry whose symbol is no longer held is closed; the exit price is the qty-weighted average of sell fills since entry. A partial sell does not settle the entry.
- [ ] **4.1.2** — Settle a closed entry: `trade_return_pct` from entry and exit price; `spy_return_pct` from SPY daily closes over the same holding window (entry date to exit date); `alpha_pct = trade_return_pct - spy_return_pct`; set `lesson_pending = 1`.
- [ ] **4.1.3** — Call close detection and settlement from housekeeping, after reconciliation.
- [ ] **4.1.4** — Add service methods `get_journal(status=None, limit)` (status values: open, closed, `pending_lessons`; none means all) and `journal_note(entry_id, note)` (stores the lesson, clears `lesson_pending`; unknown id → error).
- [ ] **4.1.5** — Write `test_journal.py`: trade +10% while SPY +2% → alpha +8% and `lesson_pending`; partial sell stays open; stop-out at a gap price settles the actual loss; `get_journal(status="pending_lessons")` returns only settled trades waiting for lessons; `journal_note` clears the flag; unknown entry id rejected.

**Validation:**

- [ ] **Build:** Gate commands exit 0.
- [ ] **Tests:** `test_journal.py` passes with every case in 4.1.5; `test_housekeeping.py` still passes.
- [ ] **Requirements:**
    - [ ] "detect closes via broker fills/positions in housekeeping; on close settle `trade_return_pct` and `spy_return_pct` over the same holding window and `alpha_pct`; mark `lesson_pending=1`."
    - [ ] "`get_journal(status="pending_lessons")` returns settled trades awaiting lessons; `journal_note(entry_id, note)` stores a lesson and clears the flag."
- [ ] **Verify:** Read the housekeeping method and confirm settlement runs after reconciliation and only for entries whose symbol is fully closed.

---

### Task 4.2: Go-live report

Implement the go-live scorecard in `report.py`.

**Modules:** `.`
**Depends on:** 4.1

**Files:**

```
src/trader_broker/report.py        (create)
tests/unit/test_report.py          (create)
```

**Sub-tasks:**

- [ ] **4.2.1** — Compute, since `paper_start_date`: weeks elapsed; portfolio return from broker portfolio history; SPY return over the same period; max drawdown from portfolio history; integrity violation count; blocks grouped by reason; reviewer stats (verdict counts per reviewer; approve-vs-outcome hit rate per reviewer on settled trades).
- [ ] **4.2.2** — Score each go-live criterion PASS/FAIL: weeks ≥ 8; portfolio return > SPY return; max drawdown < 25%; integrity violations = 0. State in the output that passing unlocks nothing by itself.
- [ ] **4.2.3** — Render a human text report and a JSON report (same data) for the `--json` flag.
- [ ] **4.2.4** — Write `test_report.py` with fixtures: all four PASS; each criterion failing on its own; boundaries (exactly 8 weeks → PASS; drawdown exactly 25% → FAIL; return equal to SPY → FAIL); reviewer hit rate computed from settled trades; JSON output parses and holds the same values.

**Validation:**

- [ ] **Build:** Gate commands exit 0.
- [ ] **Tests:** `test_report.py` passes with every case in 4.2.4.
- [ ] **Requirements:**
    - [ ] "Implement `trader-broker report` computing since `paper_start_date` … print PASS/FAIL per go-live criterion (≥8 weeks, return > SPY, max DD < 25%, 0 integrity violations); `--json` flag."
    - [ ] "The journal records every verdict, so after the paper period you can see whether each reviewer earned its seat."
- [ ] **Verify:** `test_boundaries.py` still passes, confirming `report.py` imports nothing from `alpaca`.

---

### Task 4.3: MCP server, CLI subcommands and logging

Expose exactly sixteen MCP tools from `server.py`, finish the CLI, and add structured logging.

**Modules:** `.`
**Depends on:** 2.2, 3.2, 4.2

**Files:**

```
src/trader_broker/server.py          (create)
src/trader_broker/cli.py             (modify)
src/trader_broker/service.py         (modify)
tests/unit/test_server_tools.py      (create)
tests/unit/test_cli.py               (create)
```

**Sub-tasks:**

- [ ] **4.3.1** — In `server.py`, build a FastMCP server over `TraderService` with exactly these tools. Read tools, `readOnlyHint: true`: `get_account`, `get_positions`, `get_orders`, `get_quote(symbol)`, `get_bars(symbol, timeframe, limit)`, `get_news(symbols?, limit)`, `get_movers(limit)`, `get_clock`, `get_risk_status`, `get_journal(status?, limit)`. Write tools, `readOnlyHint: false`: `propose_buy`, `sell`, `close_position`, `tighten_stop`, `cancel_entry_order`, `journal_note`; `destructiveHint: true` for `sell`, `close_position`, `cancel_entry_order`; `destructiveHint: false` for `propose_buy`, `tighten_stop`, `journal_note`. No tool changes caps, mode, kill switch or credentials.
- [ ] **4.3.2** — Every tool runs the equity refresh; write tools and `get_risk_status` run housekeeping first. Tool results are JSON-serializable dicts; errors return a structured reason instead of a traceback.
- [ ] **4.3.3** — Add CLI subcommands: `serve` (loads settings with the live guard and the `serve` check, builds `AlpacaBroker`, reviewers and state, runs the stdio server; on a config error prints the message and exits non-zero); `status` (prints `get_risk_status` for humans); `init-state [--force]`; `reset-kill-switch` (reads current equity, clears the switch, resets the peak); `report [--json]`.
- [ ] **4.3.4** — Add `market-open-gate`: compute today's date in `America/New_York`, call `Broker.get_calendar(today, today)`, print `{"wakeAgent": true}` as the last stdout line when today is a trading session, `{"wakeAgent": false}` otherwise; never check whether the market is open right now; exit 0 either way; on an API error print `{"wakeAgent": true}`.
- [ ] **4.3.5** — Add structured JSON logging to stderr and to the rotating file `<state_dir>/<mode>/logs/trader-broker.log` for every tool call, risk decision, review decision and order. Redact API keys and secrets.
- [ ] **4.3.6** — Write `test_server_tools.py`: the registered tool name set equals the sixteen names exactly; each tool's `readOnlyHint` and `destructiveHint` match 4.3.1; a write tool call triggers housekeeping and a read tool call does not.
- [ ] **4.3.7** — Write `test_cli.py`: `serve` in live mode without ACK exits non-zero with a message; market gate at 09:00 on a trading day → true; holiday → false; weekend → false; early-close day at 15:45 → true; calendar API error → true; in every case the JSON is the last line and the exit code is 0; `init-state` refuses an existing DB and succeeds with `--force`; `reset-kill-switch` clears the switch and resets the peak to current equity; a log line for a tool call contains no configured secret value.

**Validation:**

- [ ] **Build:** Gate commands exit 0.
- [ ] **Tests:** `test_server_tools.py` and `test_cli.py` pass with every case in 4.3.6 and 4.3.7; the full unit suite passes with no network.
- [ ] **Requirements:**
    - [ ] "Expose exactly these MCP tools from `server.py`; no others" with the stated annotations; "`test_server_tools.py` asserts the tool name set and annotations exactly."; "No tool changes caps, mode, kill switch, or credentials."
    - [ ] "`trader-broker market-open-gate` prints `{"wakeAgent": true}` as its last stdout line when today … is a trading session … Exits 0 either way; on API error prints wakeAgent true." with all five listed tests.
    - [ ] "Missing env var / live without ACK → Server exits non-zero at start with message".
    - [ ] CLI subcommands: serve, status, init-state, reset-kill-switch, report, market-open-gate, smoke-test.
    - [ ] SHOULD: "Structured JSON logs to stderr and to `<state_dir>/<mode>/logs/trader-broker.log` (rotating) … (never log secrets)."; MAY: `trader-broker status`.
    - [ ] "Unit tests cover every MUST above with FakeBroker and FakeReviewer; `uv run pytest -m "not paper"` passes with no network."
- [ ] **Verify:** `uv run trader-broker --help` lists all seven subcommands; `TRADER_BROKER_MODE=live uv run trader-broker serve` exits non-zero and prints a message naming `TRADER_BROKER_LIVE_ACK`.

---

## Phase 5: Hermes Profile

### Task 5.1: Profile config, persona, env template and routes

Write the `stock-trader` profile config, persona, `.env` template and the `profile_routes` example, with config tests.

**Modules:** `.`
**Depends on:** 4.3

**Files:**

```
profile/config.yaml                     (create)
profile/SOUL.md                         (create)
profile/.env.example                    (create)
profile/profile_routes.example.yaml     (create)
tests/profile/test_profile_files.py     (create)
```

**Sub-tasks:**

- [ ] **5.1.1** — Confirm the exact Hermes toolset identifiers (terminal, code execution, file read/write, skill management writes, browser, web search, memory, skills read, MCP server toolset naming) against Hermes v2026.9.24 `website/docs/reference/toolsets-reference.md`. Record each identifier with its source URL in the coder's Final Report so Task 6.1 can cite them. If the source cannot be reached, stop and report it as blocked; do not guess identifiers.
- [ ] **5.1.2** — Write `profile/config.yaml`: `timezone: America/New_York`; `model` with provider `anthropic` and a model placeholder filled at install; no `fallback_providers`; `mcp_servers.trader_broker` as stdio running `uv run --directory <INSTALL_DIR> trader-broker serve`, with `env` passing through `${...}` every `ALPACA_*`, `REVIEWER_*`, `OPENAI_API_KEY`, `XAI_API_KEY` and `TRADER_BROKER_*` variable plus the other `TRADER_*` caps variables, `trust: full`, `timeout: 300`; toolsets that enable the trader_broker MCP toolset, web search, memory and skills read, and disable terminal, code execution, file read/write, skill management writes and browser (identifiers from 5.1.1); `memory.memory_enabled: true`; approvals and plugins set so no plugin loads. Add comments that explain why there is no fallback and why trust is full.
- [ ] **5.1.3** — Write `profile/SOUL.md`: an aggressive, disciplined swing-trader persona that hunts momentum and catalysts and cuts losers fast. It states that sizing and caps are enforced by tools (the agent never picks a share count); the agent must state the next earnings date in every thesis; it must avoid holding through earnings unless the thesis is earnings-driven and the size reflects gap risk; it must post concise reports.
- [ ] **5.1.4** — Write `profile/.env.example` listing every env var from the master plan Interfaces plus the three `TRADER_` limits added in 1.1.3, each with a comment and the default value; mode set to `paper`; ACK left empty.
- [ ] **5.1.5** — Write `profile/profile_routes.example.yaml`: one Discord route (`guild_id` + `chat_id`) and one Telegram route (group `chat_id`), each mapping to profile `stock-trader`, with a comment warning not to route DM chat ids.
- [ ] **5.1.6** — Write the config part of `tests/profile/test_profile_files.py`: both YAML files parse; timezone is `America/New_York`; no `fallback_providers`; `trader_broker` server command runs `trader-broker serve` through `uv run --directory`; `trust` is `full` and `timeout` is 300; the env passthrough covers every variable `Settings` reads; the disabled toolset list contains the terminal, code, file and browser identifiers and the enabled list contains trader_broker, web search and memory; `SOUL.md` exists and mentions earnings and the tool-enforced sizing; `.env.example` names every variable `Settings` reads and sets mode `paper`; every route maps to `stock-trader`.

**Validation:**

- [ ] **Build:** Gate commands exit 0.
- [ ] **Tests:** `uv run pytest tests/profile` passes with every check in 5.1.6.
- [ ] **Requirements:**
    - [ ] "`config.yaml`: `timezone: America/New_York`; `model` provider anthropic … no `fallback_providers`; `mcp_servers.trader_broker` stdio …; `trust: full`; toolsets: enable … disable …; `memory.memory_enabled: true`."
    - [ ] "set `mcp_servers.trader_broker.timeout: 300` in profile config."
    - [ ] "Confirm exact Hermes toolset identifiers against Hermes v2026.9.24 `website/docs/reference/toolsets-reference.md`".
    - [ ] "`SOUL.md`: aggressive swing-trader persona; states that sizing/caps are enforced by tools, … earnings date in every thesis, … concise reports."
    - [ ] "`profile_routes.example.yaml`: Discord (guild_id + chat_id) and Telegram (group chat_id) route entries mapping to profile `stock-trader`, with a warning not to route DM chat ids."
    - [ ] "Env vars (all in `profile/.env.example`)".
- [ ] **Verify:** The coder's Final Report lists each toolset identifier with its source; `grep -n "fallback_providers" profile/config.yaml` prints only a comment line or nothing.

---

### Task 5.2: Trading skills and cron jobs

Write the five trading skills and the declarative cron job list.

**Modules:** `.`
**Depends on:** 5.1

**Files:**

```
profile/skills/trading/market-regime/SKILL.md      (create)
profile/skills/trading/candidate-screen/SKILL.md   (create)
profile/skills/trading/trade-analysis/SKILL.md     (create)
profile/skills/trading/position-review/SKILL.md    (create)
profile/skills/trading/journal-review/SKILL.md     (create)
profile/cron/jobs.yaml                             (create)
tests/profile/test_profile_files.py                (modify)
```

**Sub-tasks:**

- [ ] **5.2.1** — Write each skill with Hermes frontmatter (`name`, `description`, `version`, `metadata.hermes.category: trading`, `requires_toolsets` naming the trader_broker MCP toolset) and a body that lists steps and the trader-broker tools to call (by their MCP names): `market-regime` (SPY/QQQ trend and breadth; decide how hard to press today), `candidate-screen` (setups from movers, news and the watchlist), `trade-analysis` (technical, fundamental and news case; next earnings date found by web search and stated in the thesis; write thesis and invalidation; call `propose_buy` without a share count), `position-review` (re-check open trades against invalidation; `tighten_stop`; `sell`/`close_position`), `journal-review` (`get_journal(status="pending_lessons")`, compare with SPY, save lessons to memory, `journal_note`).
- [ ] **5.2.2** — Write `profile/cron/jobs.yaml` with five jobs, timezone `America/New_York`: `premarket-plan` 09:00 Mon–Fri, `entries` 09:45 Mon–Fri, `midday` 12:30 Mon–Fri, `close-review` 15:45 Mon–Fri, `weekly-report` 16:30 Fri. Each job has name, cron schedule, skills, a self-contained prompt naming its skills, `grace_minutes` (5 for entries, midday and close-review; 30 for the others), and deliver targets from `${TRADER_DISCORD_TARGET}` and `${TRADER_TELEGRAM_TARGET}`.
- [ ] **5.2.3** — Give the four market-day jobs the pre-run script `scripts/market-open-gate.sh`; `weekly-report` runs without it. The `close-review` prompt calls `get_risk_status` (so unfilled entries are cancelled before the close), runs exits, settles the journal and posts the daily report. The `weekly-report` prompt runs the go-live scorecard comparison with SPY.
- [ ] **5.2.4** — Extend `test_profile_files.py`: each skill's frontmatter parses and has every required key with category `trading` and the trader_broker toolset; each skill body names only tools that exist in the server tool set; `jobs.yaml` has exactly the five job names; each schedule is a valid 5-field cron with the stated time and days; grace values match; the gate script is set on exactly the four market-day jobs; every prompt names at least one of the five skills; deliver targets use the two env placeholders.

**Validation:**

- [ ] **Build:** Gate commands exit 0.
- [ ] **Tests:** `uv run pytest tests/profile` passes with every check in 5.2.4.
- [ ] **Requirements:**
    - [ ] "Five skills with Hermes frontmatter (`name`, `description`, `version`, `metadata.hermes.category: trading`, `requires_toolsets` naming the trader_broker MCP toolset), each listing steps and which tools to call."
    - [ ] "`cron/jobs.yaml`: five jobs (premarket-plan 09:00, entries 09:45, midday 12:30, close-review 15:45 Mon–Fri; weekly-report 16:30 Fri), timezone America/New_York, `grace_minutes` 5 (entries/midday/close) and 30 (others), pre-run script `scripts/market-open-gate.sh` for market-hours jobs, deliver targets from env …, self-contained prompts naming skills."
    - [ ] "The `trade-analysis` skill must find the next earnings date by web search and state it in the thesis."
    - [ ] "the close-review job calls `get_risk_status` so every unfilled entry is cancelled before the close".
- [ ] **Verify:** Read `trade-analysis/SKILL.md` and confirm it never asks the agent for a share count and requires the earnings date in the thesis.

---

### Task 5.3: Installer and market gate script

Write the idempotent server installer and the cron pre-run gate wrapper.

**Modules:** `.`
**Depends on:** 5.2

**Files:**

```
scripts/install-profile.sh            (create)
scripts/market-open-gate.sh           (create)
tests/profile/test_profile_files.py   (modify)
```

**Sub-tasks:**

- [ ] **5.3.1** — Write `scripts/market-open-gate.sh` (bash, `set -euo pipefail`): run `uv run --directory <repo dir> trader-broker market-open-gate` with the profile's env, and pass its stdout through so the JSON stays the last line. If the command itself cannot start, print `{"wakeAgent": true}` so a broken gate never silently skips a run.
- [ ] **5.3.2** — Write `scripts/install-profile.sh` (bash, `set -euo pipefail`, idempotent, `--dry-run` and `--enable-jobs` flags): check `hermes`, `uv` and `git` are installed; run `uv sync`; create profile `stock-trader` with `hermes profile create stock-trader --description ...` only if absent; copy `config.yaml` (with `<INSTALL_DIR>` filled in), `SOUL.md` and `skills/` into `~/.hermes/profiles/stock-trader/`, backing up any existing file with a timestamp suffix; create `.env` from `.env.example` only if absent (never overwrite).
- [ ] **5.3.3** — In the installer, read `profile/cron/jobs.yaml` (via `uv run python` with PyYAML) and, for each job whose name does not already exist in `hermes -p stock-trader cron` listings, run `hermes -p stock-trader cron create ...` with schedule, `--timezone`, `--skill`, `--name`, `--grace-minutes`, deliver targets, the gate script for gated jobs, and `--paused` unless `--enable-jobs`. Confirm the exact `hermes cron create` flags against the Hermes cron docs.
- [ ] **5.3.4** — At the end, print the `profile_routes` snippet from `profile_routes.example.yaml` and the next steps (fill `.env`, run `trader-broker init-state`, run `trader-broker smoke-test`, apply the routes to the default profile by hand, enable jobs). The installer never writes to the default profile (`~/.hermes/config.yaml`).
- [ ] **5.3.5** — Extend `test_profile_files.py`: both scripts pass `bash -n`, start with `set -euo pipefail` and are executable; with stub `hermes` and `uv` commands on `PATH` and a temp `HOME`, `install-profile.sh --dry-run` exits 0, prints the planned commands and writes no files; a normal run against the stubs creates the profile files, creates `.env` once, and a second run does not overwrite `.env` or create duplicate jobs; jobs are created with `--paused` by default; no run writes `~/.hermes/config.yaml`. With a stub `uv` that prints `{"wakeAgent": false}`, `market-open-gate.sh` prints it as the last line.

**Validation:**

- [ ] **Build:** Gate commands exit 0.
- [ ] **Tests:** `uv run pytest tests/profile` passes with every check in 5.3.5.
- [ ] **Requirements:**
    - [ ] "`scripts/install-profile.sh` (bash, `set -euo pipefail`, idempotent): check `hermes`, `uv`, `git`; `uv sync`; create profile `stock-trader` if absent …; copy config/SOUL/skills … (back up existing files with timestamp); create `.env` from `.env.example` if absent (never overwrite); create cron jobs … only if a job with that name does not exist, created `--paused` unless `--enable-jobs`; print the profile_routes snippet and next steps; never edit the default profile; support `--dry-run`."
    - [ ] "`scripts/market-open-gate.sh` — cron pre-run gate wrapper calling `trader-broker market-open-gate`".
    - [ ] Off-limits: "The user's default Hermes profile (~/.hermes/config.yaml) — installer only prints the profile_routes snippet".
- [ ] **Verify:** `grep -n "\.hermes/config.yaml" scripts/install-profile.sh` shows no write (only comments or printed text); `bash scripts/install-profile.sh --dry-run` with stubs on `PATH` exits 0.

---

## Phase 6: Documentation

### Task 6.1: Architecture document

Write `docs/system/architecture.md` describing how trader-broker and the profile work.

**Modules:** `.`
**Depends on:** 5.3

**Files:**

```
docs/system/architecture.md      (create)
```

**Sub-tasks:**

- [ ] **6.1.1** — Describe the components and data flow: Hermes `stock-trader` profile → `trader-broker` MCP server → `TraderService` → risk gate, cross-model review, SQLite state, `Broker` adapter (`AlpacaBroker`, `FakeBroker`). Include the module boundary rule (only `brokers/` imports `alpaca`).
- [ ] **6.1.2** — Document the buy path, the risk gate limits with their env vars and defaults, halts and the kill switch state machine, housekeeping order, client-id idempotency, sell path with leg cancel and re-protection, the review decision rule, the journal settlement, and the go-live report.
- [ ] **6.1.3** — Document the Hermes profile: disabled and enabled toolsets with the exact identifiers from Task 5.1.1 and their source URL (Hermes v2026.9.24 `website/docs/reference/toolsets-reference.md`), the MCP server config (`trust: full`, `timeout: 300`, no fallback), the cron schedule and the gate script, and `profile_routes` delivery.
- [ ] **6.1.4** — Document failure modes from the master plan's Error Handling table (state missing, reviewer outage, broker timeout, kill switch) and the live-mode guard.

**Validation:**

- [ ] **Build:** Gate commands exit 0.
- [ ] **Tests:** All existing tests pass; no new tests (documentation only).
- [ ] **Requirements:**
    - [ ] "Confirm exact Hermes toolset identifiers … and cite them in `docs/system/architecture.md`."
    - [ ] "`docs/system/architecture.md`" exists and matches the built code.
- [ ] **Verify:** Every tool name, env var and CLI subcommand named in `architecture.md` exists in `server.py`, `config.py` or `cli.py` (`grep` each one).

---

### Task 6.2: README, AGENTS.md and CLAUDE.md

Write the setup guide and the agent entry points.

**Modules:** `.`
**Depends on:** 6.1

**Files:**

```
README.md        (modify)
AGENTS.md        (create)
CLAUDE.md        (create)
```

**Sub-tasks:**

- [ ] **6.2.1** — Rewrite `README.md` as a setup guide: what the project does; requirements; install on the server with `scripts/install-profile.sh` (dry run first); fill `.env`; `trader-broker init-state`; run the paper smoke test (`trader-broker smoke-test` or `uv run pytest -m paper`); apply the printed `profile_routes` snippet to the default profile by hand; enable the cron jobs; the manual Hermes end-to-end install checklist; `trader-broker status`, `report` and `reset-kill-switch`; the go-live bar and the live-mode flag and ACK.
- [ ] **6.2.2** — Write `AGENTS.md`: repo layout, the gate commands, the module boundary rule, the rule that mode defaults to paper, the rule that no MCP tool may change caps, mode, kill switch or credentials, and a pointer to `docs/system/architecture.md`.
- [ ] **6.2.3** — Write `CLAUDE.md` that imports `@AGENTS.md`.

**Validation:**

- [ ] **Build:** Gate commands exit 0.
- [ ] **Tests:** All existing tests pass; no new tests (documentation only).
- [ ] **Requirements:**
    - [ ] "README.md — currently "# hermes-stock-trader" only" is replaced by the setup guide.
    - [ ] "AGENTS.md, CLAUDE.md (imports @AGENTS.md)".
    - [ ] "Hermes end-to-end … covered by install checklist in README".
- [ ] **Verify:** `grep -n "@AGENTS.md" CLAUDE.md` matches; every command in the README exists (`uv run trader-broker --help` lists it, or the script file exists).

<!-- END OF SPEC -->
