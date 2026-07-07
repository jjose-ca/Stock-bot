"""
TQQQ Intraday Momentum Bot
Strategy : VWAP + EMA9/21 pullback on 15-min bars
Data     : yfinance (5d, 1-min) — resampled to 15-min
Alert    : Discord webhook
Execution: Manual on IBKR / Wealthsimple
Cron     : */15 10-15 * * 1-5   (every 15 min, 10am-3:30pm ET, weekdays)
"""

import yfinance as yf
import pandas as pd
import numpy as np
import requests
import os
import sys
import logging
from datetime import datetime, date
from zoneinfo import ZoneInfo

# ── CONFIG ────────────────────────────────────────────────────────────────────
SYMBOL          = "TQQQ"
DISCORD_WEBHOOK = os.environ.get("DISCORD_WEBHOOK_TQQQ", "")

TARGET_PROFIT   = 0.50
STOP_LOSS       = 0.40
EMA_FAST        = 9
EMA_SLOW        = 13   # upgraded from 21 — faster reversal confirmation
VOLUME_MULT     = 1.5
PULLBACK_DIST   = 0.50
TRADE_START_H   = 10
TRADE_START_M   = 0
TRADE_END_H     = 15
TRADE_END_M     = 30

ET              = ZoneInfo("America/New_York")
FLAG_DIR        = os.path.dirname(os.path.abspath(__file__))
FLAG_FILE       = os.path.join(FLAG_DIR, ".tqqq_intraday_traded")

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


# ── HELPERS ───────────────────────────────────────────────────────────────────

def already_traded_today() -> bool:
    flag = f"{FLAG_FILE}_{date.today().isoformat()}"
    return os.path.exists(flag)


def mark_traded_today():
    flag = f"{FLAG_FILE}_{date.today().isoformat()}"
    with open(flag, "w") as f:
        f.write(datetime.now(ET).isoformat())
    log.info(f"Flag written: {flag}")


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
    Fetch 5 days of 1-min bars to seed EMA21 and 20-bar volume avg from open.
    True VWAP computed on 1-min data, then carried into 15-min bars.
    Only strictly completed 15-min bars are returned (no partial bar).
    """
    log.info("Fetching TQQQ 1-min bars (5d) from yfinance...")
    raw = yf.download(
        SYMBOL,
        period="5d",          # FIX 1: was "1d" — starved EMA/vol avg until 3pm
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

    # FIX 3: True VWAP on 1-min data (resets daily), then carry last value per 15-min bar
    raw["date"]       = raw.index.date
    raw["tp"]         = (raw["high"] + raw["low"] + raw["close"]) / 3
    raw["tp_vol"]     = raw["tp"] * raw["volume"]
    raw["cum_tp_vol"] = raw.groupby("date")["tp_vol"].cumsum()
    raw["cum_vol"]    = raw.groupby("date")["volume"].cumsum()
    raw["vwap"]       = raw["cum_tp_vol"] / raw["cum_vol"]

    # Resample to 15-min — take last VWAP value in each bar (most accurate)
    df = raw.resample("15min", label="left", closed="left").agg(
        open=("open",   "first"),
        high=("high",   "max"),
        low=("low",     "min"),
        close=("close", "last"),
        volume=("volume", "sum"),
        vwap=("vwap",   "last"),   # true VWAP at bar close
    ).dropna(subset=["open"])
    df = df[df["volume"] > 0].copy()

    # FIX 2: Drop incomplete bars — only keep bars whose 15-min window has closed
    now = datetime.now(ET)
    df  = df[df.index + pd.Timedelta(minutes=15) <= now]

    log.info(f"  {len(df)} completed 15-min bars.")
    return df


# ── INDICATORS ────────────────────────────────────────────────────────────────

def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """
    VWAP already computed in fetch. Add EMAs and volume avg.

    vol_avg is computed per-day using a grouped rolling window so that
    yesterday's late-session low-volume bars don't bleed into today's
    morning comparison baseline — which would produce false volume spikes.
    EMAs span across days intentionally (same as a live chart would show).
    """
    df = df.copy()
    df["ema_fast"] = df["close"].ewm(span=EMA_FAST, adjust=False).mean()
    df["ema_slow"] = df["close"].ewm(span=EMA_SLOW, adjust=False).mean()

    # Volume baseline: average volume for each specific 15-min time slot
    # across all days in the dataset. Compares 10am today vs historical
    # 10am bars — not against yesterday afternoon's low-volume bars.
    df["time_of_day"] = df.index.time
    time_vol_avg      = df.groupby("time_of_day")["volume"].mean()
    df["vol_avg"]     = df["time_of_day"].map(time_vol_avg)
    df = df.drop(columns=["time_of_day"])
    return df


# ── SIGNAL CHECK ──────────────────────────────────────────────────────────────

def check_signal(df: pd.DataFrame) -> dict | None:
    """
    Check the last completed 15-min bar for a momentum pullback signal.
    All incomplete bars already stripped in fetch — safe to use iloc[-1].

    Conditions (all must be true):
      1. close > VWAP
      2. EMA9 > EMA21
      3. volume > 1.5x 20-bar avg
      4. prev bar low within $0.50 of VWAP or EMA9  (pullback)
      5. close >= VWAP and close >= EMA9             (recovery)
    """
    if len(df) < EMA_SLOW + 2:
        log.info(f"Not enough bars ({len(df)}) for signal check — need {EMA_SLOW + 2}.")
        return None

    # FIX 2: Use iloc[-1] — incomplete bars already stripped in fetch
    cur  = df.iloc[-1]
    prev = df.iloc[-2]

    # Trading window check using candle END time (left-label bars):
    # A bar labeled 09:45 covers 09:45–10:00, so we check when it closes.
    candle_end_time = (cur.name + pd.Timedelta(minutes=15)).time()
    start = cur.name.replace(hour=TRADE_START_H, minute=TRADE_START_M, second=0).time()
    end   = cur.name.replace(hour=TRADE_END_H,   minute=TRADE_END_M,   second=0).time()

    if not (start <= candle_end_time <= end):
        log.info(f"Candle ending at {candle_end_time} outside trading window ({start}–{end}).")
        return None

    # Ensure prev bar is same day (don't use yesterday's bar as pullback ref)
    if cur.name.date() != prev.name.date():
        log.info("Previous bar is from yesterday — skipping pullback check.")
        return None

    # ── Condition checks ──────────────────────────────────────────────────────
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

    # ── Signal passed ─────────────────────────────────────────────────────────
    entry_zone = cur["close"]
    tp         = round(entry_zone + TARGET_PROFIT, 2)
    sl         = round(entry_zone - STOP_LOSS, 2)

    pullback_src = "VWAP + EMA9" if (near_vwap and near_ema) else ("VWAP" if near_vwap else "EMA9")

    signal = {
        "bar_time":   cur.name.strftime("%H:%M ET"),
        "close":      round(float(cur["close"]), 2),
        "vwap":       round(float(cur["vwap"]), 2),
        "ema_fast":   round(float(cur["ema_fast"]), 2),
        "ema_slow":   round(float(cur["ema_slow"]), 2),
        "volume":     int(cur["volume"]),
        "vol_avg":    int(vol_avg),
        "vol_mult":   round(cur["volume"] / vol_avg, 1),
        "prev_low":   round(float(prev["low"]), 2),
        "prev_vwap":  round(float(prev["vwap"]), 2),
        "prev_ema9":  round(float(prev["ema_fast"]), 2),
        "entry_zone": round(float(entry_zone), 2),
        "tp":         tp,
        "sl":         sl,
        "pullback_src": pullback_src,
    }

    log.info(f"SIGNAL at {signal['bar_time']} | entry ~${entry_zone:.2f} | TP ${tp} | SL ${sl}")
    return signal


# ── DISCORD ALERT ─────────────────────────────────────────────────────────────

def send_discord_alert(signal: dict):
    msg = (
        f"🟢  **TQQQ INTRADAY MOMENTUM SIGNAL**\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🕐  Bar time   : `{signal['bar_time']}`\n"
        f"💵  Entry zone : `~${signal['entry_zone']}`\n"
        f"🎯  Target     : `${signal['tp']}`  (+${TARGET_PROFIT})\n"
        f"🛑  Stop       : `${signal['sl']}`  (-${STOP_LOSS})\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📊  VWAP       : `${signal['vwap']}`  ✅ price above\n"
        f"📈  EMA{EMA_FAST}       : `${signal['ema_fast']}`  ✅ above EMA{EMA_SLOW} (${signal['ema_slow']})\n"
        f"📦  Volume     : `{signal['vol_mult']}x avg`  ✅  ({signal['volume']:,} vs avg {signal['vol_avg']:,})\n"
        f"🔽  Pullback   : `prev low ${signal['prev_low']}` near {signal['pullback_src']}  ✅\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"⚠️  Manual execution — verify chart before entering\n"
        f"🚪  Exit by **3:30 PM ET** regardless\n"
        f"📋  R:R = 1.25:1  |  15-min momentum strategy"
    )

    if not DISCORD_WEBHOOK:
        log.warning("DISCORD_WEBHOOK_TQQQ not set — printing alert only.")
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


# ── MAIN ──────────────────────────────────────────────────────────────────────

def main():
    log.info(f"── TQQQ Intraday Bot: {datetime.now(ET).strftime('%Y-%m-%d %H:%M ET')} ──")

    if not is_market_day():
        log.info("Weekend — skipping.")
        return

    if not in_trading_window():
        log.info("Outside trading window (10:00–15:30 ET) — skipping.")
        return

    if already_traded_today():
        log.info("Already traded today — skipping.")
        return

    df = fetch_15min_bars()
    if df.empty:
        log.info("No data — market may be closed or holiday.")
        return

    df = add_indicators(df)
    signal = check_signal(df)

    if signal is None:
        log.info("No signal this bar.")
        return

    send_discord_alert(signal)
    mark_traded_today()
    log.info("Done.")


if __name__ == "__main__":
    main()
