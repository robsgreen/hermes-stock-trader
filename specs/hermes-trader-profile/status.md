# Status: hermes-trader-profile

Last Updated: 2026-11-03 13:12
Branch: (none)
Worktree: (none)
PR: (none)

## Checklist

- [x] **Planning**
- [x] **Task Generation**
- [ ] **Implementation**
- [ ] **Testing**
- [ ] **Documentation**
- [ ] **Review**
- [ ] **Wrap-up**
- [ ] **PR**
- [ ] **Archive**

## Session Log

- 2026-11-03 13:12 - Spec created
- 2026-11-03 13:12 - Permissions Setup skipped: settings.local.json write denied by auto-mode classifier
- 2026-11-03 13:15 - Research in: rules/risk, Alpaca MCP, Hermes profiles; awaiting trading-agents survey
- 2026-11-03 13:15 - Research in: trading-agents survey (TradingAgents pattern, tradermonty skills, AlpacaTradingAgent caps; live LLM results weak)
- 2026-11-03 13:17 - User decision Q1: A — paper trading first, live only after passing a bar set in the plan
- 2026-11-03 13:20 - User decision Q2: A — swing trading, ~3-5 scheduled runs/day, free IEX data
- 2026-11-03 13:21 - User decision Q3: Hermes already running on DigitalOcean; providers OpenAI, xAI, Anthropic; reuse existing Discord + Telegram channels (open item: per-profile bot token vs multi-profile gateway)
- 2026-11-03 13:25 - User decision Q4: no leverage (gross ≤1.0x), long-only, 3% risk/trade at stop, 4% daily-loss halt, 20%/symbol, 20% drawdown kill; go-live bar 8wk paper, beat SPY, DD<25%, no gate failures, explicit yes
- 2026-11-03 13:29 - Research in: Hermes pre_tool_call hook can block (fails closed, runs in cron); profile_routes lets trader cron post via default bots; alpaca generated tools lack readOnlyHint (no trust:untrusted); #129 likely not stock-affecting
- 2026-11-03 14:46 - User decisions: approach A (safe broker MCP server, broker adapter for future Robinhood etc.); design checkpoint approved; add cross-model review (Claude proposes, GPT + Grok review), aggressive rule
- 2026-11-03 14:49 - master-plan.md written; refinement pass done
- 2026-11-03 14:53 - Plan review: 21 findings, 21 incorporated, 0 rebutted (M5 resolved by dropping proposer fallback)
- 2026-11-03 17:25 - User confirmed master plan (profile renamed to stock-trader)
- 2026-11-03 17:25 - Phase → Task Generation
- 2026-11-03 17:33 - Task Generation: 18 tasks / 102 sub-tasks, 37 acceptance items
- 2026-11-03 17:33 - Phase → Implementation
