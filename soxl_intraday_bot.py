"""
=============================================================================
  SOXL INTRADAY MOMENTUM BOT v1.0
=============================================================================

ACCOUNT: Non-registered Wealthsimple (NOT TFSA)
PURPOSE: Intraday momentum alerts for manual execution same day.
         Completely independent of soxl_bot.py (daily swing).

SIGNALS:
  Signal 1 — Opening Range Breakout (ORB)
    The first 30 minutes (9:30-10:00am) define the day's Opening Range.
    When a subsequent bar closes ABOVE the OR high with elevated volume,
    momentum is confirmed in the bull direction.
    Extra filters (v1.1): RSI 45-65, strong candle body (>0.3% range),
    daily 50 EMA trend filter (no ORB against daily downtrend).
    Fires: once per day, after 10:00am bar closes.

  Signal 2 — VWAP Reclaim
    VWAP (Volume Weighted Average Price) is the institutional anchor.
    When SOXL dips below VWAP and then reclaims it on a green bar
    with elevated volume, institutional buying is resuming.
    Extra filters (v1.1): minimum 0.5% dip below VWAP before reclaim,
    RSI was below 50 on prior bar, MACD histogram turning positive,
    daily 50 EMA trend filter.
    Fires: any time during the day when conditions are met.

  Signal 3 — Previous Day High Breakout (PDH)
    Yesterday's high is the level sellers defended last session.
    When SOXL breaks and closes above that level with volume today,
    prior resistance becomes support and momentum continues higher.
    Fires: once per day, after 10:00am, when price breaks PDH.
    Confluence: if ORB also fires on the same bar, labelled as
    PDH+ORB Confluence — strongest possible intraday momentum signal.

SLIPPAGE GATE:
  Each alert shows current live price vs signal bar close price.
  If price has moved more than MAX_SLIPPAGE_PCT from signal close,
  the alert is suppressed — R/R is too degraded to enter.
  You see a "GATE BLOCKED" log but no Discord message.

EXIT STRATEGY:
  Fixed target: +4% from entry (realistic for SOXL intraday range)
  Time stop:    3:35pm ET — alert fires to exit before market close
  Sell alerts:  RSI > 68 or price drops below 9 EMA after being above

SCHEDULE (GitHub Actions):
  10:45am — catches ORB after opening hour closes
  12:00pm — catches midday VWAP reclaims
  2:00pm  — catches afternoon momentum setups
  3:20pm  — final check + exit alert if position open

DISCORD LABEL: [SOXL INTRADAY - NON-REG]
  Clearly distinct from [SOXL SWING - TFSA] alerts.

HOW TO RUN:
  python soxl_intraday_bot.py           # normal run
  python soxl_intraday_bot.py --force   # bypass time gates (testing)
  python soxl_intraday_bot.py --dry-run # skip trade log writes (testing)
=============================================================================
"""

import os
import sys
import json
import time
import argparse
import pandas as pd
import ta as ta_lib   # replaces pandas_ta — no numpy/pandas version conflicts
import pytz
import requests
import yfinance as yf
from datetime import datetime, timedelta
from pathlib import Path

# =============================================================================
#  SECTION 1 - CONFIGURATION
# =============================================================================

DISCORD_WEBHOOK_URL = os.getenv("DISCORD_URL")
TICKER              = "SOXL"
TIMEZONE            = "US/Eastern"

# Portfolio sizing - non-registered account
# Adjust PORTFOLIO_VALUE to your actual non-reg account size
PORTFOLIO_VALUE     = 1000.0
POSITION_PCT        = 5.0      # 5% per intraday trade = $50 at $1,000 baseline
                                # Scale up once you have confidence in the signals

# Signal parameters
ORB_WINDOW_MINUTES  = 30       # Opening range = first 30 min (9:30-10:00am)
VOLUME_MULT         = 1.5      # Signal bar volume must be 1.5x the 10-bar avg
RSI_OVERBOUGHT      = 68       # Sell alert threshold
MAX_SLIPPAGE_PCT    = 0.005    # 0.5% max slippage — calculated for SOXL at ~$200+
                               # At 2% slippage on a $212 entry with -2% stop:
                               # live risk doubles, live reward halves → R/R inverted
                               # At 0.5%: live R/R stays ~1.8:1 (acceptable)
                               # Tighter gate = fewer alerts but valid R/R on all that fire

# Exit parameters
TARGET_PCT          = 0.04     # +4% fixed profit target from entry
STOP_PCT            = 0.02     # -2% stop loss from entry (2:1 R/R)
EXIT_HOUR           = 15       # 3pm ET
EXIT_MINUTE         = 35       # 3:35pm ET time stop

# Previous Day High config
PDH_BODY_MIN_PCT    = 0.003    # PDH breakout bar body >= 0.3% (same as ORB)

# Gap-fill filter parameters (v1.1)
MIN_VWAP_DIP_PCT    = 0.005    # VWAP reclaim: price must have been >= 0.5% below VWAP
ORB_RSI_MIN         = 45       # ORB: RSI must be in momentum zone (not just "not overbought")
ORB_RSI_MAX         = 65       # ORB: RSI ceiling — above 65 is too extended
ORB_BODY_MIN_PCT    = 0.003    # ORB: breakout bar body must be >= 0.3% (no wick-only breaks)
DAILY_TREND_FILTER  = True     # Require daily 50 EMA uptrend for both signals

# Trade log - separate from swing bot
TRADE_LOG_FILE      = "soxl_intraday_trade_log.json"

# Discord colors
COLOR_GREEN  = 5763719
COLOR_YELLOW = 16776960
COLOR_BLUE   = 3447003
COLOR_RED    = 15548997
COLOR_ORANGE = 16744272

# =============================================================================
#  SECTION 2 - DATA FETCHING
# =============================================================================

def fetch_intraday(ticker=TICKER, days=7):  # 7 calendar days ensures Monday safety
    """
    Downloads 15-min bars for the last N days.
    Returns DataFrame with completed bars only — the currently-forming
    bar is excluded using snap-to-last-closed logic.
    """
    try:
        df = yf.download(
            ticker, period=f"{days}d", interval="15m",
            auto_adjust=True, progress=False,
        )
        if df.empty:
            return None

        # Flatten MultiIndex if present
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        df.dropna(subset=["Close"], inplace=True)

        # Convert index to ET for easier time comparisons
        et_tz = pytz.timezone(TIMEZONE)
        if df.index.tz is None:
            df.index = df.index.tz_localize("UTC").tz_convert(et_tz)
        else:
            df.index = df.index.tz_convert(et_tz)

        # Snap to last closed bar
        # The currently-forming bar's close is unreliable mid-candle
        et_now        = datetime.now(et_tz)
        closed_minute = (et_now.minute // 15) * 15
        last_closed   = et_now.replace(
            minute=closed_minute, second=0, microsecond=0
        )
        df = df[df.index <= last_closed]

        if df.empty:
            return None

        delay_min = (et_now - last_closed).total_seconds() / 60
        bar_time  = df.index[-1].strftime("%I:%M %p ET")
        print(f"   Last closed bar: {bar_time} "
              f"(bot ran {delay_min:.0f} min after bar close)")
        return df

    except Exception as e:
        print(f"   Intraday fetch failed: {e}")
        return None


def fetch_live_price(ticker=TICKER):
    """Fetches current live price via yfinance fast_info."""
    try:
        p = yf.Ticker(ticker).fast_info.get("last_price")
        return float(p) if p else None
    except Exception:
        return None


# =============================================================================
#  SECTION 2b - DAILY TREND FILTER
# =============================================================================

def fetch_daily_data(ticker=TICKER):
    """
    Single daily API call returning everything needed from daily bars:
      - Daily 50 EMA trend filter (above/below = bullish/bearish)
      - Previous day High and Low (for PDH signal)

    One yfinance call covers both needs — avoids API call bloat.
    Using 1-year of daily data ensures EMA-50 is fully warmed up
    and matches TradingView exactly.

    Returns (above_ema50, ema50, daily_close, prev_high, prev_low)
    Fails open on data issues — returns (True, None, None, None, None)
    so a data problem never silently blocks all signals.
    """
    fail_open = (True, None, None, None, None)
    if not DAILY_TREND_FILTER:
        return fail_open

    try:
        df = yf.download(
            ticker, period="1y", interval="1d",
            auto_adjust=True, progress=False,
        )
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df.dropna(subset=["Close"], inplace=True)
        if len(df) < 52:   # 50 EMA + 2 days buffer
            return fail_open

        # ── Daily 50 EMA trend filter ─────────────────────────────────────
        ema50       = float(ta_lib.trend.EMAIndicator(df["Close"], window=50).ema_indicator().iloc[-1])
        daily_close = float(df["Close"].iloc[-1])
        above_ema50 = daily_close > ema50

        trend_label = "BULLISH" if above_ema50 else "BEARISH"
        print(f"   Daily trend: {trend_label} "
              f"(close ${daily_close:.2f} vs 50 EMA ${ema50:.2f})")

        # ── Previous day High / Low ───────────────────────────────────────
        # iloc[-2] is always the previous completed trading day
        # Correct on Monday (gives Friday), after holidays, any day
        prev_day  = df.iloc[-2]
        prev_high = float(prev_day["High"])
        prev_low  = float(prev_day["Low"])
        prev_date = df.index[-2].date()
        print(f"   Previous Day: {prev_date} | "
              f"High ${prev_high:.2f} | Low ${prev_low:.2f}")

        return above_ema50, ema50, daily_close, prev_high, prev_low

    except Exception as e:
        print(f"   Daily data fetch failed: {e} — failing open")
        return fail_open


# =============================================================================
#  SECTION 3 - INDICATOR CALCULATION
# =============================================================================

def calculate_indicators(df):
    """
    Adds all indicators needed for signal detection.
    VWAP, RSI, EMA 9/21, ATR, volume ratio.
    Returns enriched DataFrame.
    """
    df = df.copy()

    # RSI and EMAs
    df["RSI"]    = ta_lib.momentum.RSIIndicator(df["Close"], window=14).rsi()
    df["EMA_9"]  = ta_lib.trend.EMAIndicator(df["Close"], window=9).ema_indicator()
    df["EMA_21"] = ta_lib.trend.EMAIndicator(df["Close"], window=21).ema_indicator()
    df["ATR"]    = ta_lib.volatility.AverageTrueRange(df["High"], df["Low"], df["Close"], window=14).average_true_range()

    # VWAP — manual calculation (ta package doesn't include VWAP)
    # Resets each trading day using DatetimeIndex date grouping
    try:
        df["_TP"]       = (df["High"] + df["Low"] + df["Close"]) / 3
        df["_TPVOL"]    = df["_TP"] * df["Volume"]
        df["_CUMTPVOL"] = df.groupby(df.index.date)["_TPVOL"].cumsum()
        df["_CUMVOL"]   = df.groupby(df.index.date)["Volume"].cumsum()
        df["VWAP"]      = df["_CUMTPVOL"] / df["_CUMVOL"]
        df.drop(columns=["_TP", "_TPVOL", "_CUMTPVOL", "_CUMVOL"], inplace=True)
    except Exception:
        pass

    # MACD on 15-min bars — momentum confirmation for VWAP reclaim
    try:
        _macd_intra = ta_lib.trend.MACD(df["Close"])
        df["MACD_H"] = _macd_intra.macd_diff()
    except Exception:
        pass

    # Volume ratio — current bar vs 10-bar rolling average
    df["VOL_AVG"]   = df["Volume"].rolling(10).mean()
    df["VOL_RATIO"] = df["Volume"] / df["VOL_AVG"]

    df.dropna(subset=["RSI", "EMA_9", "EMA_21", "VWAP"], inplace=True)
    return df


def get_opening_range(df):
    """
    Returns (or_high, or_low) for today's opening range (9:30-10:00am ET).
    Returns (None, None) if not enough bars exist yet.
    """
    et_tz = pytz.timezone(TIMEZONE)
    today = datetime.now(et_tz).date()

    # Filter to today's bars only
    today_bars = df[df.index.date == today]
    if today_bars.empty:
        return None, None

    # Opening range = 9:30-10:00am = first two 15-min bars
    or_bars = today_bars.between_time("09:30", "09:59")
    if len(or_bars) < 2:
        return None, None

    or_high = float(or_bars["High"].max())
    or_low  = float(or_bars["Low"].min())
    return or_high, or_low


# =============================================================================
#  SECTION 4 - SIGNAL ENGINE
# =============================================================================

def fetch_prev_day_high(df):
    """
    Returns previous trading day's high and low from the intraday DataFrame.
    Uses the day before today's date — handles weekends and holidays by
    taking the most recent prior trading day available in the data.

    Returns (prev_high, prev_low) or (None, None) if unavailable.

    Why previous day's high matters:
      Yesterday's high is where sellers defended the price last session.
      When today's price breaks above it with conviction, those sellers
      have been overpowered — prior resistance becomes new support.
      Institutions watch this level closely and often add to positions
      on a confirmed PDH breakout.
    """
    try:
        et_tz = pytz.timezone(TIMEZONE)
        today = datetime.now(et_tz).date()

        # Get all bars from before today
        prev_bars = df[df.index.date < today]
        if prev_bars.empty:
            return None, None

        # Group by date and get the most recent prior trading day
        prev_day = prev_bars.index.date.max()
        prev_day_bars = prev_bars[prev_bars.index.date == prev_day]

        if prev_day_bars.empty:
            return None, None

        prev_high = float(prev_day_bars["High"].max())
        prev_low  = float(prev_day_bars["Low"].min())

        print(f"   Previous Day: {prev_day} | "
              f"High ${prev_high:.2f} | Low ${prev_low:.2f}")
        return prev_high, prev_low

    except Exception as e:
        print(f"   PDH fetch failed: {e}")
        return None, None


def pdh_already_fired_today():
    """Returns True if a PDH signal already fired today."""
    trades = load_trade_log()
    et_tz  = pytz.timezone(TIMEZONE)
    today  = datetime.now(et_tz).strftime("%Y-%m-%d")
    return any(
        t.get("signal_type") in ("PDH", "PDH_ORB")
        and t.get("alert_date") == today
        for t in trades
    )


def check_pdh_signal(df, or_high, prev_high):
    """
    Previous Day High Breakout signal.

    Fires when a bar closes ABOVE yesterday's high with volume and
    momentum confirmation. Only fires once per day.

    If the same bar also satisfies ORB conditions (close > or_high),
    the signal is labelled PDH+ORB Confluence — the strongest
    possible intraday momentum setup.

    Stop anchored BELOW the previous day high (now support).
    Target: +4% from entry (same as ORB).

    Returns signal dict or None.
    """
    if prev_high is None:
        return None

    et_tz   = pytz.timezone(TIMEZONE)
    et_now  = datetime.now(et_tz)

    # Only valid after 10:00am — opening chaos must settle first
    if et_now.hour < 10:
        print("   PDH: Before 10:00am — skipping")
        return None

    if pdh_already_fired_today():
        print("   PDH: Already fired today — skipping")
        return None

    scored   = df.iloc[-1]
    bar_time = df.index[-1]

    bar_close  = float(scored["Close"])
    bar_open   = float(scored["Open"])
    bar_high   = float(scored["High"])
    vol_ratio  = float(scored.get("VOL_RATIO", 0))
    rsi        = float(scored.get("RSI", 50))
    vwap       = float(scored.get("VWAP", bar_close))
    ema_9      = float(scored.get("EMA_9", bar_close))
    ema_21     = float(scored.get("EMA_21", bar_close))
    atr        = float(scored.get("ATR", 0))

    # Core PDH conditions
    broke_pdh       = bar_close > prev_high      # closed above prev day high
    green_bar       = bar_close > bar_open        # green close
    volume_ok       = vol_ratio >= VOLUME_MULT    # elevated volume
    above_vwap      = bar_close > vwap            # above institutional anchor
    rsi_ok          = ORB_RSI_MIN <= rsi <= ORB_RSI_MAX  # momentum zone 45-65
    strong_body     = (bar_close - bar_open) / bar_open >= PDH_BODY_MIN_PCT

    print(f"   PDH check: close=${bar_close:.2f} PDH=${prev_high:.2f} "
          f"broke={broke_pdh} vol={vol_ratio:.1f}x RSI={rsi:.1f} "
          f"body={(bar_close-bar_open)/bar_open*100:.2f}%")

    if not (broke_pdh and green_bar and volume_ok and
            above_vwap and rsi_ok and strong_body):
        if broke_pdh and green_bar and volume_ok:
            if not rsi_ok:
                print(f"   PDH filtered: RSI {rsi:.1f} outside {ORB_RSI_MIN}-{ORB_RSI_MAX}")
            if not strong_body:
                print(f"   PDH filtered: body too small")
            if not above_vwap:
                print(f"   PDH filtered: below VWAP ${vwap:.2f}")
        return None

    # Check for PDH + ORB confluence
    # Both conditions met on same bar = strongest possible signal
    also_orb     = (or_high is not None and bar_close > or_high
                    and not orb_already_fired_today())
    signal_type  = "PDH_ORB" if also_orb else "PDH"
    signal_label = ("PDH + ORB Confluence" if also_orb
                    else "Previous Day High Breakout")

    # Stop anchored just below previous day high (now support)
    # PDH becomes support once broken — tight stop below it
    stop_loss    = prev_high * (1 - STOP_PCT)     # 2% below PDH
    take_profit  = bar_close * (1 + TARGET_PCT)   # 4% above entry
    rr           = TARGET_PCT / STOP_PCT           # 2:1

    reasons = [
        f"PDH Breakout — closed ${bar_close:.2f} above prev high ${prev_high:.2f}",
        f"Volume: {vol_ratio:.1f}x average — institutional participation",
        f"Above VWAP ${vwap:.2f} — trend confirmed",
        f"RSI: {rsi:.1f} — momentum zone",
    ]
    if also_orb:
        reasons.append(
            f"ORB Confluence — also above OR High ${or_high:.2f} (dual breakout)"
        )

    print(f"   {signal_type} SIGNAL FIRED — "
          f"bar {bar_time.strftime('%I:%M %p ET')} "
          f"({'CONFLUENCE' if also_orb else 'PDH only'})")

    return {
        "signal_type":   signal_type,
        "signal_label":  signal_label,
        "bar_time":      bar_time.strftime("%I:%M %p ET"),
        "bar_timestamp": str(bar_time),
        "price":         round(bar_close, 2),
        "prev_high":     round(prev_high, 2),
        "or_high":       round(or_high, 2) if or_high else None,
        "stop_loss":     round(stop_loss, 2),
        "take_profit":   round(take_profit, 2),
        "rsi":           round(rsi, 1),
        "ema_9":         round(ema_9, 2),
        "ema_21":        round(ema_21, 2),
        "vwap":          round(vwap, 2),
        "atr":           round(atr, 4),
        "vol_ratio":     round(vol_ratio, 2),
        "rr_ratio":      round(rr, 2),
        "reasons":       reasons,
    }


def check_orb_signal(df, or_high, or_low):
    """
    Opening Range Breakout signal.
    Fires when a bar closes ABOVE the opening range high with
    volume at least VOLUME_MULT times the 10-bar average.

    Only valid after 10:00am (opening range must be complete).
    Only fires once per day (checked via trade log).

    Returns signal dict or None.
    """
    if or_high is None or or_low is None:
        return None

    et_tz   = pytz.timezone(TIMEZONE)
    et_now  = datetime.now(et_tz)

    # Must be after 10:00am for ORB to be valid
    if et_now.hour < 10:
        print("   ORB: Opening range not yet complete (before 10:00am)")
        return None

    scored   = df.iloc[-1]
    bar_time = df.index[-1]

    bar_close  = float(scored["Close"])
    bar_open   = float(scored["Open"])
    bar_high   = float(scored["High"])
    vol_ratio  = float(scored.get("VOL_RATIO", 0))
    rsi        = float(scored.get("RSI", 50))
    ema_9      = float(scored.get("EMA_9", bar_close))
    ema_21     = float(scored.get("EMA_21", bar_close))
    vwap       = float(scored.get("VWAP", bar_close))
    atr        = float(scored.get("ATR", 0))

    # ORB conditions — original
    broke_or_high  = bar_close > or_high         # closed above OR high
    green_bar      = bar_close > bar_open         # green close
    volume_ok      = vol_ratio >= VOLUME_MULT     # elevated volume
    above_vwap     = bar_close > vwap             # above institutional anchor

    # Gap-fill filters (v1.1)
    # RSI in momentum zone — not too cold (below 45 = not enough momentum)
    # and not too hot (above 65 = already extended, late entry)
    rsi_in_momentum_zone = ORB_RSI_MIN <= rsi <= ORB_RSI_MAX

    # Strong candle body — close must be meaningfully above open
    # Filters out wick-through breakouts where close barely cleared OR high
    candle_body_pct = (bar_close - bar_open) / bar_open
    strong_body     = candle_body_pct >= ORB_BODY_MIN_PCT

    print(f"   ORB check: close=${bar_close:.2f} OR_high=${or_high:.2f} "
          f"broke={broke_or_high} vol={vol_ratio:.1f}x RSI={rsi:.1f} "
          f"body={candle_body_pct*100:.2f}% green={green_bar}")

    if not (broke_or_high and green_bar and volume_ok and
            above_vwap and rsi_in_momentum_zone and strong_body):
        if broke_or_high and green_bar and volume_ok and above_vwap:
            # Main conditions met but new filters blocked — log why
            if not rsi_in_momentum_zone:
                print(f"   ORB filtered: RSI {rsi:.1f} outside {ORB_RSI_MIN}-{ORB_RSI_MAX} zone")
            if not strong_body:
                print(f"   ORB filtered: candle body {candle_body_pct*100:.2f}% < {ORB_BODY_MIN_PCT*100:.1f}% min")
        return None

    # Check ORB hasn't already fired today
    if orb_already_fired_today():
        print("   ORB: Already fired today - skipping")
        return None

    stop_loss   = bar_close * (1 - STOP_PCT)
    take_profit = bar_close * (1 + TARGET_PCT)
    rr          = TARGET_PCT / STOP_PCT  # 4/2 = 2.0

    reasons = [
        f"ORB Breakout — closed ${bar_close:.2f} above OR high ${or_high:.2f}",
        f"Volume: {vol_ratio:.1f}x average — institutional participation",
        f"Above VWAP ${vwap:.2f} — trend confirmed",
        f"RSI: {rsi:.1f} — momentum without being overbought",
    ]

    print(f"   ORB SIGNAL FIRED — bar {bar_time.strftime('%I:%M %p ET')}")
    return {
        "signal_type":  "ORB",
        "signal_label": "Opening Range Breakout",
        "bar_time":     bar_time.strftime("%I:%M %p ET"),
        "bar_timestamp": str(bar_time),
        "price":        round(bar_close, 2),
        "or_high":      round(or_high, 2),
        "or_low":       round(or_low, 2),
        "stop_loss":    round(stop_loss, 2),
        "take_profit":  round(take_profit, 2),
        "rsi":          round(rsi, 1),
        "ema_9":        round(ema_9, 2),
        "ema_21":       round(ema_21, 2),
        "vwap":         round(vwap, 2),
        "atr":          round(atr, 4),
        "vol_ratio":    round(vol_ratio, 2),
        "rr_ratio":     round(rr, 2),
        "reasons":      reasons,
    }


def check_vwap_signal(df):
    """
    VWAP Reclaim signal.
    Fires when:
      1. Previous bar was BELOW VWAP (dip confirmed)
      2. Current bar closes ABOVE VWAP (reclaim confirmed)
      3. Bar is green (close > open)
      4. Volume elevated (institutional participation)
      5. 9 EMA turning up (short-term momentum shifting)
      6. RSI between 35-60 (not overbought, not in freefall)

    Can fire multiple times per day on different bars.
    Returns signal dict or None.
    """
    if len(df) < 3:
        return None

    scored   = df.iloc[-1]
    prev     = df.iloc[-2]
    bar_time = df.index[-1]

    bar_close  = float(scored["Close"])
    bar_open   = float(scored["Open"])
    prev_close = float(prev["Close"])
    vwap       = float(scored.get("VWAP", bar_close))
    prev_vwap  = float(prev.get("VWAP", prev_close))
    vol_ratio  = float(scored.get("VOL_RATIO", 0))
    rsi        = float(scored.get("RSI", 50))
    prev_rsi   = float(prev.get("RSI", 50))
    ema_9      = float(scored.get("EMA_9", bar_close))
    prev_ema9  = float(prev.get("EMA_9", prev_close))
    ema_21     = float(scored.get("EMA_21", bar_close))
    atr        = float(scored.get("ATR", 0))

    # VWAP Reclaim conditions — original
    was_below_vwap  = prev_close < prev_vwap      # prior bar below VWAP
    now_above_vwap  = bar_close > vwap             # current bar reclaimed VWAP
    green_bar       = bar_close > bar_open         # green close
    volume_ok       = vol_ratio >= VOLUME_MULT     # elevated volume
    rsi_ok          = 35 <= rsi <= 60              # healthy RSI range
    ema9_turning_up = ema_9 > prev_ema9            # 9 EMA moving up

    # Gap-fill filters (v1.1)
    # Minimum dip depth: price must have been meaningfully below VWAP
    # Filters out noise where price barely grazed below VWAP
    vwap_dip_pct   = (prev_vwap - prev_close) / prev_vwap if prev_vwap > 0 else 0
    meaningful_dip = vwap_dip_pct >= MIN_VWAP_DIP_PCT

    # RSI was oversold on prior bar — confirms genuine pullback, not drift
    prior_rsi_oversold = prev_rsi < 50

    # MACD histogram turning positive — momentum shift confirmation
    macd_h      = float(scored.get("MACD_H", 0)) if "MACD_H" in scored.index else 0.0
    prev_macd_h = float(prev.get("MACD_H", 0))   if "MACD_H" in prev.index  else 0.0
    macd_turning_up = macd_h > prev_macd_h        # histogram improving

    print(f"   VWAP check: close=${bar_close:.2f} vwap=${vwap:.2f} "
          f"was_below={was_below_vwap} dip={vwap_dip_pct*100:.2f}% "
          f"now_above={now_above_vwap} vol={vol_ratio:.1f}x RSI={rsi:.1f} "
          f"macd_up={macd_turning_up}")

    if not (was_below_vwap and now_above_vwap and green_bar and
            volume_ok and rsi_ok and ema9_turning_up and
            meaningful_dip and prior_rsi_oversold and macd_turning_up):
        if was_below_vwap and now_above_vwap and green_bar and volume_ok:
            # Main conditions met but new filters blocked — log why
            if not meaningful_dip:
                print(f"   VWAP filtered: dip {vwap_dip_pct*100:.2f}% < {MIN_VWAP_DIP_PCT*100:.1f}% min")
            if not prior_rsi_oversold:
                print(f"   VWAP filtered: prior RSI {prev_rsi:.1f} not below 50")
            if not macd_turning_up:
                print(f"   VWAP filtered: MACD not turning up ({prev_macd_h:.3f} → {macd_h:.3f})")
        return None

    stop_loss   = bar_close * (1 - STOP_PCT)
    take_profit = bar_close * (1 + TARGET_PCT)
    rr          = TARGET_PCT / STOP_PCT

    reasons = [
        f"VWAP Reclaim — crossed from ${prev_close:.2f} to ${bar_close:.2f}",
        f"VWAP: ${vwap:.2f} — institutional support confirmed",
        f"Volume: {vol_ratio:.1f}x average — real buyers stepping in",
        f"RSI: {rsi:.1f} (was {prev_rsi:.1f}) — momentum shifting up",
        f"9 EMA turning up: ${prev_ema9:.2f} → ${ema_9:.2f}",
    ]

    print(f"   VWAP SIGNAL FIRED — bar {bar_time.strftime('%I:%M %p ET')}")
    return {
        "signal_type":   "VWAP",
        "signal_label":  "VWAP Reclaim",
        "bar_time":      bar_time.strftime("%I:%M %p ET"),
        "bar_timestamp": str(bar_time),
        "price":         round(bar_close, 2),
        "vwap":          round(vwap, 2),
        "stop_loss":     round(stop_loss, 2),
        "take_profit":   round(take_profit, 2),
        "rsi":           round(rsi, 1),
        "ema_9":         round(ema_9, 2),
        "ema_21":        round(ema_21, 2),
        "atr":           round(atr, 4),
        "vol_ratio":     round(vol_ratio, 2),
        "rr_ratio":      round(rr, 2),
        "reasons":       reasons,
    }


# =============================================================================
#  SECTION 5 - SLIPPAGE GATE
# =============================================================================

def slippage_gate(signal, live_price):
    """
    Checks if live price is still close enough to signal bar close
    that the original R/R ratio is approximately intact.

    Returns (pass, slippage_pct, live_rr)
    """
    signal_price = signal["price"]
    stop         = signal["stop_loss"]
    target       = signal["take_profit"]

    slippage_pct = (live_price - signal_price) / signal_price

    # Price fell too far — momentum reversed
    if slippage_pct < -MAX_SLIPPAGE_PCT:
        print(f"   GATE BLOCKED: price fell {slippage_pct*100:.1f}% from signal — "
              f"momentum reversed")
        return False, slippage_pct, 0

    # Price ran too far — R/R destroyed
    if slippage_pct > MAX_SLIPPAGE_PCT:
        print(f"   GATE BLOCKED: price ran {slippage_pct*100:.1f}% from signal — "
              f"R/R destroyed")
        return False, slippage_pct, 0

    # Calculate live R/R at current price
    live_risk   = live_price - stop
    live_reward = target - live_price
    live_rr     = live_reward / live_risk if live_risk > 0 else 0

    print(f"   GATE PASSED: slippage {slippage_pct*100:+.2f}% "
          f"live R/R {live_rr:.2f}")
    return True, slippage_pct, live_rr


# =============================================================================
#  SECTION 6 - SELL ALERTS
# =============================================================================

def check_intraday_sell_alerts(df, live_price):
    """
    Checks exit conditions on open intraday trades.
    Fires Discord alerts for:
      TARGET_HIT  - price >= take_profit
      STOP_HIT    - price <= stop_loss
      TIME_STOP   - within 10 minutes of 3:35pm
      RSI_OB      - RSI > 68 (overbought)
      EMA_LOSS    - price drops below 9 EMA after being above
    """
    trades      = load_trade_log()
    open_trades = [t for t in trades if t["status"] == "OPEN"]

    if not open_trades:
        print("   No open intraday trades to check.")
        return

    et_tz   = pytz.timezone(TIMEZONE)
    et_now  = datetime.now(et_tz)
    changed = False

    # Time stop check
    time_stop_time = et_now.replace(
        hour=EXIT_HOUR, minute=EXIT_MINUTE, second=0, microsecond=0
    )
    approaching_close = et_now >= time_stop_time

    # Technical indicators from latest bar
    scored   = df.iloc[-1]
    rsi      = float(scored.get("RSI", 50))
    ema_9    = float(scored.get("EMA_9", live_price))
    prev     = df.iloc[-2]
    prev_close = float(prev["Close"])
    prev_ema9  = float(prev.get("EMA_9", prev_close))

    for trade in open_trades:
        entry  = trade["entry"]
        target = trade["take_profit"]
        stop   = trade["stop_loss"]

        current_pnl = (live_price - entry) / entry * 100

        print(f"   Open trade: entry ${entry:.2f} | "
              f"live ${live_price:.2f} | P&L {current_pnl:+.1f}%")

        # TARGET HIT
        if live_price >= target:
            _send_sell_alert(
                reason   = "TARGET_HIT",
                title    = "TARGET REACHED - SELL NOW",
                color    = COLOR_GREEN,
                trade    = trade,
                live_price = live_price,
                pnl      = current_pnl,
                desc     = (
                    f"**SOXL has reached your profit target.**\n\n"
                    f"- Entry: `${entry:.2f}`\n"
                    f"- Target: `${target:.2f}`\n"
                    f"- Live Price: `${live_price:.2f}`\n"
                    f"- **P&L: `+{current_pnl:.1f}%`**\n\n"
                    f"_Sell on Wealthsimple non-reg now._"
                )
            )
            trade["status"]       = "WON"
            trade["outcome_date"] = str(et_now.date())
            trade["outcome_pct"]  = round(current_pnl, 2)
            changed = True

        # STOP HIT
        elif live_price <= stop:
            _send_sell_alert(
                reason   = "STOP_HIT",
                title    = "STOP LOSS HIT - EXIT NOW",
                color    = COLOR_RED,
                trade    = trade,
                live_price = live_price,
                pnl      = current_pnl,
                desc     = (
                    f"**SOXL has hit your stop loss.**\n\n"
                    f"- Entry: `${entry:.2f}`\n"
                    f"- Stop: `${stop:.2f}`\n"
                    f"- Live Price: `${live_price:.2f}`\n"
                    f"- **P&L: `{current_pnl:.1f}%`**\n\n"
                    f"_Exit on Wealthsimple non-reg to limit losses._"
                )
            )
            trade["status"]       = "LOST"
            trade["outcome_date"] = str(et_now.date())
            trade["outcome_pct"]  = round(current_pnl, 2)
            changed = True

        # TIME STOP
        elif approaching_close:
            _send_sell_alert(
                reason   = "TIME_STOP",
                title    = "TIME STOP - EXIT BEFORE CLOSE",
                color    = COLOR_YELLOW,
                trade    = trade,
                live_price = live_price,
                pnl      = current_pnl,
                desc     = (
                    f"**3:35pm ET — Exit before market close.**\n\n"
                    f"- Entry: `${entry:.2f}`\n"
                    f"- Live Price: `${live_price:.2f}`\n"
                    f"- **P&L: `{current_pnl:+.1f}%`**\n\n"
                    f"_Do not hold SOXL overnight. Sell on Wealthsimple now._"
                )
            )
            trade["status"]       = "EXPIRED"
            trade["outcome_date"] = str(et_now.date())
            trade["outcome_pct"]  = round(current_pnl, 2)
            changed = True

        # RSI OVERBOUGHT
        elif rsi > RSI_OVERBOUGHT:
            _send_sell_alert(
                reason   = "RSI_OB",
                title    = "RSI OVERBOUGHT - CONSIDER EXIT",
                color    = COLOR_ORANGE,
                trade    = trade,
                live_price = live_price,
                pnl      = current_pnl,
                desc     = (
                    f"**RSI reached {rsi:.1f} — momentum may be exhausting.**\n\n"
                    f"- Entry: `${entry:.2f}`\n"
                    f"- Live Price: `${live_price:.2f}`\n"
                    f"- P&L: `{current_pnl:+.1f}%`\n"
                    f"- RSI: `{rsi:.1f}` (threshold: {RSI_OVERBOUGHT})\n\n"
                    f"_Early warning — price may be topping. "
                    f"Consider booking profits or tightening stop._"
                )
            )

        # 9 EMA LOSS
        elif prev_close > prev_ema9 and live_price < ema_9:
            drop_pct = (prev_close - live_price) / prev_close * 100
            _send_sell_alert(
                reason   = "EMA_LOSS",
                title    = "9 EMA LOST - MOMENTUM FADING",
                color    = COLOR_YELLOW,
                trade    = trade,
                live_price = live_price,
                pnl      = current_pnl,
                desc     = (
                    f"**Price dropped below 9 EMA — short-term momentum broken.**\n\n"
                    f"- Entry: `${entry:.2f}`\n"
                    f"- Live Price: `${live_price:.2f}` "
                    f"(below 9 EMA `${ema_9:.2f}`)\n"
                    f"- Drop: `{drop_pct:.1f}%` from prior bar\n"
                    f"- P&L: `{current_pnl:+.1f}%`\n\n"
                    f"_Intraday momentum may be reversing. "
                    f"Watch closely — consider partial exit._"
                )
            )

    if changed:
        save_trade_log(trades)


def _send_sell_alert(reason, title, color, trade, live_price, pnl, desc):
    if not DISCORD_WEBHOOK_URL:
        return
    payload = {
        "content": f"**[SOXL INTRADAY - NON-REG] {title}**",
        "embeds": [{
            "title":       f"{title} | {trade.get('signal_type','?')} Trade",
            "description": desc,
            "color":       color,
            "footer":      {"text": f"SOXL Intraday Bot v1.0 | {reason}"},
        }]
    }
    _post_discord(payload)
    print(f"   Sell alert sent: {reason} @ ${live_price:.2f} P&L {pnl:+.1f}%")


# =============================================================================
#  SECTION 7 - TRADE LOG
# =============================================================================

def load_trade_log():
    try:
        p = Path(TRADE_LOG_FILE)
        if p.exists():
            with open(p) as f:
                return json.load(f)
    except Exception as e:
        print(f"   Trade log load failed: {e}")
    return []


def save_trade_log(trades):
    try:
        with open(TRADE_LOG_FILE, "w") as f:
            json.dump(trades, f, indent=2)
    except Exception as e:
        print(f"   Trade log save failed: {e}")


def log_intraday_trade(signal, live_price, slippage_pct, live_rr):
    """Logs a new intraday trade. No dedup — multiple trades per day allowed."""
    dry_run = globals().get("DRY_RUN", False)
    if dry_run:
        print("   DRY RUN — trade not logged")
        return

    try:
        trades = load_trade_log()
        et_tz  = pytz.timezone(TIMEZONE)
        now    = datetime.now(et_tz)

        # Calculate GitHub execution delay vs scheduled bar close
        # bar_time = when the signal bar closed (e.g. 10:30am)
        # now       = when GitHub actually ran the bot
        # delay_min = how late GitHub was — key for monthly review
        try:
            bar_close_str = signal.get("bar_time", "")
            if bar_close_str and bar_close_str != "live (forming bar)":
                bar_close_dt = datetime.strptime(
                    f"{now.strftime('%Y-%m-%d')} {bar_close_str}",
                    "%Y-%m-%d %I:%M %p ET"
                ).replace(tzinfo=now.tzinfo)
                github_delay_min = round((now - bar_close_dt).total_seconds() / 60, 1)
            else:
                github_delay_min = None
        except Exception:
            github_delay_min = None

        trade = {
            "id":                f"SOXL_INT_{now.strftime('%Y%m%d_%H%M')}",
            "ticker":            TICKER,
            "account":           "Non-Registered",
            "alert_date":        now.strftime("%Y-%m-%d"),
            "alert_time":        now.strftime("%H:%M ET"),
            "github_run_time":   now.strftime("%H:%M:%S ET"),    # exact execution time
            "github_delay_min":  github_delay_min,                # mins after bar close
            "signal_type":       signal.get("signal_type", "?"),
            "signal_label":      signal.get("signal_label", "?"),
            "bar_time":          signal.get("bar_time", "?"),
            "signal_price":      float(signal["price"]),          # bar close price
            "entry":             round(live_price, 2),             # actual entry price
            "slippage_pct":      round(slippage_pct * 100, 2),
            "stop_loss":     float(signal["stop_loss"]),
            "take_profit":   float(signal["take_profit"]),
            "rsi":           float(signal.get("rsi", 0)),
            "vol_ratio":     float(signal.get("vol_ratio", 0)),
            "vwap":          float(signal.get("vwap", 0)),
            "rr_ratio":      round(live_rr, 2),
            "reasons":       [str(r) for r in signal.get("reasons", [])],
            "status":        "OPEN",
            "outcome_date":  None,
            "outcome_pct":   None,
            "max_price":     round(live_price, 2),
        }
        trades.append(trade)
        save_trade_log(trades)
        print(f"   Trade logged: {trade['id']}")
    except Exception as e:
        print(f"   Trade log error: {e}")


def orb_already_fired_today():
    """Returns True if an ORB signal already fired today."""
    trades = load_trade_log()
    et_tz  = pytz.timezone(TIMEZONE)
    today  = datetime.now(et_tz).strftime("%Y-%m-%d")
    return any(
        t.get("signal_type") == "ORB" and t.get("alert_date") == today
        for t in trades
    )


def send_outcome_summary():
    """Sends daily summary of intraday trade outcomes to Discord."""
    if not DISCORD_WEBHOOK_URL:
        return
    try:
        trades   = load_trade_log()
        et_tz    = pytz.timezone(TIMEZONE)
        today    = datetime.now(et_tz).strftime("%Y-%m-%d")

        # Today's trades
        today_trades = [t for t in trades if t.get("alert_date") == today]
        open_trades  = [t for t in today_trades if t["status"] == "OPEN"]
        won          = [t for t in trades if t["status"] == "WON"]
        lost         = [t for t in trades if t["status"] == "LOST"]
        expired      = [t for t in trades if t["status"] == "EXPIRED"]

        total_closed = len(won) + len(lost)
        win_rate     = (len(won) / total_closed * 100) if total_closed > 0 else 0
        avg_win      = (sum(t["outcome_pct"] for t in won) / len(won)) if won else 0
        avg_loss     = (sum(t["outcome_pct"] for t in lost) / len(lost)) if lost else 0

        desc  = f"**Overall WR:** `{win_rate:.0f}%` "
        desc += f"({len(won)}W / {len(lost)}L / {len(expired)} expired)\n"
        desc += f"**Avg Win:** `+{avg_win:.1f}%` | **Avg Loss:** `{avg_loss:.1f}%`\n"

        if open_trades:
            desc += f"\n**Open Today ({len(open_trades)})**\n"
            for t in open_trades:
                desc += (f"- {t['signal_type']} @ ${t['entry']:.2f} | "
                         f"Target ${t['take_profit']:.2f} | "
                         f"Stop ${t['stop_loss']:.2f}\n")

        if today_trades:
            desc += f"\n**Today's Trades ({len(today_trades)})**\n"
            for t in today_trades:
                status_icon = ("✅" if t["status"] == "WON" else
                               "🛑" if t["status"] == "LOST" else
                               "⏰" if t["status"] == "EXPIRED" else "📂")
                pnl_str = (f"`{t['outcome_pct']:+.1f}%`"
                           if t.get("outcome_pct") is not None else "open")
                desc += (f"- {status_icon} {t['signal_type']} "
                         f"@ ${t['entry']:.2f} → {pnl_str}\n")

        payload = {"embeds": [{
            "title":       "SOXL Intraday Summary",
            "description": desc[:4096],
            "color":       COLOR_GREEN if win_rate >= 50 else COLOR_RED,
            "footer":      {"text": f"SOXL Intraday Bot v1.0 | Non-Reg Account"},
        }]}
        _post_discord(payload)
    except Exception as e:
        print(f"   Outcome summary error: {e}")


# =============================================================================
#  SECTION 8 - DISCORD
# =============================================================================

def _post_discord(payload):
    if not DISCORD_WEBHOOK_URL:
        print("   DISCORD_URL not set - skipping.")
        return
    try:
        r = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=10)
        if not r.ok:
            print(f"   Discord error: {r.status_code} {r.text[:200]}")
        time.sleep(0.5)
    except Exception as e:
        print(f"   Discord error: {e}")


def send_buy_alert(signal, live_price, slippage_pct, live_rr, gap_pct=None):
    """Sends intraday buy signal embed to Discord."""
    if not DISCORD_WEBHOOK_URL:
        return

    et_tz    = pytz.timezone(TIMEZONE)
    et_now   = datetime.now(et_tz)
    sig_type = signal.get("signal_type", "?")
    sig_label= signal.get("signal_label", "?")
    bar_time = signal.get("bar_time", "?")

    signal_price = signal["price"]
    stop         = signal["stop_loss"]
    target       = signal["take_profit"]

    stop_pct_from_live = (live_price - stop)   / live_price * 100
    tgt_pct_from_live  = (target - live_price) / live_price * 100

    dollar_amount = round(PORTFOLIO_VALUE * POSITION_PCT / 100, 2)

    slippage_warning = ""
    if abs(slippage_pct) > 0.005:
        slip_label = "above" if slippage_pct > 0 else "below"
        slippage_warning = (f"\n⚠️ Price is {slippage_pct*100:+.1f}% {slip_label} "
                           f"signal bar close — verify before entering.")

    rr_label = ("✅ Intact" if live_rr >= 1.8 else
                "⚠️ Reduced" if live_rr >= 1.2 else
                "🔴 Degraded")

    desc  = f"*{et_now.strftime('%I:%M %p ET')} | Non-Registered Account*\n\n"
    desc += f"**Signal Bar:** {bar_time}\n"
    desc += f"**Signal Price:** `${signal_price:.2f}` (bar close)\n\n"
    desc += f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
    desc += f"**INTRADAY MOMENTUM — {sig_label.upper()}**\n"
    desc += f"Position: **${dollar_amount:.0f}** ({POSITION_PCT}% of baseline)\n"
    desc += f"Exit: **+{TARGET_PCT*100:.0f}% target** or **3:35pm time stop**\n"
    desc += f"━━━━━━━━━━━━━━━━━━━━━━━━\n\n"

    # Gap context — informational only, not a filter
    # Shows opening gap so you can check news before acting
    if gap_pct is not None:
        if gap_pct <= -3.0:
            desc += (f"\n⚠️ **Gap down {gap_pct:.1f}%** from yesterday's close\n"
                     f"Check semiconductor news before entering.\n\n")
        elif gap_pct <= -1.5:
            desc += (f"\n📉 Gap down {gap_pct:.1f}% — normal SOXL volatility\n"
                     f"Verify no major sector news.\n\n")
        elif gap_pct >= 3.0:
            desc += (f"\n📈 **Gap up {gap_pct:.1f}%** — strong overnight momentum\n\n")
        elif gap_pct >= 1.5:
            desc += f"\n📈 Gap up {gap_pct:.1f}% — bullish overnight\n\n"
        else:
            desc += f"\n↔️ Flat open ({gap_pct:+.1f}% from yesterday)\n\n"

    desc += "**Trade Plan (at current price)**\n"
    desc += f"- Entry now: `${live_price:.2f}`\n"
    desc += f"- Target:    `${target:.2f}` (+{tgt_pct_from_live:.1f}%)\n"
    desc += f"- Stop:      `${stop:.2f}` (-{stop_pct_from_live:.1f}%)\n"
    desc += f"- R/R:       `1:{live_rr:.2f}` {rr_label}\n"
    desc += slippage_warning
    desc += "\n\n**Why This Signal**\n"
    for r in signal.get("reasons", []):
        desc += f"- {r}\n"

    if sig_type == "ORB":
        desc += f"\n- OR High: `${signal.get('or_high', 0):.2f}` | OR Low: `${signal.get('or_low', 0):.2f}`\n"
    elif sig_type in ("PDH", "PDH_ORB"):
        desc += f"\n- Prev Day High: `${signal.get('prev_high', 0):.2f}` (now support)\n"
        if sig_type == "PDH_ORB":
            desc += f"- OR High: `${signal.get('or_high', 0):.2f}` (dual breakout — high conviction)\n"

    desc += f"\n- VWAP: `${signal.get('vwap', 0):.2f}`\n"
    desc += f"- 9 EMA: `${signal.get('ema_9', 0):.2f}` | 21 EMA: `${signal.get('ema_21', 0):.2f}`\n"
    desc += f"- Volume: `{signal.get('vol_ratio', 0):.1f}x` average\n"
    desc += f"\n[TradingView](https://www.tradingview.com/chart/?symbol=SOXL)"

    if len(desc) > 4096:
        desc = desc[:4050] + "\n...(trimmed)"

    payload = {
        "content": (f"**[SOXL INTRADAY - NON-REG] {sig_label} | "
                    f"Path {sig_type} | ${live_price:.2f}**"),
        "embeds": [{
            "title":  f"INTRADAY SIGNAL — SOXL {sig_label} | {bar_time}",
            "description": desc,
            "color":  COLOR_BLUE,
            "footer": {"text": f"SOXL Intraday Bot v1.0 | Non-Reg | {sig_type}"},
        }]
    }
    _post_discord(payload)
    print(f"   Buy alert sent: {sig_type} @ ${live_price:.2f}")


# =============================================================================
#  SECTION 9 - MAIN EXECUTION LOOP
# =============================================================================

def check_market():
    et_tz   = pytz.timezone(TIMEZONE)
    et_now  = datetime.now(et_tz)

    print(f"\n{'='*60}")
    print(f"  SOXL Intraday Bot v1.2 — "
          f"{et_now.strftime('%A %b %d %Y %I:%M %p ET')}")
    print(f"  Account: Non-Registered")
    print(f"{'='*60}\n")

    force = globals().get("FORCE_RUN", False)

    # Market hours gate: 9:30am - 3:45pm ET
    mkt_open  = et_now.replace(hour=9,  minute=30, second=0, microsecond=0)
    mkt_close = et_now.replace(hour=15, minute=45, second=0, microsecond=0)
    in_market = mkt_open <= et_now <= mkt_close

    if not in_market and not force:
        print("Market closed — nothing to do.")
        return

    # Fetch intraday data
    print("Downloading SOXL 15-min bars...")
    df_raw = fetch_intraday(TICKER, days=7)
    if df_raw is None or df_raw.empty:
        print("No intraday data available.")
        return

    # Calculate indicators
    df = calculate_indicators(df_raw)
    if df is None or df.empty:
        print("Indicator calculation failed.")
        return

    # Get opening range
    or_high, or_low = get_opening_range(df)
    if or_high:
        print(f"   Opening Range: ${or_low:.2f} - ${or_high:.2f}")


    # Fetch live price
    live_price = fetch_live_price()
    if live_price is None:
        print("   Could not fetch live price.")
        return
    print(f"   SOXL live price: ${live_price:.2f}")

    # ── SELL ALERTS AND OUTCOME CHECK ────────────────────────────────────────
    print("\nChecking open positions and sell conditions...")
    check_intraday_sell_alerts(df, live_price)

    # ── TIME STOP GATE ────────────────────────────────────────────────────────
    # After 3:35pm — only sell alerts run, no new buy signals
    time_stop = et_now.replace(hour=EXIT_HOUR, minute=EXIT_MINUTE,
                                second=0, microsecond=0)
    if et_now >= time_stop:
        print(f"\nPast 3:35pm — no new buy signals. Sell alerts only.")
        send_outcome_summary()
        return

    # ── DAILY DATA (trend filter + previous day high) ───────────────────────
    # Single API call returns both the 50 EMA trend filter and PDH/PDL.
    # No separate fetch needed — one call covers everything from daily bars.
    print("\nFetching daily data (trend + PDH)...")
    daily_trend_ok, daily_ema50, daily_close, prev_high, prev_low = fetch_daily_data()
    if not daily_trend_ok:
        print(f"   DAILY TREND BEARISH — no intraday momentum signals today.")
        print(f"   (SOXL ${daily_close:.2f} below daily 50 EMA ${daily_ema50:.2f})")
        print(f"   Intraday momentum against daily downtrend has poor win rate.")
        return

    # ── BUY SIGNAL SCAN ───────────────────────────────────────────────────────
    print("\nScanning for momentum signals...")

    signal = None

    # Priority order:
    # 1. PDH check first — if PDH fires and ORB also fires, labelled as confluence
    #    PDH is checked first so confluence detection works correctly
    # 2. ORB — if PDH didn't fire, check pure ORB breakout
    # 3. VWAP Reclaim — catches intraday dip recoveries ORB/PDH miss

    if prev_high is not None:
        signal = check_pdh_signal(df, or_high, prev_high)

    if signal is None:
        # Pure ORB (no PDH confluence detected)
        signal = check_orb_signal(df, or_high, or_low)

    if signal is None:
        signal = check_vwap_signal(df)

    if signal is None:
        print(f"   No momentum setups found. | {et_now.strftime('%I:%M %p ET')}")
        return

    # ── SLIPPAGE GATE ─────────────────────────────────────────────────────────
    print(f"\nRunning slippage gate (live ${live_price:.2f} vs "
          f"signal ${signal['price']:.2f})...")
    gate_pass, slippage_pct, live_rr = slippage_gate(signal, live_price)

    if not gate_pass:
        print(f"   Signal valid but entry price degraded — no alert sent.")
        return

    # ── GAP CONTEXT ──────────────────────────────────────────────────────────
    # Calculate today's opening gap from yesterday's close (daily bars).
    # Passed into the alert as informational context — not a filter.
    # You decide whether to act based on the gap and any news you're aware of.
    gap_pct = None
    try:
        et_tz_g    = pytz.timezone(TIMEZONE)
        today_g    = datetime.now(et_tz_g).date()
        today_bars = df[df.index.date == today_g]
        if not today_bars.empty and daily_close is not None:
            today_open = float(today_bars["Open"].iloc[0])
            gap_pct    = (today_open - daily_close) / daily_close * 100
    except Exception:
        pass

    # ── SEND ALERT AND LOG ────────────────────────────────────────────────────
    send_buy_alert(signal, live_price, slippage_pct, live_rr, gap_pct)
    log_intraday_trade(signal, live_price, slippage_pct, live_rr)

    print(f"\nScan complete | {et_now.strftime('%I:%M %p ET')}\n")


# =============================================================================
#  ENTRY POINT
# =============================================================================

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="SOXL Intraday Momentum Bot v1.0")
    parser.add_argument("--force",   action="store_true",
                        help="Bypass market hours gate (testing)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Skip trade log writes (testing)")
    args = parser.parse_args()

    DRY_RUN   = args.dry_run
    FORCE_RUN = args.force

    if DRY_RUN:
        print("DRY RUN — trade log writes disabled")
    if FORCE_RUN:
        print("--force active — market hours gate bypassed")

    check_market()
