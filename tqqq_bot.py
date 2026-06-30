"""
=============================================================================
  TQQQ ALERT BOT v6.2 — Manual Trading Edition
=============================================================================

WHAT THIS BOT DOES:
  Scans TQQQ every 15 minutes during market hours (9:30am–4:00pm ET).
  Sends Discord buy alerts when signal conditions are met.
  Sends Discord sell alerts based on live technicals (RSI, EMA, MACD).
  You execute all trades manually on IBKR — no orders are placed automatically.
  Runs automatically via GitHub Actions — no server needed.

BUY SIGNAL LOGIC:
  Tier 1 — High Conviction (deploy TIER1_POSITION_PCT = 10%)
    Path A: RSI(14) < 35 — deep oversold bypass
    Path D: Pivot low reversal (higher low + green close + RSI turning up)

  Tier 2 — Medium Conviction (deploy TIER2_POSITION_PCT = 5%)
    Path E: 21 EMA Bounce — price wicks 21 EMA and closes green, RSI 40–65
    Path F: MACD Cross — MACD line crosses above signal line below zero

  All paths: RSI and ATR calculated on daily bars.
  Alerts fire every 15 min while conditions hold.
  First alert labelled "NEW SIGNAL", subsequent ones "STILL ACTIVE".

SELL SIGNAL LOGIC (price-agnostic — no entry price needed):
  RSI_OB    — RSI > 70: momentum exhausted
  RSI_CROSS — RSI crosses above 55 from below: recovery complete
  EMA_LOSS  — Price drops below 21 EMA after being above it
  MACD_PEAK — MACD histogram peaks and turns down at highs

HOW TO RUN MANUALLY:
  python tqqq_bot.py                 # Auto-detects mode from time of day
  python tqqq_bot.py --mode swing    # Force swing scan
  python tqqq_bot.py --force         # Override market-closed gate (testing)
  python tqqq_bot.py --dry-run       # Skip duplicate guard (safe for testing)
  python tqqq_bot.py --ticker TQQQ   # Scan single ticker

ARCHITECTURE:
  Stage 1: Bulk daily data download — VTI (regime) + TQQQ (1 API call)
  Stage 2: Sell alert check — live price via yfinance fast_info
  Stage 3: Buy signal scoring on daily bars
  Stage 4: Outcome tracking — open trades checked against daily closes

HOLD PERIODS:
  Tier 1: 10 trading days
  Tier 2: 5 trading days
=============================================================================
"""

import os
import sys
import json
import time
import argparse
import traceback
import pandas as pd
import ta as ta_lib   # replaces pandas_ta — no numpy/pandas version conflicts
import pytz
import requests
import yfinance as yf

import tempfile
from datetime import datetime
from pathlib import Path

try:
    import mplfinance as mpf
    import matplotlib
    matplotlib.use("Agg")   # headless — no display needed
    import matplotlib.pyplot as plt
    CHART_AVAILABLE = True
except ImportError:
    CHART_AVAILABLE = False
    print("⚠️  mplfinance not installed — chart images disabled. Run: pip install mplfinance")

# Alpaca removed — bot uses manual IBKR execution based on Discord alerts.
# Re-add alpaca-py imports and get_alpaca_client() from git history if reverting.


# =============================================================================
#  SECTION 1 — CONFIGURATION
#  ✏️  Edit this section to customize the bot for your needs.
# =============================================================================

# ── Your watchlist ────────────────────────────────────────────────────────────
# TQQQ-only bot | Hold: 10 days | RSI(14) < 32 entry | Updated: 2026-05-18
# VTI is kept for market regime check only — it is never traded.
# TQQQ: 3x leveraged Nasdaq-100 ETF — high ATR, no earnings, no 200 EMA gate.
TICKERS_USD = [
    'VTI',    # Market regime check only — NOT traded
    'TQQQ',   # Primary instrument — 3x leveraged Nasdaq-100
]

# CAD tickers (TSX) — cleared for TQQQ-only bot
TICKERS_CAD = []

# ── Discord ───────────────────────────────────────────────────────────────────
# Never hardcode your webhook URL. Set it as an environment variable instead.
# On your computer:    export DISCORD_URL="https://discord.com/api/webhooks/..."
# On GitHub Actions:   add it as a repository Secret named DISCORD_URL
DISCORD_WEBHOOK_URL = os.getenv('DISCORD_URL')

# =============================================================================
#  TWO-TIER SIGNAL SYSTEM
# =============================================================================
#
#  TIER 1 — HIGH CONVICTION (deploy more capital, ~2-3x per year)
#    Path A: RSI(14) < 35 — deep oversold, multi-day selloff
#    Path D: RSI 35–50 + pivot low confirmation
#    Expected win rate: ~75-80% | Hold: 10 days
#
#  TIER 2 — MEDIUM CONVICTION (deploy less capital, ~1x per week)
#    Path E: EMA Bounce — price touches/wicks 21 EMA and closes green
#    Path F: MACD Cross — MACD crosses above signal line below zero
#    Expected win rate: ~55-65% | Hold: 5 days (shorter — less conviction)
#
#  Discord alert labels each tier clearly so you always know what you're
#  acting on and can size accordingly.
#
#  POSITION SIZING GUIDE (set your own amounts in PORTFOLIO_VALUE):
#    Tier 1 signal → deploy TIER1_POSITION_PCT % of portfolio
#    Tier 2 signal → deploy TIER2_POSITION_PCT % of portfolio
# =============================================================================

TIER1_POSITION_PCT = 10.0   # % of portfolio per Tier 1 (high conviction) signal
TIER2_POSITION_PCT = 5.0    # % of portfolio per Tier 2 (medium conviction) signal

# Tier 2 hold period — shorter than Tier 1, less conviction
TIER2_HOLD_DAYS = 5

# Tier 2 ATR multipliers — tighter stop, same target (better R/R to compensate
# for lower win rate vs Tier 1)
TIER2_ATR_STOP_MULT   = 2.0   # tighter stop than Tier 1 (2.5x)
TIER2_ATR_TARGET_MULT = 4.0   # wider target than Tier 1 (3.5x) — R/R = 2.0

# Alert cooldown — prevents same signal type re-alerting every 15 min
# Tier 1 cooldown is longer — RSI < 35 can persist for days, don't spam
# Tier 2 cooldown is shorter — conditions are more transient
TIER1_ALERT_COOLDOWN_HOURS = 6    # re-alert if Tier 1 conditions still met after 6h
TIER2_ALERT_COOLDOWN_HOURS = 4    # re-alert if Tier 2 conditions still met after 4h
TIER_COOLDOWN_FILE = "/tmp/tier_alert_cooldowns.json"

# ── Scoring thresholds ────────────────────────────────────────────────────────
SWING_SCORE_THRESHOLD = 6

# ── Portfolio Manager Constants ───────────────────────────────────────────────
# Option B: cap new trades per run to avoid over-deploying on cluster signals
MAX_TRADES_PER_DAY    = 3

# Option A: path priority for ranking (lower = higher priority)
# Path A (deep capitulation) > Path D (pivot low) > Path B (standard)
PATH_PRIORITY = {"A": 0, "D": 1, "B": 2}

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
BASE_MAX_STOP_PCT     = 0.10   # Floor raised to 10% for TQQQ (3x leveraged ETF — 6% is too tight;
                               # TQQQ daily ATR is typically 4-7%, so 2.5x ATR stop needs room)
ABSOLUTE_MAX_STOP_PCT = 0.15   # Ceiling — never wider than 15% even for extreme high-beta
MIN_RR_RATIO          = 1.1    # Lowered from 1.5 — structural stops widen on valid setups

# ── Portfolio value ───────────────────────────────────────────────────────────
# Set this to your total trading capital in USD.
# Alerts will show exact dollar amounts to invest per signal.
PORTFOLIO_VALUE = 10000.0  # ← Update this whenever your account size changes

SWING_ATR_STOP_MULT   = 2.5    # TQQQ locked param: stop = support - (ATR x 2.5)
SWING_ATR_TARGET_MULT = 3.5    # TQQQ locked param: target = entry + (ATR x 3.5) — R/R = 1.40

# ── Volume thresholds ─────────────────────────────────────────────────────────
VOLUME_STRONG   = 2.0    # 2x relative volume = strong institutional activity
VOLUME_MODERATE = 1.2    # 1.2x = moderate interest

# ── Entry window: signals only fire in this window ───────────────────────────
# Signals fire from 12:30pm ET onwards — morning gap noise (9:30-12:30) is
# excluded since the daily candle is too immature. Afternoon setups from
# 12:30pm are valid; near-close (3:30-4pm) signals are the highest quality.
ENTRY_WINDOW_START_MIN = 180   # 180 min after 9:30am open = 12:30pm ET
ENTRY_WINDOW_END_MIN   = 390   # 390 min = 4:00pm ET (market close)

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
# /tmp files: survive within a single GitHub Actions run only (ephemeral runner).
# Cooldown is effectively per-session only — resets between cron runs.
# Not a problem in practice: entry window gate (3:30-4:00pm) limits signals to
# one window per day, so same-ticker repeat alerts within a day are rare.
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
EARNINGS_CACHE_FILE   = "earnings_cache.json"
OUTCOME_CHECK_DAYS    = 12    # Days before marking a trade EXPIRED if not resolved.
                              # Set to 2 days beyond Tier 1 hold (10 days) to give
                              # the trade room to resolve naturally before expiring.
                              # Tier 2 (5-day hold) will always resolve well before this.
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

    # Outside market hours — return None to signal the main loop to exit.
    print(f"🌙 Outside market hours ({et_now.strftime('%H:%M')} ET) — skipping scan.")
    return "closed"


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

    # Opening noise penalty removed — swing score is based on yesterday's
    # completed bar, not today's intraday action. Gap filter handles open risk.

    # Late Friday penalty removed — redundant for swing trading.
    # Bracket orders (stop + target) sit on the broker over the weekend
    # unattended regardless of entry day. The 2.5x ATR stop already absorbs
    # weekend gaps (avg 0.3-0.8% vs stop of 7-10%). Earnings within 7 days
    # is already caught by apply_earnings_penalty().

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
            tickers, period="2y", interval="1d",
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
            # Detect which level contains tickers — yfinance reversed the
            # MultiIndex order in v0.2.36 from (Ticker, Price) to (Price, Ticker)
            # so we can't hardcode level 0. Mirror the OHLCV detection in _flatten().
            ticker_level = None
            for i in range(bulk_data.columns.nlevels):
                if ticker in bulk_data.columns.get_level_values(i):
                    ticker_level = i
                    break
            if ticker_level is None:
                return None
            df = bulk_data.xs(ticker, level=ticker_level, axis=1).copy()
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
        vol_sma   = ta_lib.trend.SMAIndicator(df_daily['Volume'].astype(float), window=20).sma_indicator().iloc[-2]
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

        vti_sma   = ta_lib.trend.SMAIndicator(vti_df['Close'], window=200).sma_indicator().iloc[-1]
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
#  v6.0: Scoring uses category caps to decorrelate signals.
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

    df['EMA_21']  = ta_lib.trend.EMAIndicator(df['Close'], window=21).ema_indicator()
    df['EMA_50']  = ta_lib.trend.EMAIndicator(df['Close'], window=50).ema_indicator()
    df['EMA_200'] = ta_lib.trend.EMAIndicator(df['Close'], window=200).ema_indicator()
    df['RSI']     = ta_lib.momentum.RSIIndicator(df['Close'], window=14).rsi()
    df['ATR']     = ta_lib.volatility.AverageTrueRange(df['High'], df['Low'], df['Close'], window=14).average_true_range()

    try:
        _macd = ta_lib.trend.MACD(df['Close'])
        df['MACD_H'] = _macd.macd_diff()
        df['MACD']   = _macd.macd()
        df['MACDs']  = _macd.macd_signal()
    except Exception:
        pass

    # Write indicators back to df_daily so generate_signal_chart can access
    # RSI/EMA columns via the df_d reference passed to send_setup_alert.
    # Without this, df_daily has no RSI/EMA and chart generation fails with KeyError.
    for col in ['EMA_21', 'EMA_50', 'EMA_200', 'RSI', 'ATR', 'MACD_H']:
        if col in df.columns:
            df_daily[col] = df[col]

    try:
        _bb = ta_lib.volatility.BollingerBands(df['Close'], window=20, window_dev=2)
        df['BBL']      = _bb.bollinger_lband()
        df['BBM']      = _bb.bollinger_mavg()
        df['BBU']      = _bb.bollinger_hband()
        df['BB_WIDTH'] = _bb.bollinger_wband() / 100  # ta returns %, normalize to ratio
    except Exception:
        pass

    df.dropna(subset=['RSI', 'EMA_50', 'ATR'], inplace=True)
    if len(df) < 3:
        return None

    # ── Score on TODAY's candle (iloc[-1]) at 3:45pm ─────────────────────────
    # Since the bot fires a market order at 3:45pm (fills immediately), we score
    # on today's in-progress candle. The entry price IS today's current price,
    # so scoring on yesterday (iloc[-2]) would be one day stale.
    # Key benefits:
    #   - Bullish Engulfing: today engulfing yesterday is the live pattern
    #   - Hammer: buyers defending support RIGHT NOW as we enter
    #   - RSI/EMA: most current values at time of fill
    scored = df.iloc[-1]   # today — current bar at 3:45pm, all signal logic
    prev   = df.iloc[-2]   # yesterday — completed bar, MACD/engulfing comparison

    entry_price = float(scored['Close'])  # today's current price = actual fill price
    price    = float(scored['Close'])     # same — scoring and entry on same bar
    ema_21   = float(scored['EMA_21'])
    ema_50   = float(scored['EMA_50'])
    ema_200  = float(scored['EMA_200']) if 'EMA_200' in df.columns and not pd.isna(scored.get('EMA_200', float('nan'))) else None
    rsi      = float(scored['RSI'])
    atr      = float(scored['ATR'])
    bbl      = float(scored['BBL'])      if 'BBL'      in df.columns else None
    bb_width = float(scored['BB_WIDTH']) if 'BB_WIDTH' in df.columns else 0.0
    macd_h   = float(scored['MACD_H'])   if 'MACD_H'   in df.columns else 0.0
    prev_mh  = float(prev['MACD_H'])     if 'MACD_H'   in df.columns else 0.0

    # OHLC of scored bar — used for candlestick pattern detection
    bar_open  = float(scored['Open'])
    bar_high  = float(scored['High'])
    bar_low   = float(scored['Low'])
    bar_close = float(scored['Close'])   # same as `price` — named explicitly for clarity
    prev_open = float(prev['Open'])
    prev_close= float(prev['Close'])

    reasons = []

    # ── PATH A — DEEP OVERSOLD BYPASS ────────────────────────────────────────
    # Checked FIRST — before the structural gate below.
    # TQQQ-specific: 200 EMA gate REMOVED. For individual stocks, price > 200 EMA
    # confirms a long-term uptrend. For TQQQ (3x leveraged ETF), it routinely
    # drops below the 200 EMA during the deep corrections that are our BEST entry
    # points. Requiring price > 200 EMA would silently block the highest-conviction
    # TQQQ setups. RSI < 32 alone is the structural anchor here.
    if rsi < 35:
        trend_score     = 3
        momentum_score  = 3
        score           = trend_score + momentum_score
        threshold       = SWING_SCORE_THRESHOLD + total_penalty
        oversold_bypass = True
        daily_low       = float(scored['Low'])
        daily_close     = float(scored['Close'])
        is_bullish      = price > ema_21
        high_52w        = float(df['High'].tail(252).max())
        near_52w_high   = price >= high_52w * 0.98
        # Support anchor: use 200 EMA if available and below price, else fall back
        # to 50 EMA, then 21 EMA. For TQQQ below all EMAs, use volatility stop.
        if ema_200 is not None and price > ema_200:
            support        = ema_200
            support_source = "200 EMA"
        elif price > ema_50:
            support        = ema_50
            support_source = "50 EMA"
        elif price > ema_21:
            support        = ema_21
            support_source = "21 EMA"
        else:
            support        = entry_price   # volatility stop — TQQQ below all EMAs
            support_source = "Volatility Stop (below all EMAs)"
        stop_loss       = support - (atr * SWING_ATR_STOP_MULT)
        if stop_loss >= entry_price:
            stop_loss = entry_price - atr
        take_profit = entry_price + (atr * SWING_ATR_TARGET_MULT)
        rr_ratio = 0.0  # placeholder — validate_risk overwrites with ATR ratio (3.5/2.5=1.40)
        reasons = [
            f"💎 Deep Oversold Bounce — RSI {rsi:.1f} (TQQQ Path A)",
            f"📍 Support anchor: {support_source} ${support:.2f}",
        ]
        if ema_200 is not None:
            ema200_rel = "above" if price > ema_200 else "below"
            reasons.append(f"📊 200 EMA ${ema_200:.2f} ({ema200_rel} — informational)")
        print(f"   [{ticker}] ✅ PATH A — Deep Oversold Bypass "
              f"RSI={rsi:.1f} support={support_source} ${support:.2f}")
        return {
            "price": entry_price, "entry_price": entry_price,
            "stop_loss": round(stop_loss, 2), "take_profit": round(take_profit, 2),
            "rr_ratio": round(rr_ratio, 2), "score": score, "threshold": threshold,
            "trend_score": trend_score, "momentum_score": momentum_score,
            "oversold_bypass": oversold_bypass, "atr": round(atr, 4), "atr_source": "Daily",
            "ema_21": round(ema_21, 2), "ema_50": round(ema_50, 2),
            "ema_200": round(ema_200, 2) if ema_200 else None,
            "rsi": round(rsi, 1), "bbl": round(bbl, 2) if bbl else None,
            "bb_width": round(bb_width, 4), "macd_h": round(macd_h, 4),
            "support": round(support, 2), "support_source": support_source,
            "is_bullish": is_bullish, "near_52w_high": near_52w_high,
            "mode": "SWING", "reasons": reasons, "path": "A",
            "tier": 1, "hold_days": 10, "position_size_pct": TIER1_POSITION_PCT,
            "vwap": None, "gap_pct": 0.0, "bb_squeeze_warning": False,
        }

    # ── PATH D — DAY 2 REVERSAL / PIVOT LOW ──────────────────────────────────
    # Fires in the "dead zone" (below both 21 & 50 EMA) but only AFTER the
    # market proves the bottom exists via price confirmation on today's bar:
    #   - Today's low > yesterday's low  (sellers couldn't push lower)
    #   - Today closes green (close > open) (buyers stepped in intraday)
    #   - RSI < 50 AND rising vs yesterday (momentum turning, not drifting)
    # RSI must be 35-50: below 35 is Path A territory (better expectancy there).
    # TQQQ: 200 EMA gate REMOVED — same rationale as Path A. TQQQ can be below
    # 200 EMA for extended periods; pivot low confirmation is sufficient.
    # Stop anchored to yesterday's low — the market drew the bottom, not a formula.
    if (
        price < ema_21 and price < ema_50 and     # dead zone
        rsi >= 35 and rsi < 50 and                # not deep oversold (Path A handles that)
        bar_low > float(prev['Low']) and          # higher low vs yesterday
        bar_close > bar_open and                  # green close today
        rsi > float(prev['RSI']) if not pd.isna(prev.get('RSI', float('nan'))) else False
    ):
        trend_score     = 3
        momentum_score  = 3
        score           = trend_score + momentum_score
        threshold       = SWING_SCORE_THRESHOLD + total_penalty
        oversold_bypass = False
        is_bullish      = False
        high_52w        = float(df['High'].tail(252).max())
        near_52w_high   = price >= high_52w * 0.98
        prev_low        = float(prev['Low'])
        support         = prev_low
        support_source  = "Prior Day Pivot Low (Path D)"
        stop_loss       = support - (atr * SWING_ATR_STOP_MULT)
        if stop_loss >= entry_price:
            stop_loss = entry_price - atr
        take_profit = entry_price + (atr * SWING_ATR_TARGET_MULT)
        rr_ratio = 0.0  # placeholder — validate_risk overwrites with ATR ratio
        reasons = [
            f"🔄 Path D — Day 2 Reversal (TQQQ)",
            f"📈 Higher Low (${bar_low:.2f} > ${prev_low:.2f}) + Green Close",
            f"📉 RSI Turning Up {float(prev['RSI']):.1f} -> {rsi:.1f} (oversold zone)",
        ]
        if ema_200 is not None:
            ema200_rel = "above" if price > ema_200 else "below"
            reasons.append(f"📊 200 EMA ${ema_200:.2f} ({ema200_rel} — informational)")
        print(f"   [{ticker}] ✅ PATH D — Day 2 Reversal "
              f"RSI={rsi:.1f} HL=${bar_low:.2f}>${prev_low:.2f}")
        return {
            "price": entry_price, "entry_price": entry_price,
            "stop_loss": round(stop_loss, 2), "take_profit": round(take_profit, 2),
            "rr_ratio": round(rr_ratio, 2), "score": score, "threshold": threshold,
            "trend_score": trend_score, "momentum_score": momentum_score,
            "oversold_bypass": oversold_bypass, "atr": round(atr, 4), "atr_source": "Daily",
            "ema_21": round(ema_21, 2), "ema_50": round(ema_50, 2),
            "ema_200": round(ema_200, 2) if ema_200 else None,
            "rsi": round(rsi, 1), "bbl": round(bbl, 2) if bbl else None,
            "bb_width": round(bb_width, 4), "macd_h": round(macd_h, 4),
            "support": round(support, 2), "support_source": support_source,
            "is_bullish": is_bullish, "near_52w_high": near_52w_high,
            "mode": "SWING", "reasons": reasons, "path": "D",
            "tier": 1, "hold_days": 10, "position_size_pct": TIER1_POSITION_PCT,
            "vwap": None, "gap_pct": 0.0, "bb_squeeze_warning": False,
        }

    # ── PATH E — EMA BOUNCE (TIER 2) ─────────────────────────────────────────
    # Medium-conviction setup. Fires when TQQQ pulls back to its 21 EMA and
    # shows a live rejection — price wicks down to the EMA but closes above it,
    # signalling buyers defending the level in real time.
    #
    # Conditions:
    #   - Today's low touches or comes within 1.5% of 21 EMA (the wick)
    #   - Today closes ABOVE 21 EMA (rejection confirmed)
    #   - Today closes green (close > open — buyers won intraday)
    #   - RSI between 35–65: not deep oversold (Path A/D handles that),
    #     not overbought (no mean-reversion edge above 65)
    #   - Price above 50 EMA: broad uptrend intact, this is a pullback not collapse
    #
    # Stop: below the wick low (yesterday's low) — market drew the line there
    # Target: TIER2_ATR_TARGET_MULT x ATR (wider than Tier 1 to compensate for
    #         lower win rate — needs good R/R to be positive expectancy)
    # Hold:  TIER2_HOLD_DAYS (5 days — shorter than Tier 1's 10 days)
    ema_bounce_wick    = bar_low <= ema_21 * 1.015    # low touched within 1.5% of 21 EMA
    ema_bounce_close   = bar_close > ema_21            # closed above 21 EMA
    ema_bounce_green   = bar_close > bar_open          # green candle
    ema_bounce_rsi     = 35 <= rsi <= 65               # not extreme
    ema_bounce_trend   = price > ema_50                # above 50 EMA — uptrend intact

    if ema_bounce_wick and ema_bounce_close and ema_bounce_green and ema_bounce_rsi and ema_bounce_trend:
        support        = min(bar_low, float(prev['Low']))   # wick low as stop anchor
        support_source = "21 EMA Bounce Wick (Path E)"
        stop_loss      = support - (atr * TIER2_ATR_STOP_MULT)
        if stop_loss >= entry_price:
            stop_loss = entry_price - atr
        take_profit    = entry_price + (atr * TIER2_ATR_TARGET_MULT)
        rr_ratio       = 0.0
        high_52w       = float(df['High'].tail(252).max())
        near_52w_high  = price >= high_52w * 0.98
        tier2_reasons  = [
            f"📊 Path E — 21 EMA Bounce (TIER 2 Medium Conviction)",
            f"🕯️ Wick to EMA ${ema_21:.2f} (Low ${bar_low:.2f}) + Green Close",
            f"📈 Price above 50 EMA ${ema_50:.2f} — uptrend intact",
            f"📉 RSI {rsi:.1f} — momentum neutral, room to run",
            f"⚠️ TIER 2: Deploy {TIER2_POSITION_PCT:.0f}% of portfolio (smaller size)",
        ]
        print(f"   [{ticker}] ✅ PATH E — EMA Bounce (Tier 2) "
              f"RSI={rsi:.1f} wick=${bar_low:.2f} ema21=${ema_21:.2f}")
        return {
            "price": entry_price, "entry_price": entry_price,
            "stop_loss": round(stop_loss, 2), "take_profit": round(take_profit, 2),
            "rr_ratio": round(rr_ratio, 2), "score": 4, "threshold": 6,
            "trend_score": 2, "momentum_score": 2,
            "oversold_bypass": False, "atr": round(atr, 4), "atr_source": "Daily",
            "ema_21": round(ema_21, 2), "ema_50": round(ema_50, 2),
            "ema_200": round(ema_200, 2) if ema_200 else None,
            "rsi": round(rsi, 1), "bbl": round(bbl, 2) if bbl else None,
            "bb_width": round(bb_width, 4), "macd_h": round(macd_h, 4),
            "support": round(support, 2), "support_source": support_source,
            "is_bullish": True, "near_52w_high": near_52w_high,
            "mode": "SWING", "reasons": tier2_reasons, "path": "E",
            "tier": 2, "hold_days": TIER2_HOLD_DAYS,
            "position_size_pct": TIER2_POSITION_PCT,
            "vwap": None, "gap_pct": 0.0, "bb_squeeze_warning": False,
        }

    # ── PATH F — MACD CROSS (TIER 2) ─────────────────────────────────────────
    # Medium-conviction setup. Fires when momentum shifts from negative to
    # positive — MACD histogram crosses zero while both lines still below zero.
    # The "below zero" requirement catches early recovery from a real pullback,
    # not a late-stage cross that often precedes a fade.
    #
    # Conditions:
    #   - MACD histogram crosses zero (was negative yesterday, positive today)
    #   - Both MACD and signal line still below zero (early recovery)
    #   - RSI < 60: not already overbought at the cross
    #   - Price above 21 EMA (not 50 EMA): when MACD lines are below zero on
    #     TQQQ, price has typically sold off below the 50 EMA — requiring
    #     price > 50 EMA creates a near-impossible conflict. The 21 EMA reclaim
    #     is the correct confirmation: it shows short-term momentum has turned
    #     before the 50 EMA has even been reached.
    #   - Today closes green: price confirming the momentum shift
    #
    # Stop: below prior day's low — natural support before the cross
    # Target: TIER2_ATR_TARGET_MULT x ATR
    macd_cross      = prev_mh < 0 and macd_h >= 0          # histogram just crossed zero
    macd_line       = float(scored.get('MACD', macd_h))     # MACD line value
    macd_signal_val = float(scored.get('MACDs', 0))         # signal line value
    both_below_zero = macd_line < 0 and macd_signal_val < 0 # both still below zero (early recovery)
    macd_rsi_ok     = rsi < 60                              # not overbought
    macd_trend_ok   = price > ema_21                        # 21 EMA reclaimed (relaxed from 50 EMA)
    macd_green      = bar_close > bar_open                  # green close confirming

    if macd_cross and both_below_zero and macd_rsi_ok and macd_trend_ok and macd_green:
        prev_low_f     = float(prev['Low'])
        support        = prev_low_f
        support_source = "Prior Day Low (Path F MACD Cross)"
        stop_loss      = support - (atr * TIER2_ATR_STOP_MULT)
        if stop_loss >= entry_price:
            stop_loss = entry_price - atr
        take_profit    = entry_price + (atr * TIER2_ATR_TARGET_MULT)
        rr_ratio       = 0.0
        high_52w       = float(df['High'].tail(252).max())
        near_52w_high  = price >= high_52w * 0.98
        tier2_reasons  = [
            f"📊 Path F — MACD Cross (TIER 2 Medium Conviction)",
            f"🔄 MACD Histogram crossed zero: {prev_mh:.3f} → {macd_h:.3f}",
            f"📉 Both lines still below zero — early recovery, not late-stage",
            f"📈 Price above 21 EMA ${ema_21:.2f} (momentum turning) | RSI {rsi:.1f}",
            f"⚠️ TIER 2: Deploy {TIER2_POSITION_PCT:.0f}% of portfolio (smaller size)",
        ]
        print(f"   [{ticker}] ✅ PATH F — MACD Cross (Tier 2) "
              f"RSI={rsi:.1f} macd_h={prev_mh:.3f}→{macd_h:.3f}")
        return {
            "price": entry_price, "entry_price": entry_price,
            "stop_loss": round(stop_loss, 2), "take_profit": round(take_profit, 2),
            "rr_ratio": round(rr_ratio, 2), "score": 4, "threshold": 6,
            "trend_score": 2, "momentum_score": 2,
            "oversold_bypass": False, "atr": round(atr, 4), "atr_source": "Daily",
            "ema_21": round(ema_21, 2), "ema_50": round(ema_50, 2),
            "ema_200": round(ema_200, 2) if ema_200 else None,
            "rsi": round(rsi, 1), "bbl": round(bbl, 2) if bbl else None,
            "bb_width": round(bb_width, 4), "macd_h": round(macd_h, 4),
            "support": round(support, 2), "support_source": support_source,
            "is_bullish": True, "near_52w_high": near_52w_high,
            "mode": "SWING", "reasons": tier2_reasons, "path": "F",
            "tier": 2, "hold_days": TIER2_HOLD_DAYS,
            "position_size_pct": TIER2_POSITION_PCT,
            "vwap": None, "gap_pct": 0.0, "bb_squeeze_warning": False,
        }
    # PATH B only — Path A (RSI<35 + above 200 EMA) bypasses this entirely.
    trend_score     = 0
    oversold_bypass = False
    trend_cap       = SWING_CATEGORY_CAPS["trend"]

    # C. 21 EMA wick detection
    daily_low   = float(scored['Low'])
    daily_close = float(scored['Close'])  # same as price — yesterday's confirmed close
    # ── STRUCTURAL GATE: must be above at least one EMA ──────────────────
    # Path B only — for individual stocks this prevents signals where price is
    # below both 21 and 50 EMA (downtrends, not pullbacks).
    # TQQQ EXCEPTION: gate is bypassed. TQQQ's 3x leverage means it can be
    # below both EMAs during a sharp correction — which is exactly where the
    # best mean-reversion entries occur. Path A (RSI<35) handles the deepest
    # cases; Path B here handles moderate oversold setups (RSI 35-55).
    # The trend_floor check (trend_score >= 3) still filters garbage signals.
    if ticker != 'TQQQ' and price < ema_21 and price < ema_50:
        print(f"   [{ticker}] ❌ Below both EMAs (21 EMA ${ema_21:.2f}, "
              f"50 EMA ${ema_50:.2f}) — not a pullback, rejected.")
        return None

    wick_to_21  = (daily_low <= ema_21 * 1.005) and (daily_close > ema_21) and (rsi < 65)

    pct_from_21 = (daily_close - ema_21) / ema_21
    near_21     =  0.0   <= pct_from_21 < 0.025   # widened 1.5% → 2.5%
    below_21    = -0.025 <= pct_from_21 < 0.0        # widened 1.5% → 2.5%

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
    if 0 <= pct_from_50 < 0.03:   # widened 2% → 3%
        trend_score += 2
        reasons.append(f"📊 Pulling Back to 50 EMA Support (${ema_50:.2f})")
    elif -0.03 <= pct_from_50 < 0:   # widened 2% → 3%
        trend_score += 1
        reasons.append(f"⚠️ Below 50 EMA — Testing as Support (${ema_50:.2f})")

    # E. Bullish structure
    if price > ema_50:
        trend_score += 1
        reasons.append("✅ Price Above Daily 50 EMA (Bullish Structure)")

    # H. 52-week high zone
    high_52w      = float(df['High'].tail(252).max())
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

    # BB lower band removed — zero true touches in 3yr backtest (entry is next-day
    # open, band is never actually reached at entry). 169 signals it fired on
    # averaged only +0.08% exp vs +0.92% for non-BB signals. Actively hurt quality.

    # F. MACD
    # Backtest: negative MACD outperforms positive by +0.48pp expectancy.
    # Negative MACD = momentum pointing down = deeper pullback = better MR setup.
    # Positive MACD = momentum still up = less oversold = weaker bounce.
    # Old logic rewarded MACD>0 which was backwards for mean-reversion.
    if macd_h < 0 and macd_h > prev_mh:
        # Negative but improving — momentum turning, ideal MR entry timing
        momentum_score += 2
        reasons.append(f"🔄 Daily MACD: Negative & Turning ({macd_h:.3f} ↑ {prev_mh:.3f})")
    elif macd_h < 0:
        # Negative and still falling — deeply in pullback, valid MR setup
        momentum_score += 1
        reasons.append(f"📉 Daily MACD: Negative Histogram ({macd_h:.3f})")
    # MACD>0: 0 points — momentum still elevated, weaker mean-reversion setup

    # G. CANDLESTICK PATTERNS — reversal quality at support
    # Detects two high-quality mean-reversion candles on the scored (yesterday) bar.
    # Only fires when price is near a support level (within 4% of 21 or 50 EMA)
    # to avoid rewarding random candles in the middle of nowhere.
    #
    # Hammer:           Long lower wick (≥2× body) + small upper wick (≤50% body)
    #                   = price rejected lower levels, buyers stepped in hard
    # Bullish Engulfing: Today's body fully engulfs prior bar's body (close > prev_open,
    #                   open < prev_close, and today closed green)
    #
    # Worth +1 point only — a quality filter, not a primary signal.
    # Does not interact with momentum cap since it's added after other momentum scoring.
    candle_body       = abs(bar_close - bar_open)
    candle_range      = bar_high - bar_low
    lower_wick        = min(bar_open, bar_close) - bar_low
    upper_wick        = bar_high - max(bar_open, bar_close)
    near_support      = (abs(price - ema_21) / ema_21 < 0.04 or
                         abs(price - ema_50) / ema_50 < 0.04)
    is_hammer         = (candle_body > 0 and
                         lower_wick >= 2.0 * candle_body and
                         upper_wick <= 0.5 * candle_body and
                         bar_close > bar_open)   # must close green
    is_engulfing      = (bar_close > bar_open and           # green candle
                         prev_close < prev_open and         # prior was red
                         bar_close > prev_open and          # engulfs prior body
                         bar_open  < prev_close)

    if near_support and (is_hammer or is_engulfing):
        momentum_score += 1
        pattern_name = "Hammer" if is_hammer else "Bullish Engulfing"
        reasons.append(f"🕯️ {pattern_name} at Support (reversal candle)")

    momentum_score = min(momentum_score, momentum_cap)

    # ── Penalty (applied AFTER caps — can push score below threshold) ─────────
    penalty = 0
    if 0 < bb_width < 0.03 and rsi > 50:
        # Squeeze + RSI neutral/high = likely coiling before breakout, not pullback
        # Squeeze + RSI < 50 = oversold pullback into squeeze — valid setup, no penalty
        penalty = 2
        reasons.append(f"⚠️ BB Squeeze (width {bb_width:.3f}) — reduced edge")

    # ROC deceleration penalty removed — tested and found to block 128 valid
    # signals without improving signal quality. Backtest showed -0.144pp
    # expectancy without it. The penalty was working empirically but the
    # mechanism was flawed: fires on virtually every pullback by definition.

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

    # High score + neutral RSI = consolidation not pullback
    # Score ≥7 requires multiple categories maxed simultaneously — when RSI ≥ 50
    # this means price is sandwiched between converging EMAs, not genuinely oversold
    # Backtest: score 7 expectancy -0.041%, score 8 expectancy -0.086% (both negative)
    if score >= 7 and rsi >= 50:
        print(f"   [{ticker}] ❌ Score {score} but RSI {rsi:.1f} ≥ 50 — "
              f"consolidation not pullback. Rejected.")
        return None

    # Score 8 + RSI > 45: every indicator maxed but no real pullback.
    # Mean-reversion premise isn't true — nothing is actually mean-reverting.
    # Backtest: -1.80% avg return on 9 signals. Logic-driven gate, not curve-fit.
    if score >= 8 and rsi > 45:
        print(f"   [{ticker}] ❌ Score {score} but RSI {rsi:.1f} > 45 — "
              f"all boxes ticked but no genuine pullback. Rejected.")
        return None

    print(f"   [{ticker}] ✅ Score {score}/{threshold} — "
          f"trend={trend_score} momentum={momentum_score} penalty=-{penalty}")
    is_bullish = price > ema_21

    # Stop/target use entry_price (today's close) — that's your actual fill.
    # Support levels come from yesterday's scored bar (structurally grounded).
    # ATR also comes from yesterday's completed bar.
    #
    # Four support cases — always anchor to the nearest level BELOW entry price:
    #   BB Lower Band removed — 159 signals, 48% WR, +0.35% avg return.
    #   Weakest source by every metric. Marks where price has already fallen,
    #   not where buyers defend. EMA anchors are structurally superior.
    #   1. Price above 21 EMA (near it)          → anchor to 21 EMA
    #   2. Price above 21 EMA, below 50 EMA      → anchor to 21 EMA (50 EMA is above = resistance)
    #   3. Price below 21 EMA, above 50 EMA      → volatility stop (neither EMA is clean support)
    #   4. Price near/above 50 EMA (from below)  → anchor to 50 EMA

    above_21  = price >= ema_21
    above_50  = price >= ema_50
    near_50   = abs(price - ema_50) / ema_50 < 0.03   # within 3% of 50 EMA

    if above_21 and near_21:
        # Case 2: Price sitting just above 21 EMA — classic pullback support
        support        = ema_21
        support_source = "21 EMA"

    elif above_21 and not above_50:
        # Case 3: Price above 21 EMA but below 50 EMA
        # 50 EMA is ABOVE price = resistance not support
        # Anchor to 21 EMA which is below price = actual support
        support        = ema_21
        support_source = "21 EMA (50 EMA above = resistance)"

    elif not above_21 and above_50 and near_50:
        # Case 4a: Price just below 21 EMA, near 50 EMA from above
        # 50 EMA is the real support here
        support        = ema_50
        support_source = "50 EMA"

    elif not above_21 and above_50:
        # Case 4b: Price between EMAs — neither is clean support
        # Use volatility stop: entry - ATR×2.5
        support        = entry_price
        support_source = "Volatility Stop (between EMAs)"

    elif above_50 and near_50:
        # Case 5: Price pulling back to 50 EMA from above
        support        = ema_50
        support_source = "50 EMA"

    else:
        # Fallback: use 21 EMA (nearest EMA to price in most cases)
        support        = ema_21
        support_source = "21 EMA (fallback)"

    if support_source.startswith("Volatility Stop"):
        stop_loss = entry_price - (atr * SWING_ATR_STOP_MULT)
    else:
        stop_loss = support - (atr * SWING_ATR_STOP_MULT)

    # Safety check — if support is above entry price the stop would be nonsensical.
    # This happens when 50 EMA is above entry and slips through case logic.
    # Reject explicitly rather than letting a broken stop reach validate_risk silently.
    if support > entry_price and not support_source.startswith("Volatility Stop"):
        print(f"   [{ticker}] ❌ Support {support_source} (${support:.2f}) is above entry "
              f"(${entry_price:.2f}) — no valid stop placement. Rejected.")
        return None

    take_profit = entry_price  + (atr * SWING_ATR_TARGET_MULT)

    if stop_loss >= entry_price:
        stop_loss = entry_price - atr

    # Gap filter — measures the true overnight gap (today's Open vs yesterday's Close)
    # Since scored = iloc[-1] (today), today_open = scored['Open'],
    # and the gap is vs prev['Close'] (yesterday's confirmed close).
    today_open    = float(scored['Open'])
    prev_close_px = float(prev['Close'])
    gap_pct       = (today_open - prev_close_px) / prev_close_px * 100
    intraday_move = (entry_price - today_open) / today_open * 100  # for logging only

    print(f"   [{ticker}] 📍 Scored on today ${price:.2f} | "
          f"Gap {gap_pct:+.1f}% | Intraday {intraday_move:+.1f}% | Entry ${entry_price:.2f}")

    # Dynamic ATR-based gap filter — flat % is too strict for high-beta stocks
    # (RBLX, AMD, IOT gap 3-5% routinely) and too loose for low-vol stocks
    # (SPY gapping 3% is abnormal). Solution: allow up to 1.5× the stock's
    # own ATR% as the max gap, with a floor of 2% and ceiling of 7%.
    atr_pct_gap     = (atr / price) * 100
    dynamic_max_gap = max(2.0, min(atr_pct_gap * 1.5, 7.0))
    if abs(gap_pct) > dynamic_max_gap:
        print(f"   [{ticker}] ❌ Gap too large ({gap_pct:+.1f}% vs {dynamic_max_gap:.1f}% dynamic max "
              f"[ATR {atr_pct_gap:.1f}% × 1.5, gap rule]). Overnight gap breaks scored structure. Rejected.")
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
        "support": round(support, 2), "support_source": support_source,
        "path": "B",
        "tier": 2,           # Tier 2 — standard additive scoring, conservative 5% allocation
        "hold_days": TIER2_HOLD_DAYS,
        "position_size_pct": TIER2_POSITION_PCT,
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

# signals_conflict removed — swing-only bot, no conflict resolution needed


def calculate_position_size(score: int, threshold: int,
                             price: float, atr: float, tier: int = 1) -> dict:
    """
    Calculates position size dynamically based on Tier (1 or 2).

    Tier 1 (high conviction — Path A/D): base = TIER1_POSITION_PCT (10%)
    Tier 2 (medium conviction — Path E/F): base = TIER2_POSITION_PCT (5%)

    Both tiers then get:
      - Conviction boost: +1% per point above threshold, capped at +4%
      - Volatility reduction: ATR > 3% scales down, ATR > 6% caps at 60%
      - Hard cap: 15% max (raised from 12% to allow Tier 1 full sizing)

    TQQQ-adjusted ATR thresholds (4-7% is normal, not a warning sign):
      Low  (< 3%): no reduction — abnormally calm for TQQQ
      Mid  (3-6%): linear scale down 1.0 -> 0.6
      High (> 6%): cap at 60% of base — extreme volatility day
    """
    # Base allocation driven by tier — this is the critical fix
    base = TIER1_POSITION_PCT if tier == 1 else TIER2_POSITION_PCT

    # Conviction boost: +1% for each point above threshold, capped at +4%
    margin     = max(score - threshold, 0)
    conviction = min(margin * 1.0, 4.0)

    # Volatility adjustment (TQQQ-adjusted thresholds)
    atr_pct = (atr / price) * 100 if price > 0 else 4.0
    if atr_pct <= 3.0:
        vol_factor = 1.0
    elif atr_pct <= 6.0:
        vol_factor = 1.0 - ((atr_pct - 3.0) / 3.0) * 0.4   # linear 1.0 -> 0.6
    else:
        vol_factor = 0.6

    raw_pct = (base + conviction) * vol_factor
    pct     = round(min(raw_pct, 15.0), 1)   # cap raised to 15% to allow Tier 1 max sizing

    # Human-readable volatility tag (TQQQ-adjusted thresholds)
    if atr_pct <= 3.0:   vol_tag = "low vol"
    elif atr_pct <= 6.0: vol_tag = "mid vol"
    else:                vol_tag = "high vol ⚠️"

    dollar_amount = round(PORTFOLIO_VALUE * pct / 100, 2)
    shares        = int(dollar_amount // price) if price > 0 else 0

    if shares < 1:
        share_note = "⚠️ < 1 share — consider fractional shares"
    elif shares == 1:
        share_note = "≈ 1 share"
    else:
        share_note = f"≈ {shares} shares"

    tier_label = f"Tier {tier}"
    label = (
        f"{pct}% of portfolio = **${dollar_amount:,.2f}** ({share_note}) "
        f"[{tier_label} · score +{margin} · ATR {atr_pct:.1f}% · {vol_tag}]"
    )
    return {"pct": pct, "dollar": dollar_amount, "shares": shares, "label": label}


def build_final_signal(swing_signal: dict | None) -> dict | None:
    """Wraps swing signal as a SWING alert. Returns None if no swing signal."""
    if swing_signal is None:
        return None

    sig  = swing_signal.copy()
    tier = sig.get("tier", 1)   # Tier 1 (Path A/D) or Tier 2 (Path E/F)

    pos_c = calculate_position_size(
        sig["score"], sig["threshold"],
        sig["price"], sig["atr"],
        tier=tier                # ← critical: drives base allocation %
    )
    sig.update({
        "scenario":          "SWING",
        "scenario_label":    f"📅 TIER {tier} SWING SETUP",
        "size_guidance":     pos_c["label"],
        "position_size_pct": pos_c["pct"],
        "hold_guidance":  (
            "Enter before 4:00pm close. Hold for signal's recommended days. "
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
    
    Formula: dynamic_max = max(BASE, min(ATR% × 3.5, ABSOLUTE_CAP))

    Ceiling raised to 3.5x to accommodate support-anchored stops at 2.5x ATR.
    Entry is typically ~0.45x ATR above support, so total stop from entry
    = 0.45 + 2.5 = ~2.95x ATR. Ceiling of 3.5x gives headroom without
    being reckless. ABSOLUTE_MAX_STOP_PCT=15% is the hard safety net.

    This gives each stock room proportional to its natural daily movement:
      - Low-vol stock  (ATR 0.8%): dynamic max = max(6%, 2.8%)  = 6.0%
      - Mid-vol stock  (ATR 3.0%): dynamic max = max(6%, 10.5%) = 10.5%
      - High-vol stock (ATR 4.5%): dynamic max = max(6%, 15.75%) → capped = 15.0%
      - Extreme stock  (ATR 9.0%): dynamic max = max(6%, 31.5%) → capped = 15.0%
    """
    price      = signal["price"]
    stop_loss  = signal["stop_loss"]
    target     = signal["take_profit"]
    atr        = signal.get("atr", 0)

    atr_pct          = atr / price if price > 0 else 0
    dynamic_max_stop = max(BASE_MAX_STOP_PCT, min(atr_pct * 3.5, ABSOLUTE_MAX_STOP_PCT))
    actual_pct       = (price - stop_loss) / price

    if actual_pct > dynamic_max_stop:
        print(f"   [{ticker}] ❌ Stop too wide "
              f"({actual_pct*100:.1f}% > {dynamic_max_stop*100:.1f}% dynamic max "
              f"[ATR {atr_pct*100:.1f}% × 3.5]). Rejected.")
        return None

    # ATR model R/R — fixed ratio from multipliers (always 3.5/2.5 = 1.40)
    # Used for the rejection gate only — predictable regardless of stop placement.
    atr_rr = round(SWING_ATR_TARGET_MULT / SWING_ATR_STOP_MULT, 2)

    if atr_rr < MIN_RR_RATIO:
        print(f"   [{ticker}] ❌ ATR R/R {atr_rr:.2f} below minimum {MIN_RR_RATIO}. Rejected.")
        return None

    risk = price - stop_loss
    if risk <= 0:
        print(f"   [{ticker}] ❌ Invalid stop (risk ≤ 0). Rejected.")
        return None

    # Structural R/R — actual realized ratio from entry/stop/target price levels.
    # This is what the user actually experiences. Often differs from ATR model
    # when stop is EMA-anchored far below entry (structural R/R > 1.40).
    entry  = signal.get("price", price)
    target = signal.get("take_profit", 0)
    structural_rr = round((target - entry) / risk, 2) if risk > 0 else atr_rr

    signal["rr_ratio"]       = structural_rr  # shown in Discord alert
    signal["rr_ratio_model"] = atr_rr         # always 1.40, stored for reference

    return signal


# =============================================================================
#  SECTION 9 — EARNINGS CHECK
#  Docks EARNINGS_SCORE_PENALTY from score if earnings within warning window.
#  Re-checks threshold after penalty — rejects if score falls below.
# =============================================================================

# ── Earnings cache helpers ───────────────────────────────────────────────────

def load_earnings_cache() -> dict:
    """Loads earnings_cache.json. Returns empty dict if missing or corrupt."""
    p = Path(EARNINGS_CACHE_FILE)
    if p.exists():
        try:
            with open(p) as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_earnings_cache(cache: dict) -> None:
    """Saves earnings cache to disk."""
    try:
        with open(EARNINGS_CACHE_FILE, "w") as f:
            json.dump(cache, f, indent=2)
    except Exception as e:
        print(f"   ⚠️ Could not save earnings cache: {e}")


def check_earnings(ticker: str) -> tuple[bool, str]:
    """Returns (has_warning, message_string).

    Uses a lazy rolling cache (earnings_cache.json) to avoid hammering
    the yfinance API. Per-ticker TTL of 7 days — aligns with the earnings
    warning window so a one-day stale result is never consequential.

    Flow:
      1. ETF?          → return False immediately (no earnings)
      2. In cache AND fetched < 7 days ago? → use cached value (0 API calls)
      3. Otherwise     → fetch from yfinance, update cache, use result (1 API call)
    """
    # ── Step 1: ETFs never have earnings ─────────────────────────────────────
    ETF_TICKERS = {
        'TQQQ',                                           # Primary instrument — leveraged ETF, no earnings
        'SPY', 'SPLG', 'QQQM', 'QQQ', 'IWM', 'VTI', 'SOXQ',
        'XLY', 'GDX', 'SIL', 'XLF', 'XLK', 'SMH', 'GLD', 'SLV', 'ITB',
        'VWO', 'VEA', 'SPMO',
        'ZSP.TO', 'XEF.TO',
    }
    if ticker in ETF_TICKERS:
        return False, ""

    eastern = pytz.timezone(TIMEZONE)
    today   = datetime.now(eastern).date()

    # ── Step 2: Check rolling cache ───────────────────────────────────────────
    cache = load_earnings_cache()
    entry = cache.get(ticker)
    cache_hit = False

    if entry:
        fetched_date = datetime.strptime(entry["fetched"], "%Y-%m-%d").date()
        age_days     = (today - fetched_date).days
        if age_days < 7:
            # Cache is fresh — use it without any API call
            earnings_date_str = entry.get("earnings_date")
            cache_hit = True
            print(f"   💾 [{ticker}] Earnings cache hit (age {age_days}d)")
            if earnings_date_str is None:
                return False, ""
            earnings_date = datetime.strptime(earnings_date_str, "%Y-%m-%d").date()
            days_until = (earnings_date - today).days
            if 0 <= days_until <= EARNINGS_WARNING_DAYS:
                msg = (f"⚠️ **EARNINGS WARNING:** Report in "
                       f"{days_until} day{'s' if days_until != 1 else ''} ({earnings_date})")
                return True, msg
            return False, ""

    # ── Step 3: Cache miss or stale — fetch from yfinance ────────────────────
    try:
        print(f"   🌐 [{ticker}] Fetching earnings date from yfinance...")
        cal = yf.Ticker(ticker).calendar
        earnings_date = None

        if cal is not None:
            if isinstance(cal, dict) and 'Earnings Date' in cal:
                earnings_date = cal['Earnings Date'][0]
            elif isinstance(cal, pd.DataFrame):
                if 'Earnings Date' in cal.columns:
                    earnings_date = cal.iloc[0]['Earnings Date']
                elif not cal.empty:
                    earnings_date = cal.iloc[0, 0]

        # Save to cache regardless of whether earnings found —
        # a None result is also worth caching to avoid repeat calls
        earnings_date_str = None
        if earnings_date is not None:
            earnings_date = pd.to_datetime(earnings_date).date()
            earnings_date_str = str(earnings_date)

        cache[ticker] = {
            "earnings_date": earnings_date_str,
            "fetched":       str(today),
        }
        save_earnings_cache(cache)

        if earnings_date is None:
            return False, ""

        days_until = (earnings_date - today).days
        if 0 <= days_until <= EARNINGS_WARNING_DAYS:
            msg = (f"⚠️ **EARNINGS WARNING:** Report in "
                   f"{days_until} day{'s' if days_until != 1 else ''} ({earnings_date})")
            return True, msg

        return False, ""

    except Exception as e:
        print(f"   ⚠️ [{ticker}] Earnings fetch failed: {e}")
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
#  SECTION 10B — SIGNAL CHART GENERATOR
#  Saves a candlestick chart PNG at the moment of signal scoring.
#  Shows last 60 candles with EMA 21/50/200, RSI panel, and entry/stop/target.
#  Chart is attached to the Discord alert as a file upload.
# =============================================================================

CHARTS_DIR = Path(tempfile.gettempdir()) / "charts"
CHARTS_DIR.mkdir(exist_ok=True, parents=True)

def generate_signal_chart(ticker: str, df: pd.DataFrame, signal: dict) -> Path | None:
    """
    Generate a candlestick chart using pure matplotlib (no mplfinance panel quirks).
    Layout: top 70% = candles + EMAs + entry/stop/target lines
            bottom 30% = RSI with 30/50/70 reference lines
    """
    if not CHART_AVAILABLE:
        return None

    try:
        import traceback as _tb
        import numpy as np
        import matplotlib.patches as mpatches
        import matplotlib.dates as mdates

        # ── Slice last 60 bars ───────────────────────────────────────────────
        sub = df.tail(60).copy()
        sub.index = pd.DatetimeIndex(sub.index)
        if sub.index.tz is not None:
            sub.index = sub.index.tz_localize(None)
        n = len(sub)
        xs = np.arange(n)   # integer x positions (avoids date-gap issues)

        entry  = signal.get("price", 0)
        target = signal.get("take_profit", 0)
        stop   = signal.get("stop_loss", 0)

        # ── Safe column ──────────────────────────────────────────────────────
        def safe_col(col):
            if col not in sub.columns:
                return None
            vals = sub[col].values.astype(float)
            if np.all(np.isnan(vals)):
                return None
            mask = np.isnan(vals)
            if mask.any():
                idx = np.where(~mask, np.arange(n), 0)
                np.maximum.accumulate(idx, out=idx)
                vals[mask] = vals[idx[mask]]
            return vals

        ema21  = safe_col("EMA_21")
        ema50  = safe_col("EMA_50")
        ema200 = safe_col("EMA_200")
        rsi    = safe_col("RSI")

        opens  = sub["Open"].values.astype(float)
        highs  = sub["High"].values.astype(float)
        lows   = sub["Low"].values.astype(float)
        closes = sub["Close"].values.astype(float)

        # ── Figure with 2 panels ─────────────────────────────────────────────
        BG   = "#1a1a2e"
        PAN  = "#16213e"
        GRID = "#2a2a4a"

        fig, (ax1, ax2) = plt.subplots(
            2, 1, figsize=(14, 10),
            gridspec_kw={"height_ratios": [3, 1], "hspace": 0.06},
            facecolor=BG
        )
        ax1.set_facecolor(PAN)
        ax2.set_facecolor(PAN)

        # ── Draw candles ─────────────────────────────────────────────────────
        W = 0.6
        for i in range(n):
            bull = closes[i] >= opens[i]
            color = "#26a69a" if bull else "#ef5350"
            body_lo = min(opens[i], closes[i])
            body_hi = max(opens[i], closes[i])
            # Wick
            ax1.plot([xs[i], xs[i]], [lows[i], highs[i]], color=color, linewidth=0.8)
            # Body
            ax1.add_patch(mpatches.FancyBboxPatch(
                (xs[i] - W/2, body_lo), W, max(body_hi - body_lo, 0.001),
                boxstyle="square,pad=0", linewidth=0,
                facecolor=color, edgecolor=color
            ))

        # ── EMA overlays ─────────────────────────────────────────────────────
        if ema21  is not None: ax1.plot(xs, ema21,  color="#3399ff", linewidth=1.2, label="EMA 21")
        if ema50  is not None: ax1.plot(xs, ema50,  color="#ff9933", linewidth=1.2, label="EMA 50")
        if ema200 is not None: ax1.plot(xs, ema200, color="#cc44ff", linewidth=1.0, label="EMA 200")

        # ── Entry / target / stop lines ──────────────────────────────────────
        ax1.axhline(entry,  color="#00ff88", linewidth=1.0, linestyle="--", alpha=0.9)
        ax1.axhline(target, color="#00cc44", linewidth=1.0, linestyle="-",  alpha=0.9)
        ax1.axhline(stop,   color="#ff3333", linewidth=1.0, linestyle="-",  alpha=0.9)

        # ── Candle panel style ───────────────────────────────────────────────
        ax1.set_xlim(-1, n)
        ax1.tick_params(colors="#999999", labelsize=8)
        ax1.yaxis.label.set_color("#cccccc")
        for sp in ax1.spines.values(): sp.set_color(GRID)
        ax1.grid(color=GRID, linewidth=0.5, linestyle="--")
        # x-tick labels: show ~6 evenly spaced dates
        step = max(1, n // 6)
        tick_pos = xs[::step]
        tick_lbl = [sub.index[i].strftime("%b %d") for i in tick_pos]
        ax1.set_xticks(tick_pos)
        ax1.set_xticklabels(tick_lbl, rotation=30, ha="right", fontsize=7, color="#999999")
        ax1.set_ylabel("Price", color="#cccccc", fontsize=9)
        if ema21 is not None or ema50 is not None or ema200 is not None:
            ax1.legend(loc="upper left", fontsize=7,
                       facecolor=BG, edgecolor=GRID, labelcolor="#cccccc")

        # ── RSI panel ────────────────────────────────────────────────────────
        if rsi is not None:
            ax2.plot(xs, rsi, color="#ffffff", linewidth=1.2)
            ax2.axhline(30, color="#ff4444", linewidth=0.6, linestyle="--", alpha=0.7)
            ax2.axhline(50, color="#888888", linewidth=0.6, linestyle="--", alpha=0.7)
            ax2.axhline(70, color="#44ff44", linewidth=0.6, linestyle="--", alpha=0.7)
            ax2.fill_between(xs, rsi, 30, where=(rsi < 30), alpha=0.15, color="#ff4444")
            ax2.fill_between(xs, rsi, 70, where=(rsi > 70), alpha=0.15, color="#44ff44")
            ax2.set_ylim(0, 100)
            ax2.set_yticks([30, 50, 70])
        ax2.set_xlim(-1, n)
        ax2.set_ylabel("RSI", color="#cccccc", fontsize=9)
        ax2.tick_params(colors="#999999", labelsize=8)
        ax2.set_xticks([])
        for sp in ax2.spines.values(): sp.set_color(GRID)
        ax2.grid(color=GRID, linewidth=0.5, linestyle="--")

        # ── Title ────────────────────────────────────────────────────────────
        score    = signal.get("score", "?")
        rsi_val  = signal.get("rsi", 0)
        scenario = signal.get("scenario_label", "")
        date_str = datetime.now().strftime("%Y-%m-%d %H:%M")
        title_str = (f"{ticker}  |  Score {score}  |  RSI {rsi_val:.1f}\n"
                     f"{scenario}  |  {date_str} ET\n"
                     f"Entry ${entry:.2f}  \u25b2 Target ${target:.2f}  \u25bc Stop ${stop:.2f}")
        fig.suptitle(title_str, color="#ffffff", fontsize=10)

        out_path = CHARTS_DIR / f"{ticker}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
        fig.savefig(out_path, dpi=130, facecolor=BG)
        plt.close(fig)
        print(f"   📸 Chart saved: {out_path}")
        return out_path

    except Exception as e:
        import traceback as _tb
        print(f"   ⚠️  Chart generation failed for {ticker}: {e}")
        print(f"   ⚠️  Traceback: {_tb.format_exc()}")
        return None


# =============================================================================
#  SECTION 11 — DISCORD ALERT
#  Rich embed with trade plan, technicals, reasons, and contextual banners.
# =============================================================================

def _post_discord(payload: dict, chart_path: Path | None = None):
    if not DISCORD_WEBHOOK_URL:
        print("⚠️  DISCORD_URL not set — skipping webhook.")
        return
    try:
        if chart_path and chart_path.exists():
            # Multipart upload — embeds the chart image directly in the Discord message.
            # Discord requires payload_json for the embed when sending multipart.
            import json as _json
            with open(chart_path, 'rb') as img:
                r = requests.post(
                    DISCORD_WEBHOOK_URL,
                    data={"payload_json": _json.dumps(payload)},
                    files={"file": (chart_path.name, img, "image/png")},
                    timeout=20,
                )
            # Clean up chart file after sending — don't accumulate PNGs
            try:
                chart_path.unlink()
            except Exception:
                pass
        else:
            r = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=10)

        if not r.ok:
            print(f"❌ Discord error: {r.status_code} — {r.text[:500]}")
            return
        r.raise_for_status()
        time.sleep(0.5)   # Discord allows ~30 req/min; 0.5s gap prevents 429s on bulk alerts
    except Exception as e:
        print(f"❌ Discord error: {e}")


def send_setup_alert(ticker, currency, signal,
                     rel_vol, elapsed_minutes, mode, regime_bullish,
                     earnings_msg="", df: pd.DataFrame | None = None,
                     alert_label: str = "NEW SIGNAL"):
    """Sends a swing trade setup embed to Discord, with an auto-generated chart image."""
    et_now     = datetime.now(pytz.timezone(TIMEZONE))
    curr_sym   = 'CA$' if currency == 'CAD' else '$'
    scenario   = signal.get("scenario", "?")
    trade_mode = signal.get("mode", "UNKNOWN")
    price      = signal["price"]
    stop_loss  = signal["stop_loss"]
    target     = signal["take_profit"]
    rr         = signal.get("rr_ratio", 0.0)
    rr_model   = signal.get("rr_ratio_model", 1.40)
    atr_val    = signal.get("atr", 0.0)
    score      = signal.get("score", 0)
    threshold  = signal.get("threshold", 0)
    stop_pct   = (price - stop_loss) / price * 100
    tgt_pct    = (target - price)    / price * 100
    risk_share = price - stop_loss
    tier       = signal.get("tier", 1)
    path       = signal.get("path", "?")
    hold_days  = signal.get("hold_days", 10)
    pos_pct    = signal.get("position_size_pct", TIER1_POSITION_PCT)

    # ── Tier label and color ──────────────────────────────────────────────────
    # Tier 1 (high conviction): deep oversold RSI < 35 or pivot low reversal
    # Tier 2 (medium conviction): EMA bounce or MACD cross
    if tier == 1:
        tier_label  = "🔴 TIER 1 — HIGH CONVICTION"
        tier_size   = f"Deploy **{pos_pct:.0f}%** of portfolio (LARGER size)"
        color       = 15548997    # red — urgent, high conviction
        path_name   = {"A": "Deep Oversold RSI", "D": "Pivot Low Reversal"}.get(path, path)
    else:
        tier_label  = "🟡 TIER 2 — MEDIUM CONVICTION"
        tier_size   = f"Deploy **{pos_pct:.0f}%** of portfolio (smaller size)"
        color       = 16776960    # yellow — medium conviction
        path_name   = {"E": "21 EMA Bounce", "F": "MACD Cross"}.get(path, path)

    # ── Alert label: NEW SIGNAL vs STILL ACTIVE ───────────────────────────────
    if alert_label == "STILL ACTIVE":
        label_prefix = "🔔 STILL ACTIVE"
        label_note   = "_Signal conditions still met — setup has not resolved yet._\n"
    else:
        label_prefix = "🚨 NEW SIGNAL"
        label_note   = "_Fresh setup — conditions just triggered._\n"

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
    desc  = f"{label_note}"
    desc += f"*Triggered at {et_now.strftime('%I:%M %p ET')}*\n"
    desc += f"{regime_label} · {session} · `{currency}`\n\n"

    # Tier box — the most important thing you need to know immediately
    desc += f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
    desc += f"**{tier_label}**\n"
    desc += f"📌 Signal Type: **{path_name}** (Path {path})\n"
    desc += f"💰 Position Size: {tier_size}\n"
    desc += f"⏱️ Hold: **{hold_days} trading days**\n"
    desc += f"━━━━━━━━━━━━━━━━━━━━━━━━\n\n"

    desc += "📊 **Trade Plan**\n"
    desc += f"• **Entry:**      `{curr_sym}{price:.2f}`\n"
    desc += f"• **Target:**     `{curr_sym}{target:.2f}` (+{tgt_pct:.1f}%) 🎯\n"
    desc += f"• **Stop:**       `{curr_sym}{stop_loss:.2f}` (−{stop_pct:.1f}%) 🛑\n"
    desc += f"• **R/R:**        `1:{rr:.2f}` (structural) · `1:{rr_model:.2f}` (ATR model) ⚖️\n"
    desc += f"• **Risk/Share:** `{curr_sym}{risk_share:.2f}` | ATR `{curr_sym}{atr_val:.2f}`\n"
    desc += f"• **Size:**       `{signal.get('size_guidance', '—')}`\n"

    desc += "\n📉 **Technicals**\n"
    desc += f"• **RSI:**    `{rsi_val:.1f}` {rsi_label}\n"
    desc += f"• **21 EMA:** `{ema_str(signal.get('ema_21'))}`\n"
    if signal.get('ema_50'):
        desc += f"• **50 EMA:** `{ema_str(signal.get('ema_50'))}`\n"
    # Show which level the stop is anchored to — lets you sanity check on chart
    support_src = signal.get('support_source', '')
    support_val = signal.get('support', 0)
    ema_21_val  = signal.get('ema_21', 0)
    if support_src and support_val:
        if support_src == "Volatility Stop (between EMAs)":
            desc += (f"• **Stop Anchor:** Volatility stop — price between EMAs\n"
                     f"  21 EMA `{curr_sym}{ema_21_val:.2f}` broken (resistance) | "
                     f"50 EMA too far | stop = entry − ATR×{SWING_ATR_STOP_MULT}\n")
        else:
            desc += f"• **Stop Anchor:** `{curr_sym}{support_val:.2f}` ({support_src}) — stop is ATR×{SWING_ATR_STOP_MULT} below this\n"
    if rel_vol > 2.0:    vol_label = "🔥 Heavy"
    elif rel_vol > 1.2:  vol_label = "💪 Above Average"
    else:                vol_label = "😐 Below Average"
    desc += f"• **Volume:** `{rel_vol:.1f}x` 20-day avg · {vol_label}\n"

    desc += "\n📝 **Why This Signal**\n"
    for r in signal.get("reasons", []):
        desc += f"• {r}\n"

    if earnings_msg:
        desc += f"\n{earnings_msg}\n"

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
        "content": (f"{label_prefix} **{ticker}** | "
                    f"{'🔴 T1' if tier == 1 else '🟡 T2'} {path_name} | "
                    f"`{curr_sym}{price:.2f}` | Deploy {pos_pct:.0f}%"),
        "embeds": [{
            "title":       f"{label_prefix} — {ticker} | {path_name} | Hold {hold_days}d",
            "description": desc,
            "color":       color,
            "fields": [{"name": "🔗 Charts",
                        "value": (f"[TradingView](https://www.tradingview.com/chart/?symbol={ticker}) · "
                                  f"[Yahoo Finance](https://finance.yahoo.com/quote/{ticker})"),
                        "inline": False}],
            "footer": {"text": f"TQQQ Alert Bot v6.2 | Path {path} | Tier {tier} | ATR: {signal.get('atr_source','—')}"},
        }],
    }

    # Generate chart image using exact df the scorer used, attach to Discord embed
    chart_path = generate_signal_chart(ticker, df, signal) if df is not None else None
    if chart_path:
        payload["embeds"][0]["image"] = {"url": f"attachment://{chart_path.name}"}

    _post_discord(payload, chart_path=chart_path)
    print(f"   ✅ Alert sent: {ticker} | Scenario {scenario} | {trade_mode} | {currency}")


def send_premarket_summary(summaries: list[dict]):
    """Morning gap/watchlist briefing — informational only.
    Only shows tickers with significant gaps (>=2%) to stay within
    Discord's 4096 character embed limit. Flat tickers are counted
    but not listed to keep the message concise and actionable.
    """
    if not summaries:
        return
    et_now = datetime.now(pytz.timezone(TIMEZONE))

    # Split into meaningful gaps and flat opens — sort by gap size descending
    gap_items  = [s for s in summaries if abs(s.get('gap_pct', 0)) >= 2.0]
    flat_count = len(summaries) - len(gap_items)
    gap_items  = sorted(gap_items, key=lambda x: abs(x.get('gap_pct', 0)), reverse=True)

    lines = [
        f"• **{s['ticker']}** "
        f"`{'CA$' if s['currency'] == 'CAD' else '$'}{s['price']:.2f}` — {s['note']}"
        for s in gap_items
    ]

    if not lines:
        desc = f"No significant gaps today — {flat_count} tickers flat.\n\n_Signals fire during market hours._"
    else:
        desc  = f"**{len(gap_items)} significant gaps** | {flat_count} tickers flat\n\n"
        desc += "\n".join(lines)
        desc += "\n\n_Signals fire during market hours._"

    payload = {"embeds": [{"title": f"📋 Pre-Market Gap Summary — {et_now.strftime('%b %d, %Y')}",
        "description": desc,
        "color": COLOR_BLUE}]}
    _post_discord(payload)


def send_no_signals_notice(mode: str, count: int):
    """Logs scan completion to console only — no Discord message.
    Sending a Discord alert every 15 min when nothing is found would produce
    up to 26 noise messages per day. Silence is the correct behaviour here —
    Discord alerts only fire when there is something actionable to act on."""
    et_now = datetime.now(pytz.timezone(TIMEZONE))
    print(f"   ✅ Scan complete — no setups found | "
          f"{mode.upper()} | {et_now.strftime('%I:%M %p ET')} | "
          f"{count} ticker(s) scanned")


# =============================================================================
#  SECTION 12 — MAIN LOOP (Three-Stage Funnel)
# =============================================================================

def check_market(mode: str, tickers_override: list | None = None):
    tz     = pytz.timezone(TIMEZONE)
    et_now = datetime.now(tz)

    print(f"\n{'='*60}")
    print(f"  TQQQ Alert Bot v6.2 — {et_now.strftime('%A %b %d %Y %I:%M %p ET')}")
    print(f"  Mode: {mode.upper()}")
    print(f"{'='*60}\n")

    elapsed_min = get_elapsed_minutes(et_now)
    time_penalty, penalty_reasons = get_time_penalty(et_now)

    for r in penalty_reasons:
        print(f"⚠️  {r}")

    # ── Entry window ─────────────────────────────────────────────────────────────
    # Scans run every hour all day — Discord alerts fire any time a signal scores.
    # Alpaca orders only placed during 12:30–4:00pm ET when the daily candle
    # is mature enough to trust. --force bypasses for after-hours testing.
    in_entry_window = ENTRY_WINDOW_START_MIN <= elapsed_min < ENTRY_WINDOW_END_MIN
    force_override  = globals().get('FORCE_RUN', False)
    if not in_entry_window and not force_override:
        print(f"ℹ️  Outside order window (elapsed={elapsed_min:.0f} min) — "
              f"alerts will fire but no Alpaca orders will be placed.")

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

    # ── Check open trade outcomes (daily bar resolution) ─────────────────────
    # Runs on every scan using the bulk daily data already downloaded —
    # no extra API calls needed.
    print("📋 Checking open trade outcomes...")
    resolved = check_open_trades(bulk_data)

    # ── Real-time sell alerts (live price resolution) ─────────────────────────
    # Separate from the daily outcome check above. Fetches TQQQ's live price
    # and fires an immediate Discord alert if any open trade has hit its
    # target, stop, or trailing threshold RIGHT NOW during the session.
    # Only runs during market hours (swing mode) — not in premarket.
    if mode == "swing":
        print("🔔 Checking real-time sell conditions (live price)...")
        check_live_sell_alerts()

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
                    # Stale daily data (weekend/holiday): compare live price vs
                    # most recent completed session (prev_close = Friday on Monday).
                    # prior_close (Thursday) would inflate/deflate the gap incorrectly.
                    price = live_price
                    prev  = prev_close   # always most recent completed bar
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
                                  'price': price, 'note': note, 'gap_pct': gap_pct})
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
                candidates.append((ticker, swing_signal, False, df_daily))

        except Exception as e:
            print(f"   ⚠️ [{ticker}] Stage 2 error: {e}")

    print(f"   ✅ {len(candidates)} swing setups advance to Stage 3\n")

    # ═════════════════════════════════════════════════════════════════════════
    #  PORTFOLIO MANAGER — runs between Stage 2 and Stage 3
    #  Option A: Rank candidates by path priority → score → RSI
    #  Option B: Cap to MAX_TRADES_PER_DAY highest-conviction setups
    #  Option C: Live buying power check (Alpaca syntax — swap for IBKR later)
    # ═════════════════════════════════════════════════════════════════════════

    # ── Option A: Rank candidates ─────────────────────────────────────────────
    # Path A (capitulation, +3.03% exp) > Path D (pivot, +1.93% exp) > Path B (standard)
    # Within same path: higher score first, then lower RSI (more oversold)
    def _candidate_sort_key(c):
        sig = c[1]
        path     = sig.get("path", "B")
        priority = PATH_PRIORITY.get(path, 2)
        score    = sig.get("score", 0)
        rsi      = sig.get("rsi", 50)
        return (priority, -score, rsi)

    candidates.sort(key=_candidate_sort_key)

    if len(candidates) > 1:
        print(f"   📊 Ranked {len(candidates)} candidates by path priority → score → RSI:")
        for i, (t, sig, _, _df) in enumerate(candidates):
            print(f"      {i+1}. [{t}] Path {sig.get('path','?')} | "
                  f"Score {sig.get('score','?')} | RSI {sig.get('rsi','?')}")
        print()

    # ── Option B: Daily trade cap ─────────────────────────────────────────────
    # Never deploy capital into more than MAX_TRADES_PER_DAY new positions per run.
    # Protects against cluster signals during selloffs deploying all capital at once.
    if len(candidates) > MAX_TRADES_PER_DAY:
        skipped = candidates[MAX_TRADES_PER_DAY:]
        candidates = candidates[:MAX_TRADES_PER_DAY]
        print(f"   🚦 Daily cap: keeping top {MAX_TRADES_PER_DAY}, "
              f"skipping {len(skipped)}: {[t for t,_,_,_ in skipped]}")
        _post_discord({"embeds": [{"title": "🚦 Daily Trade Cap Reached",
            "description": (f"**{len(skipped)} signal(s) skipped** — daily cap of "
                            f"{MAX_TRADES_PER_DAY} reached.\n"
                            f"Skipped: {', '.join(t for t,_,_,_ in skipped)}\n"
                            f"_Top {MAX_TRADES_PER_DAY} by path priority → score → RSI were kept._"),
            "color": COLOR_YELLOW}]})
        print()

    # Option C (Alpaca buying power check) removed — manual trading mode.
    # You execute trades yourself on IBKR based on Discord buy alerts.
    available_cash = None   # kept as None so cash-check block below is always skipped

    # ═════════════════════════════════════════════════════════════════════════
    # ═════════════════════════════════════════════════════════════════════════
    print(f"⚡ STAGE 3: Signal validation for {len(candidates)} swing candidates...\n")
    alerts_sent = 0

    for ticker, swing_signal, _, df_daily in candidates:
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

            # Daily relative volume — df_daily unpacked from candidates tuple,
            # so it always matches this ticker with indicators already written back.
            rel_vol = calculate_daily_relative_volume(df_daily) if df_daily is not None else 1.0

            # ── Volume Pace Gate ──────────────────────────────────────────────
            # Raw volume is meaningless without context of how much of the day
            # has elapsed. 0.15x at 10am is actually strong (pacing at 1.9x);
            # 0.4x at 3pm is weak (pacing at 0.47x). Projecting forward gives
            # a single unified check that works correctly at all hours.
            #
            # Examples:
            #   10:00am — elapsed 30min — 0.15x raw → 0.15/0.077 = 1.95x pace ✅
            #   12:00pm — elapsed 150min — 0.35x raw → 0.35/0.385 = 0.91x pace ✅
            #    3:00pm — elapsed 330min — 0.40x raw → 0.40/0.846 = 0.47x pace ❌
            safe_elapsed      = max(elapsed_min, 1.0)          # prevent div/0 at open
            pct_of_day        = min(safe_elapsed / 390.0, 1.0) # 390 min = full session
            vol_pace          = rel_vol / pct_of_day
            MIN_VOL_PACE      = 0.6

            # Volume pace gate removed — backtest never included it, no bearing
            # on yesterday's completed bar score. Still logged for reference.
            print(f"   📊 Volume Pace: {vol_pace:.1f}x projected "
                  f"(actual {rel_vol:.2f}x so far, {pct_of_day*100:.0f}% of day elapsed)")

            # Fire the alert — label as NEW or STILL ACTIVE based on trade log
            # Every valid signal fires an alert regardless of prior signals.
            # "STILL ACTIVE" lets you know the setup hasn't resolved yet.
            trades_check   = load_trade_log()
            prior_open     = [t for t in trades_check
                              if t["ticker"] == ticker and t["status"] == "OPEN"]
            is_repeat      = len(prior_open) > 0
            alert_label    = "STILL ACTIVE" if is_repeat else "NEW SIGNAL"

            send_setup_alert(
                ticker=ticker, currency=currency, signal=final_signal,
                rel_vol=rel_vol, elapsed_minutes=elapsed_min, mode=mode,
                regime_bullish=regime_bullish, earnings_msg=earnings_msg,
                df=df_daily, alert_label=alert_label,
            )
            alerts_sent += 1

            # Always log every new signal for outcome tracking.
            # Multiple OPEN entries are allowed — each alert is a separate signal.
            log_new_trade(ticker, currency, final_signal)
            print(f"   ✅ [{alert_label}] Buy alert sent for {ticker} "
                  f"@ ~${final_signal['price']:.2f} | Tier {final_signal.get('tier', 1)} | "
                  f"Path {final_signal.get('path', '?')}")

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


# place_alpaca_bracket_order() removed — manual trading mode.
# Orders are executed by hand on IBKR based on Discord buy alerts.
# Re-add this function from git history if reverting to Alpaca paper trading.





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
            "id":             f"{ticker}_{now.strftime('%Y%m%d_%H%M')}",
            "ticker":         ticker,
            "currency":       currency,
            "scenario":       signal.get("scenario", "?"),
            "mode":           signal.get("mode", "?"),
            "alert_date":     now.strftime("%Y-%m-%d"),
            "alert_time":     now.strftime("%H:%M ET"),
            # --- price levels ---
            "entry":          float(signal["price"]),
            "stop_loss":      float(signal["stop_loss"]),
            "take_profit":    float(signal["take_profit"]),
            "rr_ratio":       float(signal.get("rr_ratio", 0)),
            "support":        float(signal.get("support", 0)),
            "support_source": str(signal.get("support_source", "")),
            # --- scoring ---
            "score":          int(signal.get("score", 0)),
            "threshold":      int(signal.get("threshold", 0)),
            "trend_score":    int(signal.get("trend_score", 0)),
            "momentum_score": int(signal.get("momentum_score", 0)),
            "penalty":        int(signal.get("penalty", 0)),
            "reasons":        [str(r) for r in signal.get("reasons", [])],
            # --- indicators at signal time ---
            "rsi":            float(signal.get("rsi", 0)),
            "macd_h":         float(signal.get("macd_h", 0)),
            "atr":            float(signal.get("atr", 0)),
            "ema_21":         float(signal.get("ema_21", 0)),
            "ema_50":         float(signal.get("ema_50", 0)),
            "bbl":            float(signal.get("bbl") or 0),
            "bb_width":       float(signal.get("bb_width", 0)),
            "gap_pct":        float(signal.get("gap_pct", 0)),
            "near_52w_high":  bool(signal.get("near_52w_high", False)),
            "regime_bullish": bool(signal.get("regime_bullish", True)),
            # --- outcome (filled in by check_open_trades) ---
            "status":         "OPEN",
            "outcome_date":   None,
            "outcome_pct":    None,
            "max_price":      float(signal["price"]),
            "min_price":      float(signal["price"]),
        }
        trades.append(trade)
        save_trade_log(trades)
        print(f"   📝 Trade logged: {trade['id']}")
    except Exception as e:
        print(f"   ⚠️ Trade log error: {e}")


# =============================================================================
#  SECTION 13A — REAL-TIME SELL ALERTS
#
#  Runs on every scan during market hours. Fetches TQQQ's live price via
#  yfinance fast_info (1 lightweight API call — no full bar download needed)
#  and compares it against each OPEN trade's stop_loss and take_profit.
#
#  Three sell scenarios:
#    TARGET HIT — price >= take_profit  -> "SELL — Target Reached" (green)
#    STOP HIT   — price <= stop_loss    -> "SELL — Stop Loss Hit"  (red)
#    TRAILING   — price >= trail_pct%   -> "SELL — Trailing Alert" (yellow)
#                 fires when price drops TQQQ_TRAIL_PCT% from running high
#
#  Cooldown: once a sell alert fires for a given trade ID, it is suppressed
#  for SELL_ALERT_COOLDOWN_MINUTES to avoid alert floods on choppy price.
#  Cooldown resets when the bot restarts (/tmp storage).
#
#  Accuracy note: fast_info returns the last traded price, may lag a few
#  seconds — sufficient for swing trade exit monitoring.
# =============================================================================

# Trailing stop — fires when price pulls back from running high by this %.
# Set to 0.0 to disable trailing alerts entirely.
TQQQ_TRAIL_PCT = 5.0   # Alert if price drops >5% from the intraday high watermark


def fetch_tqqq_live_price() -> float | None:
    """
    Fetches TQQQ's current live price using yfinance fast_info.
    Returns None on any failure — callers must handle gracefully.
    fast_info is a single lightweight REST call, no historical data pulled.
    """
    try:
        price = yf.Ticker("TQQQ").fast_info.get("last_price")
        if price is not None:
            return float(price)
    except Exception as e:
        print(f"   WARNING: Live price fetch failed: {e}")
    return None


def check_live_sell_alerts():
    """
    Technical sell signal runner. Called on every scan during market hours.
    Fires purely on TQQQ's live technicals — NOT tied to any specific entry price.
    This means alerts fire whether or not you took the corresponding buy signal.

    Four technical exit signals:
      RSI_OB    — RSI(14) > 70: momentum exhausted, mean reversion likely
      RSI_CROSS — RSI crossed above 55 from below: oversold recovery complete
      EMA_LOSS  — Price closes back below 21 EMA after being above it
      MACD_PEAK — MACD histogram peaks and turns down (momentum fading at highs)

    All four use the latest daily bar (bulk_data already downloaded this run).
    Live price via fast_info is used for RSI_OB and EMA_LOSS intraday checks.
    Cooldown: 4 hours per signal type to prevent 15-min spam.
    """
    trades      = load_trade_log()
    open_trades = [t for t in trades if t["status"] == "OPEN"]

    print(f"   Checking technical sell conditions ({len(open_trades)} open trade(s) in log)...")

    live_price = fetch_tqqq_live_price()
    if live_price is None:
        print("   Could not fetch live price — sell alert check skipped.")
        return

    print(f"   TQQQ live price: ${live_price:.2f}")

    # Update max_price watermark on all open trades (used for outcome tracking)
    changed = False
    for trade in trades:
        if trade["status"] != "OPEN":
            continue
        prev_max = trade.get("max_price", trade["entry"])
        if live_price > prev_max:
            trade["max_price"] = float(live_price)
            changed = True
    if changed:
        save_trade_log(trades)

    # ── Technical sell signals — price-agnostic ───────────────────────────────
    # These fire based on TQQQ's current technicals, not your specific entry.
    # You decide whether to act based on your own position.

    # We need the latest RSI, EMA_21, and MACD_H values.
    # These are available from bulk_data which was already downloaded this run.
    # We access them via the module-level bulk_data reference stored at scan time.
    try:
        import yfinance as yf
        raw = yf.download('TQQQ', period='60d', interval='1d',
                          auto_adjust=True, progress=False)
        if raw.empty:
            print("   Could not fetch TQQQ daily bars for technical sell check.")
            return
        raw.columns = raw.columns.get_level_values(0)
        raw['RSI']    = ta_lib.momentum.RSIIndicator(raw['Close'], window=14).rsi()
        raw['EMA_21'] = ta_lib.trend.EMAIndicator(raw['Close'], window=21).ema_indicator()
        try:
            _macd_sell = ta_lib.trend.MACD(raw['Close'])
            raw['MACD_H'] = _macd_sell.macd_diff()
        except Exception:
            pass
        raw.dropna(subset=['RSI', 'EMA_21'], inplace=True)
        if len(raw) < 3:
            return

        today = raw.iloc[-1]
        prev  = raw.iloc[-2]
        prev2 = raw.iloc[-3]

        rsi_today   = float(today['RSI'])
        rsi_prev    = float(prev['RSI'])
        ema_21      = float(today['EMA_21'])
        macd_today  = float(today.get('MACD_H', 0)) if 'MACD_H' in raw.columns else None
        macd_prev   = float(prev.get('MACD_H',  0)) if 'MACD_H' in raw.columns else None
        macd_prev2  = float(prev2.get('MACD_H', 0)) if 'MACD_H' in raw.columns else None

    except Exception as e:
        print(f"   Technical sell data fetch failed: {e}")
        return

    tz     = pytz.timezone(TIMEZONE)
    et_now = datetime.now(tz)

    def _sell_cooldown_key(reason):
        return f"TQQQ_TECHNICAL|{reason}"

    # Track which sell signals fired this session using /tmp
    # Not for suppression — purely so we can label NEW vs STILL ACTIVE
    sell_session_file = Path("/tmp/sell_session_fired.json")
    try:
        session_fired = json.loads(sell_session_file.read_text()) if sell_session_file.exists() else {}
    except Exception:
        session_fired = {}

    def _check_and_send(reason: str, title: str, desc: str, color: int):
        # Label as NEW or STILL ACTIVE — never suppress
        already_fired  = session_fired.get(reason, False)
        status_label   = "🔔 STILL ACTIVE" if already_fired else "🚨 NEW"
        titled         = f"{status_label} — {title}"
        payload = {"embeds": [{
            "title":       titled,
            "description": desc,
            "color":       color,
            "footer":      {"text": "TQQQ Alert Bot v6.2 | Technical Sell Signal"},
            "timestamp":   datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        }]}
        _post_discord(payload)
        session_fired[reason] = True
        try:
            sell_session_file.write_text(json.dumps(session_fired))
        except Exception:
            pass
        print(f"   [{status_label}] Sell alert [{reason}] RSI={rsi_today:.1f} price=${live_price:.2f}")

    # ── SIGNAL 1: RSI OVERBOUGHT (RSI > 70) ──────────────────────────────────
    # TQQQ momentum exhausted — high probability of mean reversion.
    # Most reliable exit signal. Acts on live intraday RSI approximation.
    if rsi_today > 70:
        _check_and_send(
            reason = "RSI_OB",
            title  = "🔴 SELL SIGNAL — TQQQ RSI Overbought",
            color  = 15548997,
            desc   = (
                f"RSI(14) has reached **{rsi_today:.1f}** — momentum is exhausted.\n\n"
                f"**Action:** Consider selling your TQQQ position.\n\n"
                f"• Live Price: `${live_price:.2f}`\n"
                f"• RSI Today: `{rsi_today:.1f}` (threshold: 70)\n"
                f"• RSI Yesterday: `{rsi_prev:.1f}`\n"
                f"• 21 EMA: `${ema_21:.2f}`\n\n"
                f"_This is a technical signal — not tied to your specific entry price. "
                f"You decide whether to act based on your position._"
            )
        )

    # ── SIGNAL 2: RSI MEAN REVERSION COMPLETE (RSI crosses above 55 from below) ──
    # Oversold recovery is done — the original buy thesis has played out.
    # RSI was below 45 yesterday (confirming prior oversold) and is above 55 today.
    elif rsi_today > 55 and rsi_prev < 45:
        _check_and_send(
            reason = "RSI_CROSS",
            title  = "🟠 SELL SIGNAL — TQQQ RSI Recovery Complete",
            color  = 16744272,   # orange
            desc   = (
                f"RSI has recovered from oversold: **{rsi_prev:.1f} → {rsi_today:.1f}**\n\n"
                f"The mean-reversion trade has played out. "
                f"This is a natural exit point.\n\n"
                f"• Live Price: `${live_price:.2f}`\n"
                f"• RSI Today: `{rsi_today:.1f}` (crossed above 55)\n"
                f"• RSI Yesterday: `{rsi_prev:.1f}` (was below 45)\n"
                f"• 21 EMA: `${ema_21:.2f}`\n\n"
                f"_Consider booking profits — the oversold setup that triggered your "
                f"buy has now fully resolved._"
            )
        )

    # ── SIGNAL 3: EMA LOSS (price closes below 21 EMA after being above) ─────
    # Short-term uptrend broken. Intraday live price used for immediacy.
    prev_close  = float(prev['Close'])
    was_above   = prev_close > float(prev['EMA_21'])
    now_below   = live_price < ema_21
    if was_above and now_below:
        drop_pct = (prev_close - live_price) / prev_close * 100
        _check_and_send(
            reason = "EMA_LOSS",
            title  = "🟡 SELL SIGNAL — TQQQ 21 EMA Lost",
            color  = 16776960,   # yellow
            desc   = (
                f"Price has dropped below the 21 EMA — short-term uptrend broken.\n\n"
                f"• Live Price: `${live_price:.2f}` (below 21 EMA `${ema_21:.2f}`)\n"
                f"• Yesterday's Close: `${prev_close:.2f}` (was above EMA)\n"
                f"• Drop: `{drop_pct:.1f}%` from yesterday's close\n"
                f"• RSI: `{rsi_today:.1f}`\n\n"
                f"_The 21 EMA is TQQQ's primary short-term support. Losing it "
                f"often signals more downside. Consider tightening your stop "
                f"or exiting if this is a Tier 2 position._"
            )
        )

    # ── SIGNAL 4: MACD HISTOGRAM PEAK (momentum fading at highs) ─────────────
    # Histogram was rising, peaked yesterday, and is now falling.
    # Only fires when MACD_H is still positive (at highs, not in a selloff).
    if (macd_today is not None and macd_prev is not None and macd_prev2 is not None):
        macd_peaked = (macd_prev > macd_prev2 and    # prev was higher than day before
                       macd_today < macd_prev and    # today is lower than prev (turning down)
                       macd_prev > 0)                # still in positive territory (at highs)
        if macd_peaked:
            _check_and_send(
                reason = "MACD_PEAK",
                title  = "🔵 SELL SIGNAL — TQQQ MACD Momentum Fading",
                color  = 3447003,   # blue
                desc   = (
                    f"MACD histogram has peaked and turned down — momentum fading.\n\n"
                    f"• Live Price: `${live_price:.2f}`\n"
                    f"• MACD Histogram: `{macd_prev2:.3f}` → `{macd_prev:.3f}` (peak) → `{macd_today:.3f}` (turning down)\n"
                    f"• RSI: `{rsi_today:.1f}`\n"
                    f"• 21 EMA: `${ema_21:.2f}`\n\n"
                    f"_MACD peak is an early warning — price hasn't broken down yet. "
                    f"Consider partial profit-taking or tightening your stop._"
                )
            )



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

            # Slice from alert_date forward to catch any days the bot missed.
            # Using iloc[-1] only would miss stop/target hits on skipped days
            # (weekends, GitHub Actions outages, holidays) — a silent memory gap.
            alert_date_str = trade.get("alert_date", "")
            try:
                df_window = df.loc[alert_date_str:] if alert_date_str else df
            except KeyError:
                df_window = df
            if df_window.empty:
                continue

            entry  = trade["entry"]
            target = trade["take_profit"]
            stop   = trade["stop_loss"]

            latest_close = float(df_window['Close'].iloc[-1])
            changed = True

            # ── Chronological row-by-row scan — first event wins ─────────────
            # Bug fix: scanning max/min across full window would mark WON trades
            # as AMBIGUOUS if the stop was later hit after target was already hit.
            # Correct approach: walk each bar in order, stop at first trigger.
            outcome_found = None
            outcome_date  = None
            outcome_bar   = None

            for bar_date, bar in df_window.iterrows():
                bar_high = float(bar['High'])
                bar_low  = float(bar['Low'])

                # Update watermarks on every bar regardless
                trade["max_price"] = float(max(trade.get("max_price", entry), bar_high))
                trade["min_price"] = float(min(trade.get("min_price", entry), bar_low))

                # On a single bar where both are hit, we cannot know order —
                # assume target hit first (favourable, consistent with limit order
                # behaviour: limit fills before stop on same bar in most brokers)
                if bar_low <= stop and bar_high >= target:
                    outcome_found = "WON"   # target assumed hit first on same bar
                    outcome_date  = str(bar_date.date() if hasattr(bar_date,'date') else bar_date)
                    outcome_bar   = bar
                    break
                elif bar_high >= target:
                    outcome_found = "WON"
                    outcome_date  = str(bar_date.date() if hasattr(bar_date,'date') else bar_date)
                    outcome_bar   = bar
                    break
                elif bar_low <= stop:
                    outcome_found = "LOST"
                    outcome_date  = str(bar_date.date() if hasattr(bar_date,'date') else bar_date)
                    outcome_bar   = bar
                    break

            if outcome_found == "WON":
                trade["status"]       = "WON"
                trade["outcome_date"] = outcome_date
                trade["outcome_pct"]  = round((target - entry) / entry * 100, 2)
                resolved.append(trade)
                print(f"   ✅ {trade['ticker']} WON — target ${target:.2f} hit on {outcome_date}")

            elif outcome_found == "LOST":
                trade["status"]       = "LOST"
                trade["outcome_date"] = outcome_date
                trade["outcome_pct"]  = round((stop - entry) / entry * 100, 2)
                resolved.append(trade)
                print(f"   🛑 {trade['ticker']} LOST — stop ${stop:.2f} hit on {outcome_date}")

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
            "footer":      {"text": f"TQQQ Alert Bot v6.2 | {len(trades)} total trades logged"},
        }]}
        _post_discord(payload)
        print(f"   📊 Outcome summary sent ({len(open_tr)} open, {len(resolved)} resolved)")

    except Exception as e:
        print(f"⚠️ Outcome summary error: {e}")

# =============================================================================
#  ENTRY POINT
# =============================================================================

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="TQQQ Alert Bot v6.2")
    parser.add_argument('--mode',
        choices=['auto', 'premarket', 'swing'],
        default='auto',
        help='Scan mode. Default: auto (detects from time of day)')
    parser.add_argument('--ticker',
        type=str, default=None,
        help='Scan a single ticker, e.g. --ticker TQQQ')
    parser.add_argument('--dry-run', action='store_true',
        help='Disable duplicate guard. Safe for after-hours testing.')
    parser.add_argument('--force', action='store_true',
        help='Force scan even if market is closed. Use for manual after-hours testing.')
    args = parser.parse_args()

    DRY_RUN = args.dry_run
    if DRY_RUN:
        print("⚠️  DRY RUN — duplicate guard disabled")

    global FORCE_RUN
    FORCE_RUN = args.force
    if FORCE_RUN:
        print("⚡ --force active — market-closed gate bypassed")

    et_now = datetime.now(pytz.timezone(TIMEZONE))

    if args.mode == 'auto':
        mode = get_scan_mode(et_now)
    else:
        mode = args.mode

    if mode == 'closed' and not args.force:
        print("🚫 Market closed — nothing to do. Exiting.")
        sys.exit(0)
    elif mode == 'closed' and args.force:
        print("⚡ --force override — running swing scan outside market hours.")
        mode = 'swing'

    tickers_override = [args.ticker.upper()] if args.ticker else None
    check_market(mode=mode, tickers_override=tickers_override)
