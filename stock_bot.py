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
  python bot.py --mode swing           # Swing scan (3:00–3:45pm ET)
  python bot.py --mode swing           # Swing scan (3:00–3:45pm ET)
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
    'SPY', 'QQQM', 'QQQ', 'IWM',
    'SOXQ', 'XLY', 'GDX', 'SIL', 'XLF', 'XLK', 'SMH', 'GLD', 'SLV', 'ITB', 'VWO', 'VEA', 'KO', 'PG', 'JNJ', 'QQQM', 'SPMO', 

    # Mega Cap
    'AAPL', 'MSFT', 'GOOGL', 'AMZN', 'META',
    'JPM', 'BAC', 'XOM', 'ABBV',

    # Mid-risk
    'NVDA', 'AVGO', 'QCOM', 'MU', 'AMAT', 'LRCX',
    'NFLX', 'ORCL', 'CRM', 'NOW', 'PANW',
    'SHOP', 'UBER', 'PYPL', 'TGT', 
    'OXY', 'DVN', 'CCL', 'DKNG', 'CVX', 'TSM', 'DIS', 

    # High beta
    'TSLA', 'PLTR', 'AMD', 'ARM', 'SMCI',
    'SOFI', 'HOOD', 'COIN', 'MSTR', 'SNOW', 
]

# CAD tickers (TSX) — alerts will be tagged CA$ automatically
TICKERS_CAD = [
    'ZSP.TO', 'XEF.TO',
    'HUT.TO', 'CVE.TO', 'MFC.TO', 'ATD.TO', 'TOU.TO', 'QQC.TO',
]

# ── Discord ───────────────────────────────────────────────────────────────────
# Never hardcode your webhook URL. Set it as an environment variable instead.
# On your computer:    export DISCORD_URL="https://discord.com/api/webhooks/..."
# On GitHub Actions:   add it as a repository Secret named DISCORD_URL
DISCORD_WEBHOOK_URL = os.getenv('DISCORD_URL')

# ── Scoring thresholds ────────────────────────────────────────────────────────
SWING_SCORE_THRESHOLD = 6

# ── Swing category caps and floors ───────────────────────────────────────────
# Trend/Structure   — Max 4: EMA 21 wick/support, EMA 50 proximity,
#                             bullish structure, 52-week high
# Momentum/Reversion — Max 4: RSI level, BB lower band close, MACD
# Floors ensure true trend-pullback alignment — a stock below both EMAs
# cannot reach trend_min=3 using only "testing support" signals, preventing
# falling-knife setups from passing on oversold momentum alone.
SWING_CATEGORY_CAPS  = {"trend": 4, "momentum": 4}
SWING_CATEGORY_FLOORS = {"trend": 3, "momentum": 2}  # both must be met

# ── Liquidity gates (minimum average daily dollar volume) ─────────────────────
MIN_DOLLAR_VOLUME = {
    "SWING": 2_000_000,   # $2M avg daily dollar volume
}

# ── Risk parameters ───────────────────────────────────────────────────────────
BASE_MAX_STOP_PCT     = 0.06   # Floor — never tighter than 6% even for low-vol stocks
ABSOLUTE_MAX_STOP_PCT = 0.15   # Ceiling — never wider than 15% even for extreme high-beta
MIN_RR_RATIO          = 1.5    # Minimum acceptable risk/reward ratio

# ── Portfolio value ───────────────────────────────────────────────────────────
# Set this to your total trading capital in USD.
# Alerts will show exact dollar amounts to invest per signal.
PORTFOLIO_VALUE = 360.0   # ← Update this whenever your account size changes

SWING_ATR_STOP_MULT   = 1.5    # Swing stop = support − (ATR × 1.5) — gives room for normal noise
SWING_ATR_TARGET_MULT = 3.5    # Swing target = price + (ATR × 3.5) — wider target to maintain R/R

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
COOLDOWN_MINUTES = {"SWING": 240}

# ── Trade outcome log ─────────────────────────────────────────────────────────
# Persisted in the repo so outcomes survive between GitHub Actions runs.
# Each alert is logged on fire; outcomes are auto-checked on every subsequent run.
TRADE_LOG_FILE        = "trade_log.json"
OUTCOME_CHECK_DAYS    = 21    # Extended from 10 — swing trades need room to develop
OUTCOME_DISCORD_DAILY = True  # Send outcome summary to Discord whenever there are
                              # open positions or newly resolved trades.
                              # Set False to suppress all outcome messages.

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
    if 9.5  <= t < 16.0: return "swing"

    # Outside defined windows — default to swing scan.
    print("🌙 Outside normal scan windows — defaulting to swing mode.")
    return "swing"


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


# is_power_hour removed — swing-only bot


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
    REQUIRED_COLS = {'Open', 'High', 'Low', 'Close', 'Volume'}
    try:
        if isinstance(bulk_data.columns, pd.MultiIndex):
            if ticker not in bulk_data.columns.get_level_values(0):
                return None
            df = bulk_data[ticker].copy()
        else:
            # Single-ticker path: triggered when --ticker CLI flag is used.
            # Validate that expected OHLCV columns are present before returning —
            # without this check a shape change in yfinance would silently return
            # wrong data with no error.
            df = bulk_data.copy()
            df = _flatten(df)
            missing = REQUIRED_COLS - set(df.columns)
            if missing:
                print(f"   ⚠️ [{ticker}] Unexpected columns — missing {missing}. Skipping.")
                return None

        df = _flatten(df)
        df.dropna(subset=['Close'], inplace=True)
        return df if not df.empty else None
    except Exception as e:
        print(f"   ⚠️ [{ticker}] Extraction failed: {e}")
        return None


# fetch_targeted_intraday removed — swing-only bot uses daily bars exclusively


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
    except Exception as e:
        # Fail-open: tickers in this watchlist are established liquid names,
        # so a calculation error almost certainly reflects a data issue rather
        # than genuine illiquidity. Log the error so it's visible, then pass.
        print(f"   ⚠️ Liquidity check error — passing ticker through: {e}")
        return True


# calculate_relative_volume removed — no intraday data in swing-only bot


def calculate_daily_relative_volume(df_daily: pd.DataFrame) -> float:
    """
    Today's volume vs 20-day average volume.
    Uses iloc[-2] for the SMA baseline to exclude today from its own average.
    Returns 1.0 as a neutral fallback on any error.
    """
    try:
        if df_daily is None or len(df_daily) < 21:
            return 1.0
        vol_sma   = ta.sma(df_daily['Volume'].astype(float), length=20).iloc[-2]
        today_vol = float(df_daily['Volume'].iloc[-1])
        if pd.isna(vol_sma) or vol_sma == 0:
            return 1.0
        return round(today_vol / vol_sma, 2)
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


# Section 5 (Day Trade Engine) removed — swing-only bot


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

def run_swing_engine(df_daily: pd.DataFrame, total_penalty: int, ticker: str = "?") -> dict | None:
    """
    Scores the daily timeframe. Returns signal dict or None if score < threshold.
    """
    if df_daily is None or len(df_daily) < 50:
        print(f"   [{ticker}] ❌ Insufficient data ({len(df_daily) if df_daily is not None else 0} bars < 50)")
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
    if len(df) < 3:
        return None

    # ── Anti-repainting: score on yesterday's COMPLETED bar ───────────────────
    # iloc[-1] is today's in-progress candle — its Close, RSI, BBL etc. will
    # keep changing until 4:00pm. Scoring on it mid-session creates false signals
    # that look nothing like the final daily close.
    # iloc[-2] is yesterday's confirmed, final, never-changing bar — safe to score.
    # iloc[-1].Close is used only for the entry price (what you actually pay today).
    scored = df.iloc[-2]   # yesterday — completed bar, all signal logic
    prev   = df.iloc[-3]   # day before yesterday — MACD direction comparison
    today  = df.iloc[-1]   # today — incomplete bar, entry price only

    entry_price = float(today['Close'])   # actual price you pay at today's close
    price    = float(scored['Close'])     # yesterday's close — all scoring based on this
    ema_21   = float(scored['EMA_21'])
    ema_50   = float(scored['EMA_50'])
    rsi      = float(scored['RSI'])
    atr      = float(scored['ATR'])
    bbl      = float(scored['BBL'])      if 'BBL'      in df.columns else None
    bb_width = float(scored['BB_WIDTH']) if 'BB_WIDTH' in df.columns else 0.0
    macd_h   = float(scored['MACD_H'])   if 'MACD_H'   in df.columns else 0.0
    prev_mh  = float(prev['MACD_H'])     if 'MACD_H'   in df.columns else 0.0

    reasons = []

    # ── Category: TREND / STRUCTURE (cap at 4) ────────────────────────────────
    trend_score = 0
    trend_cap   = SWING_CATEGORY_CAPS["trend"]

    # C. 21 EMA wick detection
    daily_low   = float(scored['Low'])
    daily_close = float(scored['Close'])  # same as price — yesterday's confirmed close
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

    # ── ROC Deceleration Penalty (Malik-style trend velocity check) ───────────
    # Inspired by: rate-of-change deceleration signals a trend running out of
    # steam even when price is still above all EMAs. If the trend is slowing,
    # reduce conviction before it reverses.
    #
    # ROC_10  = velocity of the current trend (last 10 days)
    # ROC_ref = velocity 10 days ago (rolling comparison)
    # If price is in bullish structure but ROC is meaningfully decelerating:
    #   Mild deceleration  (ROC dropped > 30%): -1 point
    #   Sharp deceleration (ROC dropped > 60%): -2 points
    roc_penalty = 0
    try:
        # Use the actual datetime index to look up 10 and 20 trading days ago,
        # rather than iloc[-11] / iloc[-21] which shift unpredictably after dropna
        # removes warmup rows. This ensures ROC always compares true calendar
        # windows regardless of how many rows were dropped earlier.
        close_series = df['Close'].iloc[:-1].dropna()  # exclude today's incomplete bar
        if len(close_series) >= 21:
            today_idx   = close_series.index[-1]  # = yesterday's close
            ten_ago_idx = close_series.index[-11]   # 10 bars back (inclusive of today)
            twenty_ago_idx = close_series.index[-21]  # 20 bars back

            close_today    = float(close_series.loc[today_idx])
            close_ten_ago  = float(close_series.loc[ten_ago_idx])
            close_twenty_ago = float(close_series.loc[twenty_ago_idx])

            roc_10  = (close_today    - close_ten_ago)    / close_ten_ago    * 100
            roc_ref = (close_ten_ago  - close_twenty_ago) / close_twenty_ago * 100

            # Only penalise when price is above 50 EMA (bullish structure but decelerating)
            if price > ema_50 and roc_ref > 1.0 and roc_10 < roc_ref:
                decel_pct = (roc_ref - roc_10) / roc_ref  # how much velocity dropped
                if decel_pct >= 0.60:
                    roc_penalty = 2
                    reasons.append(
                        f"⚠️ Trend Sharply Decelerating — ROC {roc_ref:.1f}% → {roc_10:.1f}% (−{decel_pct*100:.0f}%)"
                    )
                elif decel_pct >= 0.30:
                    roc_penalty = 1
                    reasons.append(
                        f"⚠️ Trend Decelerating — ROC {roc_ref:.1f}% → {roc_10:.1f}% (−{decel_pct*100:.0f}%)"
                    )
    except Exception:
        pass

    penalty += roc_penalty

    # ── Final score ───────────────────────────────────────────────────────────
    score     = trend_score + momentum_score - penalty
    threshold = SWING_SCORE_THRESHOLD + total_penalty
    if score < threshold:
        print(f"   [{ticker}] ❌ Score {score}/{threshold} — "
              f"trend={trend_score} momentum={momentum_score} penalty=-{penalty}")
        return None

    # ── Per-category floor check ──────────────────────────────────────────────
    # Total score alone doesn't guarantee true trend-pullback alignment.
    # trend < 3 means the stock is below both EMAs — a downtrend, not a pullback.
    # momentum < 2 means there's no real oversold signal — nothing to bounce from.
    # Both floors must be met even if total score passes.
    trend_floor    = SWING_CATEGORY_FLOORS["trend"]
    momentum_floor = SWING_CATEGORY_FLOORS["momentum"]
    if trend_score < trend_floor:
        print(f"   [{ticker}] ❌ Trend score {trend_score} below floor {trend_floor} "
              f"— stock below both EMAs, possible downtrend. Rejected.")
        return None
    if momentum_score < momentum_floor:
        print(f"   [{ticker}] ❌ Momentum score {momentum_score} below floor {momentum_floor} "
              f"— insufficient oversold signal. Rejected.")
        return None

    print(f"   [{ticker}] ✅ Score {score}/{threshold} — "
          f"trend={trend_score} momentum={momentum_score} penalty=-{penalty}")
    is_bullish = price > ema_21

    # Stop/target use entry_price (today's close) — that's your actual fill.
    # Support levels come from yesterday's scored bar (structurally grounded).
    # ATR also comes from yesterday's completed bar.
    if bbl is not None and price <= bbl * 1.02:
        support = bbl
    elif near_21:
        support = ema_21
    else:
        support = ema_50

    stop_loss   = support      - (atr * SWING_ATR_STOP_MULT)
    take_profit = entry_price  + (atr * SWING_ATR_TARGET_MULT)

    if stop_loss >= entry_price:
        stop_loss = entry_price - atr

    gap_pct = (entry_price - price) / price * 100  # how much today moved vs yesterday
    print(f"   [{ticker}] 📍 Scored on yesterday ${price:.2f} | Entry today ${entry_price:.2f} "
          f"({'↑' if gap_pct >= 0 else '↓'}{abs(gap_pct):.1f}% gap)")

    # Gap filter — if today has moved more than 3% from yesterday's scored bar,
    # the support/resistance structure the signal was based on is broken.
    # A gap-up means you're chasing; a gap-down means support failed.
    # Either way the entry is structurally disconnected from the scored setup.
    MAX_ENTRY_GAP_PCT = 3.0
    if abs(gap_pct) > MAX_ENTRY_GAP_PCT:
        print(f"   [{ticker}] ❌ Gap too large ({gap_pct:+.1f}% vs {MAX_ENTRY_GAP_PCT}% max). "
              f"Entry disconnected from scored structure. Rejected.")
        return None

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
        "price": round(entry_price, 2), "is_bullish": is_bullish,
        "direction": "long",
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

# signals_conflict removed — no day engine to conflict with


def calculate_position_size(score: int, threshold: int,
                             price: float, atr: float) -> dict:
    """
    Calculates a suggested portfolio allocation % based on:
      1. Scenario  — A/B/C sets the base conviction level
      2. Score margin — how far above threshold boosts size
      3. Volatility  — ATR as % of price shrinks size on wild movers

    Returns a dict with 'pct' (float) and 'label' (display string).

    Sizing philosophy (inspired by Malik's dynamic allocation approach):
      - Never output a vague label like "Half Size" — give a concrete number
      - High volatility (ATR > 3% of price) automatically reduces exposure
      - Strong conviction (score well above threshold) increases exposure
      - Hard caps per scenario prevent over-concentration
    """
    # Base allocation by scenario
    base = 8.0  # Flat base for all swing trades

    # Conviction boost: +1% for each point above threshold, capped at +4%
    margin     = max(score - threshold, 0)
    conviction = min(margin * 1.0, 4.0)

    # Volatility adjustment: ATR as % of price
    # Low vol  (< 1.5%): no reduction
    # Mid vol  (1.5–3%): scale down proportionally
    # High vol (> 3%):   cap at 60% of base
    atr_pct = (atr / price) * 100 if price > 0 else 2.0
    if atr_pct <= 1.5:
        vol_factor = 1.0
    elif atr_pct <= 3.0:
        vol_factor = 1.0 - ((atr_pct - 1.5) / 1.5) * 0.4   # linear 1.0 → 0.6
    else:
        vol_factor = 0.6

    raw_pct = (base + conviction) * vol_factor

    # Hard cap for swing trades
    pct = round(min(raw_pct, 12.0), 1)

    # Human-readable volatility tag
    if atr_pct <= 1.5:   vol_tag = "low vol"
    elif atr_pct <= 3.0: vol_tag = "mid vol"
    else:                vol_tag = "high vol ⚠️"

    dollar_amount = round(PORTFOLIO_VALUE * pct / 100, 2)
    shares        = int(dollar_amount // price) if price > 0 else 0

    if shares < 1:
        share_note = "⚠️ < 1 share at current price — consider fractional shares"
    elif shares == 1:
        share_note = f"≈ 1 share"
    else:
        share_note = f"≈ {shares} shares"

    label = (
        f"{pct}% of portfolio = **${dollar_amount:.2f}** ({share_note}) "
        f"[score +{margin} above min · ATR {atr_pct:.1f}% · {vol_tag}]"
    )
    return {"pct": pct, "dollar": dollar_amount, "shares": shares, "label": label}


def build_final_signal(swing_signal: dict | None) -> dict | None:
    """Wraps swing signal as a Scenario C alert. Returns None if no swing signal."""
    if swing_signal is None:
        return None

    sig   = swing_signal.copy()
    pos_c = calculate_position_size(
        swing_signal["score"], swing_signal["threshold"],
        swing_signal["price"], swing_signal["atr"]
    )
    sig.update({
        "scenario":       "SWING",
        "scenario_label": "📅 SWING SETUP",
        "size_guidance":  pos_c["label"],
        "hold_guidance":  (
            "Enter before 4:00pm close. Sell at next morning open (9:30am ET). "
            "Stop active overnight — if price gaps below stop, exit immediately at open."
        ),
        "mode": "SWING",
    })
    return sig


# should_buy_now removed — no intraday volume data in swing-only bot


# =============================================================================
#  SECTION 8 — RISK VALIDATOR
#
#  Step 1: If stop is too wide, auto-tighten it to the mode's max %.
#  Step 2: Check R/R ratio meets minimum. Reject if not.
# =============================================================================

def validate_risk(signal: dict, ticker: str = "?") -> dict | None:
    """
    Validates stop width dynamically based on the stock's own ATR.
    
    Formula: dynamic_max = max(BASE, min(ATR% × 2.0, ABSOLUTE_CAP))
    
    This gives each stock room proportional to its natural daily movement:
      - Low-vol stock  (ATR 0.8%): dynamic max = max(6%, 1.6%)  = 6.0%
      - Mid-vol stock  (ATR 3.0%): dynamic max = max(6%, 6.0%)  = 6.0%
      - High-vol stock (ATR 4.5%): dynamic max = max(6%, 9.0%)  = 9.0%
      - Extreme stock  (ATR 9.0%): dynamic max = max(6%, 18%) → capped = 15.0%
    
    The 2.0× multiplier aligns with the stop formula (support - ATR × 0.8),
    giving enough buffer above it without being reckless.
    No manual per-ticker overrides needed — self-calibrates with market conditions.
    """
    price      = signal["price"]
    stop_loss  = signal["stop_loss"]
    target     = signal["take_profit"]
    atr        = signal.get("atr", 0)

    atr_pct          = atr / price if price > 0 else 0
    dynamic_max_stop = max(BASE_MAX_STOP_PCT, min(atr_pct * 2.0, ABSOLUTE_MAX_STOP_PCT))
    actual_pct       = (price - stop_loss) / price

    if actual_pct > dynamic_max_stop:
        print(f"   [{ticker}] ❌ Stop too wide "
              f"({actual_pct*100:.1f}% > {dynamic_max_stop*100:.1f}% dynamic max "
              f"[ATR {atr_pct*100:.1f}% × 2.0]). Rejected.")
        return None

    risk   = price - stop_loss
    reward = target - price

    if risk <= 0:
        print(f"   [{ticker}] ❌ Invalid stop (risk ≤ 0). Rejected.")
        return None

    rr = round(reward / risk, 2)
    signal["rr_ratio"] = rr

    if rr < MIN_RR_RATIO:
        print(f"   [{ticker}] ❌ R/R {rr:.2f} below minimum {MIN_RR_RATIO}. Rejected.")
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
    stored_threshold = signal.get("threshold", SWING_SCORE_THRESHOLD + total_penalty)
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


def send_setup_alert(ticker, currency, signal,
                     rel_vol, elapsed_minutes, mode, regime_bullish,
                     earnings_msg=""):
    """Sends a swing trade setup embed to Discord."""
    et_now     = datetime.now(pytz.timezone(TIMEZONE))
    curr_sym   = 'CA$' if currency == 'CAD' else '$'
    scenario   = signal.get("scenario", "?")
    trade_mode = signal.get("mode", "UNKNOWN")
    price      = signal["price"]
    stop_loss  = signal["stop_loss"]
    target     = signal["take_profit"]
    rr         = signal.get("rr_ratio", 0.0)
    atr_val    = signal.get("atr", 0.0)
    score      = signal.get("score", 0)
    threshold  = signal.get("threshold", 0)
    stop_pct   = (price - stop_loss) / price * 100
    tgt_pct    = (target - price)    / price * 100
    risk_share = price - stop_loss

    color  = COLOR_BLUE   # All swing alerts use blue
    rating = "📅 SWING SETUP"

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

    # Session label
    if elapsed_minutes < 20:    session = "⏰ Opening (noisy)"
    elif elapsed_minutes > 360: session = "🕒 Near Close — entry window"
    else:                       session = "✅ Normal Hours"

    regime_label = "🟢 Bullish Market" if regime_bullish else "🔴 Bearish Market"

    # Build the embed description
    desc  = f"*Triggered at {et_now.strftime('%I:%M %p ET')}*\n"
    desc += f"**{signal.get('scenario_label', '')}**\n"
    desc += f"{regime_label} · {session} · `{currency}`\n"
    desc += f"📊 **Score:** `{score}` / min `{threshold}` (+{score - threshold} above)\n"

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

    desc += "\n📉 **Technicals**\n"
    desc += f"• **RSI:**    `{rsi_val:.1f}` {rsi_label}\n"
    desc += f"• **21 EMA:** `{ema_str(signal.get('ema_21'))}`\n"
    if signal.get('ema_50'):
        desc += f"• **50 EMA:** `{ema_str(signal.get('ema_50'))}`\n"
    if rel_vol > 2.0:    vol_label = "🔥 Heavy"
    elif rel_vol > 1.2:  vol_label = "💪 Above Average"
    else:                vol_label = "😐 Below Average"
    desc += f"• **Volume:** `{rel_vol:.1f}x` 20-day avg · {vol_label}\n"

    desc += "\n📝 **Why This Signal**\n"
    for r in signal.get("reasons", []):
        desc += f"• {r}\n"

    # Trim if over Discord 4096 char embed limit
    DISCORD_EMBED_LIMIT = 4096
    if len(desc) > DISCORD_EMBED_LIMIT:
        base_desc = desc[:desc.index("\n📝 **Why This Signal**\n")]
        base_desc += "\n📝 **Why This Signal**\n"
        for r in signal.get("reasons", []):
            candidate = base_desc + f"• {r}\n"
            if len(candidate) > DISCORD_EMBED_LIMIT - 40:
                base_desc += "_...additional reasons trimmed for length_\n"
                break
            base_desc = candidate
        desc = base_desc

    # Badges
    badges = []
    if signal.get("near_52w_high"): badges.append("📈 52W HIGH ZONE")
    if not regime_bullish:          badges.append("⚠️ BEARISH REGIME")
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

    # ── Check open trade outcomes ─────────────────────────────────────────────
    # Runs on every scan using the bulk daily data already downloaded —
    # no extra API calls needed.
    print("📋 Checking open trade outcomes...")
    resolved = check_open_trades(bulk_data)

    # Only send Discord outcome summary when there is actually something to show:
    #   - Newly resolved trades (WON/LOST/EXPIRED this run), OR
    #   - Open positions that need monitoring
    # This prevents 5 identical "no change" messages per day when the bot runs
    # every 15 minutes. An empty trade log or all-closed log stays silent.
    if OUTCOME_DISCORD_DAILY:
        open_trades = [t for t in load_trade_log() if t["status"] == "OPEN"]
        if resolved or open_trades:
            send_outcome_summary(resolved, bulk_data)

    # Pre-market: just send gap summary and exit
    if mode == "premarket":
        summaries = []
        for ticker in [t for t in all_tickers if t != 'VTI']:
            try:
                df = extract_ticker_daily(bulk_data, ticker)
                if df is None or len(df) < 2: continue

                # prev_close = last completed daily session (iloc[-1])
                # prior_close = session before that (iloc[-2]) — weekend fallback
                # live_price  = fast_info pre-open quote
                #
                # Staleness is detected via the date of the last daily bar, not
                # by comparing prices. This avoids misclassifying a genuine flat
                # open (price == prev_close within 0.01%) as stale data.
                prev_close  = float(df['Close'].iloc[-1])
                prior_close = float(df['Close'].iloc[-2])

                # Check if last daily bar is from today — if not, data is stale
                # (weekend, holiday, or pre-open before first bar is published).
                last_bar_date = df.index[-1].date() if hasattr(df.index[-1], 'date') else None
                data_is_live  = (last_bar_date == et_now.date())

                try:
                    live_price = yf.Ticker(ticker).fast_info.get("last_price")
                    live_price = float(live_price) if live_price else None
                except Exception:
                    live_price = None

                if live_price and data_is_live:
                    # Live session: fast_info quote vs yesterday's completed close
                    price = live_price
                    prev  = prev_close
                elif live_price and not data_is_live:
                    # Stale daily data (weekend/holiday): fast_info quote vs the
                    # completed close before that (prev_close IS Friday's close,
                    # prior_close is Thursday's — we want Friday vs prior_close
                    # to show the true overnight/weekend gap correctly).
                    price = live_price
                    prev  = prior_close
                else:
                    # fast_info unavailable: fall back to yesterday vs day before
                    price = prev_close
                    prev  = prior_close

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
    #  Only tickers with a valid swing signal advance to Stage 3.
    #  Scenario B (day-only) removed — swing structure required for all alerts.
    # ═════════════════════════════════════════════════════════════════════════
    scan_tickers = [t for t in all_tickers if t != 'VTI']
    print(f"🔍 STAGE 2: Swing filter on {len(scan_tickers)} tickers...")

    # candidates: list of (ticker, swing_signal, day_only_eligible=False)
    candidates = []

    for ticker in scan_tickers:
        try:
            df_daily = extract_ticker_daily(bulk_data, ticker)
            if df_daily is None:
                print(f"   [{ticker}] ❌ No daily data")
                continue

            if not passes_liquidity_filter(df_daily, "SWING"):
                continue  # passes_liquidity_filter already prints reason

            swing_signal = run_swing_engine(df_daily, total_penalty, ticker=ticker)

            if swing_signal is not None:
                candidates.append((ticker, swing_signal, False))

        except Exception as e:
            print(f"   ⚠️ [{ticker}] Stage 2 error: {e}")

    print(f"   ✅ {len(candidates)} swing setups advance to Stage 3\n")

    # ═════════════════════════════════════════════════════════════════════════
    # ═════════════════════════════════════════════════════════════════════════
    print(f"⚡ STAGE 3: Signal validation for {len(candidates)} swing candidates...\n")
    alerts_sent = 0

    for ticker, swing_signal, _ in candidates:
        try:
            currency = get_currency(ticker)
            print(f"── {ticker} ({currency}) ──")
            print(f"   Swing Engine: Score {swing_signal['score']}/{swing_signal['threshold']} ✅")

            # ── OPTIONAL: State machine check (disabled by default) ────────────
            # Uncomment to suppress re-alerts on active setups:
            # df_d = extract_ticker_daily(bulk_data, ticker)
            # daily_close = float(df_d['Close'].iloc[-1]) if df_d is not None else 0
            # if check_state(ticker, daily_close) in ("SUPPRESS_TRIGGERED", "SUPPRESS_INVALIDATED"):
            #     print(f"   🔒 State machine suppressed")
            #     continue

            # Build final signal (swing only)
            final_signal = build_final_signal(swing_signal)
            if final_signal is None:
                print(f"   ➖ No qualifying signal.")
                continue

            # ── OPTIONAL: Cooldown check (disabled by default) ─────────────────
            # Uncomment to suppress repeat alerts within the cooldown window:
            # if is_on_cooldown(ticker, final_signal["mode"]):
            #     continue

            # Risk validation
            final_signal = validate_risk(final_signal, ticker=ticker)
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
                    print(f"   [{ticker}] ❌ Rejected after earnings penalty")
                    continue
            else:
                earnings_msg = ""

            # Daily relative volume — today vs 20-day average
            df_d    = extract_ticker_daily(bulk_data, ticker)
            rel_vol = calculate_daily_relative_volume(df_d) if df_d is not None else 1.0
            print(f"   📊 Rel Vol: {rel_vol:.1f}x daily average")

            # Fire the alert
            send_setup_alert(
                ticker=ticker, currency=currency, signal=final_signal,
                rel_vol=rel_vol, elapsed_minutes=elapsed_min, mode=mode,
                regime_bullish=regime_bullish, earnings_msg=earnings_msg
            )
            alerts_sent += 1

            # Log the trade for outcome tracking
            log_new_trade(ticker, currency, final_signal)

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
        send_no_signals_notice(mode, len(scan_tickers))



# =============================================================================
#  SECTION 13 — TRADE OUTCOME TRACKER
#
#  Every alert that fires is logged to trade_log.json in the repo root.
#  On each subsequent bot run, open trades are automatically checked against
#  the latest daily close to see if they hit target, stop, or are still open.
#
#  Log structure (per trade):
#    id          — unique trade ID (ticker + timestamp)
#    ticker      — stock symbol
#    scenario    — A / B / C
#    mode        — DAY TRADE / SWING / DAY TRADE + SWING
#    entry       — price at alert time
#    stop_loss   — stop level
#    take_profit — target level
#    rr_ratio    — theoretical R/R
#    score       — signal score at alert time
#    reasons     — list of signal reasons
#    alert_date  — date alert fired (YYYY-MM-DD)
#    alert_time  — time alert fired (HH:MM ET)
#    status      — OPEN / WON / LOST / EXPIRED
#    outcome_date— date outcome was determined
#    outcome_pct — % gain/loss from entry to outcome price
#    max_price   — highest price seen while open (for tracking near-misses)
#    min_price   — lowest price seen while open (for stop tracking)
# =============================================================================

def load_trade_log() -> list:
    """Loads the trade log from the repo. Returns empty list if not found."""
    try:
        p = Path(TRADE_LOG_FILE)
        if p.exists():
            with open(p) as f:
                return json.load(f)
    except Exception as e:
        print(f"⚠️ Trade log load failed: {e}")
    return []


def save_trade_log(trades: list):
    """Saves the trade log back to the repo file."""
    try:
        with open(TRADE_LOG_FILE, "w") as f:
            json.dump(trades, f, indent=2)
    except Exception as e:
        print(f"⚠️ Trade log save failed: {e}")


def log_new_trade(ticker: str, currency: str, signal: dict):
    """Appends a new alert to the trade log. Skips if an OPEN trade already exists for this ticker."""
    try:
        trades = load_trade_log()

        # Deduplication guard — if an OPEN trade already exists for this ticker,
        # don't log another one. Without this, hourly runs log the same setup
        # repeatedly with different minute timestamps, polluting the win rate stats.
        existing_open = [t for t in trades
                         if t["ticker"] == ticker and t["status"] == "OPEN"]
        if existing_open:
            print(f"   ⏭️  {ticker} already has an OPEN trade — skipping duplicate log")
            return

        tz     = pytz.timezone(TIMEZONE)
        now    = datetime.now(tz)
        # Cast all numerics to plain Python float — signal values from yfinance
        # are numpy.float64 which json.dump cannot serialize, causing a silent
        # TypeError that swallows the entire log write.
        trade  = {
            "id":           f"{ticker}_{now.strftime('%Y%m%d_%H%M')}",
            "ticker":       ticker,
            "currency":     currency,
            "scenario":     signal.get("scenario", "?"),
            "mode":         signal.get("mode", "?"),
            "entry":        float(signal["price"]),
            "stop_loss":    float(signal["stop_loss"]),
            "take_profit":  float(signal["take_profit"]),
            "rr_ratio":     float(signal.get("rr_ratio", 0)),
            "score":        int(signal.get("score", 0)),
            "threshold":    int(signal.get("threshold", 0)),
            "atr":          float(signal.get("atr", 0)),
            "reasons":      [str(r) for r in signal.get("reasons", [])],
            "alert_date":   now.strftime("%Y-%m-%d"),
            "alert_time":   now.strftime("%H:%M ET"),
            "status":       "OPEN",
            "outcome_date": None,
            "outcome_pct":  None,
            "max_price":    float(signal["price"]),
            "min_price":    float(signal["price"]),
        }
        trades.append(trade)
        save_trade_log(trades)
        print(f"   📝 Trade logged: {trade['id']}")
    except Exception as e:
        print(f"   ⚠️ Trade log error: {e}")


def check_open_trades(bulk_data) -> list:
    """
    Checks all OPEN trades against the latest daily close.
    Marks each as WON, LOST, or EXPIRED if past OUTCOME_CHECK_DAYS.
    Returns list of newly resolved trades for the Discord summary.
    """
    trades   = load_trade_log()
    resolved = []
    tz       = pytz.timezone(TIMEZONE)
    today    = datetime.now(tz).date()
    changed  = False

    for trade in trades:
        if trade["status"] != "OPEN":
            continue

        try:
            alert_date = datetime.strptime(trade["alert_date"], "%Y-%m-%d").date()
            days_open  = (today - alert_date).days

            # Expire trades older than OUTCOME_CHECK_DAYS
            if days_open > OUTCOME_CHECK_DAYS:
                trade["status"]       = "EXPIRED"
                trade["outcome_date"] = str(today)
                trade["outcome_pct"]  = round(
                    (trade["max_price"] - trade["entry"]) / trade["entry"] * 100, 2
                )
                resolved.append(trade)
                changed = True
                print(f"   ⏰ {trade['ticker']} EXPIRED after {days_open} days "
                      f"(max reached: ${trade['max_price']:.2f})")
                continue

            # Get latest price from bulk data
            df = extract_ticker_daily(bulk_data, trade["ticker"])
            if df is None or df.empty:
                continue

            latest_high  = float(df['High'].iloc[-1])
            latest_low   = float(df['Low'].iloc[-1])
            latest_close = float(df['Close'].iloc[-1])

            # Update high/low watermarks — explicit float() to avoid numpy types
            trade["max_price"] = float(max(trade["max_price"], latest_high))
            trade["min_price"] = float(min(trade["min_price"], latest_low))
            changed = True

            entry  = trade["entry"]
            target = trade["take_profit"]
            stop   = trade["stop_loss"]

            # Check if both stop AND target were hit on the same daily bar.
            # Using daily OHLCV we cannot know which happened first intraday.
            # Marking as LOST in this case creates a systematic negative bias
            # on win rate — flag as AMBIGUOUS instead so it doesn't skew stats.
            both_hit = (latest_low <= stop) and (latest_high >= target)
            if both_hit:
                trade["status"]       = "AMBIGUOUS"
                trade["outcome_date"] = str(today)
                trade["outcome_pct"]  = 0.0
                resolved.append(trade)
                print(f"   ⚠️  {trade['ticker']} AMBIGUOUS — both stop ${stop:.2f} "
                      f"and target ${target:.2f} hit on same bar. Cannot determine order.")

            # Check stop hit (using daily low)
            elif latest_low <= stop:
                trade["status"]       = "LOST"
                trade["outcome_date"] = str(today)
                trade["outcome_pct"]  = round((stop - entry) / entry * 100, 2)
                resolved.append(trade)
                print(f"   🛑 {trade['ticker']} LOST — stop ${stop:.2f} hit "
                      f"(low: ${latest_low:.2f})")

            # Check target hit (using daily high)
            elif latest_high >= target:
                trade["status"]       = "WON"
                trade["outcome_date"] = str(today)
                trade["outcome_pct"]  = round((target - entry) / entry * 100, 2)
                resolved.append(trade)
                print(f"   ✅ {trade['ticker']} WON — target ${target:.2f} hit "
                      f"(high: ${latest_high:.2f})")

            else:
                pct_to_target = (target - latest_close) / entry * 100
                pct_to_stop   = (latest_close - stop) / entry * 100
                print(f"   📊 {trade['ticker']} OPEN — "
                      f"close ${latest_close:.2f} | "
                      f"+{pct_to_target:.1f}% to target | "
                      f"-{pct_to_stop:.1f}% to stop")

        except Exception as e:
            print(f"   ⚠️ Outcome check error on {trade['ticker']}: {e}")

    if changed:
        save_trade_log(trades)

    return resolved


def send_outcome_summary(resolved: list, bulk_data):
    """Sends a Discord embed showing open positions and newly resolved trades."""
    try:
        trades    = load_trade_log()
        open_tr   = [t for t in trades if t["status"] == "OPEN"]
        won_tr    = [t for t in trades if t["status"] == "WON"]
        lost_tr   = [t for t in trades if t["status"] == "LOST"]
        expired   = [t for t in trades if t["status"] == "EXPIRED"]
        ambiguous = [t for t in trades if t["status"] == "AMBIGUOUS"]

        # AMBIGUOUS trades excluded from win rate — both stop and target hit
        # on the same daily bar so we can't determine actual outcome
        total_closed = len(won_tr) + len(lost_tr)
        win_rate     = (len(won_tr) / total_closed * 100) if total_closed > 0 else 0

        avg_win  = (sum(t["outcome_pct"] for t in won_tr)  / len(won_tr))  if won_tr  else 0
        avg_loss = (sum(t["outcome_pct"] for t in lost_tr) / len(lost_tr)) if lost_tr else 0

        desc  = f"**Overall Win Rate:** `{win_rate:.0f}%` "
        desc += f"({len(won_tr)}W / {len(lost_tr)}L / {len(expired)} expired"
        desc += f" / {len(ambiguous)} ambiguous)\n" if ambiguous else ")\n"
        desc += f"**Avg Win:** `+{avg_win:.1f}%` | **Avg Loss:** `{avg_loss:.1f}%`\n"

        if open_tr:
            desc += f"\n📂 **Open Positions ({len(open_tr)})**\n"
            for t in open_tr[-8:]:   # Show last 8 to avoid embed limits
                days = (datetime.now(pytz.timezone(TIMEZONE)).date() -
                        datetime.strptime(t["alert_date"], "%Y-%m-%d").date()).days
                desc += (f"• **{t['ticker']}** {t['scenario']} — "
                         f"Entry ${t['entry']:.2f} | "
                         f"Target ${t['take_profit']:.2f} | "
                         f"Stop ${t['stop_loss']:.2f} | "
                         f"Day {days}\n")

        if resolved:
            desc += f"\n🔔 **Just Resolved ({len(resolved)})**\n"
            for t in resolved:
                icon = "✅" if t["status"] == "WON" else ("🛑" if t["status"] == "LOST" else "⏰")
                desc += (f"• {icon} **{t['ticker']}** {t['scenario']} — "
                         f"{t['status']} `{t['outcome_pct']:+.1f}%` "
                         f"(entry ${t['entry']:.2f})\n")



        payload = {"embeds": [{
            "title":       "📈 Trade Outcome Tracker",
            "description": desc[:4096],
            "color":       5763719 if win_rate >= 50 else 15548997,
            "footer":      {"text": f"Stock Alert Bot v4.1 | {len(trades)} total trades logged"},
        }]}
        _post_discord(payload)
        print(f"   📊 Outcome summary sent ({len(open_tr)} open, {len(resolved)} resolved)")

    except Exception as e:
        print(f"⚠️ Outcome summary error: {e}")

# =============================================================================
#  ENTRY POINT
# =============================================================================

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Stock Alert Bot v4.1")
    parser.add_argument('--mode',
        choices=['auto', 'premarket', 'swing'],
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

    tickers_override = [args.ticker.upper()] if args.ticker else None
    check_market(mode=mode, tickers_override=tickers_override)
