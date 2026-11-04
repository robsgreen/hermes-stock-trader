---
name: portfolio-bootstrap
description: Use when starting from an empty or near-empty account under pressure to deploy cash fast. Paces initial entries instead of forcing them.
version: 1.0.0
metadata:
  hermes:
    category: trading
    tags: [trading, bootstrap, pacing, risk]
    requires_toolsets: [mcp-trader_broker]
---

# Portfolio Bootstrap

## When to Use

When the account holds few or no open positions and the user asks to "build," "fill," or "get started on" the portfolio. Read this before running `market-regime` and `candidate-screen` so the pacing rules shape how many entries you look for.

## Procedure

1. Run `market-regime` first, as always. The regime sets how many *new* entries to look for today — it does not change because the account happens to be empty.
2. Treat an empty account as having zero urgency to fill. There is no rule, server-side or otherwise, that requires deploying cash by any deadline. Cash sitting idle while waiting for quality setups is a correct outcome, not a stalled task.
3. On a single session, expect to place at most the number of entries the regime allows (1-3), same as any other day. Do not interpret "build the portfolio" as "place several trades today" — it means "place good trades as they qualify, across as many sessions as that takes."
4. If every proposal from `candidate-screen` and `trade-analysis` gets blocked in one session (common on a day with a single dominant catalyst, e.g. a broad market-wide rally or a heavy earnings slate), that is itself information: say so plainly, note why (cite the reviewers' reasons), and stop for the session rather than lowering standards or widening stops to force a fill.
5. Prefer building the first few positions in names with multi-day-confirmed trends over same-day movers — see `trade-analysis`'s pitfall on same-day spikes. A bootstrap portfolio built from confirmed setups over 3-5 sessions clears review far more often than one forced into existence on day one.
6. Record a one-line watchlist note (via the `memory` tool, per `candidate-screen` step 7) for every strong but not-yet-buyable candidate, so the next session resumes with context instead of re-screening from zero.

## Pitfalls

- Do not read "the portfolio is empty" as a reason to loosen the stop, R:R, or extension rules that `trade-analysis` and the server reviewers enforce. The bar for an entry is the same whether it is the account's first position or its tenth.
- Do not propose a blocked symbol again in the same session with adjusted numbers just to make progress on filling the portfolio — this is already against `trade-analysis`'s pitfalls and does not become acceptable because the account is new.
- A day with zero fills is a normal result, not a failure of the screen or a sign a skill is missing. Report it as "no qualifying entries today" and move on.

## Verification

The report states how many sessions the regime has allowed entries, how many were actually placed, and the watchlist carried into the next session — not a forced count of "positions filled today."
