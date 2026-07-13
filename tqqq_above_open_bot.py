"""
TQQQ Intraday Above-Open Momentum Bot
Strategy : Price above today's own opening price, checked on 15-min bars,
           10:00am-3:30pm ET. Every qualifying bar fires an alert (not
           gated to once/day) — signal #1 each day is the ONLY one that
           was backtested/validated; later same-day signals are shown for
           visibility but are NOT individually validated (see README).
Data     : yfinance (5d, 1-min) — resampled to 15-min, same pipeline as
           tqqq_intraday_bot.py, reused because it's already tested.
Alert    : Discord webhook
Execution: Manual on IBKR / Wealthsimple
Cron     : 0,15,30,45 10-15 * * 1-5     (signal check, every 15 min, 10am-3:30pm ET)
           30 16 * * 1-5 --reconcile    (auto-check outcomes, 4:30pm ET)

Validated backtest (4.5yr Databento, CORRECTED honest-timing methodology,
first-signal-of-day only, INCLUDING the 10:00-10:30 window — tested
and found to improve results over excluding it): 858 trades, 65.4% win
rate, $0.1767/share expectancy, out-of-sample consistent across
2022-2024 and 2024-2026 independently (gap $0.0012), positive in every
year 2022-2026.

This bot is entirely separate from tqqq_intraday_bot.py — separate config,
separate log files, separate heartbeat entry, separate cron lines. The
original pullback bot is left completely untouched.
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
DISCORD_WEBHOOK = os.environ.get("DISCORD_URL_ABOVE_OPEN", os.environ.get("DISCORD_URL", ""))

TARGET_PROFIT   = 0.50
STOP_LOSS       = 0.40
TRADE_START_H   = 10
TRADE_START_M   = 0      # Validated: including the 10:00-10:30 window
                          # (previously excluded from the original 838-trade
                          # backtest, which used a '!=10:00' filter inherited
                          # from unrelated vwap_only diagnostics) was tested
                          # directly for THIS strategy and found to IMPROVE
                          # results: 858 trades, 65.4% WR, $0.1767 exp
                          # (vs. 838 trades, 63.8% WR, $0.1625 exp without
                          # it), with a tighter out-of-sample gap ($0.0012
                          # vs $0.0110) — more consistent, not less.
TRADE_END_H     = 15     # no new signals after this, AND forced exit cutoff
TRADE_END_M     = 30

MAX_RETRIES     = 12     # 12 x 5 seconds = 60 second max wait
WAIT_SECONDS    = 5

ET              = ZoneInfo("America/New_York")
FLAG_DIR        = os.path.dirname(os.path.abspath(__file__))
TRADE_LOG_PATH  = os.path.join(FLAG_DIR, "tqqq_above_open_trade_log.json")
HEARTBEAT_PATH  = "/root/logs/heartbeat.log"

# ── LOGGING ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(os.path.join(FLAG_DIR, "tqqq_above_open_bot.log")),
    ],
)
log = logging.getLogger(__name__)


def write_heartbeat():
    now = datetime.now(ET)
    try:
        with open(HEARTBEAT_PATH, "a") as _hb:
            _hb.write(f"[{now.strftime('%Y-%m-%d %H:%M:%S ET')}] tqqq_above_open_bot OK\n")
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


def signals_today_count() -> int:
    trades = load_trade_log()
    today  = date.today().isoformat()
    return sum(1 for t in trades if t["date"] == today)


def append_signal_to_log(signal: dict, signal_number: int):
    trades = load_trade_log()
    record = {
        "date":              date.today().isoformat(),
        "bar_time":          signal["bar_time"],
        "signal_bar_ts":     signal["bar_ts"],
        "signal_number":     signal_number,   # 1 = validated; 2+ = informational only
        "validated":         signal_number == 1,
        "alert_price":       signal["entry_zone"],
        "target":            signal["tp"],
        "stop":              signal["sl"],
        "day_open":          signal["day_open"],
        "close":             signal["close"],
        "pct_above_open":    signal["pct_above_open"],
        "alert_sent_at":     datetime.now(ET).isoformat(),
        # Auto-reconcile fields
        "reconciled":        False,
        "exit_price":        None,
        "exit_time":         None,
        "exit_reason":       None,
        "max_favorable":     None,
        "max_adverse":       None,
        "pnl_per_share":     None,
    }
    trades.append(record)
    save_trade_log(trades)
    log.info(f"Trade record appended to {TRADE_LOG_PATH}")


# ── RECONCILIATION (reuses the same corrected, gap-clamped logic already
#    tested and fixed in tqqq_intraday_bot.py — copied here so this bot has
#    zero shared imports/state with the original) ────────────────────────────

def _fetch_reconcile_raw_data():
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
    return raw[raw["volume"] > 0].copy()


def _replay_forward(raw, signal_date, entry_after, target, stop):
    """
    Same corrected logic as tqqq_intraday_bot.py's _replay_forward():
    entry_after is inclusive (>=, not >), and exit prices are clamped so a
    TARGET exit can never price below entry and a STOP exit can never price
    above entry (fixes the gap-through-target/stop mispricing bug found and
    fixed earlier in this project).
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
        bar_end_time  = ts.time()
        cutoff        = ts.replace(hour=end_h, minute=end_m, second=0).time()

        if bar_end_time >= cutoff:
            exit_price, exit_reason, exit_time = bar["open"], "TIME", ts
            break
        if bar["low"] <= stop:
            # Combines two protections, caught during review of an external
            # suggestion to use bar["open"] alone:
            #   1. Realistic gap-fill: if THIS bar gapped through stop, credit
            #      its actual open price, not just the nominal stop level.
            #   2. Sign consistency (the original bug this clamp exists for):
            #      because target/stop are computed from entry_zone (signal
            #      bar's close) while entry_price comes from execution 15min
            #      later, stop is NOT guaranteed to be below entry_price.
            #      Using bar["open"] alone (without also clamping to
            #      entry_price) reintroduces a STOP-labeled exit that can
            #      price ABOVE entry — verified by test to occur on bars
            #      after the entry bar, where bar["open"] != entry_price.
            exit_price, exit_reason, exit_time = min(stop, bar["open"], entry_price), "STOP", ts
            break
        if bar["high"] >= target:
            exit_price, exit_reason, exit_time = max(target, bar["open"], entry_price), "TARGET", ts
            break

    if exit_price is None:
        return None

    return entry_price, exit_price, exit_reason, exit_time, max_favorable, max_adverse


def reconcile(target_date: str = None):
    """
    Determines the real outcome of every unreconciled signal by replaying
    1-min price data forward from the entry point (signal_bar_ts + 15min,
    matching the backtest's validated timing convention exactly).
    """
    trades = load_trade_log()
    unreconciled = [t for t in trades if not t["reconciled"]]
    if target_date:
        unreconciled = [t for t in unreconciled if t["date"] == target_date]

    if not unreconciled:
        log.info("No unreconciled trades to process.")
        return

    log.info(f"Reconciling {len(unreconciled)} trade(s)...")
    raw = _fetch_reconcile_raw_data()
    if raw is None:
        log.warning("yfinance returned empty dataframe — cannot reconcile.")
        return

    for record in unreconciled:
        signal_ts   = pd.Timestamp(record["signal_bar_ts"])
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

        tag = "VALIDATED" if record["validated"] else "info-only"
        log.info(
            f"Reconciled {record['date']} {record['bar_time']} [{tag}, sig#{record['signal_number']}]: "
            f"entry ${entry_price:.2f} -> exit ${exit_price:.2f} [{exit_reason}]  "
            f"P&L ${trades[idx]['pnl_per_share']:+.2f}"
        )

    save_trade_log(trades)
    log.info("Reconcile complete.")


def print_summary():
    """
    Prints performance split by validated (signal #1) vs informational
    (signal #2+) — since the backtest only validated first-of-day signals,
    this split is the honest way to track whether live results match what
    was actually tested.
    """
    trades = load_trade_log()
    done   = [t for t in trades if t["reconciled"]]

    if not done:
        print("No reconciled trades yet.")
        return

    for label, subset in [
        ("VALIDATED (signal #1 each day)", [t for t in done if t["validated"]]),
        ("INFORMATIONAL (signal #2+ each day)", [t for t in done if not t["validated"]]),
        ("ALL SIGNALS COMBINED", done),
    ]:
        if not subset:
            print(f"\n{label}: no reconciled trades yet.")
            continue
        wins   = [t for t in subset if t["pnl_per_share"] > 0]
        losses = [t for t in subset if t["pnl_per_share"] <= 0]
        wr     = len(wins) / len(subset) * 100
        total  = sum(t["pnl_per_share"] for t in subset)
        avg_w  = sum(t["pnl_per_share"] for t in wins)   / len(wins)   if wins   else 0
        avg_l  = sum(t["pnl_per_share"] for t in losses) / len(losses) if losses else 0
        exp    = (wr/100 * avg_w) + ((1-wr/100) * avg_l)

        print(f"\n{'='*58}")
        print(f"  {label}")
        print(f"{'='*58}")
        print(f"  Trades       : {len(subset)}")
        print(f"  Win rate     : {wr:.1f}%  ({len(wins)}W / {len(losses)}L)")
        print(f"  Expectancy   : ${exp:.4f}/share")
        print(f"  Total P&L    : ${total:.2f}/share")

    print(f"\n{'='*58}")
    print(f"  Backtest reference (validated, first-signal-only):")
    print(f"  858 trades, 65.4% WR, $0.1767/share expectancy")
    print(f"{'='*58}\n")


# ── HELPERS ───────────────────────────────────────────────────────────────────
def in_trading_window() -> bool:
    now   = datetime.now(ET).time()
    start = datetime.now(ET).replace(hour=TRADE_START_H, minute=TRADE_START_M, second=0, microsecond=0).time()
    end   = datetime.now(ET).replace(hour=TRADE_END_H, minute=TRADE_END_M, second=0, microsecond=0).time()
    return start <= now < end


def is_market_day() -> bool:
    return datetime.now(ET).weekday() < 5


# ── DATA ──────────────────────────────────────────────────────────────────────
def fetch_15min_bars() -> pd.DataFrame:
    """
    Fetches 1-min data with retry logic (ported from tqqq_intraday_bot.py,
    including the -1 minute off-by-one fix for 1-min-fetch pipelines) and
    resamples to 15-min bars. Computes each day's own opening price —
    the only thing this strategy actually needs, no VWAP/EMA/volume baseline.
    """
    now = datetime.now(ET)
    closed_minute = (now.minute // 15) * 15
    expected_bar_time = now.replace(minute=closed_minute, second=0, microsecond=0) - timedelta(minutes=1)

    # Holiday/weekend pre-check
    try:
        _pre = yf.download(SYMBOL, period="1d", interval="1m", auto_adjust=True, progress=False)
        if _pre.empty:
            log.warning("No intraday data available — market likely closed (holiday or weekend).")
            return pd.DataFrame()
        if _pre.index[-1].tz_convert(ET).date() < now.date():
            log.warning("No today's data — market closed (holiday or early close).")
            return pd.DataFrame()
    except Exception as e:
        log.warning(f"Holiday pre-check failed ({e}) — proceeding to retry loop anyway.")

    raw = None
    for attempt in range(MAX_RETRIES):
        try:
            candidate = yf.download(SYMBOL, period="7d", interval="1m", progress=False, auto_adjust=True)
            if candidate.empty:
                time.sleep(WAIT_SECONDS)
                continue
            if isinstance(candidate.columns, pd.MultiIndex):
                candidate.columns = candidate.columns.get_level_values(0)
            candidate.index = pd.to_datetime(candidate.index)
            if candidate.index.tz is None:
                candidate.index = candidate.index.tz_localize("UTC")
            candidate.index = candidate.index.tz_convert(ET)

            if candidate.index[-1] >= expected_bar_time:
                raw = candidate
                break
            time.sleep(WAIT_SECONDS)
        except Exception as e:
            log.warning(f"Fetch attempt {attempt+1} failed: {e}")
            time.sleep(WAIT_SECONDS)
            continue

    if raw is None:
        log.warning("Retry loop exhausted — making one final attempt, proceeding with whatever's available.")
        try:
            raw = yf.download(SYMBOL, period="7d", interval="1m", progress=False, auto_adjust=True)
            if isinstance(raw.columns, pd.MultiIndex):
                raw.columns = raw.columns.get_level_values(0)
            raw.index = pd.to_datetime(raw.index)
            if raw.index.tz is None:
                raw.index = raw.index.tz_localize("UTC")
            raw.index = raw.index.tz_convert(ET)
        except Exception:
            return pd.DataFrame()

    if raw is None or raw.empty:
        return pd.DataFrame()

    raw.columns = [c.lower() for c in raw.columns]
    raw = raw.between_time("09:30", "15:59")
    raw = raw[raw["volume"] > 0].copy()
    raw["date"] = raw.index.date

    df = raw.resample("15min", label="left", closed="left").agg(
        open=("open", "first"), high=("high", "max"),
        low=("low", "min"), close=("close", "last"), volume=("volume", "sum"),
    ).dropna(subset=["open"])
    df = df[df["volume"] > 0].copy()
    df["date"] = df.index.date

    # Day's own opening price — first 15-min bar's open, per day
    day_open = df.groupby("date")["open"].transform("first")
    df["day_open"] = day_open

    now = datetime.now(ET)
    df = df[df.index + pd.Timedelta(minutes=15) <= now].copy()
    return df


# ── SIGNAL CHECK ──────────────────────────────────────────────────────────────
def check_signal(df: pd.DataFrame) -> dict | None:
    """
    Fires whenever the most recently completed 15-min bar's close is above
    today's own opening price, after 10:00am ET. Unlike the original
    pullback bot, this is NOT gated to once per day — every qualifying bar
    fires (per explicit request), but only the FIRST signal of the day was
    backtested/validated (858 trades, 65.4% WR, $0.1767 exp, out-of-sample
    consistent). Signal #2+ each day was separately backtested as "every
    qualifying bar" and found meaningfully worse (12,489 trades, 54.5% WR,
    $0.0552 exp, wider out-of-sample gap) — shown for visibility, not as an
    equally-trusted signal.
    """
    if len(df) < 1:
        return None

    cur = df.iloc[-1]
    candle_end_time = (cur.name + pd.Timedelta(minutes=15)).time()

    trade_start = datetime.now(ET).replace(
        hour=TRADE_START_H, minute=TRADE_START_M, second=0, microsecond=0).time()
    trade_end = datetime.now(ET).replace(
        hour=TRADE_END_H, minute=TRADE_END_M, second=0, microsecond=0).time()
    if not (trade_start <= candle_end_time <= trade_end):
        return None

    if pd.isna(cur["day_open"]):
        log.info(f"No signal [{candle_end_time}]: day_open not yet available")
        return None

    pct_above = (cur["close"] - cur["day_open"]) / cur["day_open"] * 100
    is_above  = cur["close"] > cur["day_open"]

    if not is_above:
        log.info(f"No signal [{candle_end_time}]: close ${cur['close']:.2f} <= "
                 f"day_open ${cur['day_open']:.2f} ({pct_above:+.2f}%)")
        return None

    entry_zone = cur["close"]
    tp = round(entry_zone + TARGET_PROFIT, 2)
    sl = round(entry_zone - STOP_LOSS, 2)

    signal = {
        "bar_time":       cur.name.strftime("%H:%M ET"),
        "bar_ts":         cur.name.isoformat(),
        "close":          round(float(cur["close"]), 2),
        "day_open":       round(float(cur["day_open"]), 2),
        "pct_above_open": round(float(pct_above), 3),
        "entry_zone":     round(float(entry_zone), 2),
        "tp":             tp,
        "sl":             sl,
    }

    log.info(f"SIGNAL at {signal['bar_time']} | close ${entry_zone:.2f} > "
             f"open ${signal['day_open']:.2f} ({pct_above:+.2f}%) | TP ${tp} | SL ${sl}")
    return signal


# ── DISCORD ALERT ─────────────────────────────────────────────────────────────
def send_discord_alert(signal: dict, signal_number: int):
    validated = signal_number == 1
    tag = "[VALIDATED SIGNAL]" if validated else f"[INFO ONLY — SIGNAL #{signal_number} TODAY]"

    validation_note = (
        "Backtested: 858 trades, 65.4% WR, $0.1767/share expectancy, "
        "out-of-sample validated."
        if validated else
        "NOT individually backtested — later same-day signals tested\n"
        "as a group and found meaningfully weaker (54.5% WR, $0.0552 exp).\n"
        "Shown for visibility, use judgment."
    )

    msg = (
        f"{tag} TQQQ ABOVE-OPEN MOMENTUM\n"
        f"------------------------------\n"
        f"Bar time    : {signal['bar_time']}\n"
        f"Entry zone  : ~${signal['entry_zone']}\n"
        f"Target      : ${signal['tp']}  (+${TARGET_PROFIT})\n"
        f"Stop        : ${signal['sl']}  (-${STOP_LOSS})\n"
        f"------------------------------\n"
        f"Day's open  : ${signal['day_open']}\n"
        f"Current     : ${signal['close']}  ({signal['pct_above_open']:+.2f}% vs open)\n"
        f"------------------------------\n"
        f"{validation_note}\n"
        f"------------------------------\n"
        f"Manual execution — verify chart before entering\n"
        f"Exit by {TRADE_END_H}:{TRADE_END_M:02d} ET regardless\n"
        f"Outcome auto-reconciles at 4:30 PM ET — no action needed."
    )

    if not DISCORD_WEBHOOK:
        print("\n" + msg + "\n")
        return
    try:
        requests.post(DISCORD_WEBHOOK, json={"content": msg}, timeout=10)
    except Exception as e:
        log.error(f"Discord send failed: {e}")


# ── MAIN ──────────────────────────────────────────────────────────────────────
def main():
    log.info(f"-- TQQQ Above-Open Bot: {datetime.now(ET).strftime('%Y-%m-%d %H:%M ET')} --")
    try:
        if not is_market_day():
            write_heartbeat()
            return

        if not in_trading_window():
            write_heartbeat()
            return

        df = fetch_15min_bars()
        if df.empty:
            write_heartbeat()
            return

        signal = check_signal(df)

        if signal:
            signal_number = signals_today_count() + 1
            send_discord_alert(signal, signal_number)
            append_signal_to_log(signal, signal_number)
            log.info(f"Done. Signal #{signal_number} fired "
                     f"({'validated' if signal_number == 1 else 'info-only'}).")
        else:
            log.info("No signal this bar.")

        write_heartbeat()

    except Exception as e:
        log.error(f"UNHANDLED EXCEPTION in main(): {e}", exc_info=True)
        if DISCORD_WEBHOOK:
            try:
                requests.post(DISCORD_WEBHOOK,
                              json={"content": f"[CRASH] tqqq_above_open_bot: {e}"},
                              timeout=10)
            except Exception:
                pass
        sys.exit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--reconcile", action="store_true",
                        help="Reconcile all unreconciled signals against real price data.")
    parser.add_argument("--date", metavar="YYYY-MM-DD",
                        help="With --reconcile, only reconcile signals from this date.")
    parser.add_argument("--summary", action="store_true",
                        help="Print performance split: validated vs informational signals.")
    args = parser.parse_args()

    try:
        if args.reconcile:
            reconcile(target_date=args.date)
        elif args.summary:
            print_summary()
        else:
            main()
    except Exception as e:
        log.error(f"UNHANDLED EXCEPTION in CLI dispatch: {e}", exc_info=True)
        sys.exit(1)
