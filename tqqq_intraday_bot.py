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
NEAR_MISS_THRESHOLD = 80   # pct_of_required >= this counts as "worth reconciling"
NEAR_MISS_LOG_PATH = os.path.join(FLAG_DIR, "tqqq_intraday_near_miss_outcomes.jsonl")
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

    `pct_of_required` is normalized to mean the same thing for every
    condition: higher % = closer to passing, 100% = right at the boundary.
    For "must meet/exceed" conditions (volume, vwap, ema_trend, recovery)
    this is actual/required directly. For "must stay under" conditions
    (pullback — a distance that must be below the threshold), it's
    required/actual — inverted on purpose, so the interpretation direction
    stays uniform across every condition without the reader needing to
    remember an exception. This uniformity is required for near-miss
    threshold filtering (NEAR_MISS_THRESHOLD) to work correctly across
    all condition types with one simple ">=" comparison.
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
    print(f"  Higher Avg %-of-req = closer to passing, for every condition")
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


def _fetch_reconcile_raw_data():
    """
    Shared 1-min data fetch for both real-trade reconciliation and
    near-miss reconciliation — extracted so both jobs reuse the SAME
    yfinance call instead of each fetching independently (halves the
    reconcile-time API load).
    """
    raw = yf.download(SYMBOL, period="7d", interval="1m", progress=False, auto_adjust=True)
    if raw.empty:
        return None
    if isinstance(raw.columns, pd.MultiIndex):
        raw.columns = raw.columns.get_level_values(0)
    raw.columns = [c.lower() for c in raw.columns]
    raw.index = pd.to_datetime(raw.index)
    if raw.index.tz is None:
        raw.index = raw.index.tz_localize("UTC")
    raw.index = raw.index.tz_convert(ET)
    raw = raw.between_time("09:30", "15:59")
    raw = raw[raw["volume"] > 0].copy()
    return raw


def _replay_forward(raw, signal_date, entry_after, target, stop):
    """
    Shared replay-forward logic — walks 1-min bars from entry_after onward,
    tracking max_favorable/max_adverse, returns (entry_price, exit_price,
    exit_reason, exit_time, max_favorable, max_adverse) or None if no data
    is available yet to determine an outcome.

    `target`/`stop` and `entry_price` can be derived from different bars up
    to 15 minutes apart (e.g. a signal's alert price vs. the next 1-min
    bar's open where the trade actually enters, or — for near-miss
    reconciliation — a rejected bar's close vs. its own +15min entry
    point). If price moves in that gap, entry_price can land outside the
    [stop, target] range the exit logic assumes, which without a fix could
    label an exit "TARGET" while pricing it below entry_price (net loss on
    a "win"), or "STOP" while pricing it above entry_price (net gain on a
    "loss") — caught during review, verified with a test that reproduced
    exactly this using synthetic gap data.

    Fixed by clamping: a TARGET exit is never priced below entry_price
    (guarantees non-negative P&L for anything labeled a win), and a STOP
    exit is never priced above entry_price (guarantees non-positive P&L
    for anything labeled a loss) — the label and the sign of the P&L can
    never contradict each other.
    """
    end_h, end_m = TRADE_END_H, TRADE_END_M
    day_bars = raw[raw.index.date == signal_date]
    future = day_bars[day_bars.index >= entry_after]

    if future.empty:
        return None

    entry_price   = future.iloc[0]["open"]
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
        if bar["low"] <= stop:
            exit_price, exit_reason, exit_time = min(stop, entry_price), "STOP", ts
            break
        if bar["high"] >= target:
            exit_price, exit_reason, exit_time = max(target, entry_price), "TARGET", ts
            break

    if exit_price is None:
        return None  # ran out of data — caller should retry later

    return entry_price, exit_price, exit_reason, exit_time, max_favorable, max_adverse


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

    Near-miss reconciliation (rejection log -> hypothetical outcomes) runs
    independently of real-trade reconciliation below — on a day with zero
    real signals, there can still be near-miss rejections worth checking.
    An earlier version returned early when there were no unreconciled real
    trades, which silently skipped near-miss reconciliation entirely on any
    no-signal day (caught in practice: a day with 20+ rejections, several
    near-misses, produced "No near-miss outcomes yet" because reconcile()
    never got past its own early-return). Fixed by fetching raw data once
    up front and always attempting both jobs against it.
    """
    trades = load_trade_log()
    unreconciled = [t for t in trades if not t["reconciled"]]

    if target_date:
        unreconciled = [t for t in unreconciled if t["date"] == target_date]

    raw = None
    if unreconciled:
        log.info(f"Reconciling {len(unreconciled)} trade(s)...")
        raw = _fetch_reconcile_raw_data()
        if raw is None:
            log.warning("yfinance returned empty dataframe — cannot reconcile real trades.")
        else:
            for record in unreconciled:
                signal_ts = pd.Timestamp(record["signal_bar_ts"])
                # Include the bar labeled exactly entry_after itself — that bar
                # (e.g. the 1-min bar labeled 10:30) covers the very first minute
                # the trade is actually open, and its high/low must be checked for
                # target/stop just like every subsequent bar. Strict '>' here would
                # skip that first minute entirely (caught during review — same
                # off-by-one class of bug as check_earlier_signals_status below).
                entry_after = signal_ts + pd.Timedelta(minutes=15)

                result = _replay_forward(raw, signal_ts.date(), entry_after,
                                         record["target"], record["stop"])
                if result is None:
                    log.info(f"Signal @ {record['bar_time']} ({record['date']}) still open "
                             f"in available data — will retry on next reconcile run.")
                    continue

                entry_price, exit_price, exit_reason, exit_time, max_favorable, max_adverse = result

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
    else:
        log.info("No unreconciled trades to process.")

    # Near-miss reconciliation runs regardless of whether real trades existed
    # today — it reads a completely separate data source (the rejection
    # log), not the trade log. If `raw` wasn't already fetched above (no
    # real trades to reconcile), fetch it now specifically for this job.
    if raw is None:
        raw = _fetch_reconcile_raw_data()
    if raw is None:
        log.warning("yfinance returned empty dataframe — cannot reconcile near-misses either.")
        return
    reconcile_near_misses(raw, target_date=target_date)


def _load_rejection_log():
    if not os.path.exists(REJECTION_LOG_PATH):
        return []
    records = []
    with open(REJECTION_LOG_PATH, "r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return records


def _load_near_miss_outcomes() -> set:
    """Returns a set of (date, time) tuples already reconciled, so we never
    replay the same near-miss twice."""
    if not os.path.exists(NEAR_MISS_LOG_PATH):
        return set()
    seen = set()
    with open(NEAR_MISS_LOG_PATH, "r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                seen.add((rec["date"], rec["time"]))
            except (json.JSONDecodeError, KeyError):
                continue
    return seen


def reconcile_near_misses(raw, target_date: str = None):
    """
    For every rejection in the rejection log where at least one failed
    condition is a near-miss (pct_of_required >= NEAR_MISS_THRESHOLD),
    replay 1-min data forward as if a trade HAD been entered at that bar,
    and record the 