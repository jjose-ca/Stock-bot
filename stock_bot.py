"""
=============================================================================
  STOCK ALERT BOT v6.0
=============================================================================

WHAT CHANGED FROM v5:
  Scoring engine stripped to what backtesting actually validated.

  REMOVED (added complexity, no measurable edge):
    ✗ ROC deceleration penalty   — penalized winning trades more than losers
    ✗ Category caps (4/4 system) — extra bookkeeping, no win rate improvement
    ✗ 52-week high zone bonus    — marginal signal, no consistent edge
    ✗ 9 EMA curl signal          — weak standalone signal, correlated with others
    ✗ Score threshold 7          — threshold 6 identical win rate (17.0% vs 17.1%)
    ✗ Complex BB penalty logic   — now just a warning badge, not a gate

  KEPT (validated by backtest data):
    ✓ 21 EMA wick / support detection   — core signal, best edge
    ✓ 50 EMA proximity + structure      — uptrend confirmation
    ✓ Deep oversold bypass (RSI < 35)   — 30% win rate, best signal in dataset
    ✓ RSI momentum score                — works, keep it simple
    ✓ BB lower band close               — strong mean-reversion signal
    ✓ MACD histogram improving          — momentum confirmation
    ✓ Trend floor (≥ 3) + momentum floor (≥ 2) — prevents falling knives
    ✓ ATR target 2.0x, stop 1.5x       — validated by sweep (21% win rate)
    ✓ 10-day hold (GTC orders)          — biggest improvement (+52% sim return)
    ✓ Dynamic gap filter (ATR-based)    — prevents entry on broken structure
    ✓ Dynamic stop width (ATR-based)    — self-calibrates to each stock's vol
    ✓ Market regime (VTI + VIX)         — macro filter, keep it
    ✓ Earnings check + penalty          — risk management, keep it
    ✓ Volume pace gate (after 11am)     — filters low-conviction bounces
    ✓ Trade outcome tracker             — essential for validation

  NET RESULT: ~800 lines vs 2140 in v5. Same watchlist, same Alpaca
  integration, same Discord alerts, same GitHub Actions compatibility.

HOW TO RUN:
  python bot_v6.py                  # auto-detects mode from time
  python bot_v6.py --mode swing     # force swing scan
  python bot_v6.py --ticker NVDA    # scan one ticker
  python bot_v6.py --test           # bypass market hours check

HOW TO SET UP:
  1. Set DISCORD_URL as environment variable or GitHub Secret
  2. Set ALPACA_API_KEY and ALPACA_SECRET_KEY for paper trading (optional)
  3. Push to GitHub — GitHub Actions runs it on schedule automatically
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

# ── Alpaca paper trading ──────────────────────────────────────────────────────
try:
    from alpaca.trading.client import TradingClient
    from alpaca.trading.requests import (MarketOrderRequest,
                                          TakeProfitRequest,
                                          StopLossRequest)
    from alpaca.trading.enums import OrderSide, TimeInForce, OrderClass
    ALPACA_AVAILABLE = True
except ImportError:
    ALPACA_AVAILABLE = False
    print("⚠️  alpaca-py not installed — order placement disabled")

ALPACA_KEY = os.environ.get("ALPACA_API_KEY")
ALPACA_SEC = os.environ.get("ALPACA_SECRET_KEY")

def get_alpaca_client():
    if not ALPACA_AVAILABLE or not ALPACA_KEY or not ALPACA_SEC:
        return None
    try:
        return TradingClient(ALPACA_KEY, ALPACA_SEC, paper=True)
    except Exception as e:
        print(f"⚠️ Alpaca client init failed: {e}")
        return None


# =============================================================================
#  SECTION 1 — CONFIGURATION
#  ✏️  Edit this section to customise the bot.
# =============================================================================

# ── Watchlist ─────────────────────────────────────────────────────────────────
# VTI is required — used for market regime check, never traded.
TICKERS_USD = [
    'VTI',          # ← required for regime check

    # ETFs
    'SPY', 'QQQM', 'QQQ', 'IWM',
    'SOXQ', 'XLY', 'XLF', 'XLK', 'SMH',
    'GLD', 'ITB', 'VWO', 'VEA', 'SPMO',

    # Mega cap
    'AAPL', 'MSFT', 'GOOGL', 'AMZN', 'META',
    'JPM', 'BAC', 'ABBV', 'KO', 'PG', 'JNJ', 'V', 'MA',

    # Mid-risk tech
    'NVDA', 'AVGO', 'QCOM', 'AMAT', 'LRCX',
    'NFLX', 'ORCL', 'CRM', 'NOW', 'PANW',
    'SHOP', 'UBER', 'PYPL',
    'CCL', 'DKNG', 'CVX', 'TSM', 'DIS',

    # High beta
    'TSLA', 'PLTR', 'AMD', 'ARM', 'RBLX', 'IOT',
    'SOFI', 'HOOD', 'COIN', 'MSTR', 'SNOW',
]

# CAD tickers (TSX) — tagged CA$ automatically
TICKERS_CAD = [
    'ZSP.TO', 'XEF.TO',
    'HUT.TO', 'CVE.TO', 'MFC.TO', 'ATD.TO', 'TOU.TO', 'ATZ.TO',
]

# ── Discord ───────────────────────────────────────────────────────────────────
DISCORD_WEBHOOK_URL = os.getenv('DISCORD_URL')

# ── Scoring ───────────────────────────────────────────────────────────────────
# v6: threshold reduced back to 6 — backtest showed 17.0% win vs 17.1% at 7.
# Not worth cutting signal count in half for 0.1% win rate improvement.
SWING_SCORE_THRESHOLD = 5   # lowered from 6 — recovers signal count
                            # backtest: score 6 vs 5 same win rate, 6 cuts 90% of signals

# Per-category floors — prevent falling knives from passing on momentum alone.
# trend ≥ 3  : price must be near at least one EMA (uptrend structure intact)
# momentum ≥ 2: must have at least one real oversold / mean-reversion signal
TREND_FLOOR    = 3
MOMENTUM_FLOOR = 2

# ── Risk parameters ───────────────────────────────────────────────────────────
PORTFOLIO_VALUE       = 2000.0  # ← update when account size changes
SWING_ATR_STOP_MULT   = 2.5     # ATR sweep winner: wider stop reduces premature exits
SWING_ATR_TARGET_MULT = 3.5     # ATR sweep winner: 3.5x/2.5x combo
                                 # expectancy +1.06% vs +0.37% at 3.0x/1.5x
                                 # max drawdown 1.3% vs 3.7%
BASE_MAX_STOP_PCT     = 0.06    # floor: never tighter than 6%
ABSOLUTE_MAX_STOP_PCT = 0.15    # ceiling: never wider than 15%
MIN_RR_RATIO          = 1.5     # minimum risk/reward to fire alert

# ── Hold period ───────────────────────────────────────────────────────────────
# 10-day hold validated by backtest — hold10 outperformed 5d on every metric.
# GTC orders on Alpaca stay live until target or stop is hit.
OUTCOME_CHECK_DAYS = 21         # 21-day expiry: 10-day hold + 11-day buffer

# ── Market regime ─────────────────────────────────────────────────────────────
VIX_ELEVATED = 20   # warn in alert, +1 threshold
VIX_PANIC    = 30   # halt all new signals (open trade monitoring still runs)

# ── Volume gate ───────────────────────────────────────────────────────────────
MIN_VOL_PACE          = 0.6     # min projected daily volume pace (after 11am)

# ── Earnings ──────────────────────────────────────────────────────────────────
EARNINGS_WARNING_DAYS  = 7
EARNINGS_SCORE_PENALTY = 2

# ── Timing ────────────────────────────────────────────────────────────────────
OPENING_NOISE_MINUTES = 30      # first 30 min: +1 threshold
LATE_FRIDAY_MINUTES   = 300     # after 2:30pm Friday: +1 threshold

# ── Persistence ──────────────────────────────────────────────────────────────
TRADE_LOG_FILE      = "trade_log.json"
EARNINGS_CACHE_FILE = "earnings_cache.json"
COOLDOWN_FILE       = "/tmp/alert_cooldowns.json"
COOLDOWN_MINUTES    = {"SWING": 240}
OUTCOME_DISCORD_DAILY = True

# ── Colors ────────────────────────────────────────────────────────────────────
COLOR_BLUE   = 3447003
COLOR_GREEN  = 5763719
COLOR_RED    = 15548997

# ── Timezone ─────────────────────────────────────────────────────────────────
TIMEZONE = "US/Eastern"

ETF_TICKERS = {
    'SPY', 'SPLG', 'QQQM', 'QQQ', 'IWM', 'VTI', 'SOXQ',
    'XLY', 'GDX', 'SIL', 'XLF', 'XLK', 'SMH', 'GLD', 'SLV', 'ITB',
    'VWO', 'VEA', 'SPMO', 'ZSP.TO', 'XEF.TO',
}


# =============================================================================
#  SECTION 2 — SCHEDULER
# =============================================================================

def get_scan_mode(et_now: datetime) -> str:
    t = et_now.hour + et_now.minute / 60.0
    if 8.5  <= t < 9.5:  return "premarket"
    if 9.5  <= t < 16.0: return "swing"
    print("🌙 Outside normal scan windows — defaulting to swing mode.")
    return "swing"


def get_time_penalty(et_now: datetime) -> tuple[int, list[str]]:
    penalty = 0
    reasons = []
    mkt_open    = et_now.replace(hour=9, minute=30, second=0, microsecond=0)
    elapsed_min = max((et_now - mkt_open).total_seconds() / 60.0, 0.0)
    if elapsed_min < OPENING_NOISE_MINUTES:
        penalty += 1
        reasons.append(f"⏰ Opening noise window (+1 threshold)")
    if et_now.weekday() == 4 and elapsed_min > LATE_FRIDAY_MINUTES:
        penalty += 1
        reasons.append("📅 Late Friday — weekend gap risk (+1 threshold)")
    return penalty, reasons


def get_elapsed_minutes(et_now: datetime) -> float:
    mkt_open = et_now.replace(hour=9, minute=30, second=0, microsecond=0)
    if et_now < mkt_open:
        return 0.0
    return min((et_now - mkt_open).total_seconds() / 60.0, 390.0)


# =============================================================================
#  SECTION 3 — DATA FETCHING
# =============================================================================

def _flatten(df: pd.DataFrame) -> pd.DataFrame:
    """Collapse MultiIndex columns to single level — handles yfinance version diffs."""
    if isinstance(df.columns, pd.MultiIndex):
        ohlcv = {'Open', 'High', 'Low', 'Close', 'Volume'}
        for i in range(df.columns.nlevels):
            vals = df.columns.get_level_values(i)
            if ohlcv.intersection(set(vals)):
                df.columns = vals
                return df
        df.columns = df.columns.get_level_values(0)
    return df


def get_currency(ticker: str) -> str:
    return 'CAD' if ticker in TICKERS_CAD else 'USD'


def fetch_bulk_daily(tickers: list) -> pd.DataFrame:
    print(f"📥 Bulk downloading 1y daily data for {len(tickers)} tickers...")
    try:
        df = yf.download(
            tickers, period="1y", interval="1d",
            group_by='ticker', auto_adjust=True, progress=False,
            multi_level_index=False
        )
        print(f"   ✅ Download complete.")
        return df
    except Exception as e:
        print(f"   ❌ Bulk download failed: {e}")
        return pd.DataFrame()


def extract_ticker_daily(bulk_data: pd.DataFrame, ticker: str) -> pd.DataFrame | None:
    REQUIRED = {'Open', 'High', 'Low', 'Close', 'Volume'}
    try:
        if isinstance(bulk_data.columns, pd.MultiIndex):
            if ticker not in bulk_data.columns.get_level_values(0):
                return None
            df = bulk_data[ticker].copy()
        else:
            df = bulk_data.copy()
            df = _flatten(df)
            if REQUIRED - set(df.columns):
                return None
        df = _flatten(df)
        df.dropna(subset=['Close'], inplace=True)
        return df if not df.empty else None
    except Exception as e:
        print(f"   ⚠️ [{ticker}] Extraction failed: {e}")
        return None


def passes_liquidity_filter(df_daily: pd.DataFrame, ticker: str) -> bool:
    try:
        tail   = df_daily.tail(20)
        avg_dv = (tail['Close'] * tail['Volume']).mean()
        if avg_dv < 2_000_000:
            print(f"   [{ticker}] 💧 Dollar vol ${avg_dv/1e6:.1f}M < $2M min. Rejected.")
            return False
        return True
    except Exception as e:
        print(f"   ⚠️ [{ticker}] Liquidity check error — passing through: {e}")
        return True


def calculate_daily_relative_volume(df_daily: pd.DataFrame) -> float:
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
#  SECTION 4 — MARKET REGIME
# =============================================================================

def check_market_regime(bulk_data: pd.DataFrame) -> tuple[int, bool, bool]:
    """
    Returns (regime_penalty, regime_bullish, is_panic).
    Layer 1: VTI vs 200 SMA — bearish macro adds +1 threshold.
    Layer 2: VIX — elevated adds +1 + warning badge, panic halts all new signals.
    """
    print("🌍 Checking market regime...")
    regime_penalty = 0
    regime_bullish = True

    # Layer 1 — VTI macro trend
    try:
        vti = extract_ticker_daily(bulk_data, 'VTI')
        if vti is not None and len(vti) >= 200:
            sma200    = ta.sma(vti['Close'], length=200).iloc[-1]
            vti_price = float(vti['Close'].iloc[-1])
            if vti_price < sma200:
                print(f"   ⚠️ BEARISH (VTI ${vti_price:.2f} < 200 SMA ${sma200:.2f}) → +1 threshold")
                regime_penalty += 1
                regime_bullish  = False
            else:
                print(f"   ✅ BULLISH (VTI ${vti_price:.2f} > 200 SMA ${sma200:.2f})")
    except Exception as e:
        print(f"   ⚠️ VTI check error: {e}")

    # Layer 2 — VIX
    try:
        vix_df = extract_ticker_daily(bulk_data, '^VIX')
        if vix_df is not None and not vix_df.empty:
            vix = float(vix_df['Close'].iloc[-1])
            if vix > VIX_PANIC:
                print(f"   🚨 VIX PANIC ({vix:.1f}) — scan halted.")
                return regime_penalty, False, True
            elif vix > VIX_ELEVATED:
                print(f"   ⚠️ VIX ELEVATED ({vix:.1f}) → +1 threshold, warning badge")
                regime_penalty += 1
            else:
                print(f"   ✅ VIX NORMAL ({vix:.1f})")
    except Exception as e:
        print(f"   ⚠️ VIX check error: {e}")

    return regime_penalty, regime_bullish, False


# =============================================================================
#  SECTION 5 — SWING ENGINE (v6 simplified scoring)
#
#  Four conditions that backtest validated as actually predictive:
#    1. Price near 21 EMA — wick, support hold, or deep oversold bypass
#    2. Price above 50 EMA — uptrend structure intact
#    3. RSI — momentum reset or deeply oversold
#    4. MACD histogram — improving or positive
#
#  Max possible score = 7 (trend 4 + momentum 3)
#  Default threshold  = 6
#  Floor checks ensure trend ≥ 3 AND momentum ≥ 2
# =============================================================================

def run_swing_engine(df_daily: pd.DataFrame, total_penalty: int,
                     ticker: str = "?") -> dict | None:
    """
    Scores yesterday's completed daily bar. Returns signal dict or None.

    TWO PATHS through this function:

    PATH A — Deep Oversold Bypass (checked FIRST):
      Condition: RSI < 35 AND price above 200 EMA
      Action:    Skip all scoring, go directly to stop/target calculation.
      Rationale: Best signal in dataset (30% win rate, +3.29% avg).
                 Must be a true early exit — any standard scoring gate
                 can block it if it lives inside the scoring block.

    PATH B — Standard Additive Scoring:
      TREND (cap 4):
        +3  21 EMA wick with strong recovery
        +2  21 EMA wick early bounce
        +2  21 EMA support hold (0–2.5% above)
        +1  Testing 21 EMA from below (0–2.5% below)
        +2  Pulling back to 50 EMA (0–3% above)
        +1  Testing 50 EMA from below (0–3% below)
        +1  Price above 50 EMA (structure)
      MOMENTUM (cap 3):
        +3  RSI deeply oversold (< 35)
        +2  RSI oversold (< 45)
        +1  RSI momentum reset (< 55)
        +3  Closed at BB lower band
        +2  MACD histogram positive
        +1  MACD histogram improving
      Threshold: 5 (lowered from 6 — recovers signal count)
      Floors: trend ≥ 3 AND momentum ≥ 2
    """
    if df_daily is None or len(df_daily) < 50:
        print(f"   [{ticker}] ❌ Insufficient data")
        return None

    df = df_daily.copy()

    # Compute indicators
    df['EMA_21']  = ta.ema(df['Close'], length=21)
    df['EMA_50']  = ta.ema(df['Close'], length=50)
    df['EMA_200'] = ta.ema(df['Close'], length=200)
    df['RSI']     = ta.rsi(df['Close'], length=14)
    df['ATR']     = ta.atr(df['High'], df['Low'], df['Close'], length=14)

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
        if all([lower_col, mid_col, upper_col]):
            df['BB_WIDTH'] = (bb[upper_col[0]] - bb[lower_col[0]]) / bb[mid_col[0]]

    df.dropna(subset=['RSI', 'EMA_50', 'ATR'], inplace=True)
    if len(df) < 3:
        return None

    # Score on yesterday's COMPLETED bar — never on today's in-progress candle
    scored = df.iloc[-2]   # yesterday — completed, all signal logic
    prev   = df.iloc[-3]   # day before — MACD direction comparison
    today  = df.iloc[-1]   # today — entry price only

    price       = float(scored['Close'])
    entry_price = float(today['Close'])
    ema_21      = float(scored['EMA_21'])
    ema_50      = float(scored['EMA_50'])
    ema_200     = float(scored['EMA_200']) if 'EMA_200' in df.columns else None
    rsi         = float(scored['RSI'])
    atr         = float(scored['ATR'])
    bbl         = float(scored['BBL'])      if 'BBL'      in df.columns else None
    bb_width    = float(scored['BB_WIDTH']) if 'BB_WIDTH' in df.columns else 0.0
    macd_h      = float(scored['MACD_H'])   if 'MACD_H'   in df.columns else 0.0
    prev_mh     = float(prev['MACD_H'])     if 'MACD_H'   in df.columns else 0.0
    daily_low   = float(scored['Low'])

    # =========================================================================
    #  PATH A — DEEP OVERSOLD BYPASS
    #  Checked FIRST. True early exit — does not touch the scoring block.
    #
    #  Requirements:
    #    RSI < 35  : deeply oversold, mean-reversion edge is strong
    #    Above 200 EMA: secular uptrend intact — not a value trap
    #
    #  Why early exit matters:
    #    In v6 initial release, the bypass lived inside the trend scoring block.
    #    It could only fire when trend_score == 0 (no EMA proximity signals).
    #    But if RSI < 35 and the stock also happened to be near the 50 EMA,
    #    trend_score would already be > 0, and the bypass condition was never
    #    reached. This caused 0 bypass trades over 3 years in the backtest.
    #    Moving it here ensures it always fires when conditions are met.
    # =========================================================================
    if rsi < 35 and ema_200 is not None and price > ema_200:
        print(f"   [{ticker}] 💎 OVERSOLD BYPASS — RSI {rsi:.1f}, above 200 EMA ${ema_200:.2f}")
        score         = 6   # fixed score — bypass always passes
        trend_score   = 3   # synthetic — satisfies floor check for logging
        momentum_score = 3  # RSI < 35 = max momentum
        oversold_bypass = True
        bb_squeeze_warning = (0 < bb_width < 0.025)
        reasons = [
            f"💎 Deep Oversold Bounce — RSI {rsi:.1f} (< 35)",
            f"✅ Above 200 EMA ${ema_200:.2f} — secular uptrend intact",
            f"🎯 Mean reversion setup — best signal in backtest (30% win rate)",
        ]
        # Jump directly to stop/target calculation — skip all scoring gates
        # (threshold check, floor check, and standard scoring all bypassed)
        pass   # fall through to stop/target block below

    else:
        # =====================================================================
        #  PATH B — STANDARD ADDITIVE SCORING
        # =====================================================================
        oversold_bypass    = False
        bb_squeeze_warning = (0 < bb_width < 0.025)
        reasons            = []

        # ── TREND SCORE (cap at 4) ────────────────────────────────────────────
        trend_score = 0

        # 21 EMA wick detection
        wick_to_21 = (daily_low <= ema_21 * 1.005) and (price > ema_21) and (rsi < 65)
        pct_21     = (price - ema_21) / ema_21
        near_21    =  0.000 <= pct_21 < 0.025
        below_21   = -0.025 <= pct_21 < 0.000

        if wick_to_21 and not near_21:
            trend_score += 3
            reasons.append(f"⚡ Wick to 21 EMA + Strong Recovery "
                           f"(Low ${daily_low:.2f} → Close ${price:.2f})")
        elif wick_to_21 and near_21:
            trend_score += 2
            reasons.append(f"⚡ Wick to 21 EMA — Early Bounce (${ema_21:.2f})")
        elif near_21 and rsi < 62:
            trend_score += 2
            reasons.append(f"📈 21 EMA Support Hold (${ema_21:.2f})")
        elif below_21 and rsi < 62:
            trend_score += 1
            reasons.append(f"⚠️ Testing 21 EMA from Below (${ema_21:.2f})")

        # 50 EMA proximity
        pct_50 = (price - ema_50) / ema_50
        if 0 <= pct_50 < 0.03:
            trend_score += 2
            reasons.append(f"📊 Pulling Back to 50 EMA (${ema_50:.2f})")
        elif -0.03 <= pct_50 < 0:
            trend_score += 1
            reasons.append(f"⚠️ Testing 50 EMA from Below (${ema_50:.2f})")

        # Bullish structure
        if price > ema_50:
            trend_score += 1
            reasons.append("✅ Price Above 50 EMA (bullish structure)")

        trend_score = min(trend_score, 4)

        # ── MOMENTUM SCORE (cap at 3) ─────────────────────────────────────────
        momentum_score = 0

        if rsi < 35:
            momentum_score += 3
            reasons.append(f"💎 RSI Deeply Oversold ({rsi:.1f})")
        elif rsi < 45:
            momentum_score += 2
            reasons.append(f"📉 RSI Oversold ({rsi:.1f})")
        elif rsi < 55:
            momentum_score += 1
            reasons.append(f"🌊 RSI Momentum Reset ({rsi:.1f})")

        if bbl is not None and price <= bbl * 1.02:
            momentum_score += 3
            reasons.append(f"🛡️ Closed at BB Lower Band (${bbl:.2f})")

        if macd_h > 0:
            momentum_score += 2
            reasons.append("🚀 MACD Histogram Positive")
        elif macd_h > prev_mh:
            momentum_score += 1
            reasons.append("🔄 MACD Histogram Improving")

        momentum_score = min(momentum_score, 3)

        # ── GATES ─────────────────────────────────────────────────────────────
        score     = trend_score + momentum_score
        threshold = min(SWING_SCORE_THRESHOLD + total_penalty, 7)

        if score < threshold:
            print(f"   [{ticker}] ❌ Score {score}/{threshold} "
                  f"(trend={trend_score} momentum={momentum_score})")
            return None

        if trend_score < TREND_FLOOR:
            print(f"   [{ticker}] ❌ Trend floor {trend_score} < {TREND_FLOOR} "
                  f"— possible downtrend. Rejected.")
            return None

        if momentum_score < MOMENTUM_FLOOR:
            print(f"   [{ticker}] ❌ Momentum floor {momentum_score} < {MOMENTUM_FLOOR} "
                  f"— no oversold signal. Rejected.")
            return None

        print(f"   [{ticker}] ✅ Score {score}/{threshold} "
              f"(trend={trend_score} momentum={momentum_score})")

    # ── STOP / TARGET PLACEMENT ───────────────────────────────────────────────
    # Both paths (bypass and standard) converge here.
    # near_21 may not be defined on bypass path — recompute safely.
    above_21 = price >= ema_21
    above_50 = price >= ema_50
    near_50  = abs(price - ema_50) / ema_50 < 0.03
    pct_21   = (price - ema_21) / ema_21
    near_21  = 0.000 <= pct_21 < 0.025   # recomputed — safe on both paths
    # threshold may not be set on bypass path (Path A sets score=6 directly)
    threshold = threshold if 'threshold' in dir() else SWING_SCORE_THRESHOLD

    # Bypass path: 200 EMA is the structural anchor for deep oversold setups.
    # 21 EMA will be above a crashed price — leads to false "support > entry" rejection.
    if oversold_bypass and ema_200 is not None:
        support, support_source = ema_200, "200 EMA (oversold bypass anchor)"
    elif bbl is not None and price <= bbl * 1.02:
        support, support_source = bbl, "BB Lower Band"
    elif above_21 and near_21:
        support, support_source = ema_21, "21 EMA"
    elif above_21 and not above_50:
        support, support_source = ema_21, "21 EMA (50 EMA above = resistance)"
    elif not above_21 and above_50 and near_50:
        support, support_source = ema_50, "50 EMA"
    elif not above_21 and above_50:
        support, support_source = entry_price, "Volatility Stop (between EMAs)"
    elif above_50 and near_50:
        support, support_source = ema_50, "50 EMA"
    else:
        support, support_source = ema_21, "21 EMA (fallback)"

    # Reject if support is above entry — would produce nonsensical stop
    if support > entry_price and not support_source.startswith("Volatility Stop"):
        print(f"   [{ticker}] ❌ Support ({support_source} ${support:.2f}) above entry "
              f"(${entry_price:.2f}) — no valid stop. Rejected.")
        return None

    if support_source.startswith("Volatility Stop"):
        stop_loss = entry_price - (atr * SWING_ATR_STOP_MULT)
    else:
        stop_loss = support - (atr * SWING_ATR_STOP_MULT)

    if stop_loss >= entry_price:
        stop_loss = entry_price - atr

    take_profit = entry_price + (atr * SWING_ATR_TARGET_MULT)

    # ── GAP FILTER ────────────────────────────────────────────────────────────
    # If today moved too far from yesterday's scored bar, structure is broken.
    # Dynamic threshold — proportional to stock's own ATR (floor 2%, ceil 7%).
    gap_pct     = (entry_price - price) / price * 100
    atr_pct     = (atr / price) * 100
    max_gap     = max(2.0, min(atr_pct * 1.5, 7.0))
    if abs(gap_pct) > max_gap:
        print(f"   [{ticker}] ❌ Gap {gap_pct:+.1f}% > {max_gap:.1f}% dynamic max. Rejected.")
        return None

    print(f"   [{ticker}] 📍 Scored ${price:.2f} | Entry ${entry_price:.2f} "
          f"({'↑' if gap_pct >= 0 else '↓'}{abs(gap_pct):.1f}%)")

    return {
        "score":            score,
        "threshold":        threshold,
        "trend_score":      trend_score,
        "momentum_score":   momentum_score,
        "reasons":          reasons,
        "price":            round(entry_price, 2),
        "stop_loss":        round(stop_loss, 2),
        "take_profit":      round(take_profit, 2),
        "support":          round(support, 2),
        "support_source":   support_source,
        "atr":              round(atr, 4),
        "atr_source":       "Daily",
        "rsi":              round(rsi, 1),
        "ema_21":           round(ema_21, 2),
        "ema_50":           round(ema_50, 2),
        "ema_200":          round(ema_200, 2) if ema_200 else None,
        "bbl":              round(bbl, 2) if bbl else None,
        "bb_width":         round(bb_width, 4),
        "bb_squeeze_warning": bb_squeeze_warning,
        "macd_h":           round(macd_h, 4),
        "gap_pct":          round(gap_pct, 2),
        "oversold_bypass":  oversold_bypass,
        "is_bullish":       price > ema_21,
        "mode":             "SWING",
        "direction":        "long",
    }


# =============================================================================
#  SECTION 6 — POSITION SIZING + RISK VALIDATION
# =============================================================================

def calculate_position_size(score: int, threshold: int,
                             price: float, atr: float) -> dict:
    """
    Dynamic allocation: base 8% + conviction boost − volatility penalty.
    Capped at 12% per trade.
    """
    base      = 8.0
    margin    = max(score - threshold, 0)
    conviction = min(margin * 1.0, 4.0)

    atr_pct = (atr / price) * 100 if price > 0 else 2.0
    if atr_pct <= 1.5:
        vol_factor = 1.0
    elif atr_pct <= 3.0:
        vol_factor = 1.0 - ((atr_pct - 1.5) / 1.5) * 0.4
    else:
        vol_factor = 0.6

    pct           = round(min((base + conviction) * vol_factor, 12.0), 1)
    dollar_amount = round(PORTFOLIO_VALUE * pct / 100, 2)
    shares        = int(dollar_amount // price) if price > 0 else 0

    vol_tag    = "low vol" if atr_pct <= 1.5 else ("mid vol" if atr_pct <= 3.0 else "high vol ⚠️")
    share_note = (f"≈ {shares} share{'s' if shares != 1 else ''}"
                  if shares >= 1 else "⚠️ < 1 share — use fractional shares")
    label = (f"{pct}% = **${dollar_amount:.2f}** ({share_note}) "
             f"[+{margin} above min · ATR {atr_pct:.1f}% · {vol_tag}]")

    return {"pct": pct, "dollar": dollar_amount, "shares": shares, "label": label}


def validate_risk(signal: dict, ticker: str = "?") -> dict | None:
    """
    Dynamic stop width check + R/R gate.
    Max stop = max(6%, min(ATR% × 2.5, 15%)) — self-calibrates to each stock's vol.
    """
    price     = signal["price"]
    stop_loss = signal["stop_loss"]
    target    = signal["take_profit"]
    atr       = signal.get("atr", 0)

    atr_pct          = atr / price if price > 0 else 0
    dynamic_max_stop = max(BASE_MAX_STOP_PCT, min(atr_pct * 2.5, ABSOLUTE_MAX_STOP_PCT))
    actual_pct       = (price - stop_loss) / price

    if actual_pct > dynamic_max_stop:
        print(f"   [{ticker}] ❌ Stop {actual_pct*100:.1f}% > {dynamic_max_stop*100:.1f}% max. Rejected.")
        return None

    risk   = price - stop_loss
    reward = target - price
    if risk <= 0:
        print(f"   [{ticker}] ❌ Invalid stop (risk ≤ 0). Rejected.")
        return None

    rr  = round(reward / risk, 2)
    rsi = signal.get("rsi", 50.0)
    min_rr = 1.5 if rsi < 45 else 1.6

    signal["rr_ratio"]    = rr
    signal["min_rr_used"] = min_rr

    if rr < min_rr:
        print(f"   [{ticker}] ❌ R/R {rr:.2f} < min {min_rr} (RSI {rsi:.1f}). Rejected.")
        return None

    return signal


# =============================================================================
#  SECTION 7 — EARNINGS CHECK
# =============================================================================

def load_earnings_cache() -> dict:
    try:
        p = Path(EARNINGS_CACHE_FILE)
        if p.exists():
            with open(p) as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def save_earnings_cache(cache: dict):
    try:
        with open(EARNINGS_CACHE_FILE, "w") as f:
            json.dump(cache, f, indent=2)
    except Exception as e:
        print(f"   ⚠️ Earnings cache save failed: {e}")


def check_earnings(ticker: str) -> tuple[bool, str]:
    """
    Returns (has_warning, message).
    Uses a rolling 7-day cache — 0 extra API calls on cache hit.
    """
    if ticker in ETF_TICKERS:
        return False, ""

    eastern = pytz.timezone(TIMEZONE)
    today   = datetime.now(eastern).date()
    cache   = load_earnings_cache()
    entry   = cache.get(ticker)

    if entry:
        age = (today - datetime.strptime(entry["fetched"], "%Y-%m-%d").date()).days
        if age < 7:
            print(f"   💾 [{ticker}] Earnings cache hit (age {age}d)")
            ed = entry.get("earnings_date")
            if ed is None:
                return False, ""
            days = (datetime.strptime(ed, "%Y-%m-%d").date() - today).days
            if 0 <= days <= EARNINGS_WARNING_DAYS:
                return True, (f"⚠️ **EARNINGS WARNING:** Report in "
                              f"{days} day{'s' if days != 1 else ''} ({ed})")
            return False, ""

    try:
        print(f"   🌐 [{ticker}] Fetching earnings date...")
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

        ed_str = None
        if earnings_date is not None:
            earnings_date = pd.to_datetime(earnings_date).date()
            ed_str = str(earnings_date)

        cache[ticker] = {"earnings_date": ed_str, "fetched": str(today)}
        save_earnings_cache(cache)

        if ed_str is None:
            return False, ""
        days = (earnings_date - today).days
        if 0 <= days <= EARNINGS_WARNING_DAYS:
            return True, (f"⚠️ **EARNINGS WARNING:** Report in "
                          f"{days} day{'s' if days != 1 else ''} ({ed_str})")
        return False, ""

    except Exception as e:
        print(f"   ⚠️ [{ticker}] Earnings fetch failed: {e}")
        return False, ""


def apply_earnings_penalty(signal: dict, total_penalty: int) -> dict | None:
    stored_threshold = signal.get("threshold", SWING_SCORE_THRESHOLD + total_penalty)
    signal["score"] -= EARNINGS_SCORE_PENALTY
    print(f"   ⚠️ Earnings penalty applied: score → {signal['score']} (min {stored_threshold})")
    if signal["score"] < stored_threshold:
        print(f"   ❌ Score below threshold after penalty. Rejected.")
        return None
    return signal


# =============================================================================
#  SECTION 8 — COOLDOWN
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
        print(f"⚠️ JSON save failed ({path}): {e}")


def is_on_cooldown(ticker: str, mode: str) -> bool:
    cooldowns = _load_json(COOLDOWN_FILE)
    key = f"{ticker}_{mode}"
    if key not in cooldowns:
        return False
    try:
        tz   = pytz.timezone(TIMEZONE)
        last = datetime.fromisoformat(cooldowns[key])
        if last.tzinfo is None:
            last = tz.localize(last)
        mins  = (datetime.now(tz) - last).total_seconds() / 60.0
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


# =============================================================================
#  SECTION 9 — DISCORD ALERTS
# =============================================================================

def _post_discord(payload: dict):
    if not DISCORD_WEBHOOK_URL:
        print("⚠️  DISCORD_URL not set — skipping webhook.")
        return
    try:
        r = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=10)
        if not r.ok:
            print(f"❌ Discord error: {r.status_code} — {r.text[:300]}")
            return
        r.raise_for_status()
        time.sleep(0.5)
    except Exception as e:
        print(f"❌ Discord error: {e}")


def send_setup_alert(ticker: str, currency: str, signal: dict,
                     rel_vol: float, elapsed_minutes: float,
                     regime_bullish: bool, earnings_msg: str = ""):
    """Sends swing trade setup embed to Discord."""
    et_now   = datetime.now(pytz.timezone(TIMEZONE))
    sym      = 'CA$' if currency == 'CAD' else '$'
    price    = signal["price"]
    stop     = signal["stop_loss"]
    target   = signal["take_profit"]
    rr       = signal.get("rr_ratio", 0.0)
    atr_val  = signal.get("atr", 0.0)
    score    = signal.get("score", 0)
    thresh   = signal.get("threshold", 0)
    stop_pct = (price - stop) / price * 100
    tgt_pct  = (target - price) / price * 100
    rsi_val  = signal.get("rsi", 0.0)

    if rsi_val < 30:    rsi_label = "🔴 Deeply Oversold"
    elif rsi_val < 45:  rsi_label = "🟠 Oversold"
    elif rsi_val < 55:  rsi_label = "🟡 Neutral"
    elif rsi_val < 65:  rsi_label = "🟢 Bullish"
    else:               rsi_label = "⚪ Extended"

    def ema_str(val):
        if not val: return "N/A"
        pct = (price - val) / val * 100
        return f"{sym}{val:.2f} ({abs(pct):.1f}% {'above' if pct >= 0 else 'below'})"

    if elapsed_minutes < 20:    session = "⏰ Opening (noisy)"
    elif elapsed_minutes > 360: session = "🕒 Near Close"
    else:                       session = "✅ Normal Hours"

    regime_label = "🟢 Bullish" if regime_bullish else "🔴 Bearish"
    size_info    = signal.get("size_guidance", "—")

    desc  = f"*{et_now.strftime('%I:%M %p ET')}*\n"
    desc += f"**📅 SWING SETUP** · {regime_label} · {session} · `{currency}`\n"
    desc += f"📊 **Score:** `{score}` / min `{thresh}` (+{score - thresh} margin)\n"

    if earnings_msg:
        desc += f"\n{earnings_msg}\n"

    desc += "\n━━━━━━━━━━━━━━━━━━━━━━━━\n"
    desc += "📊 **Trade Plan**\n"
    desc += f"• **Entry:**      `{sym}{price:.2f}`\n"
    desc += f"• **Target:**     `{sym}{target:.2f}` (+{tgt_pct:.1f}%) 🎯\n"
    desc += f"• **Stop:**       `{sym}{stop:.2f}` (−{stop_pct:.1f}%) 🛑\n"
    desc += f"• **R/R:**        `1:{rr:.2f}` ⚖️\n"
    desc += f"• **Risk/Share:** `{sym}{price - stop:.2f}` | ATR `{sym}{atr_val:.2f}`\n"
    desc += f"• **Size:**       `{size_info}`\n"
    desc += f"• **Hold:**       Up to 10 days (GTC bracket order)\n"

    desc += "\n📉 **Technicals**\n"
    desc += f"• **RSI:**    `{rsi_val:.1f}` {rsi_label}\n"
    desc += f"• **21 EMA:** `{ema_str(signal.get('ema_21'))}`\n"
    if signal.get('ema_50'):
        desc += f"• **50 EMA:** `{ema_str(signal.get('ema_50'))}`\n"

    support_src = signal.get('support_source', '')
    support_val = signal.get('support', 0)
    if support_src and support_val:
        if support_src.startswith("Volatility Stop"):
            desc += "• **Stop Anchor:** Volatility stop — price between EMAs\n"
        else:
            desc += (f"• **Stop Anchor:** `{sym}{support_val:.2f}` ({support_src}) "
                     f"— stop = support − ATR×1.5\n")

    if signal.get("bb_squeeze_warning"):
        desc += "• ⚠️ **BB Squeeze** — volatility compressed, breakout possible\n"

    if rel_vol > 2.0:    vol_label = "🔥 Heavy"
    elif rel_vol > 1.2:  vol_label = "💪 Above Average"
    else:                vol_label = "😐 Below Average"
    desc += f"• **Volume:** `{rel_vol:.1f}x` avg · {vol_label}\n"

    desc += "\n📝 **Why This Signal**\n"
    for r in signal.get("reasons", []):
        desc += f"• {r}\n"

    # Trim to Discord 4096 char limit
    LIMIT = 4096
    if len(desc) > LIMIT:
        base = desc[:desc.index("\n📝 **Why This Signal**\n")]
        base += "\n📝 **Why This Signal**\n"
        for r in signal.get("reasons", []):
            candidate = base + f"• {r}\n"
            if len(candidate) > LIMIT - 40:
                base += "_...trimmed_\n"
                break
            base = candidate
        desc = base

    # Badges
    badges = []
    if not regime_bullish:
        badges.append("⚠️ BEARISH REGIME")
    vix_val = signal.get("vix_level", 0)
    if VIX_ELEVATED < vix_val <= VIX_PANIC:
        badges.append(f"⚠️ VIX ELEVATED ({vix_val:.0f})")
    if signal.get("oversold_bypass"):
        badges.append("💎 DEEP OVERSOLD BYPASS")
    if badges:
        desc += f"\n🏷️ {' | '.join(badges)}"

    payload = {
        "content": f"🚨 **{ticker}** | SWING | `{currency}`",
        "embeds": [{
            "title":       f"📅 SWING — {ticker}  (Score {score})",
            "description": desc,
            "color":       COLOR_BLUE,
            "fields":      [{"name": "🔗 Chart",
                             "value": f"[Yahoo Finance](https://finance.yahoo.com/quote/{ticker})",
                             "inline": False}],
            "footer":      {"text": f"Stock Alert Bot v6.0 | ATR: Daily"},
        }],
    }
    _post_discord(payload)
    print(f"   ✅ Alert sent: {ticker} | Score {score} | {currency}")


def send_premarket_summary(summaries: list[dict]):
    if not summaries:
        return
    et_now    = datetime.now(pytz.timezone(TIMEZONE))
    gap_items = sorted(
        [s for s in summaries if abs(s.get('gap_pct', 0)) >= 2.0],
        key=lambda x: abs(x.get('gap_pct', 0)), reverse=True
    )
    flat_count = len(summaries) - len(gap_items)

    if not gap_items:
        desc = f"No significant gaps — {flat_count} tickers flat.\n\n_Signals fire during market hours._"
    else:
        lines = [f"• **{s['ticker']}** `{'CA$' if s['currency'] == 'CAD' else '$'}"
                 f"{s['price']:.2f}` — {s['note']}" for s in gap_items]
        desc  = f"**{len(gap_items)} significant gaps** | {flat_count} flat\n\n"
        desc += "\n".join(lines)
        desc += "\n\n_Signals fire during market hours._"

    _post_discord({"embeds": [{"title": f"📋 Pre-Market — {et_now.strftime('%b %d, %Y')}",
                               "description": desc, "color": COLOR_BLUE}]})


def send_no_signals_notice(mode: str, count: int):
    et_now = datetime.now(pytz.timezone(TIMEZONE))
    _post_discord({"embeds": [{"title": "✅ Scan Complete — No Setups",
        "description": (f"**Mode:** `{mode.upper()}` | `{et_now.strftime('%I:%M %p ET')}`\n"
                        f"**Tickers scanned:** {count}\nNo signals met threshold."),
        "color": COLOR_BLUE}]})


# =============================================================================
#  SECTION 10 — ALPACA ORDER PLACEMENT
# =============================================================================

def place_alpaca_bracket_order(ticker: str, signal: dict, elapsed_min: float) -> bool:
    """
    Places a GTC bracket order on Alpaca paper account.
    Only fires in the 3:45–4:00pm window (or test mode with elapsed_min=0).
    GTC keeps the bracket live for the full 10-day hold period.
    """
    if ".TO" in ticker:
        print(f"   🍁 {ticker} — TSX not supported on Alpaca, skipping")
        return False

    ALPACA_ORDER_START  = 375   # 3:45pm ET
    ALPACA_ORDER_CUTOFF = 390   # 4:00pm ET
    test_mode = (elapsed_min == 0)
    if not test_mode and (elapsed_min < ALPACA_ORDER_START or elapsed_min >= ALPACA_ORDER_CUTOFF):
        print(f"   ⏰ {ticker} — outside 3:45–4:00pm window, skipping Alpaca order")
        return False

    client = get_alpaca_client()
    if not client:
        print(f"   ⚠️ {ticker} — Alpaca not configured, skipping")
        return False

    try:
        entry  = signal["price"]
        target = signal["take_profit"]
        stop   = signal["stop_loss"]

        position_pct  = signal.get("position_size_pct", 4.8) / 100
        dollar_amount = PORTFOLIO_VALUE * position_pct
        qty           = max(round(dollar_amount / entry, 2), 0.01)

        print(f"   🦙 Placing bracket order: {ticker} qty={qty} "
              f"entry~${entry:.2f} target=${target:.2f} stop=${stop:.2f}")

        order = get_alpaca_client().submit_order(MarketOrderRequest(
            symbol        = ticker,
            qty           = qty,
            side          = OrderSide.BUY,
            time_in_force = TimeInForce.GTC,    # 10-day hold — stays live until filled
            order_class   = OrderClass.BRACKET,
            take_profit   = TakeProfitRequest(limit_price=round(target, 2)),
            stop_loss     = StopLossRequest(stop_price=round(stop, 2))
        ))
        print(f"   ✅ Order submitted: {order.id}")

        _post_discord({"embeds": [{"title": f"🦙 Alpaca Order Placed — {ticker}",
            "description": (f"**Qty:** {qty} @ ~${entry:.2f}\n"
                            f"**Target:** ${target:.2f} | **Stop:** ${stop:.2f}\n"
                            f"**Order ID:** `{order.id}` | _Paper account_"),
            "color": 3066993}]})
        return True

    except Exception as e:
        print(f"   ❌ Alpaca order failed for {ticker}: {e}")
        _post_discord({"embeds": [{"title": f"❌ Alpaca Order Failed — {ticker}",
            "description": f"Error: `{e}`", "color": COLOR_RED}]})
        return False


# =============================================================================
#  SECTION 11 — TRADE OUTCOME TRACKER
# =============================================================================

def load_trade_log() -> list:
    try:
        p = Path(TRADE_LOG_FILE)
        if p.exists():
            with open(p) as f:
                return json.load(f)
    except Exception as e:
        print(f"⚠️ Trade log load failed: {e}")
    return []


def save_trade_log(trades: list):
    try:
        with open(TRADE_LOG_FILE, "w") as f:
            json.dump(trades, f, indent=2)
    except Exception as e:
        print(f"⚠️ Trade log save failed: {e}")


def log_new_trade(ticker: str, currency: str, signal: dict):
    """Appends alert to trade_log.json. Skips if ticker already has an OPEN trade."""
    try:
        trades = load_trade_log()

        # Prevent duplicate open trades for same ticker
        if any(t["ticker"] == ticker and t["status"] == "OPEN" for t in trades):
            print(f"   ℹ️ {ticker} already has an open trade — skipping duplicate log")
            return

        tz  = pytz.timezone(TIMEZONE)
        now = datetime.now(tz)

        trade = {
            "id":           f"{ticker}_{now.strftime('%Y%m%d_%H%M')}",
            "ticker":       ticker,
            "currency":     currency,
            "mode":         "SWING",
            "alert_date":   now.strftime("%Y-%m-%d"),
            "alert_time":   now.strftime("%H:%M ET"),
            # Trade plan
            "entry":        float(signal["price"]),
            "stop_loss":    float(signal["stop_loss"]),
            "take_profit":  float(signal["take_profit"]),
            "rr_ratio":     float(signal.get("rr_ratio", 0)),
            "support":      float(signal.get("support", 0)),
            "support_source": signal.get("support_source", ""),
            # Scoring
            "score":        int(signal.get("score", 0)),
            "threshold":    int(signal.get("threshold", 0)),
            "trend_score":  int(signal.get("trend_score", 0)),
            "momentum_score": int(signal.get("momentum_score", 0)),
            "reasons":      [str(r) for r in signal.get("reasons", [])],
            # Technical snapshot
            "rsi":          float(signal.get("rsi", 0)),
            "atr":          float(signal.get("atr", 0)),
            "ema_21":       float(signal.get("ema_21", 0)),
            "ema_50":       float(signal.get("ema_50", 0)),
            "bbl":          float(signal["bbl"]) if signal.get("bbl") else None,
            "bb_width":     float(signal.get("bb_width", 0)),
            "macd_h":       float(signal["macd_h"]) if signal.get("macd_h") else None,
            "gap_pct":      float(signal.get("gap_pct", 0)),
            "oversold_bypass": bool(signal.get("oversold_bypass", False)),
            "regime_bullish":  bool(signal.get("regime_bullish", True)),
            "vix_level":    float(signal.get("vix_level", 0.0)),
            "vol_pace":     float(signal.get("vol_pace", 1.0)),
            # Outcome tracking
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
    Checks all OPEN trades against latest daily data.
    Marks WON / LOST / EXPIRED / AMBIGUOUS as appropriate.
    Returns list of newly resolved trades.
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

            if days_open > OUTCOME_CHECK_DAYS:
                trade["status"]       = "EXPIRED"
                trade["outcome_date"] = str(today)
                trade["outcome_pct"]  = round(
                    (trade["max_price"] - trade["entry"]) / trade["entry"] * 100, 2)
                resolved.append(trade)
                changed = True
                print(f"   ⏰ {trade['ticker']} EXPIRED after {days_open}d")
                continue

            df = extract_ticker_daily(bulk_data, trade["ticker"])
            if df is None or df.empty:
                continue

            try:
                df_window = df.loc[trade.get("alert_date", ""):] 
            except KeyError:
                df_window = df
            if df_window.empty:
                continue

            high  = float(df_window['High'].max())
            low   = float(df_window['Low'].min())
            close = float(df_window['Close'].iloc[-1])

            trade["max_price"] = float(max(trade["max_price"], high))
            trade["min_price"] = float(min(trade["min_price"], low))
            changed = True

            target = trade["take_profit"]
            stop   = trade["stop_loss"]
            entry  = trade["entry"]

            if low <= stop and high >= target:
                trade["status"]       = "AMBIGUOUS"
                trade["outcome_date"] = str(today)
                trade["outcome_pct"]  = 0.0
                resolved.append(trade)
                print(f"   ⚠️  {trade['ticker']} AMBIGUOUS — both stop and target hit same bar")
            elif low <= stop:
                trade["status"]       = "LOST"
                trade["outcome_date"] = str(today)
                trade["outcome_pct"]  = round((stop - entry) / entry * 100, 2)
                resolved.append(trade)
                print(f"   🛑 {trade['ticker']} LOST — stop ${stop:.2f} hit")
            elif high >= target:
                trade["status"]       = "WON"
                trade["outcome_date"] = str(today)
                trade["outcome_pct"]  = round((target - entry) / entry * 100, 2)
                resolved.append(trade)
                print(f"   ✅ {trade['ticker']} WON — target ${target:.2f} hit")
            else:
                pct_to_tgt = (target - close) / entry * 100
                pct_to_stp = (close - stop)   / entry * 100
                print(f"   📊 {trade['ticker']} OPEN — "
                      f"close ${close:.2f} | "
                      f"+{pct_to_tgt:.1f}% to target | "
                      f"-{pct_to_stp:.1f}% to stop")

        except Exception as e:
            print(f"   ⚠️ Outcome check error on {trade['ticker']}: {e}")

    if changed:
        save_trade_log(trades)
    return resolved


def send_outcome_summary(resolved: list, bulk_data):
    """Sends Discord embed with newly resolved trades."""
    try:
        trades = load_trade_log()
        desc   = ""

        if resolved:
            desc += f"🔔 **Just Resolved ({len(resolved)})**\n"
            for t in resolved:
                icon = "✅" if t["status"] == "WON" else ("🛑" if t["status"] == "LOST" else "⏰")
                desc += (f"• {icon} **{t['ticker']}** — {t['status']} "
                         f"`{t['outcome_pct']:+.1f}%` (entry ${t['entry']:.2f})\n")

        if not desc:
            return

        _post_discord({"embeds": [{
            "title":       "📈 Trade Outcome Tracker",
            "description": desc[:4096],
            "color":       COLOR_GREEN,
            "footer":      {"text": f"v6.0 | {len(trades)} total trades logged"},
        }]})
        print(f"   📊 Outcome summary sent ({len(resolved)} resolved)")
    except Exception as e:
        print(f"⚠️ Outcome summary error: {e}")



# =============================================================================
#  SECTION 12 — MAIN LOOP
# =============================================================================

def check_market(mode: str, tickers_override: list | None = None,
                 bypass_hours: bool = False):
    tz     = pytz.timezone(TIMEZONE)
    et_now = datetime.now(tz)

    print(f"\n{'='*60}")
    print(f"  Stock Alert Bot v6.0 — {et_now.strftime('%A %b %d %Y %I:%M %p ET')}")
    print(f"  Mode: {mode.upper()}")
    print(f"{'='*60}\n")

    elapsed_min = get_elapsed_minutes(et_now)

    MARKET_CLOSE = 390   # 4:00pm ET
    if elapsed_min >= MARKET_CLOSE and mode != "premarket" and not bypass_hours:
        print(f"🔔 Market closed — no alerts after 4:00pm. Exiting.")
        return

    time_penalty, penalty_reasons = get_time_penalty(et_now)
    for r in penalty_reasons:
        print(f"⚠️  {r}")

    # Build ticker list
    all_tickers  = tickers_override or (TICKERS_USD + TICKERS_CAD)
    bulk_tickers = list(dict.fromkeys(['^VIX', 'VTI'] + all_tickers))

    # ── STAGE 1: Bulk download ────────────────────────────────────────────────
    bulk_data = fetch_bulk_daily(bulk_tickers)
    if bulk_data is None or bulk_data.empty:
        print("❌ Bulk download failed. Aborting.")
        return

    # Regime check
    regime_penalty, regime_bullish, is_panic = check_market_regime(bulk_data)

    total_penalty = min(time_penalty + regime_penalty, 2)
    print(f"📊 Threshold penalty: +{total_penalty} "
          f"(time +{time_penalty}, regime +{regime_penalty})\n")

    # Always check open trades — even during VIX panic
    print("📋 Checking open trade outcomes...")
    resolved = check_open_trades(bulk_data)
    if OUTCOME_DISCORD_DAILY and resolved:
        send_outcome_summary(resolved, bulk_data)

    if is_panic:
        print("🚨 VIX PANIC — no new signals. Open trade monitoring complete.")
        return

    # ── Pre-market mode: gap summary ──────────────────────────────────────────
    if mode == "premarket":
        summaries = []
        for ticker in [t for t in all_tickers if t not in ('VTI', '^VIX')]:
            try:
                df = extract_ticker_daily(bulk_data, ticker)
                if df is None or len(df) < 2:
                    continue
                prev_close  = float(df['Close'].iloc[-1])
                prior_close = float(df['Close'].iloc[-2])
                last_date   = df.index[-1].date() if hasattr(df.index[-1], 'date') else None
                data_live   = (last_date == et_now.date())
                try:
                    live = yf.Ticker(ticker).fast_info.get("last_price")
                    live = float(live) if live else None
                except Exception:
                    live = None

                if live and data_live:
                    price, prev = live, prev_close
                elif live:
                    price, prev = live, prior_close
                else:
                    price, prev = prev_close, prior_close

                gap_pct  = (price - prev) / prev * 100
                currency = get_currency(ticker)
                if gap_pct >= 2.0:
                    note = f"📈 Gap UP {gap_pct:.1f}%"
                elif gap_pct <= -2.0:
                    note = f"📉 Gap DOWN {gap_pct:.1f}%"
                else:
                    note = f"Flat ({gap_pct:+.1f}%)"
                summaries.append({'ticker': ticker, 'currency': currency,
                                  'price': price, 'note': note, 'gap_pct': gap_pct})
            except Exception:
                pass
        send_premarket_summary(summaries)
        return

    # ── STAGE 2: Swing filter ─────────────────────────────────────────────────
    scan_tickers = [t for t in all_tickers if t not in ('VTI', '^VIX')]
    print(f"🔍 Scanning {len(scan_tickers)} tickers...\n")

    candidates = []
    for ticker in scan_tickers:
        try:
            df_daily = extract_ticker_daily(bulk_data, ticker)
            if df_daily is None:
                print(f"   [{ticker}] ❌ No data")
                continue
            if not passes_liquidity_filter(df_daily, ticker):
                continue
            signal = run_swing_engine(df_daily, total_penalty, ticker=ticker)
            if signal is not None:
                candidates.append((ticker, signal))
        except Exception as e:
            print(f"   ⚠️ [{ticker}] Scan error: {e}")

    print(f"\n   ✅ {len(candidates)} candidates → validation\n")

    # ── STAGE 3: Validation + alerts ─────────────────────────────────────────
    alerts_sent = 0
    for ticker, signal in candidates:
        try:
            currency = get_currency(ticker)
            print(f"── {ticker} ({currency}) ──")

            # Position size
            pos = calculate_position_size(
                signal["score"], signal["threshold"],
                signal["price"], signal["atr"]
            )
            signal["size_guidance"]     = pos["label"]
            signal["position_size_pct"] = pos["pct"]

            # Risk validation
            signal = validate_risk(signal, ticker=ticker)
            if signal is None:
                continue

            print(f"   📊 R/R {signal['rr_ratio']:.2f} | "
                  f"Stop ${signal['stop_loss']:.2f} | "
                  f"Target ${signal['take_profit']:.2f}")

            # Earnings check
            has_earnings, earnings_msg = check_earnings(ticker)
            if has_earnings:
                signal = apply_earnings_penalty(signal, total_penalty)
                if signal is None:
                    continue
            else:
                earnings_msg = ""

            # Volume pace gate (after 11am only)
            df_d    = extract_ticker_daily(bulk_data, ticker)
            rel_vol = calculate_daily_relative_volume(df_d) if df_d is not None else 1.0

            safe_elapsed = max(elapsed_min, 1.0)
            pct_of_day   = min(safe_elapsed / 390.0, 1.0)
            vol_pace     = rel_vol / pct_of_day

            vol_stale = (rel_vol < 0.05)
            if vol_stale:
                print(f"   [{ticker}] ⚠️ Volume stale ({rel_vol:.2f}x) — skipping vol gate")
            elif elapsed_min > 90 and vol_pace < MIN_VOL_PACE:
                print(f"   [{ticker}] ❌ Low volume pace ({vol_pace:.1f}x < {MIN_VOL_PACE}x). Rejected.")
                continue

            print(f"   📊 Vol pace {vol_pace:.1f}x (actual {rel_vol:.2f}x, "
                  f"{pct_of_day*100:.0f}% of day elapsed)")

            # Inject runtime values
            signal["vol_pace"]      = round(float(vol_pace), 2)
            signal["regime_bullish"] = bool(regime_bullish)
            try:
                vix_df = extract_ticker_daily(bulk_data, '^VIX')
                signal["vix_level"] = round(float(vix_df['Close'].iloc[-1]), 1) if vix_df is not None else 0.0
            except Exception:
                signal["vix_level"] = 0.0

            # Fire Discord alert
            send_setup_alert(
                ticker=ticker, currency=currency, signal=signal,
                rel_vol=rel_vol, elapsed_minutes=elapsed_min,
                regime_bullish=regime_bullish, earnings_msg=earnings_msg
            )
            alerts_sent += 1

            # Place Alpaca paper order
            place_alpaca_bracket_order(ticker, signal, elapsed_min)

            # Log trade
            log_new_trade(ticker, currency, signal)

        except Exception as e:
            print(f"   ❌ Error on {ticker}: {e}")
            traceback.print_exc()

    print(f"\n{'='*60}")
    print(f"  Scan complete | {alerts_sent} alert(s) | "
          f"{datetime.now(tz).strftime('%I:%M %p ET')}")
    print(f"{'='*60}\n")

    if alerts_sent == 0:
        send_no_signals_notice(mode, len(scan_tickers))


# =============================================================================
#  ENTRY POINT
# =============================================================================

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Stock Alert Bot v6.0")
    parser.add_argument('--mode', choices=['auto', 'premarket', 'swing'],
                        default='auto')
    parser.add_argument('--ticker', type=str, default=None,
                        help='Scan single ticker, e.g. --ticker NVDA')
    parser.add_argument('--test', action='store_true',
                        help='Bypass market hours check, force swing mode')
    args = parser.parse_args()

    et_now = datetime.now(pytz.timezone(TIMEZONE))

    if args.test:
        mode = 'swing'
        print("🧪 TEST MODE — market hours bypassed")
    elif args.mode == 'auto':
        mode = get_scan_mode(et_now)
    else:
        mode = args.mode

    tickers_override = [args.ticker.upper()] if args.ticker else None
    check_market(mode=mode, tickers_override=tickers_override,
                 bypass_hours=args.test)
