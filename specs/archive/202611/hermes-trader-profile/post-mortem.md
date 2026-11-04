# Post-Mortem: hermes-trader-profile

Date: 2026-11-04
Diff: `4007dce...63642bc` on `sdlc/hermes-trader-profile` (25 commits, 76 files, +19,563/-1). Tree clean.
Tests: 600 passed with `uv run pytest -m "not paper"`; ruff, ruff format and mypy pass (review.md, final checks after round 3).

## Summary

The branch builds what the confirmed plan describes: the `trader-broker` MCP server (Broker protocol, `AlpacaBroker`, `FakeBroker`, risk gate with code sizing, halts and kill switch, SQLite state, cross-model review, journal, go-live report, operator CLI), and the `stock-trader` Hermes profile (config, SOUL, five skills, five cron jobs, routes snippet, installer, market-open gate). The core design, "models reason, software computes", is intact: the agent never supplies a share count, every buy is a code-sized bracket, and no tool changes caps, mode, kill switch or credentials.

Drift is moderate and almost all of it came out of review: the review found that two "read" tools have side effects, that Hermes cron lacks flags the plan assumed, and that concurrent processes needed a lock. These changes were the right ones. Some of them change the surface the agent and the operator see. Three review rounds were needed (the third past the two-round cap, at the user's direction); round 2 found 5 medium money-safety defects that the round-1 fixes had introduced. The live-broker behaviour that several fixes depend on is still unverified, because no paper keys were available.

## Planned vs. Built

| Planned outcome | Evidence |
|---|---|
| Broker code only in `brokers/`; import boundary test | `src/trader_broker/brokers/{base,alpaca,fake}.py`; `tests/unit/test_boundaries.py` |
| Exactly 16 MCP tools, tests pin names and annotations | `server.py:112-247`; `tests/unit/test_server_tools.py`, `tests/component/test_tool_surface.py` |
| Code sizing `floor(min(risk, cap, cash))`, whole shares, stop/target/qty/stop-band rejects | `risk.py` (`entry_price`, `check_entry`), `tests/unit/test_sizing.py`, `test_risk.py` |
| Daily halt latch, kill switch latch, CLI-only reset | `risk.check_halts`, `state.py`, `cli.py reset-kill-switch`; `tests/unit/test_halts.py` |
| Bracket GTC buys with `tb-<uuid4>` id recorded before submit; lookup-before-retry | `service.py` `_submit_recorded`; `tests/unit/test_idempotency.py` |
| Housekeeping: TTL cancel, stop repair (8% default, market-sell below bid), reconcile, integrity rows | `service.py:90` `DEFAULT_STOP_PCT`, `_reprotect`, `_cancel_excess_stops`; `tests/unit/test_housekeeping.py` |
| Sells/close cancel legs, confirm, sell, re-protect remainder; `exceeds_position` | `service.py` exit tools; `tests/unit/test_service_sell.py`, `tests/component/test_exit_tools.py` |
| Two OpenAI-compatible reviewers, concurrent, one retry, aggressive `decide()` that never grows size | `review.py`; `tests/unit/test_review_decision.py`, `test_review_packet.py` |
| Journal vs SPY, `lesson_pending`, `journal_note`; go-live report with `--json` | `journal.py`, `report.py`; `tests/unit/test_journal.py`, `test_report.py` |
| Paper default; live needs `TRADER_BROKER_LIVE_ACK`; DB path per mode | `config.py:78`; `tests/unit/test_config.py` |
| Profile: anthropic, no fallback, `trust: full`, timeout 300, disabled terminal/code/file/browser | `profile/config.yaml:111`; `tests/profile/test_profile_files.py` |
| Installer: idempotent, `--dry-run`, `.env` never overwritten, jobs `--paused` unless `--enable-jobs` | `scripts/install-profile.sh`; installer tests against stub `hermes`/`uv` |
| Market-open gate reads the calendar, not the clock; fails open | `cli.py market-open-gate`, `scripts/market-open-gate.sh`; `tests/unit/test_cli.py` |
| Paper smoke test (bracket accept + cancel, IEX quote, SIP bars) | `tests/smoke/test_alpaca_paper.py`. **Written but never run** (no keys). |

## Plan Drift

| Area | Planned | Actual | Impact | Evidence |
|---|---|---|---|---|
| Tool annotations | `get_risk_status` and `get_journal` are read tools, `readOnlyHint: true` (plan "Expose exactly these MCP tools") | `get_risk_status` is `_DESTRUCTIVE` (its housekeeping can cancel orders, place stops and market-sell); `get_journal` is `_WRITE` (it settles trades). Writes are serialized with `writes_lock` according to the annotation | High | `server.py:63-65`, `:165`, `:178`, `:99-103`; review.md N1 |
| Cross-process concurrency | One server process; no lock was planned | `AccountLock` (`flock` beside `state.db`) wraps housekeeping, buys and exits. A new env var `TRADER_ACCOUNT_LOCK_WAIT_S` (default 60). Calls return `busy` when the wait runs out | Medium | `account_lock.py`, `config.py:83`; review.md N2, P43 |
| Cron job definition | Per-job `timezone` and `grace_minutes` 5/30 passed to `hermes cron create --timezone --grace-minutes` | Hermes v2026.9.24 has neither flag. The schedules use the profile `timezone`, grace is Hermes's default, and `grace_minutes` was removed from `jobs.yaml` | Medium | `profile/cron/jobs.yaml:8-22`; `docs/system/architecture.md:422`; status.md 18:53; review.md P23 |
| Installer job handling | Create a job only if it does not exist; never update | Existing jobs are updated with `hermes cron edit`, which keeps their paused/active state. The profile is created with `--no-skills`. The `git` requirement was dropped | Medium | `scripts/install-profile.sh:20`, `:227`, `:289`; review.md P22, P30, P38 |
| Risk rule vocabulary | 16 rule ids in `RiskDecision.failures` | Adds `limit_invalid` (agent limit judged against the ask) and `pending_unknown`. Blocks also carry `broker_error` and `review` as typed `BlockRule` | Medium | `models.py:62-84`; `risk.py:278-283`; review.md P4, P6, P11 |
| Agent-visible field names | `get_risk_status.unprotected`; movers as returned by Alpaca | `protection_repairs: list[ProtectionRepair]` (lists the repairs made); `Mover.change_pct` is a fraction, not a percent | Medium | `service.py:251`; `models.py:174-182`; review.md P16, P17 |
| Broker protocol | `replace_stop(order_id, stop_price)` | `replace_stop(order_id, stop_price, client_order_id)`, so the `tb-` id is chosen and recorded by the service, not hardcoded in the adapters | Medium | `brokers/base.py:117`; review.md P9 |
| Skill-write lockdown | Disable skill_manage writes | Hermes has no read-only skills toolset. Instead, `skills.write_approval: true` stages every skill change for a person to approve | Medium | `profile/config.yaml:113-118` |
| Test layout | `tests/unit`, `tests/profile`, `tests/smoke` | Adds `tests/component` (driving the MCP tool surface) and `tests/integration` (stdio server, installer against stubs) | Low | `tests/component/`, `tests/integration/test_stdio_server.py`; test-spec.md |
| Hermes MCP tool names | Research: `mcp_<server>_<tool>` | Skills and docs use `mcp__trader_broker__<tool>`, citing `tools/mcp_tool_schema.py` | Low | `docs/system/architecture.md:74`; `profile/skills/trading/*/SKILL.md` |

## Critical Decisions

- **Honest annotations over the plan's read/write split.** `get_risk_status` can market-sell a position whose bid is already below its repair stop. Calling it "read-only" would mislead any client that trusts the hint. Nothing relies on the read-only label because the profile uses `trust: full` (`server.py:165`, `profile/config.yaml`).
- **Lock wait is tied to the Hermes timeout.** The 60 s lock wait plus about 120 s of worst-case reviews has to stay under the MCP `timeout: 300`. Otherwise a queued `propose_buy` can place an order after the agent was told the call timed out (`config.py:83-87`; review.md P43).
- **The agent may supply a limit, but the server bounds it by the ask.** A limit above `ask × (1 + slippage)` is blocked as `limit_invalid`, so `min_price` and the stop band can no longer be gamed with a limit price (`risk.py:278-283`).
- **Installer now rewrites existing cron jobs.** Re-running it updates the prompts, including their safety lines, on jobs the user may have hand-edited (`install-profile.sh:289`).

## Risks and Concerns

- **Order-listing behaviour is unverified on Alpaca.** The P1 fix merges a `status=open&nested=true` read. If Alpaca also lists a filled bracket's legs at the top level, those legs appear twice. Then `tighten_stop` can report `failed` after it has already raised the stop (review.md O1; `service.py:1184`, `alpaca.py`).
- **500-order history window.** `_ORDER_HISTORY_LIMIT = 500` (`service.py:94`) and `_OPEN_ORDER_SCAN = 500` (`alpaca.py:78`). A `tb-` stop older than that window would get a duplicate repair stop, which the broker refuses. This is weeks away at a few orders a day, but it does not fail loudly (review.md O2).
- **Housekeeping race.** Orders are read before positions. If a take-profit leg fills between the two reads, a needed stop can be cancelled as excess, and the shares stay unprotected until the next tool call (review.md O3; `service.py:653`).
- **Lock-wait misconfiguration.** Nothing checks that `TRADER_ACCOUNT_LOCK_WAIT_S` plus two reviewer timeouts stays under the profile's MCP `timeout: 300`. Raising either env var silently reopens P43 (`config.py:83`, `REVIEWER_TIMEOUT_S` at `config.py:115`).
- **`service.py` is 1,724 lines.** Housekeeping, the exit tools and the buy path share protection helpers (`_reprotect`, `_stop_covered_qty`, `_cancel_excess_stops`). Round 2 found three defects that the round-1 fixes introduced in exactly this area (P39–P41), so future changes there carry high regression risk.
- **Earnings blackout is prompt-only**, as the plan accepted (`profile/SOUL.md`, `trade-analysis` skill).

## Learnings

- **Annotations are a contract.** Decide whether a tool has side effects before naming it a "read". Running housekeeping in `get_risk_status` was a plan-level choice, and it made the plan's own annotation table wrong (N1).
- **Check CLI flags in the plan, not at implementation time.** `--timezone` and `--grace-minutes` were taken from docs prose, yet the installed Hermes version has neither flag. The task spec had to be corrected mid-implementation (status.md 18:53).
- **Fixes in stop-protection logic need adversarial re-review.** Round-1 fixes to the protection code (P5, P8) produced most of round 2's money-safety defects (P39–P41). Code that pairs stops with shares deserves scenario tests that interleave fills, cancels and partial states, such as `delay_cancels(reads=N)` in `FakeBroker`.
- **Behaviour that only paper or live trading can show should be named in the plan as an assumption to verify.** P1, P3, P6 and P8 all rest on Alpaca semantics that no one could run in this environment.

## Action Items

- [ ] Before the paper period starts, run `uv run pytest -m paper` and `uv run trader-broker smoke-test` with the Alpaca paper keys. No bracket, client-id lookup or SIP-bar fetch has ever hit Alpaca (`tests/smoke/test_alpaca_paper.py`; test-spec IT-4/IT-5).
- [ ] After the first bracket entry fills on paper, compare `get_orders` output with the Alpaca dashboard. Check for duplicate legs (O1) and confirm that legs of a filled bracket are visible (P1). The exit tools and `tighten_stop` depend on this (`brokers/alpaca.py` open-orders read).
- [ ] In a manual `hermes -p stock-trader chat`, confirm that the tools are exposed as `mcp__trader_broker__<tool>`. Every skill and cron prompt hardcodes that prefix (`profile/skills/trading/*/SKILL.md`, `profile/cron/jobs.yaml`; test-spec IT-6 notes the mismatch).
- [ ] When you apply `profile/profile_routes.example.yaml`, route only a private channel/group and keep the default profile's allowlist narrow (a `user_id` route does not qualify for cron delivery). Anyone the default bot accepts on a routed chat can command the trader (review.md P35).

## What Went Well

Code sizes every buy, and halts never block exits. Both are pinned by 600 offline tests that run against `FakeBroker`/`FakeReviewer`. Each round-3 fix shipped with a regression test that failed before the fix (review.md Round 3).

<!-- DRIFT-SUMMARY v1.0
{ "rubricVersion": "1.0", "drift": { "high": 1, "medium": 7, "low": 2 }, "actionItems": 4 }
-->
