"""
=============================================================================
  SOXL INTRADAY BACKTEST — 15-Min Signal Replay
=============================================================================

STANDALONE script — no dependency on soxl_intraday_bot.py.
Implements the same ORB / VWAP Reclaim / PDH signal logic from
soxl_intraday_bot.py v1.2 directly, using 60 days of 15-min bar history.

PURPOSE:
  Validate whether the three intraday momentum signals produce positive
  expectancy on SOXL 15-min bars before deploying capital.
  Key questions:
    1. Do signals fire at all? (Is 1.5x volume threshold too strict?)
    2. What is the win rate per signal type?
    3. Which filter is blocking the most valid setups?
    4. What volume threshold actually produces the best expectancy?

HOW IT WORKS:
  For each trading day in the dataset:
    1. Calculate VWAP, RSI, EMA, ATR, Volume ratio for all bars
    2. Simulate bot running every 15 min from 10:00am to 3:20pm
    3. Score each bar against ORB / PDH / VWAP conditions (priority order)
    4. If signal fires, walk forward to find WON/LOST/TIME_STOP outcome
    5. Each day allows only ONE trade (same as live bot)

NO LOOKAHEAD BIAS:
  At each simulated run time, only bars up to and including the current
  bar are used for indicator calculation — no future data visible.

OUTPUT:
  Console: summary per signal type + overall stats
  soxl_intraday_backtest_trades.csv   — every trade with full details
  soxl_intraday_backtest_report.txt   — full text report

USAGE:
  python soxl_intraday_backtest.py                    # default 60 days
  python soxl_intraday_backtest.py --days 30          # last 30 days
  python soxl_intraday_backtest.py --sensitivity      # volume threshold sweep
  python soxl_intraday_backtest.py --verbose          # show every bar check
  python soxl_intraday_backtest.py --vol-mult 1.0     # test looser volume gate
  python soxl_intraday_backtest.py --no-trend-filter  # disable 50 EMA filter
  python soxl_intraday_backtest.py --rsi-min 40       # wider RSI floor
  python soxl_intraday_backtest.py --rsi-max 70       # wider RSI ceiling

=============================================================================
"""

import argparse
import warnings
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytz
import ta as ta_lib
import yfinance as yf

warnings.filterwarnings("ignore")

# =============================================================================
#  SECTION 1 - CONFIGURATION (mirrors soxl_intraday_bot.py exactly)
# =============================================================================

TICKER    = "SOXL"
TIMEZONE  = "US/Eastern"

# Signal parameters — identical to live bot
VOLUME_MULT      = 1.5     # volume threshold (tunable via --vol-mult)
ORB_RSI_MIN      = 45      # RSI floor for ORB/PDH
ORB_RSI_MAX      = 65      # RSI ceiling for ORB/PDH
ORB_BODY_MIN_PCT = 0.003   # minimum candle body for ORB/PDH
MIN_VWAP_DIP_PCT = 0.005   # minimum dip below VWAP before reclaim
DAILY_TREND_FILTER = True  # require daily 50 EMA uptrend

# Exit parameters (overridable via --target and --stop flags)
TARGET_PCT   = 0.04   # +4% profit target
STOP_PCT     = 0.02   # -2% stop loss
EXIT_HOUR    = 15     # 3pm ET time stop hour
EXIT_MINUTE  = 35     # 3:35pm ET time stop minute

# Backtest settings
ORB_MAX_HOURS         = None  # None = no limit; 2.0 = until noon
VWAP_MAX_HOURS        = None  # None = no limit; 6.5 = until 4pm; 3.5 = until 1pm
ORB_WINDOW_END_HOUR   = 10  # ORB window ends at 10:00am ET
ORB_WINDOW_END_MINUTE = 0
SESSION_START_HOUR    = 9
SESSION_START_MINUTE  = 30
SESSION_END_HOUR      = 15
SESSION_END_MINUTE    = 45

# =============================================================================
#  SECTION 2 - DATA DOWNLOAD
# =============================================================================

def download_data(days=60):
    """
    Downloads 15-min SOXL bars and 1-year daily bars.
    Returns (df_intraday, df_daily) or (None, None) on failure.
    """
    et_tz = pytz.timezone(TIMEZONE)
    print(f"Downloading {days} days of SOXL 15-min bars...")

    try:
        df = yf.download(
            TICKER,
            period=f"{days}d",
            interval="15m",
            auto_adjust=True,
            progress=False,
        )
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df.dropna(subset=["Close"], inplace=True)

        if df.index.tz is None:
            df.index = df.index.tz_localize("UTC").tz_convert(et_tz)
        else:
            df.index = df.index.tz_convert(et_tz)

        print(f"   Downloaded {len(df)} bars across {df.index.date[-1] - df.index.date[0]} days")

    except Exception as e:
        print(f"Intraday download failed: {e}")
        return None, None

    print("Downloading 1-year SOXL daily bars (for trend filter)...")
    try:
        df_daily = yf.download(
            TICKER,
            period="1y",
            interval="1d",
            auto_adjust=True,
            progress=False,
        )
        if isinstance(df_daily.columns, pd.MultiIndex):
            df_daily.columns = df_daily.columns.get_level_values(0)
        df_daily.dropna(subset=["Close"], inplace=True)
        df_daily.index = pd.to_datetime(df_daily.index).tz_localize(None)
        print(f"   Downloaded {len(df_daily)} daily bars")
    except Exception as e:
        print(f"Daily download failed: {e}")
        df_daily = None

    return df, df_daily


# =============================================================================
#  SECTION 3 - INDICATOR CALCULATION
# =============================================================================

def calculate_indicators(df):
    """
    Adds RSI, EMA 9/21, ATR, MACD, VWAP, Volume ratio.
    Identical to live bot's calculate_indicators().
    """
    df = df.copy()

    df["RSI"]    = ta_lib.momentum.RSIIndicator(df["Close"], window=14).rsi()
    df["EMA_9"]  = ta_lib.trend.EMAIndicator(df["Close"], window=9).ema_indicator()
    df["EMA_21"] = ta_lib.trend.EMAIndicator(df["Close"], window=21).ema_indicator()
    df["ATR"]    = ta_lib.volatility.AverageTrueRange(
        df["High"], df["Low"], df["Close"], window=14
    ).average_true_range()

    # VWAP — resets daily
    try:
        df["_TP"]       = (df["High"] + df["Low"] + df["Close"]) / 3
        df["_TPVOL"]    = df["_TP"] * df["Volume"]
        df["_CUMTPVOL"] = df.groupby(df.index.date)["_TPVOL"].cumsum()
        df["_CUMVOL"]   = df.groupby(df.index.date)["Volume"].cumsum()
        df["VWAP"]      = df["_CUMTPVOL"] / df["_CUMVOL"]
        df.drop(columns=["_TP", "_TPVOL", "_CUMTPVOL", "_CUMVOL"], inplace=True)
    except Exception:
        pass

    # MACD histogram
    try:
        _macd = ta_lib.trend.MACD(df["Close"])
        df["MACD_H"] = _macd.macd_diff()
    except Exception:
        pass

    # Volume ratio — 10-bar rolling mean
    df["VOL_AVG"]   = df["Volume"].rolling(10).mean()
    df["VOL_RATIO"] = df["Volume"] / df["VOL_AVG"]

    df.dropna(subset=["RSI", "EMA_9", "EMA_21", "VWAP"], inplace=True)
    return df


def get_daily_trend(df_daily, sim_date):
    """
    Returns (above_ema50, prev_high, prev_low, prev_close) for a given date.
    Uses only data available up to and including sim_date (no lookahead).
    """
    if df_daily is None or not DAILY_TREND_FILTER:
        return True, None, None, None

    try:
        # Data available up to sim_date
        sim_dt = pd.Timestamp(sim_date)
        hist   = df_daily[df_daily.index < sim_dt]
        if len(hist) < 52:
            return True, None, None, None

        ema50       = float(ta_lib.trend.EMAIndicator(hist["Close"], window=50).ema_indicator().iloc[-1])
        daily_close = float(hist["Close"].iloc[-1])
        above_ema50 = daily_close > ema50

        prev_high  = float(hist["High"].iloc[-1])
        prev_low   = float(hist["Low"].iloc[-1])
        prev_close = float(hist["Close"].iloc[-1])

        return above_ema50, prev_high, prev_low, prev_close

    except Exception:
        return True, None, None, None


# =============================================================================
#  SECTION 4 - SIGNAL CHECKERS (mirror live bot conditions exactly)
# =============================================================================

def check_orb_signal(df_slice, or_high, or_low, vol_mult, verbose=False):
    """
    Identical conditions to live bot's check_orb_signal().
    Returns signal dict or None.
    """
    if or_high is None or or_low is None:
        return None

    scored   = df_slice.iloc[-1]
    bar_time = df_slice.index[-1]

    # ORB time gate — signal only valid within ORB_MAX_HOURS after 10:00am
    # Without this, ORB fires on afternoon bars that drifted above OR High
    # hours after the opening momentum is gone (backtested: no edge after noon)
    if ORB_MAX_HOURS is not None:
        orb_window_close = bar_time.replace(
            hour=10, minute=0, second=0, microsecond=0
        ) + pd.Timedelta(hours=ORB_MAX_HOURS)
        if bar_time > orb_window_close:
            return None

    bar_close = float(scored["Close"])
    bar_open  = float(scored["Open"])
    vol_ratio = float(scored.get("VOL_RATIO", 0))
    rsi       = float(scored.get("RSI", 50))
    vwap      = float(scored.get("VWAP", bar_close))
    ema_9     = float(scored.get("EMA_9", bar_close))
    ema_21    = float(scored.get("EMA_21", bar_close))
    atr       = float(scored.get("ATR", 0))

    broke_or_high        = bar_close > or_high
    green_bar            = bar_close > bar_open
    volume_ok            = vol_ratio >= vol_mult
    above_vwap           = bar_close > vwap
    rsi_in_momentum_zone = ORB_RSI_MIN <= rsi <= ORB_RSI_MAX
    candle_body_pct      = (bar_close - bar_open) / bar_open if bar_open > 0 else 0
    strong_body          = candle_body_pct >= ORB_BODY_MIN_PCT

    if verbose:
        print(f"      ORB: close=${bar_close:.2f} OR_high=${or_high:.2f} "
              f"broke={broke_or_high} vol={vol_ratio:.2f}x RSI={rsi:.1f} "
              f"green={green_bar} body={candle_body_pct*100:.2f}%")

    if not (broke_or_high and green_bar and volume_ok and
            above_vwap and rsi_in_momentum_zone and strong_body):

        # Track which conditions failed for sensitivity analysis
        failed = []
        if not broke_or_high:        failed.append("price")
        if not green_bar:            failed.append("green")
        if not volume_ok:            failed.append("volume")
        if not above_vwap:           failed.append("vwap")
        if not rsi_in_momentum_zone: failed.append("rsi")
        if not strong_body:          failed.append("body")
        return None

    stop_loss   = bar_close * (1 - STOP_PCT)
    take_profit = bar_close * (1 + TARGET_PCT)

    return {
        "signal_type": "ORB",
        "bar_time":    bar_time,
        "price":       bar_close,
        "stop_loss":   stop_loss,
        "take_profit": take_profit,
        "rsi":         rsi,
        "vol_ratio":   vol_ratio,
        "vwap":        vwap,
        "ema_9":       ema_9,
        "ema_21":      ema_21,
        "atr":         atr,
        "or_high":     or_high,
    }


def check_pdh_signal(df_slice, or_high, prev_high, vol_mult, verbose=False):
    """
    Identical conditions to live bot's check_pdh_signal().
    Returns signal dict or None.
    """
    if prev_high is None:
        return None

    scored   = df_slice.iloc[-1]
    bar_time = df_slice.index[-1]

    bar_close = float(scored["Close"])
    bar_open  = float(scored["Open"])
    vol_ratio = float(scored.get("VOL_RATIO", 0))
    rsi       = float(scored.get("RSI", 50))
    vwap      = float(scored.get("VWAP", bar_close))
    ema_9     = float(scored.get("EMA_9", bar_close))
    ema_21    = float(scored.get("EMA_21", bar_close))
    atr       = float(scored.get("ATR", 0))

    broke_pdh   = bar_close > prev_high
    green_bar   = bar_close > bar_open
    volume_ok   = vol_ratio >= vol_mult
    above_vwap  = bar_close > vwap
    rsi_ok      = ORB_RSI_MIN <= rsi <= ORB_RSI_MAX
    body_pct    = (bar_close - bar_open) / bar_open if bar_open > 0 else 0
    strong_body = body_pct >= ORB_BODY_MIN_PCT

    if verbose:
        print(f"      PDH: close=${bar_close:.2f} PDH=${prev_high:.2f} "
              f"broke={broke_pdh} vol={vol_ratio:.2f}x RSI={rsi:.1f}")

    if not (broke_pdh and green_bar and volume_ok and
            above_vwap and rsi_ok and strong_body):
        return None

    also_orb    = (or_high is not None and bar_close > or_high)
    signal_type = "PDH_ORB" if also_orb else "PDH"

    stop_loss   = prev_high * (1 - STOP_PCT)
    take_profit = bar_close * (1 + TARGET_PCT)

    return {
        "signal_type": signal_type,
        "bar_time":    bar_time,
        "price":       bar_close,
        "stop_loss":   stop_loss,
        "take_profit": take_profit,
        "rsi":         rsi,
        "vol_ratio":   vol_ratio,
        "vwap":        vwap,
        "ema_9":       ema_9,
        "ema_21":      ema_21,
        "atr":         atr,
        "prev_high":   prev_high,
        "or_high":     or_high,
    }


def check_vwap_signal(df_slice, vol_mult, verbose=False):
    """
    Identical conditions to live bot's check_vwap_signal().
    Returns signal dict or None.
    """
    if len(df_slice) < 3:
        return None

    scored     = df_slice.iloc[-1]
    prev       = df_slice.iloc[-2]
    bar_time   = df_slice.index[-1]

    # VWAP time gate — signal only valid within VWAP_MAX_HOURS after 9:30am open
    # Afternoon VWAP reclaims fire on low-conviction institutional wrap-up
    # not fresh trend initiation — backtest shows higher loss rate after 1pm
    if VWAP_MAX_HOURS is not None:
        session_open = bar_time.replace(
            hour=9, minute=30, second=0, microsecond=0
        )
        vwap_window_close = session_open + pd.Timedelta(hours=VWAP_MAX_HOURS)
        if bar_time > vwap_window_close:
            return None

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

    was_below_vwap  = prev_close < prev_vwap
    now_above_vwap  = bar_close > vwap
    green_bar       = bar_close > bar_open
    volume_ok       = vol_ratio >= vol_mult
    rsi_ok          = 35 <= rsi <= 60
    ema9_turning_up = ema_9 > prev_ema9

    vwap_dip_pct   = (prev_vwap - prev_close) / prev_vwap if prev_vwap > 0 else 0
    meaningful_dip = vwap_dip_pct >= MIN_VWAP_DIP_PCT

    prior_rsi_oversold = prev_rsi < 50

    macd_h      = float(scored.get("MACD_H", 0)) if "MACD_H" in scored.index else 0.0
    prev_macd_h = float(prev.get("MACD_H", 0))   if "MACD_H" in prev.index  else 0.0
    macd_turning_up = macd_h > prev_macd_h

    if verbose:
        print(f"      VWAP: close=${bar_close:.2f} vwap=${vwap:.2f} "
              f"was_below={was_below_vwap} dip={vwap_dip_pct*100:.2f}% "
              f"vol={vol_ratio:.2f}x RSI={rsi:.1f} macd_up={macd_turning_up}")

    if not (was_below_vwap and now_above_vwap and green_bar and
            volume_ok and rsi_ok and ema9_turning_up and
            meaningful_dip and prior_rsi_oversold and macd_turning_up):
        return None

    stop_loss   = bar_close * (1 - STOP_PCT)
    take_profit = bar_close * (1 + TARGET_PCT)

    return {
        "signal_type": "VWAP",
        "bar_time":    bar_time,
        "price":       bar_close,
        "stop_loss":   stop_loss,
        "take_profit": take_profit,
        "rsi":         rsi,
        "vol_ratio":   vol_ratio,
        "vwap":        vwap,
        "ema_9":       ema_9,
        "ema_21":      ema_21,
        "atr":         atr,
        "vwap_dip_pct": vwap_dip_pct,
    }


# =============================================================================
#  SECTION 5 - TRADE OUTCOME WALKER
# =============================================================================

def walk_forward_outcome(df_today, signal, entry_bar_idx):
    """
    Walks forward from entry bar to find WON/LOST/TIME_STOP outcome.

    At each future bar:
      1. If High >= take_profit → WON
      2. If Low <= stop_loss    → LOST
      3. If bar time >= 3:35pm  → TIME_STOP (use bar close as exit price)
      4. If no more bars        → TIME_STOP at last bar close

    Returns dict with outcome details.
    """
    et_tz       = pytz.timezone(TIMEZONE)
    take_profit = signal["take_profit"]
    stop_loss   = signal["stop_loss"]
    entry_price = signal["price"]

    # Walk forward bars after the signal bar
    future_bars = df_today.iloc[entry_bar_idx + 1:]

    for bar_time, bar in future_bars.iterrows():
        bar_high  = float(bar["High"])
        bar_low   = float(bar["Low"])
        bar_close = float(bar["Close"])

        # Time stop check
        is_time_stop = (
            bar_time.hour > EXIT_HOUR or
            (bar_time.hour == EXIT_HOUR and bar_time.minute >= EXIT_MINUTE)
        )

        # Check target and stop simultaneously
        # If both hit in same bar — conservative: stop wins
        if bar_low <= stop_loss and bar_high >= take_profit:
            outcome_price = stop_loss
            outcome       = "LOST"
        elif bar_high >= take_profit:
            outcome_price = take_profit
            outcome       = "WON"
        elif bar_low <= stop_loss:
            outcome_price = stop_loss
            outcome       = "LOST"
        elif is_time_stop:
            outcome_price = bar_close
            outcome       = "TIME_STOP"
        else:
            continue

        pnl_pct = (outcome_price - entry_price) / entry_price * 100

        return {
            "outcome":       outcome,
            "outcome_price": round(outcome_price, 2),
            "outcome_time":  bar_time.strftime("%I:%M %p ET"),
            "pnl_pct":       round(pnl_pct, 2),
            "bars_held":     list(future_bars.index).index(bar_time) + 1,
        }

    # No outcome found — TIME_STOP at last bar
    if not future_bars.empty:
        last_close = float(future_bars["Close"].iloc[-1])
        last_time  = future_bars.index[-1]
        pnl_pct    = (last_close - entry_price) / entry_price * 100
        return {
            "outcome":       "TIME_STOP",
            "outcome_price": round(last_close, 2),
            "outcome_time":  last_time.strftime("%I:%M %p ET"),
            "pnl_pct":       round(pnl_pct, 2),
            "bars_held":     len(future_bars),
        }

    return {
        "outcome": "NO_DATA", "outcome_price": entry_price,
        "outcome_time": "?", "pnl_pct": 0.0, "bars_held": 0,
    }


# =============================================================================
#  SECTION 6 - MAIN BACKTEST ENGINE
# =============================================================================

def run_backtest(df, df_daily, vol_mult=1.5, verbose=False):
    """
    Simulates the bot running every 15 minutes on each trading day.
    Returns list of trade dicts.
    """
    et_tz  = pytz.timezone(TIMEZONE)
    trades = []

    # Get unique trading days
    trading_days = sorted(set(df.index.date))
    print(f"\nRunning backtest across {len(trading_days)} trading days...")
    print(f"Volume threshold: {vol_mult}x | Target: +{TARGET_PCT*100:.0f}% | "
          f"Stop: -{STOP_PCT*100:.0f}%\n")

    for sim_date in trading_days:
        # Get daily trend and PDH data
        above_ema50, prev_high, prev_low, prev_close = get_daily_trend(
            df_daily, sim_date
        )

        if not above_ema50 and DAILY_TREND_FILTER:
            if verbose:
                print(f"  {sim_date}: DAILY TREND BEARISH — skipping")
            continue

        # Get all bars for this day
        day_bars = df[df.index.date == sim_date].copy()
        if len(day_bars) < 5:
            continue

        # Opening range: 9:30-10:00am ET (first two 15-min bars)
        or_bars = day_bars.between_time("09:30", "09:59")
        or_high = float(or_bars["High"].max())  if len(or_bars) >= 2 else None
        or_low  = float(or_bars["Low"].min())   if len(or_bars) >= 2 else None

        if verbose:
            print(f"\n  {sim_date} | OR: ${or_low:.2f}-${or_high:.2f} | "
                  f"PDH: ${prev_high:.2f}" if prev_high else f"\n  {sim_date}")

        signal_fired_today = False

        # Simulate every bar from 10:00am onwards
        scannable = day_bars[day_bars.index.time >= pd.Timestamp("10:00").time()]

        for i, (bar_time, _) in enumerate(scannable.iterrows()):
            # Skip time stop window — no new entries after 3:20pm
            if bar_time.hour > 15 or (bar_time.hour == 15 and bar_time.minute >= 20):
                break

            if signal_fired_today:
                break

            # Build the slice visible at this simulated run time (no lookahead)
            # Include all bars up to and including current bar
            slice_end = day_bars.index.get_loc(bar_time)
            df_slice  = day_bars.iloc[:slice_end + 1]

            if verbose:
                print(f"    Scanning {bar_time.strftime('%I:%M %p ET')}...")

            signal = None

            # Priority 1: PDH
            if prev_high is not None:
                signal = check_pdh_signal(df_slice, or_high, prev_high, vol_mult, verbose)

            # Priority 2: ORB (if no PDH)
            if signal is None:
                signal = check_orb_signal(df_slice, or_high, or_low, vol_mult, verbose)

            # Priority 3: VWAP
            if signal is None:
                signal = check_vwap_signal(df_slice, vol_mult, verbose)

            if signal is None:
                continue

            # Signal fired — walk forward to find outcome
            entry_bar_idx = day_bars.index.get_loc(bar_time)
            result = walk_forward_outcome(day_bars, signal, entry_bar_idx)

            trade = {
                "date":          str(sim_date),
                "signal_type":   signal["signal_type"],
                "entry_time":    bar_time.strftime("%I:%M %p ET"),
                "entry_price":   round(signal["price"], 2),
                "take_profit":   round(signal["take_profit"], 2),
                "stop_loss":     round(signal["stop_loss"], 2),
                "rsi":           round(signal["rsi"], 1),
                "vol_ratio":     round(signal["vol_ratio"], 2),
                "vwap":          round(signal["vwap"], 2),
                "outcome":       result["outcome"],
                "outcome_price": result["outcome_price"],
                "outcome_time":  result["outcome_time"],
                "pnl_pct":       result["pnl_pct"],
                "bars_held":     result["bars_held"],
                "above_ema50":   above_ema50,
                "or_high":       round(or_high, 2) if or_high else None,
                "prev_high":     round(prev_high, 2) if prev_high else None,
            }
            trades.append(trade)

            outcome_icon = "✅" if result["outcome"] == "WON" else (
                           "🛑" if result["outcome"] == "LOST" else "⏰")
            print(f"  {sim_date} | {signal['signal_type']:7s} @ "
                  f"${signal['price']:.2f} {bar_time.strftime('%I:%M%p')} → "
                  f"{outcome_icon} {result['outcome']:9s} "
                  f"{result['pnl_pct']:+.1f}% "
                  f"({result['outcome_time']})")

            signal_fired_today = True

    return trades


# =============================================================================
#  SECTION 7 - SENSITIVITY ANALYSIS
# =============================================================================

def run_sensitivity(df, df_daily):
    """
    Sweeps volume multiplier from 0.5x to 2.5x to find optimal threshold.
    Shows how win rate, trade count, and expectancy change.
    """
    print("\n" + "="*60)
    print("  VOLUME THRESHOLD SENSITIVITY ANALYSIS")
    print("="*60)
    print(f"  Target: +{TARGET_PCT*100:.1f}%  |  Stop: -{STOP_PCT*100:.1f}%")
    print(f"{'Vol Mult':>10} {'Trades':>8} {'Win Rate':>10} "
          f"{'Avg Win':>9} {'Avg Loss':>10} {'Expectancy':>12}")
    print("-"*60)

    results = []
    for vol_mult in [0.5, 0.75, 1.0, 1.25, 1.5, 1.75, 2.0, 2.5]:
        trades = run_backtest(df, df_daily, vol_mult=vol_mult, verbose=False)

        if not trades:
            print(f"  {vol_mult:>8.2f}x {'0':>8} {'N/A':>10}")
            continue

        won  = [t for t in trades if t["outcome"] == "WON"]
        lost = [t for t in trades if t["outcome"] == "LOST"]
        ts   = [t for t in trades if t["outcome"] == "TIME_STOP"]

        total_closed = len(won) + len(lost)
        win_rate = (len(won) / total_closed * 100) if total_closed > 0 else 0
        avg_win  = (sum(t["pnl_pct"] for t in won)  / len(won))  if won  else 0
        avg_loss = (sum(t["pnl_pct"] for t in lost) / len(lost)) if lost else 0
        expectancy = (win_rate/100 * avg_win) + ((1-win_rate/100) * avg_loss)

        results.append({
            "vol_mult":   vol_mult,
            "trades":     len(trades),
            "win_rate":   win_rate,
            "avg_win":    avg_win,
            "avg_loss":   avg_loss,
            "expectancy": expectancy,
        })

        marker = " ← current" if vol_mult == 1.5 else ""
        print(f"  {vol_mult:>8.2f}x {len(trades):>8} {win_rate:>9.1f}% "
              f"{avg_win:>+8.1f}% {avg_loss:>+9.1f}% "
              f"{expectancy:>+11.2f}%{marker}")

    print("-"*60)
    if results:
        best = max(results, key=lambda x: x["expectancy"])
        print(f"\n  Best expectancy: {best['vol_mult']}x volume "
              f"({best['expectancy']:+.2f}% per trade)")

    return results


# =============================================================================
#  SECTION 8 - REPORTING
# =============================================================================

def print_report(trades, vol_mult=1.5, days=60):
    """Prints full backtest report to console and files."""

    print("\n" + "="*60)
    print("  SOXL INTRADAY BACKTEST REPORT")
    print("="*60)

    if not trades:
        print("\n  No trades generated under current parameters.")
        print(f"  Volume threshold: {vol_mult}x may be too strict.")
        print("  Try: python soxl_intraday_backtest.py --sensitivity")
        return

    df_trades = pd.DataFrame(trades)

    # Overall stats
    won  = [t for t in trades if t["outcome"] == "WON"]
    lost = [t for t in trades if t["outcome"] == "LOST"]
    ts   = [t for t in trades if t["outcome"] == "TIME_STOP"]

    total        = len(trades)
    total_closed = len(won) + len(lost)
    win_rate     = (len(won) / total_closed * 100) if total_closed > 0 else 0
    avg_win      = (sum(t["pnl_pct"] for t in won)  / len(won))  if won  else 0
    avg_loss     = (sum(t["pnl_pct"] for t in lost) / len(lost)) if lost else 0
    avg_ts       = (sum(t["pnl_pct"] for t in ts)   / len(ts))   if ts   else 0
    expectancy   = (win_rate/100 * avg_win) + ((1-win_rate/100) * avg_loss) if total_closed > 0 else 0
    total_pnl    = sum(t["pnl_pct"] for t in trades)

    print(f"\n  Period:     Last {days} trading days")
    print(f"  Vol Mult:   {vol_mult}x")
    print(f"  Target:     +{TARGET_PCT*100:.0f}%  |  Stop: -{STOP_PCT*100:.0f}%")
    print(f"  Trend filter: {'ON (50 EMA)' if DAILY_TREND_FILTER else 'OFF'}")
    print()
    print(f"  Total trades:    {total}")
    print(f"  Won:             {len(won)}  ({win_rate:.1f}%)")
    print(f"  Lost:            {len(lost)}")
    print(f"  Time stopped:    {len(ts)}")
    print()
    print(f"  Avg win:         {avg_win:+.2f}%")
    print(f"  Avg loss:        {avg_loss:+.2f}%")
    print(f"  Avg time stop:   {avg_ts:+.2f}%")
    print(f"  Expectancy:      {expectancy:+.2f}% per trade")
    print(f"  Total P&L:       {total_pnl:+.2f}%")

    # Per signal type breakdown
    print("\n  ── By Signal Type ──────────────────────────────────")
    for sig_type in ["ORB", "PDH", "PDH_ORB", "VWAP"]:
        sig_trades = [t for t in trades if t["signal_type"] == sig_type]
        if not sig_trades:
            continue
        sig_won  = [t for t in sig_trades if t["outcome"] == "WON"]
        sig_lost = [t for t in sig_trades if t["outcome"] == "LOST"]
        sig_total_closed = len(sig_won) + len(sig_lost)
        sig_wr   = (len(sig_won) / sig_total_closed * 100) if sig_total_closed > 0 else 0
        sig_exp  = sum(t["pnl_pct"] for t in sig_trades) / len(sig_trades)
        print(f"  {sig_type:8s}: {len(sig_trades):3d} trades | "
              f"WR {sig_wr:.0f}% | "
              f"Expectancy {sig_exp:+.2f}%")

    # Entry time distribution
    print("\n  ── Entry Time Distribution ─────────────────────────")
    df_trades["entry_hour"] = pd.to_datetime(
        df_trades["entry_time"], format="%I:%M %p ET", errors="coerce"
    ).dt.hour
    for hour in range(10, 16):
        hour_trades = df_trades[df_trades["entry_hour"] == hour]
        if len(hour_trades) == 0:
            continue
        bar = "█" * len(hour_trades)
        print(f"  {hour:02d}:xx  {bar} ({len(hour_trades)})")

    # Vol ratio distribution of signals that fired
    print("\n  ── Volume Ratio at Signal Time ─────────────────────")
    avg_vol = df_trades["vol_ratio"].mean()
    min_vol = df_trades["vol_ratio"].min()
    max_vol = df_trades["vol_ratio"].max()
    print(f"  Average: {avg_vol:.2f}x | Min: {min_vol:.2f}x | Max: {max_vol:.2f}x")
    print(f"  (Threshold was {vol_mult}x — all signals above this)")

    # Save CSV
    csv_path = "soxl_intraday_backtest_trades.csv"
    df_trades.to_csv(csv_path, index=False)
    print(f"\n  ✅ Trades saved to {csv_path}")

    # Save text report
    report_path = "soxl_intraday_backtest_report.txt"
    with open(report_path, "w") as f:
        f.write(f"SOXL INTRADAY BACKTEST REPORT\n")
        f.write(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n")
        f.write(f"Period: Last {days} trading days\n")
        f.write(f"Volume threshold: {vol_mult}x\n\n")
        f.write(f"Total trades:  {total}\n")
        f.write(f"Won:           {len(won)} ({win_rate:.1f}%)\n")
        f.write(f"Lost:          {len(lost)}\n")
        f.write(f"Time stopped:  {len(ts)}\n")
        f.write(f"Avg win:       {avg_win:+.2f}%\n")
        f.write(f"Avg loss:      {avg_loss:+.2f}%\n")
        f.write(f"Expectancy:    {expectancy:+.2f}% per trade\n")
        f.write(f"Total P&L:     {total_pnl:+.2f}%\n\n")
        f.write("TRADES:\n")
        f.write(df_trades.to_string(index=False))
    print(f"  ✅ Report saved to {report_path}")


# =============================================================================
#  ENTRY POINT
# =============================================================================

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="SOXL Intraday Backtest — ORB / VWAP / PDH signal replay"
    )
    parser.add_argument("--days",           type=int,   default=60,
                        help="Number of calendar days to backtest (default: 60)")
    parser.add_argument("--vol-mult",       type=float, default=1.5,
                        help="Volume multiplier threshold (default: 1.5)")
    parser.add_argument("--rsi-min",        type=int,   default=45,
                        help="RSI floor for ORB/PDH signals (default: 45)")
    parser.add_argument("--rsi-max",        type=int,   default=65,
                        help="RSI ceiling for ORB/PDH signals (default: 65)")
    parser.add_argument("--no-trend-filter",action="store_true",
                        help="Disable daily 50 EMA trend filter")
    parser.add_argument("--sensitivity",    action="store_true",
                        help="Run volume threshold sensitivity sweep (0.5x-2.5x)")
    parser.add_argument("--verbose",        action="store_true",
                        help="Show every bar evaluation")
    parser.add_argument("--orb-hours",      type=float, default=None,
                        help="Max hours after 10am ORB can fire (e.g. 2 = until noon)")
    parser.add_argument("--vwap-hours",     type=float, default=None,
                        help="Max hours after 9:30am VWAP can fire (e.g. 3.5 = until 1pm, 6.5 = until 4pm)")
    parser.add_argument("--target",         type=float, default=4.0,
                        help="Profit target %% (default: 4.0 = +4%%)")
    parser.add_argument("--stop",           type=float, default=2.0,
                        help="Stop loss %% (default: 2.0 = -2%%)")
    args = parser.parse_args()

    # Apply args to global params
    VOLUME_MULT         = args.vol_mult
    ORB_RSI_MIN         = args.rsi_min
    ORB_RSI_MAX         = args.rsi_max
    DAILY_TREND_FILTER  = not args.no_trend_filter
    ORB_MAX_HOURS       = args.orb_hours
    VWAP_MAX_HOURS      = args.vwap_hours
    TARGET_PCT          = args.target / 100.0
    STOP_PCT            = args.stop   / 100.0

    print("="*60)
    print("  SOXL INTRADAY BACKTEST v1.0")
    print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print("="*60)
    print(f"  Signals:      ORB, PDH, VWAP Reclaim")
    print(f"  Period:       Last {args.days} days")
    print(f"  Vol threshold:{args.vol_mult}x")
    print(f"  RSI range:    {args.rsi_min}-{args.rsi_max}")
    print(f"  Trend filter: {'ON' if DAILY_TREND_FILTER else 'OFF'}")
    print(f"  Target:       +{TARGET_PCT*100:.1f}% | Stop: -{STOP_PCT*100:.1f}%")
    if args.orb_hours:
        print(f"  ORB window:   10:00am - {int(10 + args.orb_hours):02d}:{int((args.orb_hours % 1)*60):02d}am/pm")
    if args.vwap_hours:
        vwap_close_h = int(9 + args.vwap_hours)
        vwap_close_m = int((args.vwap_hours % 1) * 60)
        print(f"  VWAP window:  9:30am - {vwap_close_h:02d}:{vwap_close_m:02d}pm")

    # Download data
    df, df_daily = download_data(days=args.days)
    if df is None:
        print("Data download failed. Exiting.")
        exit(1)

    # Calculate indicators on full dataset
    print("\nCalculating indicators...")
    df = calculate_indicators(df)
    print(f"  Indicators calculated on {len(df)} bars")

    if args.sensitivity:
        # Volume sensitivity sweep
        run_sensitivity(df, df_daily)
    else:
        # Standard backtest
        trades = run_backtest(df, df_daily, vol_mult=args.vol_mult, verbose=args.verbose)
        print_report(trades, vol_mult=args.vol_mult, days=args.days)
