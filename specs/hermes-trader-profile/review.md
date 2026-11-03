# Review: hermes-trader-profile

Frozen diff: `4007dce...0d0f4f3` (74 files, +17,849). The tree was clean at freeze, so HEAD is the review snapshot.
Panel: A=sdlc-reviewer/opus, B=sdlc-reviewer/opus. Fan-out: each seat ran three dispatches. Two covered `src/trader_broker`, split by passes (1–2 and 3–4). The third covered everything outside `src/` on all four passes. That makes 6 reports, consolidated as round 1.
Overall contract tier: **3** (A src 1–2, B src 1–2, B profile, A profile). No Tier 1/2 gate.

## Initial mechanical checks
- Check: uv run ruff check . → pass
- Check: uv run ruff format --check . → pass
- Check: uv run mypy src → pass
- Check: uv run pytest -m "not paper" → pass (550 passed)

## Findings (round 1)

Columns: Sev = severity, Seats = which reviewers raised it, Class = fix class. Every finding's origin is introduced.

**Disposition key**
- **G**: gating.
- **GM**: gating-mechanical.
- **RF**: red-flag (selected).
- **V**: escalated vocabulary.
- **E**: logic observation escalated by the orchestrator because it touches money safety or something the agent or operator sees.
- **O**: observation.

| ID | Sev | Seats | Class | Finding | Disp |
|----|-----|-------|-------|---------|------|
| P1 | high | A, B(NUD) | logic | sell, tighten_stop and the cancel-confirm poll read `get_orders("open")`. That depends on unverified Alpaca behaviour: whether working legs of a filled entry are listed under `status=open&nested=true`. Housekeeping reads `all`. | G |
| P2 | high | B (A: medium) | logic | Installer: `hermes profile create` seeds a placeholder `.env`, so the `.env.example` template is never installed. The test stub hides this. | G |
| P3 | high | A | logic | The candidate-screen skill filters ADV using IEX-only `get_bars` volume. That rejects most names the server's SIP-based `min_adv` would accept. | G |
| P4 | medium | A, B | logic | `limit_price` supplied by the agent is never compared with the quote. `min_price` and the stop band are judged against the limit. Ran: a $3 stock placed through a $5 limit. | E |
| P5 | medium | A, B | logic | After `cancel_unconfirmed`, or a BrokerError in the confirm poll, the cancelled stops are not replaced and the position is unprotected until the next tool call. Ran. | E |
| P6 | medium | A, B | logic | Lost submit reply plus one lookup that misses: the order is never journaled, which leads to `foreign_position`. The integrity bar then fails permanently, and the agent may re-propose. | E |
| P7 | medium | B | logic | `_protect` treats bid 0 (no IEX quote) as below the stop and market-sells the whole position. Ran. | E |
| P8 | medium | B | logic | Stop coverage credits the legs of a partially filled entry, which Alpaca documents as inactive until the entry fills completely. A TTL cancel of a partial entry also cancels its legs, and legs in `pending_cancel` still count as cover. | E |
| P9 | medium | A, A, B, B | logic | Red flag, information leakage: the `tb-` prefix is hardcoded in `replace_stop` in both adapters, and the replacement id is never recorded. | RF |
| P10 | medium | A, B, B | logic | Red flag, repetition: the NY market timezone, `_market_date`, `_return` and close-on-or-before are each copied across 6 modules, and SPY has two names. | RF |
| P11 | medium | A, B | logic | Red flag, information leakage: the block-reason text format is parsed back in the report, and typed rule ids are dropped. | RF |
| P12 | medium | B | logic | Red flag, repetition: SizedOrder money fields are derived twice. `_with_size` leaves a stale `bound_by`. Ran. | RF |
| P13 | medium | A, B | logic | Concepts and vocabulary: `OrderRole` `stop`/`take_profit` are never written. The doc row is wrong. `_submit_recorded(sizing=)` is a dead parameter. | V |
| P14 | medium | B | logic | Concepts and vocabulary: `HousekeepingResult.equity: EquityStatus` uses a vague name. | V |
| P15 | medium | A, B | logic | Red flag, repetition: the "no verdict → error" mapping is coded 3 times, and the Verdict literal is declared twice. | RF |
| P16 | medium | A | logic | Concepts and vocabulary: `unprotected` lists positions that were just protected. Ran. | V |
| P17 | medium | A | logic | Concepts and vocabulary: `Mover.percent_change` is a real percent, while every other `_pct` field is a fraction. | V |
| P18 | medium | A, B | mechanical | Doc drift: the `get_risk_status` side effects are understated (it can market-sell and cancel sells), and the `symbol_cap_pct` docstring omits open buys. | GM |
| P19 | medium | A | logic | FakeBroker cancels synchronously, but the doc says it is asynchronous. There is no test of a cancel confirmed after N polls. | E |
| P20 | medium | A | logic | `AlpacaBroker.get_fills` paging and `get_portfolio_history` are untested. Journal exits and the go-live return depend on them. | E |
| P21 | medium | A | logic | architecture.md says daily bars are SIP and end at the previous close. The agent's `get_bars` is IEX and includes today. | E |
| P22 | medium | A | logic | Re-running the installer never updates existing cron jobs, so prompt changes, including the safety lines, silently don't apply. | E |
| P23 | medium | A, B | logic | `grace_minutes` in jobs.yaml is dead config that the tests pin. | E |
| P24 | low | B | logic | `decide()` "applied" reasons list modifications that were superseded. This is false output to the agent. Ran. | E |
| P25 | low | A, B | logic | `save_halts` has no production caller. `ReviewDecision.proceed` duplicates `order is not None`. | E |
| P26 | low | B | logic | Red flag, nonobvious code: there are two definitions of a stop (`_STOP_TYPES` vs `tighten_stop`, which accepts only type `stop`). | RF |
| P27 | low | A | logic | Red flag, repetition: the price-tick rule is coded in both `risk.round_price` and `alpaca._price`. | RF |
| P28 | low | A | logic | Efficiency: `settle_trades` reads all fills on every pass while an entry is pending. | O |
| P29 | low | B | logic | Journal `entry_price` and `qty` come from the limit and the order qty, not the fill. That biases returns and reviewer hit rates. | E |
| P30 | low | B | logic | `profile create` runs without `--no-skills`, so the profile gets every bundled Hermes skill. | E |
| P31 | low | B | mechanical | The comment and doc claim `disabled_toolsets` can never be undone by `hermes tools`. That is false: an operator's explicit enable clears it. | GM |
| P32 | low | B | mechanical | The close-review prompt says `get_risk_status` cancels entries "before the close". It actually cancels on the TTL. | GM |
| P33 | low | B | mechanical | CI runs `uv sync` without `--locked`. | GM |
| P34 | low | A | logic | On early-close days, close-review runs after the close, and its earnings exits would queue to the next open. | E |
| P35 | low | A | logic | Docs don't state that anyone the default bot accepts can command the trader through a route. They should recommend a private channel and a `user_id`. | E |
| P36 | low | A | logic | The installer substitutes INSTALL_DIR into bash and `--model` into YAML without escaping. | E |
| P37 | low | A | mechanical | The `--dry-run` "changes nothing" claim is false: `uv run` creates `.venv`. | GM |
| P38 | low | A | mechanical | The installer requires `git` but never uses it. | GM |

### Needs user decision → adjudicated by the orchestrator
The questions reviewers raised are technical, and the confirmed plan's intent answers them, so I adjudicated them rather than escalating:
- **N1: read-only annotations** (A, A, B). `get_risk_status` runs housekeeping, which can cancel, place stops and market-sell. `get_journal` writes settlements. Decision:
  - Annotate `get_risk_status` as `readOnlyHint=false, destructiveHint=true`.
  - Annotate `get_journal` as `readOnlyHint=false, destructiveHint=false`.
  - Update the tests and the spec record.
  - Reason: the annotations must be honest, and nothing depends on the read-only label (`trust: full`).
- **N2: more than one `serve` process or CLI `status`** (B). The in-process lock doesn't cover several processes. Decision: add a cross-process exclusive lock (a file lock beside `state.db`) around housekeeping, `propose_buy` and the exit tools. That covers CLI `status` too.
- **B34-NUD: one open-orders view.** Folded into the P1 fix.

## Pre-existing
(none — new repo)

## Repro not confirmed
- Repro not confirmed: P1 alpaca.py:162 — A, B — needs a filled paper bracket; no keys in this environment.
- Repro not confirmed: P6 service.py:901 — A, B — needs a real lost reply on an Alpaca write.
- Repro not confirmed: P8 service.py:1382 — B — needs a partial fill on paper; the evidence is Alpaca's docs.
- Repro not confirmed: P3 candidate-screen SKILL.md:27 — A — needs Alpaca keys to compare IEX and SIP volume.

## Reproduce Panel Findings
- **P2**: reviewer-right. Both seats reproduced it independently by running the installer with a `hermes` stub that seeds `.env` the way `hermes_cli/profiles.py:_finish_profile_layout` does. The coder adds that stub as the regression test.
- **P1**: not-reproducible: it needs a filled bracket on Alpaca paper (no keys). It stays gating, and the fix removes the dependency on the unverified behaviour.
- **P3**: not-reproducible: it needs Alpaca market data keys. It stays gating, and the fix removes the IEX-volume filter from the skill.
- Deviation: I dispatched no testers, because P1 and P3 can't be run without keys and both seats already reproduced P2.

## Fix Once
Selected: every finding above except P28 (observation).
- The selected set went to two coders in parallel, over disjoint file sets. The diff is too large for one coder, which is a deviation from "one coder".
  - **Coder 1** owns `src/`, `tests/unit`, `tests/component` and `tests/integration`.
  - **Coder 2** owns `profile/`, `scripts/`, `tests/profile`, CI, README, AGENTS.md and `docs/`.
- Names I fixed up front so both coders stay consistent:
  - `unprotected`/`UnprotectedPosition` → `protection_repairs`/`ProtectionRepair`
  - `Mover.percent_change` → `change_pct` (a fraction)
  - `HousekeepingResult.equity: EquityStatus` → `reading: AccountReading`
  - `get_risk_status` becomes destructive; `get_journal` becomes a write tool
