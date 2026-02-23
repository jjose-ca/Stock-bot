"""
=============================================================================
  STOCK ALERT BOT v4.1 — Single File Edition
=============================================================================

WHAT THIS BOT DOES:
  Scans your watchlist every 5 minutes during market hours (Mon–Fri).
  Detects day trade and swing trade opportunities.
  Sends rich Discord alerts with entry, stop loss, target, and R/R ratio.
  Runs automatically via GitHub Actions — no server needed.

v4.1 CHANGES:
  1. COMPLETED-BAR SIGNALING — Day engine now signals on the last completed
     5m candle (iloc[-2]), not the in-progress bar. Prevents false triggers
     from partial candles that reverse before close.
  2. CATEGORY-CAPPED SCORING — Signals are grouped into categories (Trend,
     Momentum, Confirmation) with independent hard caps. This forces setups
     to show alignment across different market dynamics instead of stacking
     points from correlated signals.
  3. DAILY ATR STOPS — Day trade stops now use daily ATR instead of 5m ATR.
     The old 5m ATR × 1.5 stop was ~0.5–1% on most stocks and got hit by
     routine noise. Daily ATR gives structurally meaningful stop levels.

HOW TO RUN MANUALLY:
  python bot.py                        # Auto-detects mode from time of day
  python bot.py --mode premarket       # Morning gap summary
  python bot.py --mode intraday        # Day trade scan
  python bot.py --mode power_hour      # 3pm swing buy window
  python bot.py --ticker NVDA          # Scan one ticker only

HOW TO SET UP:
  1. Add your tickers to the TICKERS_USD / TICKERS_CAD lists below
  2. Set your Discord webhook as an environment variable: DISCORD_URL
  3. Push to GitHub and add DISCORD_URL as a GitHub Secret
  4. The GitHub Actions schedule (in .github/workflows/stock_bot.yml) runs it automatically

ARCHITECTURE — Three-Stage Funnel:
  Stage 1: One bulk API call downloads 1 year of daily data for ALL tickers
  Stage 2: Swing filter runs on daily data (no extra API calls)
  Stage 3: 5-minute intraday data fetched ONLY for tickers that passed Stage 2
  This means a 50-ticker list typically makes only 5–15 intraday API calls.
=============================================================================
"""

import os
import json
import time
import argparse
import traceback
import pandas as pd
import pandas_ta as ta
import pytz
import requests
import yfinance as yf

from datetime import datetime
from pathlib import Path


# =============================================================================
#  SECTION 1 — CONFIGURATION
#  ✏️  Edit this section to customize the bot for your needs.
# =============================================================================

# ── Your watchlist ────────────────────────────────────────────────────────────
# USD tickers (NYSE / NASDAQ)
TICKERS_USD = [
    'VTI',          # ← Keep this — used for market regime check, not traded

    # ETFs
    'SPY', 'SPLG', 'QQQM', 'QQQ', 'IWM',
    'SOXQ', 'XLY', 'GDX', 'SIL', 'XLF', 'XLK', 'SMH', 'GLD', 'SLV', 'ITB',

    # Mega Cap
    'AAPL', 'MSFT', 'GOOGL', 'AMZN', 'META',
    'JPM', 'BAC', 'XOM', 'ABBV',

    # Mid-risk
    'NVDA', 'AVGO', 'QCOM', 'MU', 'AMAT', 'LRCX',
    'NFLX', 'ORCL', 'CRM', 'NOW', 'PANW',
    'SHOP', 'UBER', 'PYPL', 'TGT', 'SQ',
    'OXY', 'DVN', 'CCL', 'DKNG',

    # High beta
    'TSLA', 'PLTR', 'AMD', 'ARM', 'SMCI',
    'SOFI', 'HOOD', 'COIN', 'MSTR', 'SNOW',
]

# CAD tickers (TSX) — alerts will be tagged CA$ automatically
TICKERS_CAD = [
    'ZSP.TO', 'XEF.TO',
    'HUT.TO', 'CVE.TO', 'MFC.TO', 'ATD.TO', 'TOU.TO',
]

# ── Discord ───────────────────────────────────────────────────────────────────
# Never hardcode your webhook URL. Set it as an environment variable instead.
# On your computer:    export DISCORD_URL="https://discord.com/api/webhooks/..."
# On GitHub Actions:   add it as a repository Secret named DISCORD_URL
DISCORD_WEBHOOK_URL = os.getenv('DISCORD_URL')

# ── Scoring thresholds ────────────────────────────────────────────────────────
# Lowered from 6/8 after adding category caps (v4.1).
# Category caps prevent correlated signals from stacking unboundedly,
# so thresholds need to be lower to compensate for the reduced max score.
DAY_SCORE_THRESHOLD   = 5
SWING_SCORE_THRESHOLD = 6

# ── Category caps (v4.1) ─────────────────────────────────────────────────────
# Each scoring signal belongs to a category. Each category has a hard cap.
# This forces setups to show alignment ACROSS different market dynamics
# before firing, rather than stacking points from one correlated cluster.
#
#   Day Engine (max possible = 8):
#     Trend/Structure      — Max 3: VWAP position, EMA 9>21, VWAP reclaim
#     Momentum/Reversion   — Max 3: RSI level, BB lower touch, RSI curling
#     Confirmation         — Max 2: Higher high/low, Opening range breakout
#
#   Swing Engine (max possible = 8, before BB squeeze penalty):
#     Trend/Structure      — Max 4: EMA 21 wick/support, EMA 50 proximity,
#                                    bullish structure, 52-week high
#     Momentum/Reversion   — Max 4: RSI level, BB lower band close, MACD
DAY_CATEGORY_CAPS   = {"trend": 3, "momentum": 3, "confirmation": 2}
SWING_CATEGORY_CAPS = {"trend": 4, "momentum": 4}

# ── Liquidity gates (minimum average daily dollar volume) ─────────────────────
MIN_DOLLAR_VOLUME = {
    "DAY TRADE": 10_000_000,   # $10M avg daily dollar volume
    "SWING":      2_000_000,   # $2M avg daily dollar volume
}

# ── Risk parameters ───────────────────────────────────────────────────────────
MAX_STOP_PCT = {
    "DAY TRADE":         0.02,   # Stop can't be more than 2% away on day trades
    "SWING":             0.06,   # 6% max stop on swings
    "DAY TRADE + SWING": 0.03,   # 3% when both engines agree
}
MIN_RR_RATIO          = 1.5    # Minimum acceptable risk/reward ratio

# Day trade stops now use DAILY ATR (v4.1) — 5m ATR was too tight and got
# stopped out by normal noise. Daily ATR gives structurally meaningful levels.
DAY_ATR_STOP_MULT     = 0.5    # Day trade stop = price − (daily_ATR × 0.5)
DAY_ATR_TARGET_MULT   = 1.0    # Day trade target = price + (daily_ATR × 1.0)
SWING_ATR_STOP_MULT   = 0.8    # Swing stop relative to support level
SWING_ATR_TARGET_MULT = 2.5    # Swing target ATR multiplier

# ── Volume thresholds ─────────────────────────────────────────────────────────
VOLUME_STRONG   = 2.0    # 2x relative volume = strong institutional activity
VOLUME_MODERATE = 1.2    # 1.2x = moderate interest

# ── Power hour (3–4pm ET) ─────────────────────────────────────────────────────
# High-confidence setups during this window get a "buy before close" recommendation
POWER_HOUR_MIN_VOL_RATIO = 2.0  # Need 2x+ volume to recommend buying before close

# ── Earnings warning ──────────────────────────────────────────────────────────
EARNINGS_WARNING_DAYS  = 7   # Flag earnings within 7 days
EARNINGS_SCORE_PENALTY = 2   # Dock 2 points from score if earnings near

# ── Time penalties (added to thresholds — makes signals harder to fire) ────────
OPENING_NOISE_MINUTES = 30   # First 30 min of session: threshold +1
LATE_FRIDAY_MINUTES   = 300  # After 2:30pm Friday: threshold +1 (9:30am + 300min = 2:30pm)

# ── State & cooldown persistence ──────────────────────────────────────────────
# These files survive between steps in a single GitHub Actions run.
# They reset between separate runs (i.e., between 5-min cron jobs).
# To use them for intraday deduplication, you'd need GitHub Actions cache or
# an external store. For now they're ready to use but disabled by default.
COOLDOWN_FILE    = "/tmp/alert_cooldowns.json"
STATE_FILE       = "/tmp/setup_states.json"
COOLDOWN_MINUTES = {"DAY TRADE": 30, "SWING": 240, "DAY TRADE + SWING": 30}

# ── Discord embed colors ──────────────────────────────────────────────────────
COLOR_GREEN  = 5763719    # Scenario A — full alignment
COLOR_YELLOW = 16776960   # Scenario B/C — partial signal
COLOR_BLUE   = 3447003    # Informational
COLOR_RED    = 15548997   # Warning / bearish

# ── Timezone ──────────────────────────────────────────────────────────────────
TIMEZONE = "US/Eastern"


# =============================================================================
#  SECTION 2 — SCHEDULER
#  Determines what scan mode to run based on current ET time.
#  Also computes time-of-day penalties (noisy open, late Friday).
# =============================================================================

def get_scan_mode(et_now: datetime) -> str:
    """Returns the appropriate scan mode based on current ET time."""
    t = et_now.hour + et_now.minute / 60.0

    if 8.5  <= t < 9.5:  return "premarket"
    if 9.5  <= t < 15.0: return "intraday"
    if 15.0 <= t < 16.0: return "power_hour"

    print("🌙 Outside scan windows — nothing to do.")
    return "off_hours"


def get_time_penalty(et_now: datetime) -> tuple[int, list[str]]:
    """
    Returns (penalty, reasons).
    Penalty is added to score thresholds to suppress noise during known
    high-false-signal periods.
    """
    penalty = 0
    reasons = []

    mkt_open    = et_now.replace(hour=9, minute=30, second=0, microsecond=0)
    elapsed_min = max((et_now - mkt_open).total_seconds() / 60.0, 0.0)

    if elapsed_min < OPENING_NOISE_MINUTES:
        penalty += 1
        reasons.append(f"⏰ Opening {OPENING_NOISE_MINUTES}-min noise window (+1 threshold)")

    if et_now.weekday() == 4 and elapsed_min > LATE_FRIDAY_MINUTES:
        penalty += 1
        reasons.append("📅 Late Friday — weekend gap risk (+1 threshold)")

    return penalty, reasons


def get_elapsed_minutes(et_now: datetime) -> float:
    """Minutes elapsed since 9:30am open. Caps at 390 (full session)."""
    mkt_open = et_now.replace(hour=9, minute=30, second=0, microsecond=0)
    if et_now < mkt_open:
        return 0.0
    return min((et_now - mkt_open).total_seconds() / 60.0, 390.0)


def is_power_hour(et_now: datetime) -> bool:
    return 15 <= et_now.hour < 16


# =============================================================================
#  SECTION 3 — DATA FETCHING (Funnel Architecture)
#
#  Stage 1: fetch_bulk_daily()
#    One API call for ALL tickers. Returns ~252 daily bars each.
#    Enough for EMA-50, EMA-200, MACD — no data starvation.
#
#  Stage 3: fetch_targeted_intraday()
#    Called ONLY for tickers that survived the swing filter.
#    5 days × 5-minute bars, filtered to RTH (09:30–16:00 ET).
# =============================================================================

def _flatten(df: pd.DataFrame) -> pd.DataFrame:
    """
    Safety net: collapses a MultiIndex column frame to a single level.
    Should no longer trigger in normal operation now that both yf.download
    calls pass multi_level_index=False (requires yfinance ≥ 0.2.38).
    Kept here to guard against unexpected library regressions.

    Detects which level contains OHLCV names rather than assuming level 0,
    because yfinance ≥ 0.2.36 reversed the MultiIndex order to (Price, Ticker)
    whereas older versions used (Ticker, Price). Assuming level 0 would
    silently return wrong column names on one of those two versions.
    """
    if isinstance(df.columns, pd.MultiIndex):
        ohlcv = {'Open', 'High', 'Low', 'Close', 'Volume'}
        for i in range(df.columns.nlevels):
            level_vals = df.columns.get_level_values(i)
            if ohlcv.intersection(set(level_vals)):
                df.columns = level_vals
                return df
        # Fallback: collapse level 0 if no OHLCV names found in any level
        df.columns = df.columns.get_level_values(0)
    return df


def get_currency(ticker: str) -> str:
    return 'CAD' if ticker in TICKERS_CAD else 'USD'


def fetch_bulk_daily(tickers: list) -> pd.DataFrame:
    """Stage 1: One bulk download for all tickers — 1 year of daily bars."""
    print(f"📥 STAGE 1: Bulk downloading 1y daily data for {len(tickers)} tickers...")
    try:
        df = yf.download(
            tickers, period="1y", interval="1d",
            group_by='ticker', auto_adjust=True, progress=False,
            multi_level_index=False   # Requires yfinance ≥ 0.2.38 — flat columns, no MultiIndex
        )
        print(f"   ✅ Bulk download complete.")
        return df
    except Exception as e:
        print(f"   ❌ Bulk download failed: {e}")
        return pd.DataFrame()


def extract_ticker_daily(bulk_data: pd.DataFrame, ticker: str) -> pd.DataFrame | None:
    """Extracts a single ticker's OHLCV from the bulk MultiIndex DataFrame."""
    try:
        if isinstance(bulk_data.columns, pd.MultiIndex):
            if ticker not in bulk_data.columns.get_level_values(0):
                return None
            df = bulk_data[ticker].copy()
        else:
            df = bulk_data.copy()

        df = _flatten(df)
        df.dropna(subset=['Close'], inplace=True)
        return df if not df.empty else None
    except Exception as e:
        print(f"   ⚠️ [{ticker}] Extraction failed: {e}")
        return None


def fetch_targeted_intraday(ticker: str) -> pd.DataFrame | None:
    """
    Stage 3: Fetches 5 days of 5-minute bars for a single ticker.
    Called ONLY after a ticker passes the swing filter.
    RTH filtered to 09:30–16:00 ET.
    """
    try:
        # period="1mo" gives ~19 comparison days per time slot for RVAT median
        # calculations, versus only 4 days with "5d". Zero extra API calls —
        # just a wider window on the same targeted per-ticker fetch.
        df = yf.download(ticker, period="1mo", interval="5m",
                         auto_adjust=True, progress=False,
                         multi_level_index=False)   # Requires yfinance ≥ 0.2.38
        if df is None or df.empty:
            return None

        df = _flatten(df)

        if df.index.tz is None:
            df.index = df.index.tz_localize('UTC')
        df.index = df.index.tz_convert(TIMEZONE)

        df_rth = df.between_time('09:30', '15:55').copy()
        if df_rth.empty:
            return None

        # Guard against yfinance silently returning stale data.
        # If the newest bar is more than 12 minutes old during market hours,
        # the feed is lagging and signals would be based on stale prices.
        try:
            tz          = pytz.timezone(TIMEZONE)
            newest_bar  = df_rth.index[-1]
            age_minutes = (datetime.now(tz) - newest_bar).total_seconds() / 60.0
            if age_minutes > 12:
                print(f"   ⚠️ [{ticker}] Stale data: newest bar is {age_minutes:.0f}min old. Skipping.")
                return None
        except Exception:
            pass  # Don't block on age check errors — let caller decide

        return df_rth
    except Exception as e:
        print(f"   ⚠️ [{ticker}] Intraday fetch failed: {e}")
        return None


def passes_liquidity_filter(df_daily: pd.DataFrame, mode: str) -> bool:
    """Rejects thinly traded tickers where spread/slippage destroys R/R."""
    try:
        tail     = df_daily.tail(20)
        avg_dv   = (tail['Close'] * tail['Volume']).mean()
        minimum  = MIN_DOLLAR_VOLUME.get(mode, 2_000_000)
        if avg_dv < minimum:
            print(f"   💧 Dollar vol ${avg_dv/1e6:.1f}M < ${minimum/1e6:.0f}M min. Rejected.")
            return False
        return True
    except Exception:
        return True


def calculate_relative_volume(df_intraday: pd.DataFrame) -> float:
    """
    Relative Volume At Time (RVAT).
    Compares last completed bar's volume to the median volume at the same
    5-minute time slot across prior days. Uses iloc[-2] to avoid partial-bar bias.
    """
    try:
        if df_intraday is None or len(df_intraday) < 2:
            return 1.0

        df               = df_intraday.copy()
        df['time_slot']  = df.index.time
        df['date']       = df.index.date

        last_bar   = df.iloc[-2]
        check_time = last_bar.name.time()
        check_date = last_bar.name.date()
        check_vol  = float(last_bar['Volume'])

        historical = df[(df['time_slot'] == check_time) & (df['date'] != check_date)]
        if historical.empty:
            return 1.0

        median_vol = historical['Volume'].median()
        if median_vol == 0 or pd.isna(median_vol):
            return 1.0

        return round(check_vol / median_vol, 2)
    except Exception:
        return 1.0


# =============================================================================
#  SECTION 4 — MARKET REGIME FILTER
#  Checks VTI vs its 200-day SMA. Extracted from the bulk download — no
#  extra API call needed. Bearish regime adds +1 to all thresholds.
# =============================================================================

def check_market_regime(bulk_data: pd.DataFrame) -> tuple[int, bool]:
    """
    Returns (regime_penalty: int, regime_bullish: bool).
    regime_penalty is added to all score thresholds.
    """
    print(f"🌍 Checking market regime (VTI vs 200 SMA)...")
    try:
        vti_df = extract_ticker_daily(bulk_data, 'VTI')
        if vti_df is None or len(vti_df) < 200:
            print("   ⚠️ VTI: Insufficient data — defaulting BULLISH")
            return 0, True

        vti_sma   = ta.sma(vti_df['Close'], length=200).iloc[-1]
        vti_price = float(vti_df['Close'].iloc[-1])

        if vti_price < vti_sma:
            print(f"   ⚠️ Regime: BEARISH (VTI ${vti_price:.2f} < 200 SMA ${vti_sma:.2f}) → +1 threshold")
            return 1, False

        print(f"   ✅ Regime: BULLISH (VTI ${vti_price:.2f} > 200 SMA ${vti_sma:.2f})")
        return 0, True
    except Exception as e:
        print(f"   ⚠️ Regime check error: {e} — defaulting BULLISH")
        return 0, True


# =============================================================================
#  SECTION 5 — DAY TRADE ENGINE (5-Minute Bars)
#
#  v4.1 CHANGES:
#    1. Signals on COMPLETED bars (iloc[-2]) to avoid partial-bar false triggers.
#       iloc[-1] is only used for the live price reference in the output dict.
#    2. Scoring uses category caps to prevent correlated signals from stacking.
#    3. Stops use DAILY ATR (passed in) instead of 5m ATR, which was too tight.
#
#  Categories (capped independently):
#    TREND/STRUCTURE (max 3):
#      A. VWAP position (+3)              — primary intraday direction filter
#      C. 5m EMA 9 > 21 (+2)             — short-term bullish momentum
#      D. VWAP reclaim (+2)              — dipped below VWAP and recovered
#    MOMENTUM/REVERSION (max 3):
#      B. 5m RSI oversold (+1 to +3)     — deeply oversold = stronger signal
#      F. BB lower band touch (+2)       — mean reversion setup
#      G. RSI curling upward (+2)        — direction of momentum from oversold
#    CONFIRMATION (max 2):
#      E. Higher high + higher low (+1)  — momentum shift confirmation
#      H. Opening range breakout (+1)    — price breaks first 30-min high
# =============================================================================

def run_day_engine(df_today: pd.DataFrame, total_penalty: int,
                   daily_atr: float | None = None) -> dict | None:
    """
    Scores today's 5m bars. Returns signal dict or None if score < threshold.

    Args:
        df_today:      Today's 5m RTH bars.
        total_penalty:  Sum of time + regime penalties.
        daily_atr:      ATR from the daily timeframe, used for stop/target.
                        Falls back to 5m ATR if not provided (not recommended).
    """
    if df_today is None or len(df_today) < 20:  # RSI(14) needs ≥15 bars; 20 gives safe headroom
        return None

    df = df_today.copy()

    df['EMA_9']  = ta.ema(df['Close'], length=9)
    df['EMA_21'] = ta.ema(df['Close'], length=21)
    df['RSI']    = ta.rsi(df['Close'], length=14)
    df['ATR']    = ta.atr(df['High'], df['Low'], df['Close'], length=14)
    df['VWAP']   = ta.vwap(df['High'], df['Low'], df['Close'], df['Volume'])

    bb = ta.bbands(df['Close'], length=20, std=2)
    if bb is not None and not bb.empty:
        bbl_col = [c for c in bb.columns if c.startswith('BBL')]
        if bbl_col: df['BBL'] = bb[bbl_col[0]]

    df.dropna(subset=['RSI', 'EMA_9', 'EMA_21', 'VWAP', 'ATR'], inplace=True)
    if len(df) < 4:
        return None

    # ── Completed-bar signaling (v4.1) ────────────────────────────────────────
    # Signal bar = last COMPLETED 5m candle (iloc[-2]).
    # Current bar (iloc[-1]) is in-progress and can reverse before close.
    # We only use iloc[-1] for the live entry price in the output dict.
    sig_bar  = df.iloc[-2]   # last completed bar — all signals read from here
    prev_bar = df.iloc[-3]   # bar before signal bar — for comparisons
    live_bar = df.iloc[-1]   # current in-progress bar — entry price only

    price  = float(live_bar['Close'])   # live price for entry reference
    vwap   = float(sig_bar['VWAP'])
    rsi    = float(sig_bar['RSI'])
    ema_9  = float(sig_bar['EMA_9'])
    ema_21 = float(sig_bar['EMA_21'])
    atr_5m = float(sig_bar['ATR'])

    # Use daily ATR for stops (v4.1); fall back to 5m ATR only if unavailable
    atr_for_stops = daily_atr if daily_atr is not None else atr_5m
    atr_source    = "Daily" if daily_atr is not None else "5m (fallback)"

    reasons = []

    # ── Category: TREND / STRUCTURE (cap at 3) ────────────────────────────────
    trend_score = 0
    trend_cap   = DAY_CATEGORY_CAPS["trend"]

    # A. VWAP position (signal bar closed above VWAP)
    sig_close  = float(sig_bar['Close'])
    above_vwap = sig_close > vwap
    if above_vwap:
        trend_score += 3
        reasons.append(f"✅ Price Above VWAP (${vwap:.2f})")

    # C. 5m EMA stack (on signal bar)
    if ema_9 > ema_21:
        trend_score += 2
        reasons.append("🚀 Bullish EMA Stack (9 > 21)")

    # D. VWAP reclaim (recent dip below VWAP, signal bar closed above)
    low_recent = df['Low'].iloc[-4:-1].min()  # 3 completed bars
    if low_recent < vwap and sig_close > vwap:
        trend_score += 2
        reasons.append("⚡ VWAP Reclaim — Intraday Bounce Confirmed")

    trend_score = min(trend_score, trend_cap)

    # ── Category: MOMENTUM / MEAN REVERSION (cap at 3) ───────────────────────
    momentum_score = 0
    momentum_cap   = DAY_CATEGORY_CAPS["momentum"]

    # B. 5m RSI (on signal bar)
    if rsi < 35:
        momentum_score += 3
        reasons.append(f"💎 Deeply Oversold (RSI {rsi:.1f})")
    elif rsi < 45:
        momentum_score += 2
        reasons.append(f"📉 Oversold (RSI {rsi:.1f})")
    elif rsi < 55:
        momentum_score += 1
        reasons.append(f"🌊 Momentum Reset (RSI {rsi:.1f})")

    # F. BB lower band touch (on signal bar)
    if 'BBL' in df.columns and not pd.isna(sig_bar.get('BBL', float('nan'))):
        bbl = float(sig_bar['BBL'])
        if sig_close <= bbl * 1.01:
            momentum_score += 2
            reasons.append(f"🛡️ 5m BB Lower Band Touch (${bbl:.2f})")

    # G. RSI curling upward — three consecutive higher RSI on COMPLETED bars.
    rsi_prev1 = float(df['RSI'].iloc[-3])
    rsi_prev2 = float(df['RSI'].iloc[-4])

    if (rsi > rsi_prev1 > rsi_prev2) and (rsi < 50):
        momentum_score += 2
        reasons.append(
            f"🔄 RSI Curling Up from Oversold "
            f"({rsi_prev2:.0f} → {rsi_prev1:.0f} → {rsi:.0f})"
        )

    momentum_score = min(momentum_score, momentum_cap)

    # ── Category: CONFIRMATION (cap at 2) ─────────────────────────────────────
    confirm_score = 0
    confirm_cap   = DAY_CATEGORY_CAPS["confirmation"]

    # E. Higher high + higher low (signal bar vs previous completed bar)
    if (float(sig_bar['High']) > float(prev_bar['High']) and
            float(sig_bar['Low']) > float(prev_bar['Low'])):
        confirm_score += 1
        reasons.append("📈 5m Higher High + Higher Low")

    # H. Opening range breakout (signal bar close broke first-30min high)
    orb_high = None
    try:
        today_date = df.index[-1].date()
        orb_bars   = df[df.index.date == today_date].between_time('09:30', '09:55')
        if not orb_bars.empty:
            orb_high = float(orb_bars['High'].max())
            if sig_close > orb_high:
                confirm_score += 1
                reasons.append(f"🚀 Opening Range Breakout (above ${orb_high:.2f})")
    except Exception:
        pass

    confirm_score = min(confirm_score, confirm_cap)

    # ── Final score ───────────────────────────────────────────────────────────
    score     = trend_score + momentum_score + confirm_score
    threshold = DAY_SCORE_THRESHOLD + total_penalty
    if score < threshold:
        return None

    # ── Stop / target using DAILY ATR (v4.1) ─────────────────────────────────
    # Daily ATR gives structurally meaningful levels that survive normal
    # 5-minute noise. The old 5m ATR × 1.5 stop was ~0.5–1% and got hit
    # constantly even on directionally correct trades.
    stop_loss   = round(price - (atr_for_stops * DAY_ATR_STOP_MULT), 2)
    take_profit = round(price + (atr_for_stops * DAY_ATR_TARGET_MULT), 2)

    return {
        "score": score, "threshold": threshold, "reasons": reasons,
        "score_breakdown": {
            "trend": trend_score, "momentum": momentum_score,
            "confirmation": confirm_score,
        },
        "stop_loss": stop_loss, "take_profit": take_profit,
        "atr": round(atr_for_stops, 4), "atr_5m": round(atr_5m, 4),
        "atr_source": atr_source,
        "vwap": round(vwap, 2), "rsi": round(rsi, 1),
        "ema_21": round(ema_21, 2), "ema_50": None,
        "price": round(price, 2), "is_bullish": above_vwap,
        "mode": "DAY TRADE", "orb_high": orb_high,
    }


# =============================================================================
#  SECTION 6 — SWING TRADE ENGINE (Full 1-Year Daily Bars)
#
#  Runs on ~252 daily bars from the bulk download.
#  EMA-50 is mathematically valid at this bar count (was broken in v2 with ~43 bars).
#
#  v4.1: Scoring uses category caps to decorrelate signals.
#
#  Categories (capped independently):
#    TREND/STRUCTURE (max 4):
#      C. 21 EMA wick detection (+2 or +3)  — wicked to EMA, closed above it
#      D. 21 EMA support hold (+1 or +2)
#      E. 50 EMA proximity — direction-aware (+1 or +2)
#      F. Price above 50 EMA (+1)           — bullish structure
#      H. 52-week high zone (+1)
#    MOMENTUM/REVERSION (max 4):
#      A. Daily RSI oversold (+1 to +3)
#      B. BB lower band close (+3)
#      G. MACD positive/improving (+1 or +2)
#    PENALTY (applied after caps):
#      BB squeeze (−2)                      — no edge when bands too narrow
# =============================================================================

def run_swing_engine(df_daily: pd.DataFrame, total_penalty: int) -> dict | None:
    """
    Scores the daily timeframe. Returns signal dict or None if score < threshold.
    """
    if df_daily is None or len(df_daily) < 50:
        return None

    df = df_daily.copy()

    df['EMA_21'] = ta.ema(df['Close'], length=21)
    df['EMA_50'] = ta.ema(df['Close'], length=50)
    df['RSI']    = ta.rsi(df['Close'], length=14)
    df['ATR']    = ta.atr(df['High'], df['Low'], df['Close'], length=14)

    macd = ta.macd(df['Close'])
    if macd is not None:
        hist_cols = [c for c in macd.columns if c.startswith('MACDh')]
        if hist_cols:
            df['MACD_H'] = macd[hist_cols[0]]

    bb = ta.bbands(df['Close'], length=20, std=2)
    if bb is not None and not bb.empty:
        lower_col = [c for c in bb.columns if c.startswith('BBL')]
        mid_col   = [c for c in bb.columns if c.startswith('BBM')]
        upper_col = [c for c in bb.columns if c.startswith('BBU')]
        if lower_col: df['BBL'] = bb[lower_col[0]]
        if mid_col:   df['BBM'] = bb[mid_col[0]]
        if upper_col: df['BBU'] = bb[upper_col[0]]
        if all([lower_col, mid_col, upper_col]):
            df['BB_WIDTH'] = (df['BBU'] - df['BBL']) / df['BBM']

    df.dropna(subset=['RSI', 'EMA_50', 'ATR'], inplace=True)
    if len(df) < 2:
        return None

    last = df.iloc[-1]
    prev = df.iloc[-2]

    price    = float(last['Close'])
    ema_21   = float(last['EMA_21'])
    ema_50   = float(last['EMA_50'])
    rsi      = float(last['RSI'])
    atr      = float(last['ATR'])
    bbl      = float(last['BBL'])      if 'BBL'      in df.columns else None
    bb_width = float(last['BB_WIDTH']) if 'BB_WIDTH' in df.columns else 0.0
    macd_h   = float(last['MACD_H'])   if 'MACD_H'   in df.columns else 0.0
    prev_mh  = float(prev['MACD_H'])   if 'MACD_H'   in df.columns else 0.0

    reasons = []

    # ── Category: TREND / STRUCTURE (cap at 4) ────────────────────────────────
    trend_score = 0
    trend_cap   = SWING_CATEGORY_CAPS["trend"]

    # C. 21 EMA wick detection
    daily_low   = float(last['Low'])
    daily_close = float(last['Close'])
    wick_to_21  = (daily_low <= ema_21 * 1.005) and (daily_close > ema_21) and (rsi < 65)

    pct_from_21 = (daily_close - ema_21) / ema_21
    near_21     =  0.0   <= pct_from_21 < 0.015
    below_21    = -0.015 <= pct_from_21 < 0.0

    if wick_to_21 and not near_21:
        trend_score += 3
        reasons.append(
            f"⚡ Daily Wick to 21 EMA + Strong Recovery "
            f"(Low ${daily_low:.2f} → Close ${daily_close:.2f})"
        )
    elif wick_to_21 and near_21:
        trend_score += 2
        reasons.append(f"⚡ Daily Wick to 21 EMA — Early Bounce (${ema_21:.2f})")
    elif near_21 and rsi < 55:
        trend_score += 2
        reasons.append(f"📈 Daily 21 EMA Support Hold (${ema_21:.2f})")
    elif below_21 and rsi < 55:
        trend_score += 1
        reasons.append(f"⚠️ Below 21 EMA — Testing as Support (${ema_21:.2f})")

    # D. 50 EMA proximity — direction-aware
    pct_from_50 = (price - ema_50) / ema_50
    if 0 <= pct_from_50 < 0.02:
        trend_score += 2
        reasons.append(f"📊 Pulling Back to 50 EMA Support (${ema_50:.2f})")
    elif -0.02 <= pct_from_50 < 0:
        trend_score += 1
        reasons.append(f"⚠️ Below 50 EMA — Testing as Support (${ema_50:.2f})")

    # E. Bullish structure
    if price > ema_50:
        trend_score += 1
        reasons.append("✅ Price Above Daily 50 EMA (Bullish Structure)")

    # H. 52-week high zone
    high_52w      = float(df['Close'].tail(252).max())
    near_52w_high = price >= high_52w * 0.98
    if near_52w_high:
        trend_score += 1
        reasons.append("📈 Within 2% of 52-Week High")

    trend_score = min(trend_score, trend_cap)

    # ── Category: MOMENTUM / MEAN REVERSION (cap at 4) ───────────────────────
    momentum_score = 0
    momentum_cap   = SWING_CATEGORY_CAPS["momentum"]

    # A. Daily RSI
    if rsi < 35:
        momentum_score += 3
        reasons.append(f"💎 Daily RSI Deeply Oversold ({rsi:.1f})")
    elif rsi < 45:
        momentum_score += 2
        reasons.append(f"📉 Daily RSI Oversold ({rsi:.1f})")
    elif rsi < 55:
        momentum_score += 1
        reasons.append(f"🌊 Daily Momentum Reset ({rsi:.1f})")

    # B. BB lower band close
    if bbl is not None and price <= bbl * 1.01:
        momentum_score += 3
        reasons.append(f"🛡️ Closed at Daily BB Lower (${bbl:.2f})")

    # F. MACD
    if macd_h > 0:
        momentum_score += 2
        reasons.append("🚀 Daily MACD: Positive Histogram")
    elif macd_h > prev_mh:
        momentum_score += 1
        reasons.append("🔄 Daily MACD: Improving Momentum")

    momentum_score = min(momentum_score, momentum_cap)

    # ── Penalty (applied AFTER caps — can push score below threshold) ─────────
    penalty = 0
    if 0 < bb_width < 0.03:
        penalty = 2
        reasons.append(f"⚠️ BB Squeeze (width {bb_width:.3f}) — reduced edge")

    # ── Final score ───────────────────────────────────────────────────────────
    score     = trend_score + momentum_score - penalty
    threshold = SWING_SCORE_THRESHOLD + total_penalty
    if score < threshold:
        return None

    is_bullish = price > ema_21

    # Support-aware stop: use the nearest support level as reference
    if bbl is not None and price <= bbl * 1.02:
        support = bbl
    elif near_21:
        support = ema_21
    else:
        support = ema_50

    stop_loss   = support - (atr * SWING_ATR_STOP_MULT)
    take_profit = price   + (atr * SWING_ATR_TARGET_MULT)

    if stop_loss >= price:
        stop_loss = price - atr

    return {
        "score": score, "threshold": threshold, "reasons": reasons,
        "score_breakdown": {
            "trend": trend_score, "momentum": momentum_score,
            "penalty": -penalty,
        },
        "stop_loss": round(stop_loss, 2), "take_profit": round(take_profit, 2),
        "atr": round(atr, 4), "atr_source": "Daily",
        "vwap": None, "rsi": round(rsi, 1),
        "ema_21": round(ema_21, 2), "ema_50": round(ema_50, 2),
        "price": round(price, 2), "is_bullish": is_bullish,
        "mode": "SWING", "near_52w_high": near_52w_high,
    }


# =============================================================================
#  SECTION 7 — CONFLICT GATE + DECISION MATRIX
#
#  Conflict gate:
#    If both engines fire but disagree on direction (e.g., 5m bullish but
#    daily bearish), the alert is suppressed — it's likely a bear trap.
#
#  Decision matrix — three scenarios:
#    A: Both engines agree → Full size, dual stops
#    B: Day engine only    → Small size, must exit by 3:45pm
#    C: Swing engine only  → Half size, wait for VWAP reclaim at next open
# =============================================================================

def signals_conflict(day_signal: dict | None, swing_signal: dict | None) -> bool:
    """
    Returns True if both signals fired but disagree on direction.

    Day engine:   is_bullish = price > VWAP
    Swing engine: is_bullish = price > EMA_21

    Using those raw flags directly caused false conflicts (e.g. a stock above
    its 21 EMA but briefly below VWAP after a morning dip).  We now resolve
    direction through a common reference: both signals carry a 'price' value
    and a 'stop_loss' value.  If both stops are below price the setups are
    aligned long; any other combination is a genuine conflict.
    """
    if day_signal is None or swing_signal is None:
        return False
    day_long   = day_signal["price"]   > day_signal["stop_loss"]
    swing_long = swing_signal["price"] > swing_signal["stop_loss"]
    return day_long != swing_long


def build_final_signal(
    day_signal:   dict | None,
    swing_signal: dict | None,
    rel_vol:      float,
    elapsed_min:  float,
    mode:         str,
) -> dict | None:
    """Combines engine outputs into one actionable signal. Returns None if no setup."""
    day_ok   = day_signal   is not None
    swing_ok = swing_signal is not None

    if not day_ok and not swing_ok:
        return None

    # Scenario A: Both engines agree
    if day_ok and swing_ok:
        sig = swing_signal.copy()
        sig.update({
            "scenario":       "A",
            "scenario_label": "⚡ SCENARIO A — DAY + SWING FULLY ALIGNED",
            "size_guidance":  "Full Size — Both timeframes confirmed",
            "hold_guidance":  (
                f"Day target: ${day_signal['take_profit']:.2f} (daily ATR × {DAY_ATR_TARGET_MULT}). "
                f"Trail remainder with daily ATR stop for multi-day hold."
            ),
            "day_stop":    day_signal["stop_loss"],
            "day_target":  day_signal["take_profit"],
            "mode":        "DAY TRADE + SWING",
            "vwap":        day_signal.get("vwap"),
            "rsi":         day_signal.get("rsi"),     # Fresher 5m RSI
            "atr_source":  "Daily (swing) + 5m (day)",
            "score":       day_signal["score"] + swing_signal["score"],
            "reasons": (
                [f"[5m] {r}"    for r in day_signal.get("reasons", [])] +
                [f"[Daily] {r}" for r in swing_signal.get("reasons", [])]
            ),
        })
        return sig

    # Scenario B: Day engine only
    if day_ok and not swing_ok:
        sig = day_signal.copy()
        sig.update({
            "scenario":       "B",
            "scenario_label": "⚡ SCENARIO B — INTRADAY SCALP ONLY",
            "size_guidance":  "Small Size — No daily structure confirmation",
            "hold_guidance":  "Must exit before 3:45pm ET. No overnight hold.",
            "mode":           "DAY TRADE",
        })
        return sig

    # Scenario C: Swing engine only
    if swing_ok and not day_ok:
        sig = swing_signal.copy()
        sig.update({
            "scenario":       "C",
            "scenario_label": "📅 SCENARIO C — SWING (Awaiting Intraday Confirmation)",
            "size_guidance":  "Half Size — Add on VWAP reclaim with volume",
            "hold_guidance":  (
                "Daily structure valid. Best entry: next open or VWAP reclaim "
                "with 1.5x+ relative volume on the 5m chart."
            ),
            "mode": "SWING",
        })
        return sig

    return None


def should_buy_now(signal: dict, rel_vol: float, mode: str) -> bool:
    """
    Determines whether to show the 'buy before close' power hour banner.
    Only fires on Scenario A or C, with strong volume, during power hour.
    """
    if mode != "power_hour":
        return False
    if signal.get("scenario") not in ("A", "C"):
        return False
    if rel_vol < POWER_HOUR_MIN_VOL_RATIO:
        return False
    price  = signal["price"]
    ema_50 = signal.get("ema_50", price)
    return ema_50 is not None and price > ema_50


# =============================================================================
#  SECTION 8 — RISK VALIDATOR
#
#  Step 1: If stop is too wide, auto-tighten it to the mode's max %.
#  Step 2: Check R/R ratio meets minimum. Reject if not.
# =============================================================================

def validate_risk(signal: dict, mode: str) -> dict | None:
    price     = signal["price"]
    stop_loss = signal["stop_loss"]
    target    = signal["take_profit"]

    mode_key  = mode  # "DAY TRADE + SWING" is a distinct key — don't collapse it
    max_stop  = MAX_STOP_PCT.get(mode_key, 0.05)
    actual_pct = (price - stop_loss) / price

    if actual_pct > max_stop:
        signal["stop_loss"]     = round(price * (1 - max_stop), 2)
        signal["stop_adjusted"] = True
        print(f"   ⚙️  Stop tightened to {max_stop*100:.0f}% max: ${stop_loss:.2f} → ${signal['stop_loss']:.2f}")
    else:
        signal["stop_adjusted"] = False

    risk   = price - signal["stop_loss"]
    reward = target - price

    if risk <= 0:
        print(f"   ❌ Invalid stop (risk ≤ 0). Rejected.")
        return None

    rr = round(reward / risk, 2)
    signal["rr_ratio"] = rr

    if rr < MIN_RR_RATIO:
        print(f"   📉 R/R {rr:.2f} below minimum {MIN_RR_RATIO}. Rejected.")
        return None

    return signal


# =============================================================================
#  SECTION 9 — EARNINGS CHECK
#  Docks EARNINGS_SCORE_PENALTY from score if earnings within warning window.
#  Re-checks threshold after penalty — rejects if score falls below.
# =============================================================================

def check_earnings(ticker: str) -> tuple[bool, str]:
    """Returns (has_warning, message_string)."""
    try:
        cal = yf.Ticker(ticker).calendar
        if cal is None:
            return False, ""

        eastern       = pytz.timezone(TIMEZONE)
        earnings_date = None

        if isinstance(cal, dict) and 'Earnings Date' in cal:
            earnings_date = cal['Earnings Date'][0]
        elif isinstance(cal, pd.DataFrame):
            if 'Earnings Date' in cal.columns:
                earnings_date = cal.iloc[0]['Earnings Date']
            elif not cal.empty:
                earnings_date = cal.iloc[0, 0]

        if earnings_date is None:
            return False, ""

        earnings_date = pd.to_datetime(earnings_date).date()
        today         = datetime.now(eastern).date()
        days_until    = (earnings_date - today).days

        if 0 <= days_until <= EARNINGS_WARNING_DAYS:
            msg = (f"⚠️ **EARNINGS WARNING:** Report in "
                   f"{days_until} day{'s' if days_until != 1 else ''} ({earnings_date})")
            return True, msg

        return False, ""
    except Exception:
        return False, ""


def apply_earnings_penalty(signal: dict, total_penalty: int) -> dict | None:
    """Applies score penalty and re-checks threshold. Returns None if rejected."""
    # Use the threshold already stored in the signal rather than re-deriving it
    # from scratch.  For Scenario A the score is the *sum* of both engines, so
    # recalculating from a single base threshold was too lenient.
    stored_threshold = signal.get("threshold", DAY_SCORE_THRESHOLD + total_penalty)
    signal["score"] -= EARNINGS_SCORE_PENALTY
    print(f"   ⚠️ Earnings penalty: score {signal['score'] + EARNINGS_SCORE_PENALTY} → {signal['score']} "
          f"(min {stored_threshold})")

    if signal["score"] < stored_threshold:
        print(f"   ⚠️ Score below threshold after penalty. Skipping.")
        return None
    return signal


# =============================================================================
#  SECTION 10 — STATE MACHINE + COOLDOWN
#  These are ready to use but DISABLED by default in the main loop.
#  To enable: uncomment the relevant blocks in Section 12.
#
#  State machine: CLEAR → TRIGGERED → INVALIDATED → CLEAR
#  Cooldown: per-ticker, per-mode time gate
# =============================================================================

def _load_json(path: str) -> dict:
    try:
        if Path(path).exists():
            with open(path) as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def _save_json(path: str, data: dict):
    try:
        with open(path, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        print(f"⚠️ State save failed: {e}")


def is_on_cooldown(ticker: str, mode: str) -> bool:
    cooldowns = _load_json(COOLDOWN_FILE)
    key = f"{ticker}_{mode}"
    if key not in cooldowns:
        return False
    try:
        tz   = pytz.timezone(TIMEZONE)
        last = datetime.fromisoformat(cooldowns[key])
        # Ensure both sides are timezone-aware to avoid TypeError when
        # mixed with et_now (which is always tz-aware).
        if last.tzinfo is None:
            last = tz.localize(last)
        now  = datetime.now(tz)
        mins  = (now - last).total_seconds() / 60.0
        limit = COOLDOWN_MINUTES.get(mode, 30)
        if mins < limit:
            print(f"   ⏱️ [{ticker}] Cooldown: {mins:.0f}/{limit} min")
            return True
    except Exception:
        pass
    return False


def set_cooldown(ticker: str, mode: str):
    cooldowns = _load_json(COOLDOWN_FILE)
    cooldowns[f"{ticker}_{mode}"] = datetime.now(pytz.timezone(TIMEZONE)).isoformat()
    _save_json(COOLDOWN_FILE, cooldowns)


def check_state(ticker: str, current_price: float) -> str:
    states = _load_json(STATE_FILE)
    record = states.get(ticker, {"state": "CLEAR"})
    state  = record.get("state", "CLEAR")

    if state == "TRIGGERED":
        stop = record.get("stop_loss")
        if stop is not None and current_price < stop:
            record["state"] = "INVALIDATED"
            states[ticker]  = record
            _save_json(STATE_FILE, states)
            print(f"   ⛔ [{ticker}] Stop ${stop:.2f} hit → INVALIDATED")
            return "SUPPRESS_INVALIDATED"
        return "SUPPRESS_TRIGGERED"

    if state == "INVALIDATED":
        states[ticker] = {"state": "CLEAR"}
        _save_json(STATE_FILE, states)
        print(f"   🔄 [{ticker}] Reset → CLEAR")
        return "SUPPRESS_INVALIDATED"

    return "ALLOW_ALERT"


def set_state(ticker: str, new_state: str, stop_loss: float = None, mode: str = None):
    states = _load_json(STATE_FILE)
    states[ticker] = {
        "state":     new_state,
        "stop_loss": stop_loss,
        "mode":      mode,
        "updated":   datetime.now(pytz.timezone(TIMEZONE)).isoformat(),
    }
    _save_json(STATE_FILE, states)


# =============================================================================
#  SECTION 11 — DISCORD ALERT
#  Rich embed with trade plan, technicals, reasons, and contextual banners.
# =============================================================================

def _post_discord(payload: dict):
    if not DISCORD_WEBHOOK_URL:
        print("⚠️  DISCORD_URL not set — skipping webhook.")
        return
    try:
        r = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=10)
        r.raise_for_status()
        time.sleep(0.5)   # Discord allows ~30 req/min; 0.5s gap prevents 429s on bulk alerts
    except Exception as e:
        print(f"❌ Discord error: {e}")


def send_setup_alert(ticker, currency, signal, rel_vol,
                     elapsed_minutes, mode, regime_bullish,
                     earnings_msg="", buy_now=False):
    """Sends a full trade setup embed to Discord."""
    et_now    = datetime.now(pytz.timezone(TIMEZONE))
    curr_sym  = 'CA$' if currency == 'CAD' else '$'
    scenario  = signal.get("scenario", "?")
    trade_mode = signal.get("mode", "UNKNOWN")
    price     = signal["price"]
    stop_loss = signal["stop_loss"]
    target    = signal["take_profit"]
    rr        = signal.get("rr_ratio", 0.0)
    atr_val   = signal.get("atr", 0.0)
    score     = signal.get("score", 0)
    threshold = signal.get("threshold", 0)
    stop_pct  = (price - stop_loss) / price * 100
    tgt_pct   = (target - price)    / price * 100
    risk_share = price - stop_loss

    color_map  = {"A": COLOR_GREEN, "B": COLOR_YELLOW, "C": COLOR_BLUE}
    rating_map = {"A": "🔥 HIGH CONVICTION", "B": "⚡ INTRADAY SCALP", "C": "📅 SWING SETUP"}
    color  = color_map.get(scenario, COLOR_RED)
    rating = rating_map.get(scenario, "🚨 ALERT")

    # RSI label
    rsi_val = signal.get("rsi", 0.0)
    if rsi_val < 30:    rsi_label = "🔴 Deeply Oversold"
    elif rsi_val < 45:  rsi_label = "🟠 Oversold"
    elif rsi_val < 55:  rsi_label = "🟡 Neutral"
    elif rsi_val < 65:  rsi_label = "🟢 Bullish"
    else:               rsi_label = "⚪ Extended"

    # EMA distances (direction-aware)
    def ema_str(val):
        if not val: return "N/A"
        pct = (price - val) / val * 100
        return f"{curr_sym}{val:.2f} ({abs(pct):.1f}% {'above' if pct >= 0 else 'below'})"

    # VWAP
    vwap = signal.get("vwap")
    if vwap:
        vwap_pct = (price - vwap) / vwap * 100
        vwap_str = f"{curr_sym}{vwap:.2f} ({abs(vwap_pct):.1f}% {'above' if vwap_pct >= 0 else 'below'})"
    else:
        vwap_str = "N/A"

    # Volume label
    vol_dir   = "Buying" if signal.get("is_bullish") else "Selling"
    if rel_vol > 2.0:    vol_label = "🔥 Heavy"
    elif rel_vol > 1.2:  vol_label = "💪 Strong"
    else:                vol_label = "😐 Normal"
    vol_str = f"{rel_vol:.1f}x · {vol_label} {vol_dir}"

    # Session label
    if elapsed_minutes < 20:       session = "⏰ Opening (noisy)"
    elif elapsed_minutes > 360:    session = "🕒 Late Session"
    else:                          session = "✅ Normal Hours"

    regime_label = "🟢 Bullish Market" if regime_bullish else "🔴 Bearish Market"

    # Power hour banner
    buy_banner = ""
    if buy_now:
        buy_banner = (
            "\n🔔 **POWER HOUR — BUY BEFORE CLOSE**\n"
            "> Strong volume + high confidence. Consider entering **today** "
            "to avoid a gap-up tomorrow. Set stop immediately after entry.\n"
        )
    elif mode == "power_hour" and not buy_now:
        buy_banner = (
            "\n⏳ **3pm Scan — Wait for tomorrow's open.**\n"
            "> Volume or confidence not sufficient for pre-close entry.\n"
        )

    # Stop adjusted notice
    stop_adj_msg = ""
    if signal.get("stop_adjusted"):
        mode_key = "DAY TRADE" if "DAY" in trade_mode else trade_mode
        pct      = MAX_STOP_PCT.get(mode_key, 0.05) * 100
        stop_adj_msg = f"\n⚙️ *Stop auto-tightened to {pct:.0f}% max for {trade_mode}.*\n"

    # Build the embed description
    desc  = f"*Triggered at {et_now.strftime('%I:%M %p ET')}*\n"
    desc += f"**{signal.get('scenario_label', '')}**\n"
    desc += f"{regime_label} · {session} · `{currency}`\n"
    desc += f"📊 **Score:** `{score}` / min `{threshold}` (+{score - threshold} above)\n"
    desc += buy_banner + stop_adj_msg

    if earnings_msg:
        desc += f"\n{earnings_msg}\n"

    desc += "\n━━━━━━━━━━━━━━━━━━━━━━━━\n"
    desc += "📊 **Trade Plan**\n"
    desc += f"• **Entry:**      `{curr_sym}{price:.2f}`\n"
    desc += f"• **Target:**     `{curr_sym}{target:.2f}` (+{tgt_pct:.1f}%) 🎯\n"
    desc += f"• **Stop:**       `{curr_sym}{stop_loss:.2f}` (−{stop_pct:.1f}%) 🛑\n"
    desc += f"• **R/R:**        `1:{rr:.2f}` ⚖️\n"
    desc += f"• **Risk/Share:** `{curr_sym}{risk_share:.2f}` | ATR `{curr_sym}{atr_val:.2f}`\n"
    desc += f"• **Size:**       `{signal.get('size_guidance', '—')}`\n"
    desc += f"• **Hold:**       {signal.get('hold_guidance', '—')}\n"

    # Scenario A: show dual stops
    if scenario == "A" and "day_stop" in signal:
        desc += f"\n📌 **Intraday Scale-Out Plan**\n"
        desc += f"• **Day Stop:**   `{curr_sym}{signal['day_stop']:.2f}` (daily ATR × {DAY_ATR_STOP_MULT})\n"
        desc += f"• **Day Target:** `{curr_sym}{signal['day_target']:.2f}` (daily ATR × {DAY_ATR_TARGET_MULT})\n"

    desc += "\n📉 **Technicals**\n"
    desc += f"• **RSI:**    `{rsi_val:.1f}` {rsi_label}\n"
    desc += f"• **21 EMA:** `{ema_str(signal.get('ema_21'))}`\n"
    if signal.get('ema_50'):
        desc += f"• **50 EMA:** `{ema_str(signal.get('ema_50'))}`\n"
    desc += f"• **VWAP:**   `{vwap_str}`\n"
    desc += f"• **Volume:** `{vol_str}`\n"

    desc += "\n📝 **Why This Signal**\n"
    for r in signal.get("reasons", []):
        desc += f"• {r}\n"

    # ── Discord embed safety: description is capped at 4096 chars ─────────────
    # Reasons are trimmed first since the trade plan above is more important.
    # Scenario A can accumulate 10+ reasons which regularly exceeds the limit.
    DISCORD_EMBED_LIMIT = 4096
    if len(desc) > DISCORD_EMBED_LIMIT:
        # Rebuild reasons section, trimming until it fits
        base_desc = desc[:desc.index("\n📝 **Why This Signal**\n")]
        base_desc += "\n📝 **Why This Signal**\n"
        for r in signal.get("reasons", []):
            candidate = base_desc + f"• {r}\n"
            if len(candidate) > DISCORD_EMBED_LIMIT - 40:  # 40 char buffer for overflow note
                base_desc += "_...additional reasons trimmed for length_\n"
                break
            base_desc = candidate
        desc = base_desc

    # Badges
    badges = []
    if signal.get("near_52w_high"):  badges.append("📈 52W HIGH ZONE")
    if signal.get("orb_high"):       badges.append("🚀 ORB BREAKOUT")
    if buy_now:                      badges.append("🔔 POWER HOUR BUY")
    if currency == "CAD":            badges.append("🍁 TSX LISTED")
    if not regime_bullish:           badges.append("⚠️ BEARISH REGIME")
    if badges:
        desc += f"\n🏷️ {' | '.join(badges)}"

    payload = {
        "content": f"🚨 **{ticker}** | Mode: **{trade_mode}** | `{currency}`",
        "embeds": [{
            "title":       f"{rating} — {ticker}  (Score {score})",
            "description": desc,
            "color":       color,
            "fields": [{"name": "🔗 Chart",
                        "value": f"[Yahoo Finance](https://finance.yahoo.com/quote/{ticker})",
                        "inline": False}],
            "footer": {"text": f"Stock Alert Bot v4.1 | ATR: {signal.get('atr_source','—')}"},
        }],
    }
    _post_discord(payload)
    print(f"   ✅ Alert sent: {ticker} | Scenario {scenario} | {trade_mode} | {currency}")


def send_premarket_summary(summaries: list[dict]):
    """Morning gap/watchlist briefing — informational only."""
    if not summaries:
        return
    et_now = datetime.now(pytz.timezone(TIMEZONE))
    lines  = [
        f"• **{s['ticker']}** "
        f"`{'CA$' if s['currency'] == 'CAD' else '$'}{s['price']:.2f}` — {s['note']}"
        for s in summaries
    ]
    payload = {"embeds": [{"title": f"📋 Pre-Market Watchlist — {et_now.strftime('%b %d, %Y')}",
        "description": "Radar items only — no trade entries yet.\n\n" +
                       "\n".join(lines) + "\n\n_Signals fire during market hours._",
        "color": COLOR_BLUE}]}
    _post_discord(payload)


def send_no_signals_notice(mode: str, count: int):
    """Confirmation that the bot ran and found nothing — prevents silent failures."""
    et_now = datetime.now(pytz.timezone(TIMEZONE))
    payload = {"embeds": [{"title": "✅ Scan Complete — No Setups",
        "description": (f"**Mode:** `{mode.upper()}` | `{et_now.strftime('%I:%M %p ET')}`\n"
                        f"**Tickers in funnel:** {count}\nNo signals met the threshold."),
        "color": COLOR_BLUE}]}
    _post_discord(payload)


# =============================================================================
#  SECTION 12 — MAIN LOOP (Three-Stage Funnel)
# =============================================================================

def check_market(mode: str, tickers_override: list | None = None):
    tz     = pytz.timezone(TIMEZONE)
    et_now = datetime.now(tz)

    print(f"\n{'='*60}")
    print(f"  Stock Alert Bot v4.1 — {et_now.strftime('%A %b %d %Y %I:%M %p ET')}")
    print(f"  Mode: {mode.upper()}")
    print(f"{'='*60}\n")

    elapsed_min = get_elapsed_minutes(et_now)
    time_penalty, penalty_reasons = get_time_penalty(et_now)

    for r in penalty_reasons:
        print(f"⚠️  {r}")

    # ── Build ticker list ─────────────────────────────────────────────────────
    if tickers_override:
        all_tickers = tickers_override
    else:
        all_tickers = TICKERS_USD + TICKERS_CAD

    # Always include VTI for regime check
    bulk_tickers = list(dict.fromkeys(['VTI'] + all_tickers))

    # ═════════════════════════════════════════════════════════════════════════
    #  STAGE 1: BULK DAILY DOWNLOAD
    # ═════════════════════════════════════════════════════════════════════════
    bulk_data = fetch_bulk_daily(bulk_tickers)
    if bulk_data is None or bulk_data.empty:
        print("❌ Bulk download failed. Aborting.")
        return

    # Regime check (extracted from bulk — no extra API call)
    regime_penalty, regime_bullish = check_market_regime(bulk_data)
    total_penalty = time_penalty + regime_penalty
    print(f"📊 Total threshold penalty: +{total_penalty} "
          f"(time +{time_penalty}, regime +{regime_penalty})\n")

    # Pre-market: just send gap summary and exit
    if mode == "premarket":
        summaries = []
        for ticker in [t for t in all_tickers if t != 'VTI']:
            try:
                df = extract_ticker_daily(bulk_data, ticker)
                if df is None or len(df) < 2: continue

                # Use live pre-market price if available; fall back to last daily close.
                # bulk daily data lags during pre-market so fast_info gives a real gap.
                prev = float(df['Close'].iloc[-1])
                try:
                    price = yf.Ticker(ticker).fast_info.get("last_price") or prev
                    price = float(price)
                except Exception:
                    price = prev

                gap_pct = (price - prev) / prev * 100
                currency = get_currency(ticker)
                if gap_pct >= 2.0:
                    note = f"📈 Gap UP {gap_pct:.1f}% — watch for continuation"
                elif gap_pct <= -2.0:
                    note = f"📉 Gap DOWN {gap_pct:.1f}% — watch for reversal"
                else:
                    note = f"Flat open ({gap_pct:+.1f}%) — no significant gap"
                summaries.append({'ticker': ticker, 'currency': currency,
                                  'price': price, 'note': note})
                print(f"   {ticker}: {note}")
            except Exception:
                pass
        send_premarket_summary(summaries)
        return

    # ═════════════════════════════════════════════════════════════════════════
    #  STAGE 2: SWING FILTER (daily data — no extra API calls)
    #  Two types advance to Stage 3:
    #    Type 1: swing_signal is valid (Scenario C or A possible)
    #    Type 2: swing_signal is None BUT daily structure is bullish enough
    #            for a pure intraday scalp (Scenario B only)
    # ═════════════════════════════════════════════════════════════════════════
    scan_tickers = [t for t in all_tickers if t != 'VTI']
    print(f"🔍 STAGE 2: Swing filter on {len(scan_tickers)} tickers...")

    # candidates: list of (ticker, swing_signal | None, day_only_eligible: bool)
    candidates = []

    for ticker in scan_tickers:
        try:
            df_daily = extract_ticker_daily(bulk_data, ticker)
            if df_daily is None:
                continue

            if not passes_liquidity_filter(df_daily, "SWING"):
                continue

            swing_signal = run_swing_engine(df_daily, total_penalty)

            if swing_signal is not None:
                candidates.append((ticker, swing_signal, False))
            else:
                # Check if basic daily structure qualifies for day-only Scenario B
                try:
                    df_tmp = df_daily.copy()
                    df_tmp['EMA_50'] = ta.ema(df_tmp['Close'], length=50)
                    df_tmp['RSI']    = ta.rsi(df_tmp['Close'], length=14)
                    df_tmp.dropna(subset=['EMA_50', 'RSI'], inplace=True)
                    if not df_tmp.empty:
                        last_close = float(df_tmp['Close'].iloc[-1])
                        last_ema50 = float(df_tmp['EMA_50'].iloc[-1])
                        last_rsi   = float(df_tmp['RSI'].iloc[-1])
                        # Bullish structure + not overbought = day-only eligible
                        if last_close > last_ema50 and last_rsi < 65:
                            if passes_liquidity_filter(df_daily, "DAY TRADE"):
                                candidates.append((ticker, None, True))
                except Exception:
                    pass

        except Exception as e:
            print(f"   ⚠️ [{ticker}] Stage 2 error: {e}")

    type1 = sum(1 for _, s, _ in candidates if s is not None)
    type2 = sum(1 for _, s, d in candidates if s is None and d)
    print(f"   ✅ {type1} swing setups + {type2} day-only = "
          f"{len(candidates)}/{len(scan_tickers)} advance to Stage 3\n")

    # ═════════════════════════════════════════════════════════════════════════
    #  STAGE 3: TARGETED INTRADAY (5m fetch for Stage 2 survivors only)
    # ═════════════════════════════════════════════════════════════════════════
    print(f"⚡ STAGE 3: Intraday analysis for {len(candidates)} tickers...\n")
    alerts_sent = 0

    for ticker, swing_signal, day_only_eligible in candidates:
        try:
            currency = get_currency(ticker)
            print(f"── {ticker} ({currency}) {'[day-only]' if day_only_eligible else ''} ──")

            # ── OPTIONAL: State machine check (disabled by default) ────────────
            # Uncomment to suppress re-alerts on active setups:
            # df_d = extract_ticker_daily(bulk_data, ticker)
            # daily_close = float(df_d['Close'].iloc[-1]) if df_d is not None else 0
            # if check_state(ticker, daily_close) in ("SUPPRESS_TRIGGERED", "SUPPRESS_INVALIDATED"):
            #     print(f"   🔒 State machine suppressed")
            #     continue

            # Targeted 5m fetch
            df_intraday = fetch_targeted_intraday(ticker)
            if df_intraday is None:
                print(f"   ⚠️ No intraday data available.")
                continue

            today_date = et_now.date()
            df_today   = df_intraday[df_intraday.index.date == today_date].copy()

            if df_today.empty:
                print(f"   ⚠️ No today bars (pre-open or market closed).")
                if swing_signal is None:
                    continue

            # Extract daily ATR for day trade stops (v4.1)
            daily_atr = None
            try:
                df_d = extract_ticker_daily(bulk_data, ticker)
                if df_d is not None and len(df_d) >= 14:
                    daily_atr_series = ta.atr(df_d['High'], df_d['Low'],
                                              df_d['Close'], length=14)
                    if daily_atr_series is not None and not daily_atr_series.empty:
                        daily_atr = float(daily_atr_series.iloc[-1])
            except Exception:
                pass

            # Day engine (now receives daily ATR for structurally meaningful stops)
            day_signal = (run_day_engine(df_today, total_penalty, daily_atr=daily_atr)
                          if not df_today.empty else None)
            print(f"   Day Engine:   " + (
                f"Score {day_signal['score']}/{day_signal['threshold']} ✅"
                if day_signal else "❌ Below threshold"
            ))

            # Enforce day-only constraint
            if day_only_eligible and day_signal is None:
                print(f"   ➖ Day-only ticker with no day signal. Skipping.")
                continue

            print(f"   Swing Engine: " + (
                f"Score {swing_signal['score']}/{swing_signal['threshold']} ✅"
                if swing_signal else "N/A (day-only eligible)"
            ))

            # Conflict gate
            if signals_conflict(day_signal, swing_signal):
                print(f"   ⚔️  Direction conflict — suppressed.")
                continue

            # Relative volume
            rel_vol = calculate_relative_volume(df_intraday)

            # Decision matrix
            final_signal = build_final_signal(day_signal, swing_signal,
                                              rel_vol, elapsed_min, mode)
            if final_signal is None:
                print(f"   ➖ No qualifying scenario.")
                continue

            print(f"   🎯 Scenario {final_signal['scenario']} | {final_signal['mode']}")

            # ── OPTIONAL: Cooldown check (disabled by default) ─────────────────
            # Uncomment to suppress repeat alerts within the cooldown window:
            # if is_on_cooldown(ticker, final_signal["mode"]):
            #     continue

            # Risk validation
            final_signal = validate_risk(final_signal, final_signal["mode"])
            if final_signal is None:
                continue

            print(f"   📊 R/R {final_signal['rr_ratio']:.2f} | "
                  f"Stop ${final_signal['stop_loss']:.2f} | "
                  f"Target ${final_signal['take_profit']:.2f}")

            # Earnings check
            has_earnings, earnings_msg = check_earnings(ticker)
            if has_earnings:
                final_signal = apply_earnings_penalty(final_signal, total_penalty)
                if final_signal is None:
                    continue
            else:
                earnings_msg = ""

            # Power hour buy recommendation
            buy_now = should_buy_now(final_signal, rel_vol, mode)

            # Fire the alert
            send_setup_alert(
                ticker=ticker, currency=currency, signal=final_signal,
                rel_vol=rel_vol, elapsed_minutes=elapsed_min, mode=mode,
                regime_bullish=regime_bullish, earnings_msg=earnings_msg,
                buy_now=buy_now
            )
            alerts_sent += 1

            # ── OPTIONAL: Write state + cooldown (disabled by default) ──────────
            # Uncomment both lines below to activate state tracking:
            # set_state(ticker, "TRIGGERED", stop_loss=final_signal["stop_loss"], mode=final_signal["mode"])
            # set_cooldown(ticker, final_signal["mode"])

        except Exception as e:
            print(f"   ❌ Error on {ticker}: {e}")
            traceback.print_exc()

    print(f"\n{'='*60}")
    print(f"  Scan complete | {alerts_sent} alert(s) sent | "
          f"{datetime.now(tz).strftime('%I:%M %p ET')}")
    print(f"{'='*60}\n")

    if alerts_sent == 0:
        send_no_signals_notice(mode, len(candidates))


# =============================================================================
#  ENTRY POINT
# =============================================================================

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Stock Alert Bot v4.1")
    parser.add_argument('--mode',
        choices=['auto', 'premarket', 'intraday', 'power_hour'],
        default='auto',
        help='Scan mode. Default: auto (detects from time of day)')
    parser.add_argument('--ticker',
        type=str, default=None,
        help='Scan a single ticker, e.g. --ticker NVDA')
    args = parser.parse_args()

    et_now = datetime.now(pytz.timezone(TIMEZONE))

    if args.mode == 'auto':
        mode = get_scan_mode(et_now)
    else:
        mode = args.mode

    if mode == "off_hours":
        print("🌙 Outside scan windows — nothing to run.")
    else:
        tickers_override = [args.ticker.upper()] if args.ticker else None
        check_market(mode=mode, tickers_override=tickers_override)
