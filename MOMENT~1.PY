#!/usr/bin/env python3
"""
momentum_trader.py

One momentum trade per day on a Robinhood account, in STOCKS, OPTIONS, or BOTH.

How it works in one breath: once a day, after a warm-up window past the open,
the runner builds a list of names from your universe that are moving on the day
(with each name's beta), hands that list to your chosen AI (Claude, OpenAI,
Grok, or Gemini), and the AI returns ONE pick with a conviction rating, a
direction, and an instrument (shares, a call, or a put), or passes. If the pick
clears your conviction gate, the runner deploys cash into it (how much depends
on your risk profile) and then carries the position on a ratcheting trailing
stop, including overnight, until the stop trips or (for options) expiry gets
too close. The AI only chooses WHAT to buy. Every dollar of risk math (sizing,
stops, the daily loss breaker, the expiry guard) is plain deterministic Python.

Three things you set, all at the top:
  1. ACTIVE_PROFILE: "conservative", "normal", or "degen"  (or "custom")
  2. asset_mode: "stocks", "options", or "both"
  3. pick_provider: "claude" (default, uses Claude Code, no API key needed),
     or "openai" / "grok" / "gemini" (each needs that provider's API key)

The broker rail (quotes, account, orders, option chains) always runs through
the Robinhood agentic MCP via the Claude Code CLI, no matter which AI makes the
pick. So you need Claude Code installed and the Robinhood MCP authorized either
way. Your chosen pick provider is just the decision brain.

READ THE README BEFORE YOU RUN THIS WITH REAL MONEY.
This is an experiment, not a money machine. It trades real money with no
approval prompts. Options can and do go to ZERO. The degen profile puts your
whole balance behind a single idea. It holds overnight. The code is unaudited.
Not financial advice. Use money you can afford to lose, and start tiny.
"""

import argparse
import datetime as dt
import json
import os
import platform
import subprocess
import sys
import time
from pathlib import Path
from zoneinfo import ZoneInfo

# ============================================================================
# >>> CONFIG BUILDER PASTE STARTS HERE <<<
# (docs/index.html generates everything from this line down to the matching
#  "PASTE ENDS HERE" marker. Select between the two markers and paste over.)
# ============================================================================
# 1. PICK A RISK PROFILE. This is the easy knob. Change one word.
# ============================================================================
# Each profile sets how much it bets, how picky it is, and how the stops behave
# for shares and for option premium (options move much faster, so their stops
# are wider on purpose).
#
#   conservative: small size, strongest setups only, tight stops, locks gains
#                 fast. In options mode it buys deeper-in-the-money contracts
#                 with more time on the clock. Built to protect capital.
#   normal:       balanced. Half the cash, high conviction only. A reasonable
#                 default to learn the system on.
#   degen:        full port behind one idea, takes medium conviction too, jumps
#                 in earlier, gives the trade lots of room. Highest risk by far,
#                 and in options mode that means the premium can go to zero.
#
# To roll your own, edit the "custom" profile below and set ACTIVE_PROFILE
# to "custom".
PROFILES = {
    "conservative": {
        # ---- shared ----
        "deploy_fraction": 0.25,      # bet a quarter of settled cash
        "conviction_accept": ["high"],
        "candidate_min_daychg": 3.0,  # only strong movers qualify
        "candidate_min_beta": 0.0,    # 0 = beta floor off; try 1.2 to demand movers
        "entry_after_minutes": 45,    # wait longer for the trend to prove itself
        "max_daily_loss_usd": 25.0,   # halt new entries after this much loss
        # ---- share stops ----
        "initial_stop_pct": 0.010,    # 1.0% protective stop
        "activate_pct": 0.010,        # arm breakeven at +1.0%
        "trail_pct": 0.005,           # trail 0.5% under the peak
        # ---- option premium stops (on the contract's mark, not the stock) ----
        "opt_initial_stop_pct": 0.15, # cut the contract at -15% premium
        "opt_activate_pct": 0.10,     # arm breakeven at +10% premium
        "opt_trail_pct": 0.10,        # then trail 10% under the premium peak
        # ---- option contract selection ----
        "opt_dte_min": 21,            # more time = less theta bleed
        "opt_dte_max": 45,
        "opt_delta_target": 0.60,     # deeper in the money, moves like stock
    },
    "normal": {
        "deploy_fraction": 0.50,
        "conviction_accept": ["high"],
        "candidate_min_daychg": 2.0,
        "candidate_min_beta": 0.0,
        "entry_after_minutes": 30,
        "max_daily_loss_usd": 50.0,
        "initial_stop_pct": 0.015,
        "activate_pct": 0.010,
        "trail_pct": 0.010,
        "opt_initial_stop_pct": 0.25,
        "opt_activate_pct": 0.15,
        "opt_trail_pct": 0.15,
        "opt_dte_min": 14,
        "opt_dte_max": 30,
        "opt_delta_target": 0.50,     # at the money
    },
    "degen": {
        "deploy_fraction": 1.00,      # full port behind one idea
        "conviction_accept": ["high", "medium"],
        "candidate_min_daychg": 1.5,
        "candidate_min_beta": 0.0,
        "entry_after_minutes": 15,
        "max_daily_loss_usd": 250.0,
        "initial_stop_pct": 0.030,
        "activate_pct": 0.020,
        "trail_pct": 0.020,
        "opt_initial_stop_pct": 0.40, # give the premium a lot of room
        "opt_activate_pct": 0.25,
        "opt_trail_pct": 0.25,
        "opt_dte_min": 7,             # shorter dated, cheaper, faster, riskier
        "opt_dte_max": 21,
        "opt_delta_target": 0.40,     # slightly out of the money, more leverage
    },
    # Edit these freely, then set ACTIVE_PROFILE = "custom".
    "custom": {
        "deploy_fraction": 0.50,
        "conviction_accept": ["high"],
        "candidate_min_daychg": 2.0,
        "candidate_min_beta": 0.0,
        "entry_after_minutes": 30,
        "max_daily_loss_usd": 50.0,
        "initial_stop_pct": 0.015,
        "activate_pct": 0.010,
        "trail_pct": 0.010,
        "opt_initial_stop_pct": 0.25,
        "opt_activate_pct": 0.15,
        "opt_trail_pct": 0.15,
        "opt_dte_min": 14,
        "opt_dte_max": 30,
        "opt_delta_target": 0.50,
    },
}

ACTIVE_PROFILE = "normal"   # "conservative" | "normal" | "degen" | "custom"

# ============================================================================
# 2. THE REST OF THE CONFIG
# ============================================================================
CONFIG = {
    # The agentic-enabled Robinhood account the bot trades. Ask Claude
    # "list my robinhood accounts" once the MCP is authorized.
    "account_number": "YOUR_ACCOUNT_NUMBER",

    # MCP server name exactly as it shows in `claude mcp list`.
    "mcp_server": "robinhood",

    # ----- what the bot is allowed to buy -----
    # "stocks":  shares only, exactly the classic momentum bot.
    # "options": single-leg long calls (and long puts if allow_puts) only.
    # "both":    the AI chooses shares or a contract per pick, and the runner
    #            falls back to shares if the contract does not fit the budget.
    "asset_mode": "stocks",

    # Allow bearish trades via long puts. Only applies in options/both modes.
    # When true, strong RED movers also become candidates and the AI may pick
    # direction "down" (a put). Long puts are defined-risk (you can only lose
    # the premium), but they can absolutely go to zero.
    "allow_puts": False,

    # Force-close any option position this many days before it expires, no
    # matter what the stop says. Prevents waking up with an expiring contract.
    "opt_close_dte": 1,

    # The names the bot may choose from. Keep these liquid (tight option
    # spreads matter a LOT in options mode). Leveraged and inverse ETFs decay
    # on multi-day holds, so leave them out unless you mean it.
    "universe": [
        "NVDA", "AAPL", "MSFT", "AMZN", "META",
        "GOOGL", "TSLA", "AMD", "AVGO", "SPY",
    ],

    # ----- which AI makes the pick -----
    # "claude": uses your Claude Code CLI. No API key. Can web search for fresh
    #           catalysts out of the box. This is the easiest and the default.
    # "openai" / "grok" / "gemini": use that provider's chat API. Set the
    #           matching API key as an environment variable (see README).
    "pick_provider": "claude",

    # Model name per provider. These change over time, so set whatever your
    # account actually has access to.
    "openai_model": "gpt-4o",
    "grok_model": "grok-3",
    "gemini_model": "gemini-2.0-flash",

    # Your own standing instructions to the AI, appended to every pick prompt.
    # Example: "Only large caps. Avoid biotech and anything reporting earnings
    # this week. Favor AI and energy names." Leave empty for none.
    "extra_pick_guidance": "",

    # Trading window. Default is the regular US session in Eastern time.
    "tz": "America/New_York",
    "session_open": "09:30",
    "session_close": "16:00",

    # How the bot calls Claude Code headless for the broker rail. Leave as is.
    "claude_bin": "claude",
    "claude_timeout_sec": 240,
    "http_timeout_sec": 90,
}
# ============================================================================
# >>> CONFIG BUILDER PASTE ENDS HERE <<<
# ============================================================================

STATE_PATH = Path.home() / ".momentum_trader_state.json"
LOG_PATH = Path.home() / ".momentum_trader.log"


def risk():
    """Return the active risk parameters from the chosen profile."""
    if ACTIVE_PROFILE not in PROFILES:
        raise SystemExit(f"Unknown ACTIVE_PROFILE '{ACTIVE_PROFILE}'. "
                         f"Choose one of: {', '.join(PROFILES)}")
    return PROFILES[ACTIVE_PROFILE]


# ============================================================================
# Logging and Telegram notifications
# ============================================================================
def log(msg):
    stamp = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{stamp}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:
        pass


def notify(text):
    """Send a Telegram message if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are set.
    No-op (and never raises) if they are missing, so the bot runs fine without it."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    log(text)
    if not token or not chat_id:
        return
    try:
        import urllib.request
        import urllib.parse
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        data = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
        urllib.request.urlopen(url, data=data, timeout=15).read()
    except Exception as exc:
        log(f"notify failed: {exc}")


# ============================================================================
# State
# ============================================================================
def load_state():
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except Exception as exc:
            log(f"state read failed, starting clean: {exc}")
    return {
        "position": None,            # dict when held, else None
        "last_entry_date": None,     # YYYY-MM-DD, at most one entry per day
        "last_sale_date": None,      # YYYY-MM-DD, never buy on a sale day
        "pnl_date": None,            # the date day_realized_pnl belongs to
        "day_realized_pnl": 0.0,     # realized PnL accumulated today
    }


def save_state(state):
    STATE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")


# ============================================================================
# Small JSON helpers
# ============================================================================
def extract_json(text):
    if not text:
        return {"error": "empty model output"}
    cleaned = text.replace("```json", "").replace("```", "").strip()
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end == -1 or end < start:
        return {"error": f"no JSON found: {cleaned[:300]}"}
    try:
        return json.loads(cleaned[start:end + 1])
    except Exception as exc:
        return {"error": f"JSON parse failed: {exc}: {cleaned[:300]}"}


def http_post(url, headers, payload, timeout):
    import urllib.request
    body = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


# ============================================================================
# The Claude Code headless bridge. This is the broker rail (quotes, account,
# orders, option chains) and also the default pick brain. Strict JSON only.
# ============================================================================
def claude_cli(prompt, allowed_tools=None, timeout=None):
    """Run `claude -p` headless and return raw stdout text, or '' on failure."""
    timeout = timeout or CONFIG["claude_timeout_sec"]
    args = [CONFIG["claude_bin"], "-p", "--dangerously-skip-permissions",
            "--output-format", "text"]
    if allowed_tools:
        args += ["--allowedTools", ",".join(allowed_tools)]
    args += [prompt]

    run_kwargs = {"capture_output": True, "text": True, "timeout": timeout}
    if platform.system() == "Windows":
        run_kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW

    try:
        proc = subprocess.run(args, **run_kwargs)
    except subprocess.TimeoutExpired:
        log("claude call timed out")
        return ""
    except FileNotFoundError:
        log("claude binary not found on PATH")
        return ""
    except Exception as exc:
        log(f"claude call failed: {exc}")
        return ""
    return (proc.stdout or "").strip()


def claude_json(prompt, allowed_tools=None, timeout=None):
    return extract_json(claude_cli(prompt, allowed_tools, timeout))


def mcp_tools():
    s = CONFIG["mcp_server"]
    return [f"mcp__{s}__*"]


# ============================================================================
# Broker reads and writes, each a scoped Claude + MCP call returning JSON
# ============================================================================
def get_account_snapshot():
    acct = CONFIG["account_number"]
    prompt = (
        f"Use the {CONFIG['mcp_server']} MCP. For Robinhood account {acct}, get the "
        f"settled cash, the list of open equity positions, and the list of open option "
        f"positions. Reply with ONLY a JSON object, no prose, shaped exactly like: "
        f'{{"settled_cash": 0.0, '
        f'"positions": [{{"symbol": "ABC", "quantity": 0.0}}], '
        f'"option_positions": [{{"occ_symbol": "ABC...", "underlying": "ABC", '
        f'"contracts": 0}}]}}'
    )
    res = claude_json(prompt, allowed_tools=mcp_tools())
    if "error" in res:
        return None, res["error"]
    res.setdefault("settled_cash", 0.0)
    res.setdefault("positions", [])
    res.setdefault("option_positions", [])
    return res, None


def get_quotes(symbols):
    prompt = (
        f"Use the {CONFIG['mcp_server']} MCP. Get current quotes for these symbols: "
        f"{', '.join(symbols)}. For each, compute day change percent as "
        f"(last - previous_close) / previous_close * 100, and include the stock's beta "
        f"from fundamentals if available (null if not). Reply with ONLY a JSON object "
        f"mapping symbol to fields, no prose, shaped exactly like: "
        f'{{"ABC": {{"last": 0.0, "day_change_pct": 0.0, "beta": null}}}}'
    )
    res = claude_json(prompt, allowed_tools=mcp_tools())
    if "error" in res:
        return None, res["error"]
    return res, None


def get_last_price(symbol):
    quotes, err = get_quotes([symbol])
    if err:
        return None, err
    q = quotes.get(symbol) or {}
    last = q.get("last")
    if last is None:
        return None, f"no last price for {symbol}"
    return float(last), None


def place_buy(symbol, dollars):
    acct = CONFIG["account_number"]
    prompt = (
        f"Use the {CONFIG['mcp_server']} MCP. On Robinhood account {acct}, FIRST review, "
        f"THEN place a real market BUY of {symbol} for a dollar amount of {dollars:.2f} "
        f"(dollar-based, fractional allowed). After it fills, reply with ONLY a JSON object, "
        f"no prose, shaped exactly like: "
        f'{{"filled": true, "symbol": "{symbol}", "avg_price": 0.0, "quantity": 0.0, "order_id": "..."}}. '
        f'If it did not fill, reply {{"filled": false, "reason": "..."}}.'
    )
    res = claude_json(prompt, allowed_tools=mcp_tools())
    if "error" in res:
        return None, res["error"]
    if not res.get("filled"):
        return None, res.get("reason", "buy not filled")
    return res, None


def place_sell_all(symbol, quantity):
    acct = CONFIG["account_number"]
    prompt = (
        f"Use the {CONFIG['mcp_server']} MCP. On Robinhood account {acct}, place a real "
        f"market SELL closing the entire {symbol} position (quantity {quantity}). After it "
        f"fills, reply with ONLY a JSON object, no prose, shaped exactly like: "
        f'{{"filled": true, "symbol": "{symbol}", "avg_price": 0.0, "quantity": 0.0, "order_id": "..."}}. '
        f'If it did not fill, reply {{"filled": false, "reason": "..."}}.'
    )
    res = claude_json(prompt, allowed_tools=mcp_tools())
    if "error" in res:
        return None, res["error"]
    if not res.get("filled"):
        return None, res.get("reason", "sell not filled")
    return res, None


# ============================================================================
# Option chain reads and option orders (all via the same MCP rail)
# ============================================================================
def find_option_contract(symbol, direction, r):
    """Ask the MCP for the single-leg contract that best fits the profile:
    a call for direction 'up', a put for 'down', expiring opt_dte_min to
    opt_dte_max days out, with delta closest to opt_delta_target, and a
    reasonable bid-ask spread. Returns the contract dict or an error."""
    kind = "call" if direction == "up" else "put"
    prompt = (
        f"Use the {CONFIG['mcp_server']} MCP. Look at the option chain for {symbol}. "
        f"Find the single {kind.upper()} contract that best matches ALL of this: "
        f"expiration between {r['opt_dte_min']} and {r['opt_dte_max']} days from today, "
        f"absolute delta closest to {r['opt_delta_target']:.2f}, decent open interest, "
        f"and a bid-ask spread under roughly 10 percent of the mark. "
        f"Reply with ONLY a JSON object, no prose, shaped exactly like: "
        f'{{"found": true, "occ_symbol": "...", "underlying": "{symbol}", '
        f'"type": "{kind}", "strike": 0.0, "expiration": "YYYY-MM-DD", '
        f'"ask": 0.0, "mark": 0.0, "delta": 0.0}}. '
        f'If nothing fits, reply {{"found": false, "reason": "..."}}.'
    )
    res = claude_json(prompt, allowed_tools=mcp_tools())
    if "error" in res:
        return None, res["error"]
    if not res.get("found"):
        return None, res.get("reason", "no suitable contract")
    return res, None


def get_option_mark(occ_symbol):
    prompt = (
        f"Use the {CONFIG['mcp_server']} MCP. Get the current quote for the option "
        f"contract {occ_symbol}. Reply with ONLY a JSON object, no prose, shaped "
        f'exactly like: {{"mark": 0.0, "bid": 0.0, "ask": 0.0}}.'
    )
    res = claude_json(prompt, allowed_tools=mcp_tools())
    if "error" in res:
        return None, res["error"]
    mark = res.get("mark") if res.get("mark") is not None else res.get("bid")
    if mark is None:
        return None, f"no mark for {occ_symbol}"
    return float(mark), None


def place_option_buy(contract, contracts):
    acct = CONFIG["account_number"]
    prompt = (
        f"Use the {CONFIG['mcp_server']} MCP. On Robinhood account {acct}, FIRST review, "
        f"THEN place a real BUY TO OPEN of {contracts} contract(s) of "
        f"{contract['occ_symbol']} ({contract['underlying']} {contract['expiration']} "
        f"{contract['strike']} {contract['type']}) as a limit order at the current ask. "
        f"After it fills, reply with ONLY a JSON object, no prose, shaped exactly like: "
        f'{{"filled": true, "occ_symbol": "...", "avg_price": 0.0, "contracts": 0, "order_id": "..."}}. '
        f'If it does not fill within a couple of minutes, cancel it and reply '
        f'{{"filled": false, "reason": "..."}}.'
    )
    res = claude_json(prompt, allowed_tools=mcp_tools())
    if "error" in res:
        return None, res["error"]
    if not res.get("filled"):
        return None, res.get("reason", "option buy not filled")
    return res, None


def place_option_sell_all(occ_symbol, contracts):
    acct = CONFIG["account_number"]
    prompt = (
        f"Use the {CONFIG['mcp_server']} MCP. On Robinhood account {acct}, place a real "
        f"SELL TO CLOSE of the entire position in {occ_symbol} ({contracts} contract(s)) "
        f"as a limit order at the current bid, and chase the bid if needed until it "
        f"fills. After it fills, reply with ONLY a JSON object, no prose, shaped exactly "
        f'like: {{"filled": true, "occ_symbol": "...", "avg_price": 0.0, "contracts": 0, '
        f'"order_id": "..."}}. If it did not fill, reply {{"filled": false, "reason": "..."}}.'
    )
    res = claude_json(prompt, allowed_tools=mcp_tools())
    if "error" in res:
        return None, res["error"]
    if not res.get("filled"):
        return None, res.get("reason", "option sell not filled")
    return res, None


# ============================================================================
# The single AI judgment: pick one name (and instrument) or pass.
# ============================================================================
def allowed_instruments():
    mode = CONFIG["asset_mode"]
    if mode == "stocks":
        return ["stock"]
    if mode == "options":
        return ["option"]
    return ["stock", "option"]


def build_pick_prompt(candidates):
    lines = []
    for c in candidates:
        beta_txt = f", beta {c['beta']:.2f}" if c.get("beta") is not None else ""
        lines.append(f"- {c['symbol']}: {c['day_change_pct']:+.2f} percent on the day, "
                     f"last {c['last']:.2f}{beta_txt}")

    instruments = allowed_instruments()
    if instruments == ["stock"]:
        inst_block = 'The instrument is always "stock".'
    elif instruments == ["option"]:
        inst_block = ('The instrument is always "option" (a single-leg long contract; '
                      "the runner picks the exact strike and expiry).")
    else:
        inst_block = ('Choose the instrument too: "stock" for steady momentum you want '
                      'to hold with tight risk, "option" when the move looks strong and '
                      "fresh enough to justify paying premium (higher beta and a hard "
                      "catalyst favor the option; a grind favors the stock).")

    if CONFIG["allow_puts"] and CONFIG["asset_mode"] in ("options", "both"):
        dir_block = ('Direction can be "up" (buy shares or a call) for upside momentum '
                     'or "down" (buy a put) for strong downside momentum.')
    else:
        dir_block = 'Direction is always "up" (long only).'

    guidance = CONFIG.get("extra_pick_guidance", "").strip()
    guidance_block = (f"\n\nAdditional standing instructions from the operator: {guidance}"
                      if guidance else "")

    return (
        "You are picking ONE momentum trade to hold on a ratcheting trailing stop. "
        "Here are today's candidates from a fixed universe (day change and beta shown):\n"
        + "\n".join(lines)
        + "\n\nPrefer a clean move with a real, durable catalyst over a thin, news-less pop. "
        "Higher beta means the name moves harder, both ways. Avoid names reporting "
        "earnings today or tomorrow, and avoid anything halted. "
        + inst_block + " " + dir_block
        + "\nRate your conviction high, medium, or low, and pick the single best trade, or pass."
        + guidance_block
        + "\nReply with ONLY a JSON object, no prose, shaped exactly like: "
        '{"decision": "buy", "symbol": "ABC", "instrument": "stock", "direction": "up", '
        '"conviction": "high", "reason": "one short line"} '
        'or {"decision": "pass", "reason": "one short line"}.'
    )


def pick_via_claude(prompt):
    # The Claude Code path can web search for fresh catalysts.
    return claude_json(prompt, allowed_tools=["WebSearch"] + mcp_tools())


def pick_via_openai_compatible(base_url, env_key, model, prompt):
    key = os.environ.get(env_key, "").strip()
    if not key:
        return {"error": f"{env_key} not set"}
    try:
        data = http_post(
            base_url,
            {"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            {"model": model, "messages": [{"role": "user", "content": prompt}]},
            CONFIG["http_timeout_sec"],
        )
        text = data["choices"][0]["message"]["content"]
        return extract_json(text)
    except Exception as exc:
        return {"error": f"provider call failed: {exc}"}


def pick_via_gemini(model, prompt):
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key:
        return {"error": "GEMINI_API_KEY not set"}
    try:
        url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
               f"{model}:generateContent?key={key}")
        data = http_post(
            url,
            {"Content-Type": "application/json"},
            {"contents": [{"parts": [{"text": prompt}]}]},
            CONFIG["http_timeout_sec"],
        )
        text = data["candidates"][0]["content"]["parts"][0]["text"]
        return extract_json(text)
    except Exception as exc:
        return {"error": f"gemini call failed: {exc}"}


def pick_name(candidates):
    prompt = build_pick_prompt(candidates)
    provider = CONFIG["pick_provider"].lower()
    if provider == "claude":
        res = pick_via_claude(prompt)
    elif provider == "openai":
        res = pick_via_openai_compatible(
            "https://api.openai.com/v1/chat/completions",
            "OPENAI_API_KEY", CONFIG["openai_model"], prompt)
    elif provider == "grok":
        res = pick_via_openai_compatible(
            "https://api.x.ai/v1/chat/completions",
            "XAI_API_KEY", CONFIG["grok_model"], prompt)
    elif provider == "gemini":
        res = pick_via_gemini(CONFIG["gemini_model"], prompt)
    else:
        return None, f"unknown pick_provider '{provider}'"
    if "error" in res:
        return None, res["error"]
    return res, None


# ============================================================================
# Session clock
# ============================================================================
def now_tz():
    return dt.datetime.now(ZoneInfo(CONFIG["tz"]))


def parse_hhmm(s):
    h, m = s.split(":")
    return int(h), int(m)


def in_session(now):
    if now.weekday() >= 5:
        return False
    oh, om = parse_hhmm(CONFIG["session_open"])
    ch, cm = parse_hhmm(CONFIG["session_close"])
    open_t = now.replace(hour=oh, minute=om, second=0, microsecond=0)
    close_t = now.replace(hour=ch, minute=cm, second=0, microsecond=0)
    return open_t <= now <= close_t


def minutes_since_open(now):
    oh, om = parse_hhmm(CONFIG["session_open"])
    open_t = now.replace(hour=oh, minute=om, second=0, microsecond=0)
    return (now - open_t).total_seconds() / 60.0


def today_str(now):
    return now.strftime("%Y-%m-%d")


def days_to_expiry(expiration_str, now):
    try:
        exp = dt.date.fromisoformat(expiration_str)
        return (exp - now.date()).days
    except Exception:
        return None


# ============================================================================
# Broker reconciliation. If state says we hold but the broker shows flat, a
# manual exit happened outside the bot. Mark it sold and resync.
# ============================================================================
def reconcile(state, snapshot, now):
    pos = state.get("position")
    if not pos:
        return False

    if pos.get("instrument") == "option":
        occ = pos.get("occ_symbol")
        held = 0
        for p in snapshot.get("option_positions", []):
            if p.get("occ_symbol") == occ:
                held = int(p.get("contracts") or 0)
                break
        if held <= 0:
            notify(f"Reconcile: state held {occ} but the account shows no contracts. "
                   f"Treating it as a manual exit and resyncing to flat.")
            state["position"] = None
            state["last_sale_date"] = today_str(now)
            save_state(state)
            return True
        return False

    symbol = pos["symbol"]
    held_qty = 0.0
    for p in snapshot.get("positions", []):
        if p.get("symbol") == symbol:
            held_qty = float(p.get("quantity") or 0.0)
            break
    if held_qty <= 0.0:
        notify(f"Reconcile: state held {symbol} but the account is flat. "
               f"Treating it as a manual exit and resyncing to flat.")
        state["position"] = None
        state["last_sale_date"] = today_str(now)
        save_state(state)
        return True
    return False


# ============================================================================
# Trailing-stop management for an open position (shares or a contract)
# ============================================================================
def roll_daily_pnl(state, now):
    if state.get("pnl_date") != today_str(now):
        state["pnl_date"] = today_str(now)
        state["day_realized_pnl"] = 0.0


def close_option(state, now, pos, why):
    fill, err = place_option_sell_all(pos["occ_symbol"], pos["contracts"])
    if err:
        notify(f"{pos['occ_symbol']}: SELL FAILED: {err}. Will retry next tick.")
        return
    proceeds = float(fill["avg_price"]) * float(fill["contracts"]) * 100.0
    cost = pos["entry"] * pos["contracts"] * 100.0
    realized = proceeds - cost
    roll_daily_pnl(state, now)
    state["day_realized_pnl"] += realized
    state["position"] = None
    state["last_sale_date"] = today_str(now)
    save_state(state)
    notify(f"{pos['occ_symbol']}: closed at {float(fill['avg_price']):.2f} ({why}). "
           f"Realized {realized:+.2f}. Day PnL {state['day_realized_pnl']:+.2f}.")


def manage_option_position(state, now, r):
    pos = state["position"]
    occ = pos["occ_symbol"]

    # Expiry guard runs before anything else. Never carry into expiration.
    dte = days_to_expiry(pos.get("expiration", ""), now)
    if dte is not None and dte <= CONFIG["opt_close_dte"]:
        notify(f"{occ}: {dte} day(s) to expiry, inside the opt_close_dte guard. Closing.")
        close_option(state, now, pos, "expiry guard")
        return

    mark, err = get_option_mark(occ)
    if err:
        log(f"manage: option quote failed for {occ}: {err}")
        return

    entry = pos["entry"]
    pos["peak"] = max(pos["peak"], mark)

    if not pos["breakeven_armed"] and mark >= entry * (1 + r["opt_activate_pct"]):
        pos["breakeven_armed"] = True
        pos["stop"] = max(pos["stop"], entry)
        notify(f"{occ}: premium trigger hit, stop armed to breakeven {pos['stop']:.2f}.")

    if pos["breakeven_armed"]:
        trailed = pos["peak"] * (1 - r["opt_trail_pct"])
        if trailed > pos["stop"]:
            pos["stop"] = trailed

    save_state(state)

    if mark <= pos["stop"]:
        notify(f"{occ}: premium stop hit at {mark:.2f} (stop {pos['stop']:.2f}). Selling all.")
        close_option(state, now, pos, "trailing stop")


def manage_stock_position(state, now, r):
    pos = state["position"]
    symbol = pos["symbol"]
    last, err = get_last_price(symbol)
    if err:
        log(f"manage: quote failed for {symbol}: {err}")
        return

    entry = pos["entry"]
    pos["peak"] = max(pos["peak"], last)

    if not pos["breakeven_armed"] and last >= entry * (1 + r["activate_pct"]):
        pos["breakeven_armed"] = True
        pos["stop"] = max(pos["stop"], entry)
        notify(f"{symbol}: profit trigger hit, stop armed to breakeven {pos['stop']:.2f}.")

    if pos["breakeven_armed"]:
        trailed = pos["peak"] * (1 - r["trail_pct"])
        if trailed > pos["stop"]:
            pos["stop"] = trailed

    save_state(state)

    if last <= pos["stop"]:
        notify(f"{symbol}: stop hit at {last:.2f} (stop {pos['stop']:.2f}). Selling all.")
        fill, err = place_sell_all(symbol, pos["quantity"])
        if err:
            notify(f"{symbol}: SELL FAILED: {err}. Will retry next tick.")
            return
        proceeds = float(fill["avg_price"]) * float(fill["quantity"])
        cost = entry * float(pos["quantity"])
        realized = proceeds - cost
        roll_daily_pnl(state, now)
        state["day_realized_pnl"] += realized
        state["position"] = None
        state["last_sale_date"] = today_str(now)
        save_state(state)
        notify(f"{symbol}: closed at {float(fill['avg_price']):.2f}. "
               f"Realized {realized:+.2f}. Day PnL {state['day_realized_pnl']:+.2f}.")


def manage_position(state, now, r):
    if state["position"].get("instrument") == "option":
        manage_option_position(state, now, r)
    else:
        manage_stock_position(state, now, r)


# ============================================================================
# Entry helpers
# ============================================================================
def enter_stock(state, now, r, pick, settled):
    symbol = pick["symbol"]
    dollars = round(settled * r["deploy_fraction"], 2)
    notify(f"[{ACTIVE_PROFILE}/{CONFIG['asset_mode']}] Entering {symbol} shares "
           f"({pick.get('conviction')}): {pick.get('reason', '')}. Deploying {dollars:.2f}.")

    fill, err = place_buy(symbol, dollars)
    if err:
        notify(f"{symbol}: BUY FAILED: {err}.")
        return

    entry_price = float(fill["avg_price"])
    qty = float(fill["quantity"])
    state["position"] = {
        "instrument": "stock",
        "symbol": symbol,
        "entry": entry_price,
        "quantity": qty,
        "peak": entry_price,
        "stop": entry_price * (1 - r["initial_stop_pct"]),
        "breakeven_armed": False,
        "entered_at": now.isoformat(),
    }
    state["last_entry_date"] = today_str(now)
    save_state(state)
    notify(f"{symbol}: filled {qty} at {entry_price:.2f}. "
           f"Initial stop {state['position']['stop']:.2f}. Now riding the trail.")


def enter_option(state, now, r, pick, settled):
    symbol = pick["symbol"]
    direction = pick.get("direction", "up")
    if direction == "down" and not CONFIG["allow_puts"]:
        log("entry: AI suggested a put but allow_puts is off. No trade.")
        return

    contract, err = find_option_contract(symbol, direction, r)
    if err:
        notify(f"{symbol}: no suitable contract ({err}).")
        if CONFIG["asset_mode"] == "both" and direction == "up":
            log("entry: falling back to shares.")
            enter_stock(state, now, r, pick, settled)
        return

    budget = settled * r["deploy_fraction"]
    per_contract = float(contract["ask"]) * 100.0
    contracts = int(budget // per_contract) if per_contract > 0 else 0
    if contracts < 1:
        notify(f"{symbol}: budget {budget:.2f} cannot cover one contract "
               f"({per_contract:.2f}).")
        if CONFIG["asset_mode"] == "both" and direction == "up":
            log("entry: falling back to shares.")
            enter_stock(state, now, r, pick, settled)
        return

    kind = contract["type"]
    notify(f"[{ACTIVE_PROFILE}/{CONFIG['asset_mode']}] Entering {symbol} {kind.upper()} "
           f"{contract['strike']} exp {contract['expiration']} x{contracts} "
           f"({pick.get('conviction')}): {pick.get('reason', '')}. "
           f"About {contracts * per_contract:.2f} of premium.")

    fill, err = place_option_buy(contract, contracts)
    if err:
        notify(f"{symbol}: OPTION BUY FAILED: {err}.")
        return

    entry_price = float(fill["avg_price"])
    filled_contracts = int(fill["contracts"])
    state["position"] = {
        "instrument": "option",
        "symbol": symbol,
        "occ_symbol": fill.get("occ_symbol", contract["occ_symbol"]),
        "type": kind,
        "strike": contract["strike"],
        "expiration": contract["expiration"],
        "entry": entry_price,
        "contracts": filled_contracts,
        "peak": entry_price,
        "stop": entry_price * (1 - r["opt_initial_stop_pct"]),
        "breakeven_armed": False,
        "entered_at": now.isoformat(),
    }
    state["last_entry_date"] = today_str(now)
    save_state(state)
    notify(f"{state['position']['occ_symbol']}: filled {filled_contracts} contract(s) at "
           f"{entry_price:.2f}. Premium stop {state['position']['stop']:.2f}. "
           f"Expiry guard at {CONFIG['opt_close_dte']} DTE. Now riding the trail.")


# ============================================================================
# Entry logic, with the guards that make a good-faith violation impossible
# ============================================================================
def maybe_enter(state, snapshot, now, r):
    roll_daily_pnl(state, now)

    if state.get("last_entry_date") == today_str(now):
        return
    if state.get("last_sale_date") == today_str(now):
        return
    if minutes_since_open(now) < r["entry_after_minutes"]:
        return
    if state["day_realized_pnl"] <= -abs(r["max_daily_loss_usd"]):
        return

    settled = float(snapshot.get("settled_cash") or 0.0)
    if settled < 1.0:
        return

    quotes, err = get_quotes(CONFIG["universe"])
    if err:
        log(f"entry: quotes failed: {err}")
        return

    include_red = CONFIG["allow_puts"] and CONFIG["asset_mode"] in ("options", "both")
    candidates = []
    for sym in CONFIG["universe"]:
        q = quotes.get(sym) or {}
        dc = q.get("day_change_pct")
        last = q.get("last")
        beta = q.get("beta")
        if dc is None or last is None:
            continue
        dc = float(dc)
        moved_enough = (dc >= r["candidate_min_daychg"] or
                        (include_red and dc <= -r["candidate_min_daychg"]))
        if not moved_enough:
            continue
        if r["candidate_min_beta"] > 0 and beta is not None \
                and float(beta) < r["candidate_min_beta"]:
            continue
        candidates.append({
            "symbol": sym,
            "day_change_pct": dc,
            "last": float(last),
            "beta": float(beta) if beta is not None else None,
        })

    if not candidates:
        log("entry: no candidates clear the day-change floor.")
        return

    candidates.sort(key=lambda c: abs(c["day_change_pct"]), reverse=True)
    pick, err = pick_name(candidates)
    if err:
        log(f"entry: pick failed: {err}")
        return

    if pick.get("decision") != "buy":
        notify(f"Pass for today: {pick.get('reason', 'no reason given')}")
        return

    conviction = str(pick.get("conviction", "")).lower()
    if conviction not in r["conviction_accept"]:
        notify(f"Pick {pick.get('symbol')} was {conviction} conviction, below the gate. No trade.")
        return

    instrument = str(pick.get("instrument", "stock")).lower()
    if instrument not in allowed_instruments():
        instrument = allowed_instruments()[0]

    if instrument == "option":
        enter_option(state, now, r, pick, settled)
    else:
        enter_stock(state, now, r, pick, settled)


# ============================================================================
# One tick
# ============================================================================
def tick():
    now = now_tz()
    r = risk()
    state = load_state()

    if not in_session(now):
        log("Outside the session. Idle.")
        return

    snapshot, err = get_account_snapshot()
    if err or snapshot is None:
        log(f"tick: account snapshot failed: {err}. Skipping this tick.")
        return

    reconcile(state, snapshot, now)
    state = load_state()

    if state.get("position"):
        manage_position(state, now, r)
    else:
        maybe_enter(state, snapshot, now, r)


def main():
    ap = argparse.ArgumentParser(description="One-a-day momentum trader (stocks/options).")
    ap.add_argument("--once", action="store_true", help="run a single tick (for a scheduler)")
    ap.add_argument("--loop", action="store_true", help="run a foreground poll loop")
    ap.add_argument("--interval", type=int, default=60, help="loop seconds between ticks")
    args = ap.parse_args()

    if not args.once and not args.loop:
        ap.print_help()
        sys.exit(1)

    log(f"profile={ACTIVE_PROFILE} mode={CONFIG['asset_mode']} "
        f"provider={CONFIG['pick_provider']} puts={CONFIG['allow_puts']}")

    if args.once:
        tick()
        return

    log("Starting loop. Ctrl+C to stop.")
    while True:
        try:
            tick()
        except KeyboardInterrupt:
            log("Stopped.")
            break
        except Exception as exc:
            log(f"tick crashed (continuing): {exc}")
        time.sleep(max(5, args.interval))


if __name__ == "__main__":
    main()
