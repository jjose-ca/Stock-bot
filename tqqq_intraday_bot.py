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
import argparse
import logging
from datetime import datetime, date
from zoneinfo import ZoneInfo

# ── CONFIG ────────────────────────────────────────────────────────────────────
SYMBOL          = "TQQQ"
DISCORD_WEBHOOK = os.environ.get("DISCORD_URL", "")

TARGET_PROFIT   = 0.50
STOP_LOSS       = 0.40
EMA_FAST        = 9
EMA_SLOW        = 13
VOLUME_MULT     = 1.0
PULLBACK_DIST   = 0.50
TRADE_START_H   = 10
TRADE_START_M   = 0
TRADE_END_H     = 15
TRADE_END_M     = 30

ET              = ZoneInfo("America/New_York")
FLAG_DIR        = os.path.dirname(os.path.abspath(__file__))
TRADE_LOG_PATH  = os.path.join(FLAG_DIR, "tqqq_intraday_trade_log.json")
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

    raw = yf.download(SYMBOL, period="5d", interval="1m", progress=False, auto_adjust=True)
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
        future = day_bars[day_bars.index > entry_after]

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
    Fetch 5 days of 1-min bars to seed EMA13 and time-of-day volume avg from
    open. True VWAP computed on 1-min data, then carried into 15-min bars.
    Only strictly completed 15-min bars are returned (no partial bar).
    """
    log.info("Fetching TQQQ 1-min bars (5d) from yfinance...")
    raw = yf.download(
        SYMBOL,
        period="5d",
        interval="1m",
        progress=False,
        auto_adjust=True,
    )

    if raw.empty:
        log.warning("yfinance returned empty dataframe.")
        return pd.DataFrame()

    if isinstance(raw.columns, pd.MultiIndex):
        raw.columns = raw.columns.get_level_values(0)

    raw.index = pd.to_datetime(raw.index)
    if raw.index.tz is None:
        raw.index = raw.index.tz_localize("UTC")
    raw.index = raw.index.tz_convert(ET)
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
    vol_avg uses time-of-day averaging across the 5-day window, so a
    10am bar is compared against historical 10am bars, not yesterday
    afternoon's slower volume.
    """
    df = df.copy()
    df["ema_fast"] = df["close"].ewm(span=EMA_FAST, adjust=False).mean()
    df["ema_slow"] = df["close"].ewm(span=EMA_SLOW, adjust=False).mean()

    df["time_of_day"] = df.index.time
    time_vol_avg       = df.groupby("time_of_day")["volume"].mean()
    df["vol_avg"]      = df["time_of_day"].map(time_vol_avg)
    df = df.drop(columns=["time_of_day"])
    return df


# ── SIGNAL CHECK ──────────────────────────────────────────────────────────────

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

    c1 = cur["close"] > cur["vwap"]
    if not c1:
        reasons.append(f"close ${cur['close']:.2f} <= VWAP ${cur['vwap']:.2f}")

    c2 = cur["ema_fast"] > cur["ema_slow"]
    if not c2:
        reasons.append(f"EMA{EMA_FAST} ${cur['ema_fast']:.2f} <= EMA{EMA_SLOW} ${cur['ema_slow']:.2f}")

    vol_avg = cur["vol_avg"]
    c3 = pd.notna(vol_avg) and cur["volume"] >= VOLUME_MULT * vol_avg
    if not c3:
        reasons.append(f"volume {cur['volume']:,.0f} < {VOLUME_MULT}x avg {vol_avg:,.0f}")

    near_vwap = abs(prev["low"] - prev["vwap"])     <= PULLBACK_DIST
    near_ema  = abs(prev["low"] - prev["ema_fast"]) <= PULLBACK_DIST
    c4 = near_vwap or near_ema
    if not c4:
        reasons.append(
            f"prev low ${prev['low']:.2f} not within ${PULLBACK_DIST} "
            f"of VWAP ${prev['vwap']:.2f} or EMA9 ${prev['ema_fast']:.2f}"
        )

    c5 = cur["close"] >= cur["vwap"] and cur["close"] >= cur["ema_fast"]
    if not c5:
        reasons.append("close did not recover above VWAP and EMA9")

    if not all([c1, c2, c3, c4, c5]):
        log.info(f"No signal [{candle_end_time}]: {' | '.join(reasons)}")
        return None

    entry_zone = cur["close"]
    tp         = round(entry_zone + TARGET_PROFIT, 2)
    sl         = round(entry_zone - STOP_LOSS, 2)

    pullback_src = "VWAP + EMA9" if (near_vwap and near_ema) else ("VWAP" if near_vwap else "EMA9")

    signal = {
        "bar_time":     cur.name.strftime("%H:%M ET"),
        "bar_ts":       cur.name.isoformat(),   # exact timestamp, used by reconcile
        "close":        round(float(cur["close"]), 2),
        "vwap":         round(float(cur["vwap"]), 2),
        "ema_fast":     round(float(cur["ema_fast"]), 2),
        "ema_slow":     round(float(cur["ema_slow"]), 2),
        "volume":       int(cur["volume"]),
        "vol_avg":      int(vol_avg),
        "vol_mult":     round(cur["volume"] / vol_avg, 1),
        "prev_low":     round(float(prev["low"]), 2),
        "prev_vwap":    round(float(prev["vwap"]), 2),
        "prev_ema9":    round(float(prev["ema_fast"]), 2),
        "entry_zone":   round(float(entry_zone), 2),
        "tp":           tp,
        "sl":           sl,
        "pullback_src": pullback_src,
    }

    log.info(f"SIGNAL at {signal['bar_time']} | entry ~${entry_zone:.2f} | TP ${tp} | SL ${sl}")
    return signal


# ── DISCORD ALERT ─────────────────────────────────────────────────────────────

def send_discord_alert(signal: dict, signal_number: int = 1):
    label = f"[SIGNAL #{signal_number} TODAY]" if signal_number > 1 else "[SIGNAL]"
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
            send_discord_alert(signal, signal_number=signal_number)
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
