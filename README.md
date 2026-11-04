# hermes-stock-trader

A Hermes Agent profile that swing-trades US stocks on Alpaca, with the risk limits enforced in code rather than in the prompt.

> **Risk warning.** This is an experiment, not financial advice. LLM trading agents commonly lose money, and nothing here has a track record. The server starts in paper mode (simulated money). Run it on paper for at least the full go-live period, read every report, and trade real money only with an amount you can afford to lose entirely. A stop order does not protect you from a price gap. You alone are responsible for any trade it places.

## What it does

The repository holds two things:

- **`trader-broker`**, a Python MCP server and command-line tool (`src/trader_broker/`). It is the agent's only path to the broker. It sizes every order, enforces the caps and halts, has two models from other vendors (OpenAI and xAI) review each buy, places a bracket order with a stop and a target, and journals the trade.
- **The `stock-trader` Hermes profile** (`profile/`). Claude decides what to buy and why, through five trading skills and five scheduled jobs on US market days. The profile switches off Hermes' terminal, code-execution, file and browser tools, so the agent cannot change its limits, read its API keys or reach Alpaca directly. Reports go to a trading channel through your existing Discord and Telegram bots.

The default limits: 3% of equity at risk per trade, 20% of equity per symbol, a halt on new buys after a 4% daily loss, and a kill switch at a 20% drawdown from peak. Long only, no margin, no options. They are set in the profile's `.env`; no agent tool can change them.

How it works in detail: [docs/system/architecture.md](docs/system/architecture.md).

## Requirements

- A Linux server where Hermes Agent (v2026.9.24 or later) already runs, with its Discord and/or Telegram bots on the default profile.
- `git` and [`uv`](https://docs.astral.sh/uv/) on that server. uv installs Python 3.12 itself if needed.
- An [Alpaca](https://alpaca.markets/) account with **paper trading** API keys. The free market-data plan is enough.
- API keys for Anthropic (the proposer, Claude), OpenAI (reviewer 1) and xAI (reviewer 2).

## Setup

Run these steps as the user that runs Hermes. The commands assume the checkout lives at `~/hermes-stock-trader`; the profile is installed into `~/.hermes/profiles/stock-trader/`.

### 1. Clone and install

```bash
git clone https://github.com/robsgreen/hermes-stock-trader.git ~/hermes-stock-trader
cd ~/hermes-stock-trader
uv sync
```

### 2. Install the profile: dry run first

```bash
scripts/install-profile.sh --model <claude-model-id> --dry-run
```

The dry run prints every file it would write and every `hermes` command it would run, and changes nothing under `~/.hermes` (uv may still create the checkout's `.venv` to read the job list). When it looks right, install:

```bash
scripts/install-profile.sh --model <claude-model-id>
```

`--model` is the Claude model id written into the profile's `config.yaml` (you can set `ANTHROPIC_MODEL` instead). The installer:

- creates the `stock-trader` profile without Hermes's bundled skills (`--no-skills`), and copies in its config, persona, trading skills and gate script;
- writes `~/.hermes/profiles/stock-trader/.env` from `profile/.env.example` while that file has no `NAME=value` line (absent, or the comment-only placeholder Hermes creates with the profile). A `.env` with settings is never overwritten;
- creates the five scheduled jobs **paused**. `--enable-jobs` would create them active; do not use it on a first install.

It never edits the default profile's `~/.hermes/config.yaml`. At the end it prints the routes snippet you need in step 6. Re-running it is safe: changed files are backed up with a timestamp suffix before they are replaced, and an existing job whose schedule, prompt, skills, gate script or delivery targets differ from `profile/cron/jobs.yaml` is updated with `hermes cron edit`, which keeps it paused or active as it was. Jobs are matched by name, so a job you rename is created again.

### 3. Fill in the profile's `.env`

Edit `~/.hermes/profiles/stock-trader/.env`. Every variable must keep a value; the file's comments explain each one. At minimum fill in:

| Variable | Value |
|---|---|
| `ANTHROPIC_API_KEY` | Your Anthropic key (the proposer). |
| `ALPACA_API_KEY`, `ALPACA_SECRET_KEY` | Your Alpaca **paper** keys. |
| `TRADER_BROKER_MODE` | Leave at `paper`. Leave `TRADER_BROKER_LIVE_ACK` empty. |
| `REVIEWER_1_MODEL` | An OpenAI model id for reviewer 1. |
| `REVIEWER_2_MODEL` | An xAI (Grok) model id for reviewer 2. |
| `OPENAI_API_KEY`, `XAI_API_KEY` | The reviewers' keys. |
| `TRADER_DISCORD_TARGET`, `TRADER_TELEGRAM_TARGET` | Where reports go, e.g. `discord:<channel_id>` and `telegram:<group_chat_id>`. Use the trading channel and group you route in step 6. |

The risk caps (`TRADER_RISK_PCT` and the rest) are already set to the defaults above.

If the targets were empty when you ran the installer, it warned you. Re-run it now; it sets the targets on the existing jobs and changes nothing else:

```bash
scripts/install-profile.sh --model <claude-model-id>
```

### 4. Load the profile's settings in your shell

`trader-broker` reads its settings from the environment. This shell function runs it with the profile's `.env` loaded, without leaving the keys in your shell; add it to `~/.bashrc`:

```bash
tb() {
    (set -a; . ~/.hermes/profiles/stock-trader/.env; set +a
     uv run --directory ~/hermes-stock-trader trader-broker "$@")
}
```

The rest of this guide uses `tb`. Without it, run `uv run --directory ~/hermes-stock-trader trader-broker <command>` after loading the `.env` yourself.

### 5. Create the state database and run the paper smoke test

```bash
tb init-state
tb smoke-test
```

`init-state` creates the state database under `TRADER_BROKER_STATE_DIR/paper/`, with the equity peak at the account's current equity, and starts the paper period the go-live report measures from. Without a database every buy is blocked.

`smoke-test` checks the Alpaca paper account end to end. It fetches a quote and daily bars, places a 1-share bracket buy for SPY at half the last close (so it cannot fill), checks the broker kept every price, finds it by client order id, and cancels it. Every line must read `PASS`. It runs only in paper mode. The same checks also run as a test:

```bash
(set -a; . ~/.hermes/profiles/stock-trader/.env; set +a; uv run pytest -m paper)
```

**Do not go on until the smoke test passes.** If a step fails, fix the keys or account first; if the cleanup step fails, cancel the `smoke-...` order in the Alpaca dashboard.

### 6. Route a trading channel to the profile

The profile has no bot tokens; it uses the default profile's bots. Create a dedicated trading channel in your Discord server and/or a dedicated Telegram group, and add your bot to them. Then merge the routes the installer printed (also in `profile/profile_routes.example.yaml`) into the `gateway:` section of the **default** profile's `~/.hermes/config.yaml`, replacing the example ids with your channel and group ids:

```yaml
gateway:
  multiplex_profiles: true
  profile_routes:
    - name: stock-trader-discord
      platform: discord
      guild_id: "<your server id>"
      chat_id: "<trading channel id>"
      profile: stock-trader
    - name: stock-trader-telegram
      platform: telegram
      chat_id: "<trading group chat id>"
      profile: stock-trader
```

**Never route a direct-message chat id.** A Telegram DM's chat id is the user's own id, so a DM route would send all of that person's private chats with the bot to the trading agent.

**Anyone who can post in a routed chat can command the trader**, as long as the default profile's bot accepts them: ask it to sell, close positions or propose buys. A route only picks the profile; who may talk to the bot is still decided by the default profile's allowlist. So:

- make the trading channel and group private, with only you (and the bot) able to post;
- keep the default profile's user allowlist to people you trust with the account.

A route can also carry a `user_id`, which matches only that sender. It does not lock the chat down on its own: cron delivery needs a route without `user_id` for the same chat, and that route matches every sender.

Restart the Hermes gateway so it reads the routes and the profile's `.env`.

### 7. Check the install by hand

Hermes itself is not covered by the automated tests, so walk through this checklist once before enabling the jobs:

- [ ] `hermes -p stock-trader cron list` shows five jobs, `premarket-plan`, `entries`, `midday`, `close-review` and `weekly-report`, all paused and each with your delivery targets.
- [ ] `hermes -p stock-trader chat`, then ask for the risk status. The agent calls `mcp__trader_broker__get_risk_status`, and its answer matches `tb status`. If the agent reports no trader-broker tools, the server failed to start: check the reviewer models and keys in `.env`.
- [ ] In the same chat, ask the agent to run a shell command, read a file, or raise its risk limit. It has no tool for any of these and says so.
- [ ] Post a message in the trading channel (and the Telegram group). The `stock-trader` profile answers. A DM to the bot is still answered by your default profile.
- [ ] The server log, `~/.local/share/trader-broker/paper/logs/trader-broker.log` by default, shows `tool_call` events from the chat, with API keys shown as `[REDACTED]`.
- [ ] `tb market-open-gate` prints `{"wakeAgent": true}` on a trading day and `{"wakeAgent": false}` on a weekend or market holiday.

### 8. Enable the jobs

Resume each job:

```bash
hermes -p stock-trader cron list
hermes -p stock-trader cron resume <job id>
```

All times are US Eastern; the four market-day jobs skip weekends and market holidays:

| Job | When | Does |
|---|---|---|
| `premarket-plan` | 09:00 Mon–Fri | Market regime and a shortlist; no orders. |
| `entries` | 09:45 Mon–Fri | Analyses the shortlist and proposes buys. |
| `midday` | 12:30 Mon–Fri | Reviews positions, raises stops, exits trades with earnings before the next session, at most one new entry. |
| `close-review` | 15:45 Mon–Fri | Exits, cancels unfilled entries, records lessons, posts the daily report. On an early-close day the market is already shut, so it lists the exits for you instead of placing them. |
| `weekly-report` | 16:30 Fri | Weekly scorecard against SPY and the go-live bar. |

On the next trading day the 09:00 plan should appear in your trading channel.

## Operating it

| Command | When |
|---|---|
| `tb status` | Any time: equity, day P&L, drawdown, halts, exposure, open entries and recent blocks. It also runs housekeeping, which can cancel stale entries, cancel take-profit or limit sells that hold shares back from a stop, place missing stops, and market-sell a position whose bid is already at or below its stop (with no bid it places the stop instead). It waits for the account lock that running MCP servers share, and exits 1 with a message if another process holds it too long; run it again shortly. |
| `tb report` | Weekly: scores the paper period against the go-live bar (`--json` for JSON). This is the authoritative pass or fail; the agent's Friday scorecard is not. |
| `tb reset-kill-switch` | After the kill switch trips (a 20% drawdown from peak). Clears it and restarts the peak at current equity. Find out why it tripped first. |
| `tb init-state --force` | Only to replace a lost or corrupt state database, or one from an older schema (the server reads a schema-1 database as missing, so buys are blocked until you run this). It discards the journal, reviews, blocks and halts. Restart the gateway afterwards. |

After any change to the profile's `.env`, restart the gateway; `trader-broker` reads its settings only when it starts. To update, `git pull` in the checkout and re-run `scripts/install-profile.sh --model <claude-model-id>` (dry run first). If the update changed the state schema (the server log warns that the state database `has schema version 1, expected 2`, and every buy is blocked with `state_unavailable`), run `tb init-state --force`, which discards the existing history, then restart the gateway. Changes the agent proposes to its own skills wait for your approval under `/skills pending`.

When something goes wrong (a reviewer outage, a broker timeout, a missing database), see [Failure modes](docs/system/architecture.md#failure-modes-and-the-live-mode-guard).

## Going live

**Stay on paper until `tb report` shows every criterion as PASS:**

| Criterion | Pass when |
|---|---|
| Time | At least 8 weeks of paper trading since `init-state`. |
| Return | The portfolio's return beats SPY over the same days. |
| Drawdown | The maximum drawdown stays under 25%. |
| Integrity | Reconciliation found no integrity violations. |

Passing unlocks nothing. Live trading is a manual change you make only after deciding, on your own, to risk real money:

1. In the profile's `.env`, replace the Alpaca keys with live keys, and set `TRADER_BROKER_MODE=live` and `TRADER_BROKER_LIVE_ACK=I_ACCEPT_REAL_MONEY_RISK`. Without that exact phrase every `trader-broker` command, the MCP server included, refuses to start.
2. Run `tb init-state`. Live mode keeps its own state database and logs under `TRADER_BROKER_STATE_DIR/live/`, so it starts with a fresh peak and journal. `smoke-test` does not run in live mode.
3. Restart the gateway.

To go back to paper, restore the paper keys, set `TRADER_BROKER_MODE=paper`, empty `TRADER_BROKER_LIVE_ACK`, and restart the gateway.

## Development

```bash
uv sync
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run pytest -m "not paper"
```

See [AGENTS.md](AGENTS.md) for the repository layout and the rules for changing it.
