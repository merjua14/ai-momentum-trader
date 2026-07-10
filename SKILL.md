# Strategy: One-a-Day Momentum, Stocks or Options (conviction-picked)

The playbook behind `momentum_trader.py`. One position, one entry per day. The
strategy and the risk math are deterministic. The single model judgment is
which trade to take: the name, the direction, and (in `both` mode) whether to
express it in shares or a single-leg option.

## The loop, one tick at a time

1. Outside the regular session: idle.
2. Holding a position: manage the trailing stop (and, for options, the expiry
   guard). This runs every tick, every day, until the stop trips. No forced
   close at the bell.
3. Flat: only consider a new entry if you have not already entered today and
   did not sell today, and only after the warm-up window past the open.

## Picking the trade

After the warm-up, the runner builds the candidate list: universe names moving
at least `candidate_min_daychg` percent on the day (green only, or red too when
`allow_puts` is on), each with day change, last price, and beta. Optionally a
beta floor (`candidate_min_beta`) filters out sleepy names. The list goes to
the chosen AI, which may web search (Claude path) for catalysts, earnings
dates, and halts, and returns ONE pick — symbol, direction, instrument,
conviction — or declines. The trade only happens if conviction clears
`conviction_accept`.

## Instruments

- **Shares**: dollar-based market buy of `deploy_fraction` × settled cash.
- **Call / put**: the runner (not the AI) selects the contract — expiry between
  `opt_dte_min` and `opt_dte_max` days, delta nearest `opt_delta_target`, sane
  spread and open interest — then buys as many whole contracts as fit the same
  budget. If zero contracts fit, `both` mode falls back to shares.

## Sizing and the settlement guard

Three rules make a good-faith settlement violation structurally impossible on
a cash account: at most one entry per day, never enter on the same calendar
day as a sale (so under T+1 the cash behind any buy has settled), and size to
settled cash, not raw buying power.

## The trailing stop (deterministic)

State per position: entry, peak, stop, breakeven_armed.

**Shares** are stopped on the share price with the profile's share bands.
**Options** are stopped on the contract's mark with the (much wider) premium
bands, because premium moves several times faster than the underlying:

- Before the trigger: stop sits at entry × (1 − initial stop).
- At +`activate_pct` (or `opt_activate_pct`): the stop jumps to breakeven.
- After arming: the stop trails `trail_pct` (or `opt_trail_pct`) under the
  peak and ratchets up only.
- Exit: when the price or the mark touches the stop, close at market/limit.
  The stop is the only exit — except for options, where the **expiry guard**
  force-closes any contract at `opt_close_dte` days to expiration.

Robinhood cannot rest stops on fractional shares or trail option premium, so
the runner manages every stop in software, every tick. That is why it must
keep running.

## Broker reconciliation (the desync fix)

Every tick, before anything else, the runner compares its records to the
broker — shares and option contracts both. If it thinks it holds something the
account no longer shows (you closed it by hand), it marks the exit, records
the sale date, and goes flat. A manual exit can never freeze the runner.

## Which AI makes the pick

Claude (default, via the Claude Code CLI, with web search), OpenAI, Grok, or
Gemini. Order execution always runs through the Robinhood MCP via Claude Code
regardless — the chosen provider is the decision brain only.

## Direction

Long shares and long calls express "up." Long puts (off by default) express
"down" with defined risk. There is no shorting and no multi-leg anything —
single positions only, so every trade's worst case is knowable in advance.
