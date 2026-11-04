# AGENTS.md

## What Is This?

A Hermes Agent profile that swing-trades US stocks on Alpaca, plus `trader-broker`, the MCP server that is the agent's only path to the broker. Claude, running in the Hermes `stock-trader` profile, proposes trades (symbol, stop, target, thesis). The server owns every number: it sizes the order from the stop distance, checks the risk caps and halts, has two reviewer models from other vendors approve, modify or reject the buy, places a bracket order, and journals the trade so it can be settled against SPY. The go-live report scores the paper period against a fixed bar; passing it unlocks nothing by itself.

## Backend/Dependencies

- **Alpaca** trading and market-data APIs, reached only through `brokers/alpaca.py` (`alpaca-py`, pinned exactly in `pyproject.toml`).
- **OpenAI and xAI** chat-completions APIs for the two trade reviewers (`review.py`, OpenAI-compatible client).
- **Hermes Agent** (v2026.9.24) launches `trader-broker serve` as a stdio MCP server and runs the profile's cron jobs. Hermes is not available in tests.
- **SQLite** state database per mode at `TRADER_BROKER_STATE_DIR/<mode>/state.db`.

## Tech Stack

- Python 3.12, uv (src layout, package `trader_broker`, console script `trader-broker`)
- FastMCP 3 (MCP server), pydantic 2 (settings and records), httpx, openai, alpaca-py
- pytest, ruff (lint and format), mypy (strict, pydantic plugin)
- Bash for `scripts/`; YAML and Markdown for the Hermes profile

## Architecture at a Glance

Hermes cron or a routed chat → Claude in the `stock-trader` profile → MCP tool call → `server.py` (one call at a time, JSON log; write tools also hold the cross-process account lock) → `TraderService` (equity refresh and halt latches, housekeeping) → `risk.py` gate and sizing → `review.py` (two reviewers in parallel) → `Broker` protocol → `AlpacaBroker` → Alpaca. State and journal live in `state.py` / `journal.py`. The operator uses the same code through the other `trader-broker` subcommands.

Full design, tool list and failure modes: [docs/system/architecture.md](docs/system/architecture.md).

## Before Making Changes

| Area | Read |
|---|---|
| Overall design, data flow, tools | `docs/system/architecture.md` |
| Buy path, risk gate, sizing | `docs/system/architecture.md` "The buy path", `src/trader_broker/risk.py` |
| Cross-model review | "Cross-model review", `src/trader_broker/review.py` |
| Broker adapter | "AlpacaBroker", `src/trader_broker/brokers/` |
| State database, halts, kill switch | "State database", "Halts and the kill switch", `src/trader_broker/state.py` |
| Settings and the live-mode guard | "Failure modes and the live-mode guard", `src/trader_broker/config.py` |
| Hermes profile, skills, cron jobs | "The Hermes profile", `profile/`, `tests/profile/test_profile_files.py` |
| Installer and gate script | "Installer", "Cron schedule and the gate script", `scripts/` |
| Operator setup | `README.md` |

## Key Conventions

1. **Module boundary.** Only the `brokers/` package imports `alpaca`. Everything else uses the `Broker` protocol and the error types in `brokers/base.py`. `tests/unit/test_boundaries.py` enforces it.
2. **Mode defaults to paper.** `TRADER_BROKER_MODE` defaults to `paper`; `live` needs `TRADER_BROKER_LIVE_ACK=I_ACCEPT_REAL_MONEY_RISK`, or every command exits 2. Never change a default, example or test so that live mode is easier to reach.
3. **No MCP tool may change caps, mode, kill switch or credentials.** Limits are read once from the environment at start. The kill switch is cleared and the state database created only by operator CLI subcommands. Do not add a tool, argument or skill instruction that could.
4. **The agent never picks a share count.** The server sizes every order; `propose_buy` takes no quantity.
5. **Fail closed.** `StateStore.open` never creates a database; a missing or unreadable one blocks buys, while exits keep working. Broker writes never retry; an unknown outcome is resolved by client order id, and a buy whose outcome stays unknown is never resubmitted.
6. **A new setting goes in three places:** `config.py`, the `env:` mapping in `profile/config.yaml`, and `profile/.env.example`. The profile tests check they agree.
7. **Hermes facts cite their source.** Profile keys, toolset names and cron fields were checked against Hermes v2026.9.24; keep the source comments in `profile/` when changing them.
8. **Never push, merge or place real orders from automation.** The paper smoke test (`-m paper`) runs only by hand with paper keys.
9. **Keep docs in sync.** A change in behaviour updates `docs/system/architecture.md` (and `README.md` when the operator's steps change) in the same commit.

## Build & Dev

Gate commands, as CI runs them; all must exit 0:

```bash
uv sync
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run pytest -m "not paper"
```

Opt-in, against an Alpaca paper account (needs `ALPACA_API_KEY`, `ALPACA_SECRET_KEY`): `uv run pytest -m paper`.

CLI: `uv run trader-broker --help` (`serve`, `status`, `init-state`, `reset-kill-switch`, `report`, `market-open-gate`, `smoke-test`).

## Directory Map

```
src/trader_broker/
├── cli.py            # trader-broker subcommands; smoke-test steps
├── server.py         # FastMCP server: 16 tools, logging setup
├── service.py        # TraderService: logic behind every tool
├── risk.py           # pure caps, sizing and halt checks
├── review.py         # review packet, reviewers, decision rule
├── state.py          # SQLite StateStore (schema 2) and init_state
├── account_lock.py   # cross-process flock on <state_dir>/<mode>/trader-broker.lock
├── market.py         # New York trading date, SPY benchmark, returns
├── journal.py        # journal entries, settlement against SPY
├── report.py         # go-live scorecard
├── config.py         # Settings from the environment, live-mode guard
├── models.py         # pydantic records, Price, round_price
├── indicators.py     # SMA, RSI, ATR, 52-week high, dollar volume
└── brokers/
    ├── base.py       # Broker protocol and error types
    ├── alpaca.py     # AlpacaBroker (the only alpaca importer)
    └── fake.py       # FakeBroker for unit tests
profile/              # Hermes stock-trader profile: config, SOUL.md, skills/, cron/, .env.example
scripts/              # install-profile.sh, market-open-gate.sh
tests/                # unit/, component/ (in-process MCP server), integration/ (stdio server),
                      # profile/ (profile files), smoke/ (paper, opt-in)
docs/system/          # architecture.md
docs/specs/           # YYYYMM/<spec>.md retros: why decisions were made
```
