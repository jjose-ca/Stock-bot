"""
TQQQ Intraday Momentum Bot
Strategy : VWAP + EMA9/13 pullback on 15-min bars
Data     : yfinance (5d, 1-min) — resampled to 15-min
Alert    : Discord webhook
Execution: Manual on IBKR / Wealthsimple
Cron     : 0,15,30,45 10-15 * * 1-5     (signal check, every 15 min, 10am-3:30pm ET)
           30 16 * * 1-5 --reconcile    (auto-check outcomes, 4:30pm ET)

Reconciliation:
  Every signal is logged with its alert price. --reconcile automatically
  scans 1-min price data forward from the signal to determine whether it
  hit TARGET, hit STOP, or ran out the clock (TIME), and records the max
  favorable and max adverse price reached along the way — no manual entry
  needed. This tells you the real outcome of every signal, whether or not
  you actually traded it.
"""

import yfinance as yf
import pandas as pd
import numpy as np
import requests
import os
import sys
import json
import time
import argparse
import logging
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo

# ── CONFIG ────────────────────────────────────────────────────────────────────
SYMBOL          = "TQQQ"
DISCORD_WEBHOOK = os.environ.get("DISCORD_URL", "")

TARGET_PROFIT   = 0.50
STOP_LOSS       = 0.40
EMA_FAST        = 9
EMA_SLOW        = 13
VOLUME_MULT     = 1.2   # re-validated against corrected time-of-day volume methodology (was 1.0, tuned against a flawed rolling-20-bar baseline)
PULLBACK_DIST   = 0.75   # widened from 0.50 — sweep showed tighter threshold was filtering out good setups
TRADE_START_H   = 10
TRADE_START_M   = 0
TRADE_END_H     = 15
TRADE_END_M     = 30

# Retry logic — matches soxl_intraday_bot.py's convention. After a bar's
# window closes, yfinance/Yahoo can take 30-90 seconds to actually publish
# it. Without retry, the bot may evaluate stale data one bar behind.
MAX_RETRIES     = 12   # 12 x 5 seconds = 60 second max wait
WAIT_SECONDS    = 5

ET              = ZoneInfo("America/New_York")
FLAG_DIR        = os.path.dirname(os.path.abspath(__file__))
TRADE_LOG_PATH  = os.path.join(FLAG_DIR, "tqqq_intraday_trade_log.json")
REJECTION_LOG_PATH = os.path.join(FLAG_DIR, "tqqq_intraday_rejections.jsonl")
HEARTBEAT_PATH  = "/root/logs/heartbeat.log"

# ── LOGGING ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(os.path.join(FLAG_DIR, "tqqq_intraday_bot.log")),
    ],
)
log = logging.getLogger(__name__)


def write_heartbeat():
    """
    Append a single OK line to the shared heartbeat.log, matching the exact
    convention used by soxl_intraday_bot.py — check /root/logs/heartbeat.log
    to verify cron is firing correctly. Only called on successful completion
    of a normal (non-reconcile) run; silently swallowed on failure since a
    missing/failed write here shouldn't ever break the bot itself.
    """
    now = datetime.now(ET)
    try:
        with open(HEARTBEAT_PATH, "a") as _hb:
            _hb.write(f"[{now.strftime('%Y-%m-%d %H:%M:%S ET')}] tqqq_intraday_bot OK\n")
    except Exception:
        pass


# ── TRADE LOG HELPERS ─────────────────────────────────────────────────────────

def load_trade_log() -> list:
    if not os.path.exists(TRADE_LOG_PATH):
        return []
    try:
        with open(TRADE_LOG_PATH, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, FileNotFoundError):
        log.warning("Trade log unreadable or missing — starting fresh.")
        return []


def save_trade_log(trades: list):
    with open(TRADE_LOG_PATH, "w") as f:
        json.dump(trades, f, indent=2, default=str)


def log_rejection(candle_end_time, failed_detail: dict):
    """
    Append one line to the rejection log for a bar that failed one or more
    signal conditions. JSON Lines format (one JSON object per line) — cheap
    to append (no read-rewrite-whole-file), and safe to grep/tail directly
    like any other log, unlike a single large JSON array.

    failed_detail: dict keyed by condition name, each value containing
    {actual, required, pct_of_required} — not just which condition failed,
    but how close it was. This is what lets --blocker-summary distinguish
    a near-miss (e.g. volume at 84% of threshold) from a wide miss (40%),
    and retrospectively answer "would a looser threshold have helped" from
    real live data, not just the backtest.

    Note on `pct_of_required` direction: for volume/vwap/ema_trend/recovery,
    higher % = closer to passing (100% = right at the boundary). For
    `pullback`, it's inverted — `actual` is a distance that must be BELOW
    the threshold to pass, so higher % = further from passing, not closer.
    """
    record = {
        "date":   date.today().isoformat(),
        "time":   candle_end_time.strftime("%H:%M"),
        "failed": failed_detail,   # dict: {condition: {actual, required, pct_of_required}}
    }
    try:
        with open(REJECTION_LOG_PATH, "a") as f:
            f.write(json.dumps(record) + "\n")
    except Exception as e:
        log.warning(f"Could not write rejection log: {e}")


def blocker_summary(days: int = 30):
    """
    Read the rejection log and tally which condition(s) blocked a signal
    most often, over the last N calendar days. Also reports the average
    "closeness" (pct_of_required) per condition, so a condition that fails
    often but by a wide margin can be distinguished from one that fails
    often but is usually a near-miss.
    """
    if not os.path.exists(REJECTION_LOG_PATH):
        print("No rejection log yet — nothing to summarize.")
        return

    cutoff = (datetime.now(ET) - pd.Timedelta(days=days)).date()
    counts = {}
    pct_sums = {}
    total_checks = 0
    total_rejections = 0

    with open(REJECTION_LOG_PATH, "r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            rec_date = date.fromisoformat(rec["date"])
            if rec_date < cutoff:
                continue
            total_checks += 1
            failed = rec.get("failed", {})
            if failed:
                total_rejections += 1
                for cond, detail in failed.items():
                    counts[cond] = counts.get(cond, 0) + 1
                    pct = detail.get("pct_of_required") if isinstance(detail, dict) else None
                    if pct is not None:
                        pct_sums.setdefault(cond, []).append(pct)

    if total_checks == 0:
        print(f"No rejection log entries in the last {days} days.")
        return

    print(f"\n{'='*66}")
    print(f"  TQQQ INTRADAY — BLOCKER SUMMARY (last {days} days)")
    print(f"{'='*66}")
    print(f"  Total bar checks logged : {total_checks}")
    print(f"  Bars with a rejection   : {total_rejections}")
    print(f"{'-'*66}")
    print(f"  {'Condition':<14} {'Count':>7} {'% of rejections':>16} {'Avg %-of-req':>14}")
    for cond, n in sorted(counts.items(), key=lambda x: -x[1]):
        pct_of_rej = n / total_rejections * 100 if total_rejections else 0
        avg_pct = sum(pct_sums.get(cond, [])) / len(pct_sums[cond]) if pct_sums.get(cond) else None
        avg_str = f"{avg_pct:.1f}%" if avg_pct is not None else "n/a"
        print(f"  {cond:<14} {n:>7} {pct_of_rej:>15.1f}% {avg_str:>14}")
    print(f"{'-'*66}")
    print(f"  Note: for 'pullback', lower avg %-of-req = closer to passing")
    print(f"  (inverted vs other conditions — see log_rejection() docstring)")
    print(f"{'='*66}\n")


def signals_today_count() -> int:
    """Count how many signals have already fired today (for context, not gating)."""
    trades = load_trade_log()
    today  = date.today().isoformat()
    return sum(1 for t in trades if t["date"] == today)


def append_signal_to_log(signal: dict, signal_number: int = 1):
    """
    Write a new trade record when a signal fires. Outcome fields start as
    null and are filled in automatically later by --reconcile.
    """
    trades = load_trade_log()
    record = {
        "date":              date.today().isoformat(),
        "bar_time":          signal["bar_time"],          # signal bar, e.g. "10:15 ET"
        "signal_bar_ts":     signal["bar_ts"],             # ISO timestamp of signal bar (left-label)
        "signal_number":     signal_number,                # 1st, 2nd, 3rd... signal that day
        "alert_price":       signal["entry_zone"],         # assumed entry = signal bar close
        "target":            signal["tp"],
        "stop":              signal["sl"],
        "vwap":              signal["vwap"],
        "ema_fast":          signal["ema_fast"],
        "ema_slow":          signal["ema_slow"],
        "vol_mult":          signal["vol_mult"],
        "pullback_src":      signal["pullback_src"],
        "prev_low":          signal["prev_low"],
        "prev_vwap":         signal["prev_vwap"],
        "prev_ema9":         signal["prev_ema9"],
        "ema_spread_cur":     signal["ema_spread_cur"],      # logged silently — not on live alert
        "ema_spread_prev":    signal["ema_spread_prev"],     # (see check_signal for reasoning)
        "momentum_note":      signal["momentum_note"],
        "vwap_extension_pct": signal["vwap_extension_pct"],  # shown on live alert
        "alert_sent_at":     datetime.now(ET).isoformat(),
        # ── Filled in automatically by --reconcile ──────────────────────────
        "reconciled":        False,
        "exit_price":        None,
        "exit_time":         None,
        "exit_reason":       None,   # TARGET / STOP / TIME
        "max_favorable":     None,   # highest price reached after entry (spike/potential)
        "max_adverse":       None,   # lowest price reached after entry (drawdown/zigzag)
        "pnl_per_share":     None,
    }
    trades.append(record)
    save_trade_log(trades)
    log.info(f"Trade record appended to {TRADE_LOG_PATH}")


def reconcile(target_date: str = None):
    """
    Automatically determine the outcome of every unreconciled signal by
    replaying 1-min price data forward from the entry point.

    Entry is assumed to happen on the next 1-min bar after the signal's
    15-min bar closes (matching backtest logic exactly). From there we
    scan forward minute by minute:
      - track running high  -> max_favorable (how far it went in your favor)
      - track running low   -> max_adverse   (how far it went against you)
      - first touch of TP or SL wins; if neither, exit at 3:30 PM close (TIME)

    Note: yfinance only retains ~7 days of 1-min history, so reconcile
    must run within a few days of the signal (the 4:30pm same-day cron
    slot is the intended use).
    """
    trades = load_trade_log()
    unreconciled = [t for t in trades if not t["reconciled"]]

    if target_date:
        unreconciled = [t for t in unreconciled if t["date"] == target_date]

    if not unreconciled:
        log.info("No unreconciled trades to process.")
        return

    log.info(f"Reconciling {len(unreconciled)} trade(s)...")

    raw = yf.download(SYMBOL, period="7d", interval="1m", progress=False, auto_adjust=True)
    if raw.empty:
        log.warning("yfinance returned empty dataframe — cannot reconcile.")
        return
    if isinstance(raw.columns, pd.MultiIndex):
        raw.columns = raw.columns.get_level_values(0)
    raw.columns = [c.lower() for c in raw.columns]
    raw.index = pd.to_datetime(raw.index)
    if raw.index.tz is None:
        raw.index = raw.index.tz_localize("UTC")
    raw.index = raw.index.tz_convert(ET)
    raw = raw.between_time("09:30", "15:59")
    raw = raw[raw["volume"] > 0].copy()

    end_h, end_m = TRADE_END_H, TRADE_END_M

    for record in unreconciled:
        signal_ts = pd.Timestamp(record["signal_bar_ts"])
        entry_after = signal_ts + pd.Timedelta(minutes=15)  # signal bar closes here

        day_bars = raw[raw.index.date == signal_ts.date()]
        # Include the bar labeled exactly entry_after itself — that bar
        # (e.g. the 1-min bar labeled 10:30) covers the very first minute
        # the trade is actually open, and its high/low must be checked for
        # target/stop just like every subsequent bar. Strict '>' here would
        # skip that first minute entirely (caught during review — same
        # off-by-one class of bug as check_earlier_signals_status below).
        future = day_bars[day_bars.index >= entry_after]

        if future.empty:
            log.info(f"No 1-min data available yet for signal @ {record['bar_time']} "
                     f"({record['date']}) — will retry on next reconcile run.")
            continue

        entry_price = future.iloc[0]["open"]
        tp = record["target"]
        sl = record["stop"]

        max_favorable = entry_price
        max_adverse   = entry_price
        exit_price = exit_reason = exit_time = None

        for ts, bar in future.iterrows():
            max_favorable = max(max_favorable, bar["high"])
            max_adverse   = min(max_adverse, bar["low"])

            bar_end_time = ts.time()
            cutoff = ts.replace(hour=end_h, minute=end_m, second=0).time()

            if bar_end_time >= cutoff:
                exit_price, exit_reason, exit_time = bar["open"], "TIME", ts
                break
            if bar["low"] <= sl:
                exit_price, exit_reason, exit_time = sl, "STOP", ts
                break
            if bar["high"] >= tp:
                exit_price, exit_reason, exit_time = tp, "TARGET", ts
                break

        if exit_price is None:
            # Ran out of available data without a clean exit — leave for next run
            log.info(f"Signal @ {record['bar_time']} ({record['date']}) still open "
                     f"in available data — will retry on next reconcile run.")
            continue

        idx = trades.index(record)
        trades[idx]["reconciled"]    = True
        trades[idx]["exit_price"]    = round(float(exit_price), 4)
        trades[idx]["exit_time"]     = exit_time.strftime("%H:%M:%S")
        trades[idx]["exit_reason"]   = exit_reason
        trades[idx]["max_favorable"] = round(float(max_favorable), 4)
        trades[idx]["max_adverse"]   = round(float(max_adverse), 4)
        trades[idx]["pnl_per_share"] = round(float(exit_price) - float(entry_price), 4)

        log.info(
            f"Reconciled {record['date']} {record['bar_time']}: "
            f"entry ${entry_price:.2f} -> exit ${exit_price:.2f} [{exit_reason}]  "
            f"P&L ${trades[idx]['pnl_per_share']:+.2f}  "
            f"(max favorable ${max_favorable:.2f}, max adverse ${max_adverse:.2f})"
        )

    save_trade_log(trades)
    log.info("Reconcile complete.")


def print_summary():
    """
    Print performance stats across every reconciled signal — every alert
    is treated as a trade taken at the alert price, so this reflects the
    bot's live signal performance end to end, automatically.
    """
    trades = load_trade_log()
    done   = [t for t in trades if t["reconciled"]]

    if not done:
        print("No reconciled trades yet.")
        return

    wins   = [t for t in done if t["pnl_per_share"] > 0]
    losses = [t for t in done if t["pnl_per_share"] <= 0]
    wr     = len(wins) / len(done) * 100
    total_pnl = sum(t["pnl_per_share"] for t in done)

    print(f"\n{'='*58}")
    print(f"  TQQQ INTRADAY — LIVE SIGNAL PERFORMANCE")
    print(f"{'='*58}")
    print(f"  Reconciled signals : {len(done)}")
    print(f"  Win rate           : {wr:.1f}%  ({len(wins)}W / {len(losses)}L)")
    print(f"  Total P&L/share    : ${total_pnl:+.2f}")
    print(f"{'-'*58}")
    for t in done:
        num = f"#{t.get('signal_number', 1)}"
        fav_diff = t["max_favorable"] - t["alert_price"]
        adv_diff = t["max_adverse"]   - t["alert_price"]
        print(f"  {t['date']} {t['bar_time']:>9} {num:>3}  "
              f"entry ${t['alert_price']:.2f}  exit ${t['exit_price']:.2f}  "
              f"[{t['exit_reason']:<6}]  P&L ${t['pnl_per_share']:+.2f}  "
              f"(range: {adv_diff:+.2f} to {fav_diff:+.2f})")
    print(f"{'='*58}\n")


# ── HELPERS ───────────────────────────────────────────────────────────────────

def in_trading_window() -> bool:
    now   = datetime.now(ET).time()
    start = datetime.now(ET).replace(hour=TRADE_START_H, minute=TRADE_START_M, second=0, microsecond=0).time()
    end   = datetime.now(ET).replace(hour=TRADE_END_H, minute=TRADE_END_M + 5, second=0, microsecond=0).time()
    return start <= now < end  # +5 min buffer so 3:30 cron run processes the last bar


def is_market_day() -> bool:
    return datetime.now(ET).weekday() < 5


# ── DATA ──────────────────────────────────────────────────────────────────────

def fetch_15min_bars() -> pd.DataFrame:
    """
    Fetch 7 days (Yahoo's hard limit for 1-min data) of bars to seed EMA13 and time-of-day volume avg from
    open. True VWAP computed on 1-min data, then carried into 15-min bars.
    Only strictly completed 15-min bars are returned (no partial bar).

    Uses retry logic to handle Yahoo Finance API publication lag, matching
    soxl_intraday_bot.py's convention. After a 1-min bar's minute elapses,
    Yahoo can take 30-90 seconds to actually publish it. Without retry, the
    bot may evaluate slightly stale data — missing the most recent minute(s)
    needed to correctly determine whether the latest 15-min bar has fully
    closed yet.

    Retry logic:
      Calculates the expected last-closed-bar boundary from the current time
      Polls yfinance every 5 seconds until fresh-enough data is published
      Gives up after 60 seconds (12 retries x 5 seconds) and proceeds with
      whatever data is available at that point (fail open, not fail closed)

    A lightweight holiday/weekend pre-check runs first — on US market
    holidays yfinance returns no fresh intraday data for today at all, and
    without this check the retry loop would burn a full 60 seconds waiting
    for bars that will never arrive (e.g. July 4th, Thanksgiving, Christmas).
    """
    now = datetime.now(ET)
    closed_minute = (now.minute // 15) * 15
    # We fetch 1-MIN bars (unlike soxl_intraday_bot.py, which fetches native
    # 15-min bars directly). closed_minute lands on the START of the just-
    # opened 15-min window (e.g. 10:15 at 10:15:02) — a 1-min bar with that
    # exact label cannot possibly exist yet, since that minute just started.
    # We actually want to confirm the LAST 1-min bar of the previously-closed
    # window has arrived (e.g. the 10:14 bar, which closed at 10:15:00) —
    # hence the -1 minute adjustment below.
    expected_bar_time = now.replace(minute=closed_minute, second=0, microsecond=0) - timedelta(minutes=1)

    # ── Holiday/weekend pre-check ────────────────────────────────────────────
    try:
        _pre = yf.download(SYMBOL, period="1d", interval="1m",
                           auto_adjust=True, progress=False)
        if isinstance(_pre.columns, pd.MultiIndex):
            _pre.columns = _pre.columns.get_level_values(0)
        if _pre.empty:
            log.warning("No intraday data available — market likely closed (holiday or weekend).")
            return pd.DataFrame()
        _last = _pre.index[-1]
        if hasattr(_last, "tz_convert"):
            _last = pd.Timestamp(_last).tz_convert(ET) if _last.tzinfo else pd.Timestamp(_last).tz_localize("UTC").tz_convert(ET)
        if _last.date() < now.date():
            log.warning("No today's data — market closed (holiday or early close).")
            return pd.DataFrame()
    except Exception:
        pass  # If pre-check fails, proceed to retry loop normally

    # ── Retry loop — polls until Yahoo publishes fresh enough 1-min data ─────
    log.info("Fetching TQQQ 1-min bars (7d) from yfinance...")
    raw = None
    for attempt in range(MAX_RETRIES):
        try:
            candidate = yf.download(SYMBOL, period="7d", interval="1m",
                                    progress=False, auto_adjust=True)
            if candidate.empty:
                time.sleep(WAIT_SECONDS)
                continue

            if isinstance(candidate.columns, pd.MultiIndex):
                candidate.columns = candidate.columns.get_level_values(0)
            candidate.index = pd.to_datetime(candidate.index)
            if candidate.index.tz is None:
                candidate.index = candidate.index.tz_localize("UTC")
            candidate.index = candidate.index.tz_convert(ET)

            last_bar = candidate.index[-1]
            if last_bar >= expected_bar_time:
                raw = candidate
                if attempt > 0:
                    log.info(f"yfinance: got expected bar after {attempt * WAIT_SECONDS}s delay")
                break
            else:
                if attempt == 0:
                    log.info(f"yfinance: waiting for {expected_bar_time.strftime('%I:%M %p')} bar "
                             f"(latest: {last_bar.strftime('%I:%M %p')})...")
                time.sleep(WAIT_SECONDS)
        except Exception:
            if attempt < MAX_RETRIES - 1:
                time.sleep(WAIT_SECONDS)
            continue

    if raw is None:
        # Timeout — proceed with whatever's available rather than blocking entirely
        log.warning(f"yfinance: bar publication timeout after {MAX_RETRIES * WAIT_SECONDS}s "
                    f"— using latest available data")
        try:
            raw = yf.download(SYMBOL, period="7d", interval="1m",
                              progress=False, auto_adjust=True)
            if isinstance(raw.columns, pd.MultiIndex):
                raw.columns = raw.columns.get_level_values(0)
            raw.index = pd.to_datetime(raw.index)
            if raw.index.tz is None:
                raw.index = raw.index.tz_localize("UTC")
            raw.index = raw.index.tz_convert(ET)
        except Exception:
            log.warning("yfinance returned no usable data after timeout fallback.")
            return pd.DataFrame()

    if raw.empty:
        log.warning("yfinance returned empty dataframe.")
        return pd.DataFrame()

    raw.columns = [c.lower() for c in raw.columns]

    raw = raw.between_time("09:30", "15:59")
    raw = raw[raw["volume"] > 0].copy()

    if len(raw) < 30:
        log.warning(f"Only {len(raw)} 1-min bars — market may not be open.")
        return pd.DataFrame()

    raw["date"]       = raw.index.date
    raw["tp"]         = (raw["high"] + raw["low"] + raw["close"]) / 3
    raw["tp_vol"]     = raw["tp"] * raw["volume"]
    raw["cum_tp_vol"] = raw.groupby("date")["tp_vol"].cumsum()
    raw["cum_vol"]    = raw.groupby("date")["volume"].cumsum()
    raw["vwap"]       = raw["cum_tp_vol"] / raw["cum_vol"]

    df = raw.resample("15min", label="left", closed="left").agg(
        open=("open",   "first"),
        high=("high",   "max"),
        low=("low",     "min"),
        close=("close", "last"),
        volume=("volume", "sum"),
        vwap=("vwap",   "last"),
    ).dropna(subset=["open"])
    df = df[df["volume"] > 0].copy()

    now = datetime.now(ET)
    df  = df[df.index + pd.Timedelta(minutes=15) <= now].copy()

    log.info(f"  {len(df)} completed 15-min bars.")
    return df


# ── INDICATORS ────────────────────────────────────────────────────────────────

def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """
    VWAP already computed in fetch. Add EMAs and volume avg.

    vol_avg uses time-of-day averaging across PRIOR trading days only —
    today's own bars are explicitly excluded from their own baseline.
    Without this exclusion, today's own 11:15 bar would be one of only
    ~4-6 data points feeding its own comparison average (self-referencing:
    a high-volume bar inflates the very average it's being measured
    against, and vice versa) — caught during review, since with a short
    ~7-day window today's bar can carry 15-25% of the average's weight,
    unlike the 1,128-day backtest dataset where any single day is
    negligible. A 10am bar is compared against PRIOR days' 10am bars only,
    never against itself or later bars from today.
    """
    df = df.copy()
    df["ema_fast"] = df["close"].ewm(span=EMA_FAST, adjust=False).mean()
    df["ema_slow"] = df["close"].ewm(span=EMA_SLOW, adjust=False).mean()

    df["time_of_day"] = df.index.time
    df["cal_date"]     = df.index.date
    today               = date.today()

    prior_days_only     = df[df["cal_date"] < today]
    if prior_days_only.empty:
        # Extreme edge case: no prior-day data at all (e.g. very first day
        # ever run, or a long VPS outage). Fall back to including today
        # rather than leaving vol_avg entirely NaN and blocking all signals.
        log.warning("No prior-day data available for volume baseline — "
                    "falling back to same-day average (self-referencing).")
        time_vol_avg = df.groupby("time_of_day")["volume"].mean()
    else:
        time_vol_avg = prior_days_only.groupby("time_of_day")["volume"].mean()

    df["vol_avg"] = df["time_of_day"].map(time_vol_avg)
    df = df.drop(columns=["time_of_day", "cal_date"])
    return df


# ── SIGNAL CHECK ──────────────────────────────────────────────────────────────

def check_earlier_signals_status(df: pd.DataFrame) -> list:
    """
    For any signal(s) already fired today, determine their current live
    status using the 15-min df already fetched for this run (no extra API
    call). This is a lightweight, same-day-only estimate — the authoritative
    outcome still comes from --reconcile at 4:30pm using 1-min data. Here we
    only need "is it still open, or has it likely hit target/stop already"
    to give context on a new alert, not a precise fill price.

    Returns a list of dicts: {bar_time, status, detail}
    """
    trades  = load_trade_log()
    today   = date.today().isoformat()
    todays  = [t for t in trades if t["date"] == today]
    results = []

    for t in todays:
        entry_price = t["alert_price"]
        tp, sl      = t["target"], t["stop"]
        signal_ts   = pd.Timestamp(t["signal_bar_ts"])
        entry_after = signal_ts + pd.Timedelta(minutes=15)

        # Include the 15-min bar labeled exactly entry_after itself — that
        # bar (e.g. labeled 10:30) covers 10:30-10:45, which is when the
        # trade actually enters and starts being exposed to target/stop.
        # Strict '>' would skip this first 15-min window entirely (caught
        # during review — same off-by-one class of bug as reconcile() above).
        future = df[df.index >= entry_after]
        if future.empty:
            results.append({"bar_time": t["bar_time"], "status": "OPEN",
                            "detail": "no bars yet since entry"})
            continue

        hit_target = (future["high"] >= tp).any()
        hit_stop   = (future["low"]  <= sl).any()

        if hit_target and hit_stop:
            # Both touched at some point across available 15-min bars —
            # can't tell which came first at this resolution; flag as
            # ambiguous rather than guess. --reconcile resolves this
            # precisely later using 1-min data.
            results.append({"bar_time": t["bar_time"], "status": "LIKELY CLOSED",
                            "detail": "both target and stop touched — see reconcile for exact outcome"})
        elif hit_target:
            results.append({"bar_time": t["bar_time"], "status": "CLOSED",
                            "detail": f"hit target ${tp}"})
        elif hit_stop:
            results.append({"bar_time": t["bar_time"], "status": "CLOSED",
                            "detail": f"hit stop ${sl}"})
        else:
            last_price = float(future["close"].iloc[-1])
            unrealized = round(last_price - entry_price, 2)
            results.append({"bar_time": t["bar_time"], "status": "OPEN",
                            "detail": f"currently ${last_price:.2f} ({unrealized:+.2f} unrealized)"})

    return results


def check_signal(df: pd.DataFrame) -> dict | None:
    """
    Check the last completed 15-min bar for a momentum pullback signal.
    All incomplete bars already stripped in fetch — safe to use iloc[-1].

    Conditions (all must be true):
      1. close > VWAP
      2. EMA9 > EMA13
      3. volume > 1.0x time-of-day avg
      4. prev bar low within $0.50 of VWAP or EMA9  (pullback)
      5. close >= VWAP and close >= EMA9             (recovery)
    """
    if len(df) < EMA_SLOW + 2:
        log.info(f"Not enough bars ({len(df)}) for signal check — need {EMA_SLOW + 2}.")
        return None

    cur  = df.iloc[-1]
    prev = df.iloc[-2]

    candle_end_time = (cur.name + pd.Timedelta(minutes=15)).time()
    start = cur.name.replace(hour=TRADE_START_H, minute=TRADE_START_M, second=0).time()
    end   = cur.name.replace(hour=TRADE_END_H,   minute=TRADE_END_M,   second=0).time()

    if not (start <= candle_end_time <= end):
        log.info(f"Candle ending at {candle_end_time} outside trading window ({start}-{end}).")
        return None

    if cur.name.date() != prev.name.date():
        log.info("Previous bar is from yesterday — skipping pullback check.")
        return None

    reasons = []
    failed_detail = {}   # structured detail for rejection log — condition -> {actual, required, pct_of_required}

    c1 = cur["close"] > cur["vwap"]
    if not c1:
        reasons.append(f"close ${cur['close']:.2f} <= VWAP ${cur['vwap']:.2f}")
        failed_detail["vwap"] = {
            "actual":   round(float(cur["close"]), 4),
            "required": round(float(cur["vwap"]), 4),
            "pct_of_required": round(float(cur["close"]) / float(cur["vwap"]) * 100, 1) if cur["vwap"] else None,
        }

    c2 = cur["ema_fast"] > cur["ema_slow"]
    if not c2:
        reasons.append(f"EMA{EMA_FAST} ${cur['ema_fast']:.2f} <= EMA{EMA_SLOW} ${cur['ema_slow']:.2f}")
        failed_detail["ema_trend"] = {
            "actual":   round(float(cur["ema_fast"]), 4),
            "required": round(float(cur["ema_slow"]), 4),
            "pct_of_required": round(float(cur["ema_fast"]) / float(cur["ema_slow"]) * 100, 1) if cur["ema_slow"] else None,
        }

    vol_avg = cur["vol_avg"]
    c3 = pd.notna(vol_avg) and cur["volume"] >= VOLUME_MULT * vol_avg
    if not c3:
        required_vol = VOLUME_MULT * vol_avg if pd.notna(vol_avg) else None
        reasons.append(f"volume {cur['volume']:,.0f} < {VOLUME_MULT}x avg {vol_avg:,.0f}")
        failed_detail["volume"] = {
            "actual":   int(cur["volume"]),
            "required": round(float(required_vol), 0) if required_vol is not None else None,
            "pct_of_required": round(cur["volume"] / required_vol * 100, 1) if required_vol else None,
        }

    near_vwap = abs(prev["low"] - prev["vwap"])     <= PULLBACK_DIST
    near_ema  = abs(prev["low"] - prev["ema_fast"]) <= PULLBACK_DIST
    c4 = near_vwap or near_ema
    if not c4:
        actual_dist = min(abs(prev["low"] - prev["vwap"]), abs(prev["low"] - prev["ema_fast"]))
        reasons.append(
            f"prev low ${prev['low']:.2f} not within ${PULLBACK_DIST} "
            f"of VWAP ${prev['vwap']:.2f} or EMA9 ${prev['ema_fast']:.2f}"
        )
        failed_detail["pullback"] = {
            "actual":   round(float(actual_dist), 4),      # closest distance achieved (smaller = closer to passing)
            "required": PULLBACK_DIST,                      # max allowed distance
            "pct_of_required": round(actual_dist / PULLBACK_DIST * 100, 1) if PULLBACK_DIST else None,
        }

    c5 = cur["close"] >= cur["vwap"] and cur["close"] >= cur["ema_fast"]
    if not c5:
        reasons.append("close did not recover above VWAP and EMA9")
        shortfall = max(cur["vwap"] - cur["close"], cur["ema_fast"] - cur["close"], 0)
        reference = max(cur["vwap"], cur["ema_fast"])
        failed_detail["recovery"] = {
            "actual":   round(float(cur["close"]), 4),
            "required": round(float(reference), 4),
            "pct_of_required": round(float(cur["close"]) / float(reference) * 100, 1) if reference else None,
        }

    if not all([c1, c2, c3, c4, c5]):
        log.info(f"No signal [{candle_end_time}]: {' | '.join(reasons)}")
        log_rejection(candle_end_time, failed_detail)
        return None

    entry_zone = cur["close"]
    tp         = round(entry_zone + TARGET_PROFIT, 2)
    sl         = round(entry_zone - STOP_LOSS, 2)

    pullback_src = "VWAP + EMA9" if (near_vwap and near_ema) else ("VWAP" if near_vwap else "EMA9")

    # ── Momentum trend: EMA spread on current bar vs prior bar ─────────────────
    # Logged silently to the trade log for future analysis — NOT shown on the
    # live Discord alert. Decided against surfacing this live: unlike VWAP
    # extension below, it doesn't answer a decision that's actually come up
    # in practice, and there's no backtest evidence trend-of-spread (as
    # opposed to spread threshold, which was tested) predicts anything. Kept
    # here so --summary can check later whether it correlates with outcomes
    # once enough live signals accumulate.
    cur_spread  = float(cur["ema_fast"])  - float(cur["ema_slow"])
    prev_spread = float(prev["ema_fast"]) - float(prev["ema_slow"])
    if cur_spread > prev_spread:
        momentum_note = "Accelerating (spread widening)"
    elif cur_spread < prev_spread:
        momentum_note = "Decelerating (spread narrowing)"
    else:
        momentum_note = "Steady (spread unchanged)"

    # ── VWAP extension: how far price has stretched from the session anchor ──
    # Shown live on the alert. Not a quality signal (every valid setup is
    # positive, by definition of condition c1) — a stretch/risk gauge: small
    # extension = fresh move, more room before mean-reversion pressure;
    # large extension = already-stretched, higher odds of entering late.
    # No backtested threshold exists yet for this specific strategy — shown
    # as context for judgment, not a filter.
    vwap_extension_pct = ((float(cur["close"]) - float(cur["vwap"])) / float(cur["vwap"])) * 100

    signal = {
        "bar_time":       cur.name.strftime("%H:%M ET"),
        "bar_ts":         cur.name.isoformat(),   # exact timestamp, used by reconcile
        "close":          round(float(cur["close"]), 2),
        "vwap":           round(float(cur["vwap"]), 2),
        "ema_fast":       round(float(cur["ema_fast"]), 2),
        "ema_slow":       round(float(cur["ema_slow"]), 2),
        "volume":         int(cur["volume"]),
        "vol_avg":        int(vol_avg),
        "vol_mult":       round(cur["volume"] / vol_avg, 1),
        "prev_low":       round(float(prev["low"]), 2),
        "prev_vwap":      round(float(prev["vwap"]), 2),
        "prev_ema9":      round(float(prev["ema_fast"]), 2),
        "entry_zone":     round(float(entry_zone), 2),
        "tp":             tp,
        "sl":             sl,
        "pullback_src":   pullback_src,
        "ema_spread_cur":     round(cur_spread, 4),
        "ema_spread_prev":    round(prev_spread, 4),
        "momentum_note":      momentum_note,
        "vwap_extension_pct": round(vwap_extension_pct, 3),
    }

    log.info(f"SIGNAL at {signal['bar_time']} | entry ~${entry_zone:.2f} | TP ${tp} | SL ${sl} | "
             f"VWAP ext {vwap_extension_pct:.2f}% | {momentum_note}")
    return signal


# ── DISCORD ALERT ─────────────────────────────────────────────────────────────

def send_discord_alert(signal: dict, signal_number: int = 1, earlier_status: list = None):
    label = f"[SIGNAL #{signal_number} TODAY]" if signal_number > 1 else "[SIGNAL]"

    # ── Time remaining until forced exit ────────────────────────────────────
    now    = datetime.now(ET)
    cutoff = now.replace(hour=TRADE_END_H, minute=TRADE_END_M, second=0, microsecond=0)
    remaining = cutoff - now
    if remaining.total_seconds() > 0:
        rem_h, rem_rem = divmod(int(remaining.total_seconds()), 3600)
        rem_m = rem_rem // 60
        time_left_note = f"{rem_h}h {rem_m}m until 3:30 PM ET cutoff"
    else:
        time_left_note = "past 3:30 PM ET cutoff"

    # ── Earlier same-day signal status block (only if any exist) ───────────
    earlier_block = ""
    if earlier_status:
        lines = [f"  {e['bar_time']}: {e['status']} — {e['detail']}" for e in earlier_status]
        earlier_block = (
            f"------------------------------\n"
            f"Earlier signal(s) today:\n" + "\n".join(lines) + "\n"
        )

    msg = (
        f"{label} TQQQ INTRADAY MOMENTUM\n"
        f"------------------------------\n"
        f"Bar time   : {signal['bar_time']}\n"
        f"Entry zone : ~${signal['entry_zone']}\n"
        f"Target     : ${signal['tp']}  (+${TARGET_PROFIT})\n"
        f"Stop       : ${signal['sl']}  (-${STOP_LOSS})\n"
        f"------------------------------\n"
        f"VWAP       : ${signal['vwap']}  [OK] price above\n"
        f"EMA{EMA_FAST}       : ${signal['ema_fast']}  [OK] above EMA{EMA_SLOW} (${signal['ema_slow']})\n"
        f"Volume     : {signal['vol_mult']}x avg  [OK]  ({signal['volume']:,} vs avg {signal['vol_avg']:,})\n"
        f"Pullback   : prev low ${signal['prev_low']} near {signal['pullback_src']}  [OK]\n"
        f"VWAP ext   : {signal['vwap_extension_pct']:+.2f}% above session VWAP\n"
        f"Time left  : {time_left_note}\n"
        + earlier_block +
        f"------------------------------\n"
        + (f"Note: {signal_number - 1} earlier signal(s) fired today — use judgment.\n"
           if signal_number > 1 else "")
        + f"Manual execution — verify chart before entering\n"
        f"Exit by 3:30 PM ET regardless\n"
        f"R:R = 1.25:1  |  15-min momentum strategy\n"
        f"------------------------------\n"
        f"Outcome auto-reconciles at 4:30 PM ET — no action needed."
    )

    if not DISCORD_WEBHOOK:
        log.warning("DISCORD_URL not set — printing alert only.")
        print("\n" + msg + "\n")
        return

    try:
        resp = requests.post(DISCORD_WEBHOOK, json={"content": msg}, timeout=10)
        if resp.status_code in (200, 204):
            log.info("Discord alert sent.")
        else:
            log.error(f"Discord returned {resp.status_code}: {resp.text}")
    except Exception as e:
        log.error(f"Discord send failed: {e}")


def send_reconcile_summary_to_discord():
    """Post today's reconciled results to Discord after --reconcile runs."""
    trades = load_trade_log()
    today  = date.today().isoformat()
    todays = [t for t in trades if t["date"] == today and t["reconciled"]]

    if not todays:
        return

    lines = [f"[RECONCILE] TQQQ Intraday — {len(todays)} signal(s) today", "-" * 30]
    total_pnl = 0
    for t in todays:
        total_pnl += t["pnl_per_share"]
        lines.append(
            f"{t['bar_time']:>9}  entry ${t['alert_price']:.2f} -> "
            f"exit ${t['exit_price']:.2f} [{t['exit_reason']}]  "
            f"P&L ${t['pnl_per_share']:+.2f}"
        )
    lines.append("-" * 30)
    lines.append(f"Total P&L today: ${total_pnl:+.2f}/share")
    msg = "\n".join(lines)

    if not DISCORD_WEBHOOK:
        print("\n" + msg + "\n")
        return
    try:
        requests.post(DISCORD_WEBHOOK, json={"content": msg}, timeout=10)
    except Exception as e:
        log.error(f"Discord send failed: {e}")


# ── MAIN ──────────────────────────────────────────────────────────────────────

def main():
    log.info(f"-- TQQQ Intraday Bot: {datetime.now(ET).strftime('%Y-%m-%d %H:%M ET')} --")

    # Heartbeat convention matches soxl_intraday_bot.py: written once per
    # normal (non-reconcile) run, on successful completion of the checks
    # below — regardless of whether a signal fired. A stale heartbeat.log
    # (no new entries during market hours) is itself the "something's
    # wrong" signal, checked separately from the Discord crash alert below.
    try:
        if not is_market_day():
            log.info("Weekend — skipping.")
            write_heartbeat()
            return

        if not in_trading_window():
            log.info("Outside trading window (10:00-15:30 ET) — skipping.")
            write_heartbeat()
            return

        df = fetch_15min_bars()
        if df.empty:
            log.warning("No data — market may be closed, holiday, or yfinance issue.")
            write_heartbeat()
            return

        df = add_indicators(df)
        signal = check_signal(df)

        if signal is None:
            log.info("No signal this bar.")
        else:
            prior_count = signals_today_count()
            signal_number = prior_count + 1
            earlier_status = check_earlier_signals_status(df) if prior_count > 0 else None
            send_discord_alert(signal, signal_number=signal_number, earlier_status=earlier_status)
            append_signal_to_log(signal, signal_number=signal_number)
            log.info(f"Done. Signal #{signal_number} today.")

        write_heartbeat()

    except Exception as e:
        log.error(f"UNHANDLED EXCEPTION in main(): {e}", exc_info=True)
        alert_msg = f"[ERROR] TQQQ Intraday Bot crashed: {e}\nCheck tqqq_intraday_bot.log for full traceback."
        if DISCORD_WEBHOOK:
            try:
                requests.post(DISCORD_WEBHOOK, json={"content": alert_msg}, timeout=10)
            except Exception:
                pass  # don't let a failed alert mask the original crash
        sys.exit(1)  # handled here — don't let it bubble up and double-alert


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="TQQQ Intraday Momentum Bot")
    parser.add_argument(
        "--reconcile", action="store_true",
        help="Automatically determine outcome (TARGET/STOP/TIME) for all "
             "unreconciled signals by replaying price data. Run once after "
             "market close (e.g. 4:30 PM ET cron)."
    )
    parser.add_argument(
        "--date", metavar="YYYY-MM-DD",
        help="Restrict --reconcile to a specific date."
    )
    parser.add_argument(
        "--summary", action="store_true",
        help="Print a performance summary of all reconciled signals."
    )
    parser.add_argument(
        "--blocker-summary", nargs="?", const=30, type=int, metavar="DAYS",
        help="Print a ranked tally of which condition(s) blocked a signal "
             "most often, over the last N days (default 30). Reads the "
             "structured rejection log — separate from the human-readable "
             "text log. Example: --blocker-summary  or  --blocker-summary 90"
    )
    parser.add_argument(
        "--notify", action="store_true",
        help="With --reconcile, also post today's results to Discord."
    )
    args = parser.parse_args()

    try:
        if args.reconcile:
            reconcile(target_date=args.date)
            if args.notify:
                send_reconcile_summary_to_discord()
        elif args.summary:
            print_summary()
        elif args.blocker_summary is not None:
            blocker_summary(days=args.blocker_summary)
        else:
            main()  # main() has its own internal try/except + heartbeat

    except Exception as e:
        log.error(f"UNHANDLED EXCEPTION: {e}", exc_info=True)
        alert_msg = f"[ERROR] TQQQ Intraday Bot crashed: {e}\nCheck tqqq_intraday_bot.log for full traceback."
        if DISCORD_WEBHOOK:
            try:
                requests.post(DISCORD_WEBHOOK, json={"content": alert_msg}, timeout=10)
            except Exception:
                pass
        sys.exit(1)
