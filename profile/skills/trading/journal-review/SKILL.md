---
name: journal-review
description: Read settled trades awaiting a lesson, compare each with SPY over the same holding window, record the lesson with journal_note and keep durable lessons in memory; also builds the weekly scorecard against SPY.
version: 1.0.0
metadata:
  hermes:
    category: trading
    tags: [trading, journal, lessons]
    requires_toolsets: [mcp-trader_broker]
---

# Journal Review

## When to Use

After the day's exits, to turn settled trades into lessons, and for the weekly scorecard.

## Procedure

### Lessons from settled trades

1. Call `mcp__trader_broker__get_journal` with `status="pending_lessons"`. Each entry is a closed trade with `trade_return_pct`, `spy_return_pct` (SPY over the same holding window) and `alpha_pct` (the difference), all as shares of a whole (0.05 is 5%). If the list is empty, say so and stop.
2. For each entry, re-read the thesis and invalidation and work out what happened: stopped out, target hit, closed early, or held past the horizon; whether the stated earnings date was right; whether the trade beat SPY.
3. Write a lesson of one or two sentences: specific and actionable, about the setup, the levels or the timing, not about luck. Example: "Breakouts on under 1.5x volume in a defensive tape failed 3 of 3; wait for volume."
4. Call `mcp__trader_broker__journal_note` with the entry's `id` as `entry_id` and the lesson as `note`. This stores the lesson and clears the pending flag.
5. When a lesson is a durable pattern (it repeats an earlier lesson, or it would change how you trade), save it with the `memory` tool as one line starting `Trading lesson:`. Memory is small: merge it into a similar existing lesson with `replace` rather than adding a near-duplicate, and remove lessons that later trades contradicted.

### Weekly scorecard

1. Call `mcp__trader_broker__get_journal` with `status="closed"` and `limit=200`. For this week's closes and for all closes, report the count, win rate, average `trade_return_pct`, average `alpha_pct`, and the best and worst trade.
2. Call `mcp__trader_broker__get_account` and `mcp__trader_broker__get_risk_status` for equity, the peak, the current drawdown, halts and recent blocked buys.
3. Call `mcp__trader_broker__get_bars` for `SPY` with `timeframe="1Day"` and `limit=10` for SPY's return this week, and compare it with the account's.
4. List the go-live criteria with what you can see: at least 8 weeks of paper trading, return above SPY, maximum drawdown under 25%, and no integrity violations. The operator's `trader-broker report` command gives the authoritative pass or fail for each.

## Pitfalls

- A trade that lost money but followed the plan is not a mistake; a winner that broke the plan is. Write lessons about the process.
- Never leave a pending lesson unrecorded: the journal keeps asking until `journal_note` is called.

## Verification

`mcp__trader_broker__get_journal` with `status="pending_lessons"` returns no entries at the end, and each new lesson appears in the report.
