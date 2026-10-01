# Bybit Demo Automation

This integration is deliberately **demo-only**. The code hard-codes `https://api-demo.bybit.com` and refuses any other Bybit base URL.

## What it does

The existing BTC/ETH/SOL/ZEC swing watcher remains the signal engine. GitHub Actions starts every 5 minutes, then runs five internal cycles roughly 60 seconds apart. Each cycle refreshes all four swing states and, when demo credentials are configured, evaluates them for Bybit demo execution.

Auto-entry states:

- `LONG_TRIGGERED`
- `SHORT_TRIGGERED`
- `ADD_ALLOWED` only when a same-direction demo position already exists

Never auto-entered:

- `EARLY_SETUP`
- `ARMED_LONG`
- `ARMED_SHORT`
- `NO_TRADE`
- `MISSED_DO_NOT_CHASE`

`INVALIDATED` closes an existing demo position with a reduce-only market order. `HOLD_MANAGE` does nothing.

## Swing-only safety gates

The executor accepts schema `1.3.0+` only and requires `hard_stop_type=core_swing_structure_volatility`.

Minimum accepted stop distance:

- BTC: 0.65%
- ETH: 0.85%
- SOL: 1.00%
- ZEC: 1.25%

This explicitly rejects scalp-like geometry such as a 0.1% stop.

Default sizing:

- `1R = 1%` of demo account equity
- initial trigger = `0.50R`
- add = `0.35R`
- max tracked risk per asset = `1.25R`
- notional cap = `2x` account equity

Wider swing stops reduce quantity; they do not increase the R budget.

Every demo entry/add attaches an exchange-side MarkPrice stop loss. Local checkpoints are not automatic take-profit orders.

## Bybit setup

1. Log in to the normal Bybit site/account.
2. Switch to **Demo Trading**.
3. From the Demo Trading account, create an API key and secret with trading access.
4. Do not paste the key or secret into ChatGPT, issues, commits, files, or logs.
5. In GitHub repository settings, add Actions secrets:
   - `BYBIT_DEMO_API_KEY`
   - `BYBIT_DEMO_API_SECRET`
6. Keep the Bybit demo derivatives position mode in **one-way mode** (`positionIdx=0`).

Once both secrets exist, the scheduled execution workflow automatically enables demo order submission. If either secret is missing, the workflow refreshes signals but skips all Bybit orders.

## Persisted demo files

- `bybit_demo_state.json`: dedup/risk ledger and last executor results
- `bybit_demo_history.csv`: demo order decisions and returned Bybit order IDs

No secret is written to either file.

## Important limitation

GitHub Actions cron itself is still 5-minute minimum/best-effort scheduling. The workflow approximates 1-minute polling by staying alive for five internal cycles. If GitHub delays the job start, the whole five-minute block is correspondingly delayed. A continuously running process/WebSocket host is still the correct architecture for true low-latency execution.
