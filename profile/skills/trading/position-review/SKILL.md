---
name: position-review
description: Re-check every open position against its thesis, invalidation, horizon and the next earnings date; raise stops on winners with tighten_stop and exit broken or expiring trades with sell or close_position.
version: 1.0.0
metadata:
  hermes:
    category: trading
    tags: [trading, risk, exits]
    requires_toolsets: [mcp-trader_broker]
---

# Position Review

## When to Use

In every market-hours run that holds positions or open entry orders, before looking for new trades.

## Procedure

1. Call `mcp__trader_broker__get_clock`. If `is_open` is false (before the open, after the close, or after an early close), do not call `mcp__trader_broker__sell` or `mcp__trader_broker__close_position`: a market order would wait for the next open. List each exit you would make under "Exit at next open" in the report instead.
2. Call `mcp__trader_broker__get_risk_status`. Its housekeeping cancels unfilled entry orders older than their time limit and puts a stop on any position that lacks one; if the bid is already at or below a missing stop, it sells those shares at market (with no bid it places the stop instead). Report anything listed under `protection_repairs`, any halt, and the drawdown.
3. Call `mcp__trader_broker__get_positions`, `mcp__trader_broker__get_orders` (the stop and take-profit legs of each bracket are listed with `parent_id` set) and `mcp__trader_broker__get_journal` with `status="open"` for each trade's thesis, invalidation, stop, target, horizon and open date.
4. For each position, call `mcp__trader_broker__get_quote`, `mcp__trader_broker__get_bars` with `timeframe="1Day"` and `limit=30`, and `mcp__trader_broker__get_news` with `symbols=[symbol]` and `limit=5`. Find the next earnings date in the thesis; if it is missing or may have moved, confirm it with `web_search`.
5. Decide, in this order, and act:
   - **Invalidation hit or thesis broken** (the price or fact in the invalidation happened, or the catalyst reversed): call `mcp__trader_broker__close_position`.
   - **Earnings before the next session** and the thesis is not earnings-driven: call `mcp__trader_broker__close_position`.
   - **Horizon passed** without the expected move: call `mcp__trader_broker__close_position`.
   - **Near the target with fading momentum**: take part of the gain with `mcp__trader_broker__sell` (`symbol`, `qty` at most the shares held); the server keeps a stop on the rest.
   - **Up at least 1R** (the gain per share is at least the entry minus the original stop): raise the stop to breakeven or just under the latest swing low with `mcp__trader_broker__tighten_stop`. **Up at least 2R**: trail it under the 20-day average or the latest swing low.
   - Otherwise hold, and say why in one line.
6. For an unfilled entry order whose setup has broken, call `mcp__trader_broker__cancel_entry_order` with its order id. Do not cancel stop or take-profit legs; the server refuses.

## Pitfalls

- A stop only moves up. `tighten_stop` rejects a lower stop or one at or above the current bid. It also rejects with `unsupported_stop` when a stop limit or trailing stop placed outside trader-broker protects the shares; report it for the operator and leave it.
- `sell` and `close_position` return `failed` with `cancel_unconfirmed` when the broker did not confirm cancelling the orders that hold the shares: nothing was sold, and any unprotected shares got a stop, listed under `protection_repairs`. Report it and try the exit again in a minute. If a repair there is `failed`, the shares have no stop until the cancels finish: call `mcp__trader_broker__get_risk_status` again in a minute, which places it, and report the result. Report any `protection_repairs` an exit returns.
- Never widen a stop, average down, or re-buy a stock you just closed to "get back in" the same day.
- Sells and stop raises work during a halt or with the kill switch set. Protecting the account always comes first.
- An error with reason `busy` means another trader-broker process holds the lock and nothing was done. Try the same call again in a minute.

## Verification

Every open position has a decision (hold, raise stop, partial sell or close) with a one-line reason, and every position held has a stop at the end.
