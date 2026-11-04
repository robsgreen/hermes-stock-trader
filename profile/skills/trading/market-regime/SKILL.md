---
name: market-regime
description: Read the US market regime from SPY and QQQ trend and breadth, check halts and event risk, and decide how hard to press today (press, normal, defensive or stand aside).
version: 1.0.0
metadata:
  hermes:
    category: trading
    tags: [trading, market, regime]
    requires_toolsets: [mcp-trader_broker]
---

# Market Regime

## When to Use

At the start of every trading run, before screening or analysing any stock. The posture you set here decides how many new entries you look for today and how picky you are.

## Procedure

1. Call `mcp__trader_broker__get_risk_status`. If `kill_switch` or `daily_halt` is true, the posture is **stand aside**: no new buys are possible today. Say so and skip to step 6.
2. Call `mcp__trader_broker__get_bars` for `SPY` and for `QQQ` with `timeframe="1Day"` and `limit=260`. For each, work out the last close against its 20-, 50- and 200-day simple moving averages, whether the 50-day average is rising or falling over the last 10 sessions, and the 5-day change.
3. Gauge breadth. Call `mcp__trader_broker__get_movers` with `limit=20` and compare the size and spread of the gainers with the losers. Call `mcp__trader_broker__get_bars` (`"1Day"`, `limit=60`) for `IWM` and the sector funds `XLK`, `XLF`, `XLE`, `XLV`, `XLY` and `XLI`, and count how many close above their 20-day average.
4. Check event risk. Call `mcp__trader_broker__get_news` with no symbols and `limit=20`, and use `web_search` for today's US economic calendar (CPI, PPI, jobs report, FOMC decision or minutes, major index earnings).
5. Pick one posture:
   - **Press**: SPY and QQQ above a rising 50-day and above the 200-day, most sector funds above their 20-day, no major event today. Look for up to 3 new entries; strong and good setups both qualify.
   - **Normal**: SPY above its 200-day but trend or breadth mixed. Up to 2 new entries, strong setups only.
   - **Defensive**: SPY below its 50-day, breadth weak, or a major event today. At most 1 new entry, only an exceptional setup; tighten stops on laggards.
   - **Stand aside**: SPY below a falling 50-day and below its 200-day, or a halt is active. No new buys.
6. Write a regime line for the report: posture, SPY and QQQ close against their averages, breadth count, today's event risk, and any halt.

## Pitfalls

- The posture limits how many entries you look for; it never changes position size. The server sizes every order.
- Do not upgrade the posture because one stock looks great. Strong stocks in a weak tape fail more often.
- A halt or kill switch cannot be cleared by you; only the operator can reset the kill switch.

## Verification

The report states one posture and the numbers behind it, and the posture matches the rules in step 5.
