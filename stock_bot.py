"""
=============================================================================
  STOCK ALERT BOT v3.0 — Funnel Architecture + Day Trading + Swing Fallback
=============================================================================

ARCHITECTURE: Three-Stage Funnel
─────────────────────────────────────────────────────────────────────────────
  STAGE 1 — BULK DAILY DOWNLOAD (1 API call for all tickers)
    └─ 1 year of daily bars per ticker
    └─ VTI regime check (200 SMA) — extracted from the same bulk pull
    └─ Reliable EMA-50, EMA-200, MACD, BB — no data starvation

  STAGE 2 — SWING FILTER (runs on bulk daily data, no extra API calls)
    └─ Liquidity filter (dollar volume gate)
    └─ Swing Engine scoring on full daily history
    └─ Only tickers with qualifying swing score (OR bullish structure
       flagged explicitly as day-only) advance to Stage 3

  STAGE 3 — TARGETED INTRADAY (5m fetch ONLY for Stage 2 survivors)
    └─ State machine check (fast fail before API call)
    └─ fetch_targeted_intraday() — 5d × 5m RTH bars
    └─ Day Engine scoring on today's 5m bars only
    └─ Conflict gate → Decision matrix → Risk validator → Alert

KEY FIXES vs v2.0:
  - Bulk daily gives ~252 bars (vs ~43 from 60d resample) — EMA-50/200 valid
  - 5m API calls made ONLY for tickers that passed swing filter
  - No is_bullish_structure silent pass-through — day-only plays explicitly flagged
  - curr_price for state machine uses live 5m data (not stale daily close)
  - Earnings penalty fully implemented (was a no-op placeholder in proposal)
  - MultiIndex flattening applied at extraction point (not assumed upstream)
  - Complete scoring logic from v2.0 (BB, MACD, squeeze penalty all intact)
=============================================================================
"""

import yfinance as yf
import pandas_ta as ta
import requests
import os
import json
import pandas as pd
import numpy as np
from datetime import datetime
from pathlib import Path
import pytz

# ─────────────────────────────────────────────────────────────────────────────
#  CONFIGURATION
# ─────────────────────────────────────────────────────────────────────────────

TICKERS = [
    'VTI',          # Market proxy — regime check

    # ── Safe Foundation (ETFs) ────────────────────────────────────────────────
    'SPY', 'ZSP.TO', 'XEF.TO', 'SPLG', 'QQQM', 'QQQ', 'IWM',

    # ── Sector ETFs ───────────────────────────────────────────────────────────
    'SOXQ', 'XLY', 'GDX', 'SIL', 'XLF', 

    # ── Tier 1 — Safest (Mega Cap, Deep Liquidity) ────────────────────────────
    'AAPL', 'MSFT', 'GOOGL', 'AMZN', 'META',   # Big Tech
    'JPM', 'BAC',                                # Financials
    'XOM',                                       # Energy
    'ABBV',                                      # Defensive

    # ── Tier 2 — Moderate Risk (Higher Beta, Sector Sensitive) ───────────────
    'NVDA', 'AVGO', 'QCOM', 'MU', 'AMAT', 'LRCX',  # Semiconductors
    'NFLX', 'ORCL', 'CRM', 'NOW', 'PANW',           # Software & Cloud
    'SHOP', 'UBER', 'PYPL',                          # Consumer Tech
    'TGT', 'SQ',                                      # Financials
    'OXY', 'DVN',                                    # Energy
    'CCL', 'DKNG',                                   # Consumer
    'ITB', 'XLK', 'SMH', 'GLD', 'SLV', 

    # ── Tier 3 — High Risk (High Beta, Momentum Driven) ──────────────────────
    'TSLA', 'PLTR', 'AMD', 'ARM', 'SMCI',      # High-beta tech
    'SOFI', 'HOOD',                     # Fintech
    'COIN', 'MSTR',                             # Crypto proxy
    'SNOW',                                     # High-growth unprofitable
    
    # ── Canadian Growth & Swings (TSX) ───────────────────────────────────────
    'HUT.TO', 'CVE.TO', 'MFC.TO', 'ATD.TO', 'TOU.TO',
]

WEBHOOK_URL = os.getenv('DISCORD_URL')

# Scoring thresholds
DAY_SCORE_THRESHOLD   = 6
SWING_SCORE_THRESHOLD = 5

# Risk parameters
MAX_STOP_PCT = {
    "DAY TRADE":          0.02,
    "SWING":              0.06,
    "DAY TRADE + SWING":  0.03,
}
MIN_RR_RATIO = 1.5

# Liquidity gate (avg daily dollar volume)
MIN_DOLLAR_VOLUME = {
    "DAY TRADE": 10_000_000,
    "SWING":      2_000_000,
}

# State & cooldown persistence
COOLDOWN_FILE    = "/tmp/alert_cooldowns.json"
STATE_FILE       = "/tmp/setup_states.json"
COOLDOWN_MINUTES = {"DAY TRADE": 30, "SWING": 240, "DAY TRADE + SWING": 30}

EARNINGS_WARNING_DAYS = 7


# =============================================================================
#  SECTION 1 — UTILITIES: TIME, STATE MACHINE, COOLDOWN
# =============================================================================

def get_market_minutes_elapsed() -> float:
    tz  = pytz.timezone('US/Eastern')
    now = datetime.now(tz)
    mkt = now.replace(hour=9, minute=30, second=0, microsecond=0)
    if now < mkt:
        return 0.0
    return min((now - mkt).total_seconds() / 60.0, 390.0)


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
            json.dump(data, f)
    except Exception as e:
        print(f"⚠️ Could not save {path}: {e}")


# ── Cooldown ──────────────────────────────────────────────────────────────────

def is_on_cooldown(ticker: str, mode: str) -> bool:
    cooldowns = _load_json(COOLDOWN_FILE)
    key = f"{ticker}_{mode}"
    if key in cooldowns:
        try:
            last  = datetime.fromisoformat(cooldowns[key])
            mins  = (datetime.now() - last).total_seconds() / 60
            limit = COOLDOWN_MINUTES.get(mode, 30)
            if mins < limit:
                print(f"   ⏱️ [{ticker}] On cooldown for {mode} ({mins:.0f}/{limit} min)")
                return True
        except Exception:
            pass
    return False


def set_cooldown(ticker: str, mode: str):
    cooldowns = _load_json(COOLDOWN_FILE)
    cooldowns[f"{ticker}_{mode}"] = datetime.now().isoformat()
    _save_json(COOLDOWN_FILE, cooldowns)


# ── State Machine: CLEAR → TRIGGERED → INVALIDATED → CLEAR ───────────────────

def get_setup_state(ticker: str) -> dict:
    states = _load_json(STATE_FILE)
    return states.get(ticker, {"state": "CLEAR", "stop_loss": None, "mode": None})


def update_setup_state(ticker: str, new_state: str,
                       stop_loss: float = None, mode: str = None):
    states = _load_json(STATE_FILE)
    states[ticker] = {
        "state":     new_state,
        "stop_loss": stop_loss,
        "mode":      mode,
        "updated":   datetime.now().isoformat(),
    }
    _save_json(STATE_FILE, states)


def check_and_update_state(ticker: str, current_price: float) -> str:
    """
    Returns 'ALLOW_ALERT', 'SUPPRESS_TRIGGERED', or 'SUPPRESS_INVALIDATED'.
    Call this with the LIVE price from 5m data — not the stale daily close.
    """
    record = get_setup_state(ticker)
    state  = record["state"]

    if state == "TRIGGERED":
        stop = record.get("stop_loss")
        if stop is not None and current_price < stop:
            update_setup_state(ticker, "INVALIDATED",
                               stop_loss=stop, mode=record.get("mode"))
            print(f"   ⛔ [{ticker}] Stop ${stop:.2f} hit. → INVALIDATED")
            return "SUPPRESS_INVALIDATED"
        return "SUPPRESS_TRIGGERED"

    if state == "INVALIDATED":
        update_setup_state(ticker, "CLEAR")
        print(f"   🔄 [{ticker}] INVALIDATED → CLEAR")
        return "SUPPRESS_INVALIDATED"

    return "ALLOW_ALERT"


# =============================================================================
#  SECTION 2 — DATA FETCHING (FUNNEL ARCHITECTURE)
# =============================================================================

def fetch_bulk_daily(tickers: list) -> pd.DataFrame:
    """
    STAGE 1: Single bulk download — 1 year of daily bars for ALL tickers.
    Gives ~252 trading bars: enough for EMA-50, EMA-200 (VTI), and MACD.
    auto_adjust=True ensures split/dividend-adjusted prices consistently.
    """
    print(f"📥 STAGE 1: Bulk downloading 1y daily data for {len(tickers)} tickers...")
    try:
        df = yf.download(
            tickers, period="1y", interval="1d",
            group_by='ticker', auto_adjust=True, progress=False
        )
        print(f"   ✅ Bulk download complete.")
        return df
    except Exception as e:
        print(f"   ❌ Bulk download failed: {e}")
        return pd.DataFrame()


def extract_ticker_daily(bulk_data: pd.DataFrame, ticker: str) -> pd.DataFrame | None:
    """
    Extracts a single ticker's daily OHLCV from the bulk MultiIndex DataFrame.
    Handles both multi-ticker MultiIndex and single-ticker flat column formats.
    Flattens MultiIndex columns and drops fully-null rows.
    """
    try:
        # Multi-ticker bulk download produces a MultiIndex
        if isinstance(bulk_data.columns, pd.MultiIndex):
            if ticker not in bulk_data.columns.get_level_values(0):
                return None
            df = bulk_data[ticker].copy()
        else:
            # Single ticker fallback (shouldn't happen in normal operation)
            df = bulk_data.copy()

        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        df.dropna(subset=['Close'], inplace=True)
        return df if not df.empty else None

    except Exception as e:
        print(f"   ⚠️ [{ticker}] Daily extraction failed: {e}")
        return None


def fetch_targeted_intraday(ticker: str) -> pd.DataFrame | None:
    """
    STAGE 3: Targeted 5-minute fetch — called ONLY for Stage 2 survivors.
    5 days is sufficient: today's bars for Day Engine + 4 prior days for rel vol.
    RTH filter (09:30–16:00 ET) applied before returning.
    """
    try:
        df = yf.download(
            ticker, period="5d", interval="5m",
            auto_adjust=True, progress=False
        )
        if df is None or df.empty:
            return None

        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        if df.index.tz is None:
            df.index = df.index.tz_localize('UTC')
        df.index = df.index.tz_convert('US/Eastern')

        df_rth = df.between_time('09:30', '16:00').copy()
        return df_rth if not df_rth.empty else None

    except Exception as e:
        print(f"   ⚠️ [{ticker}] Intraday fetch failed: {e}")
        return None


# =============================================================================
#  SECTION 3 — LIQUIDITY FILTER
# =============================================================================

def passes_liquidity_filter(df_daily: pd.DataFrame, mode: str) -> bool:
    """
    Rejects thinly traded tickers where spread/slippage destroys the R/R.
    Computes average dollar volume over the last 20 daily bars.
    """
    try:
        tail       = df_daily.tail(20)
        avg_close  = tail['Close'].mean()
        avg_volume = tail['Volume'].mean()
        dollar_vol = avg_close * avg_volume
        minimum    = MIN_DOLLAR_VOLUME.get(mode, 2_000_000)
        if dollar_vol < minimum:
            print(f"   💧 Liquidity ${dollar_vol/1e6:.1f}M < ${minimum/1e6:.0f}M min. Rejected.")
            return False
        return True
    except Exception:
        return True  # Don't reject on calculation error


# =============================================================================
#  SECTION 4 — EARNINGS CHECK
# =============================================================================

def get_earnings_warning(ticker: str) -> tuple[bool, str]:
    try:
        cal = yf.Ticker(ticker).calendar
        if cal is None:
            return False, ""

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

        eastern = pytz.timezone('US/Eastern')
        if isinstance(earnings_date, (datetime, pd.Timestamp)):
            earnings_date = pd.to_datetime(earnings_date).replace(tzinfo=eastern).date()
        else:
            earnings_date = pd.to_datetime(earnings_date).date()

        today      = datetime.now(eastern).date()
        days_until = (earnings_date - today).days

        if 0 <= days_until <= EARNINGS_WARNING_DAYS:
            return True, (f"⚠️ **EARNINGS WARNING:** Report in "
                          f"{days_until} days ({earnings_date})")
        return False, ""

    except Exception:
        return False, ""


# =============================================================================
#  SECTION 5 — RELATIVE VOLUME (from already-fetched 5m data)
# =============================================================================

def calculate_relative_volume(df_intraday: pd.DataFrame) -> float:
    """
    Uses median volume at the same 5m time-slot across prior days
    to compute RVAT. No extra API call — uses the Stage 3 intraday fetch.
    Compares last COMPLETED bar (iloc[-2]) to avoid live partial-bar bias.
    """
    try:
        if df_intraday is None or len(df_intraday) < 2:
            return 1.0

        df             = df_intraday.copy()
        df['time_slot'] = df.index.time
        df['date']      = df.index.date

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

        return check_vol / median_vol

    except Exception:
        return 1.0


# =============================================================================
#  SECTION 6 — DAY ENGINE (5-Minute Bars, today only)
# =============================================================================

def run_day_engine(df_today: pd.DataFrame, regime_penalty: int) -> dict | None:
    """
    All indicators on today's 5m RTH bars. RSI, EMA, ATR, VWAP all live
    in the same timeframe — no phantom setups from daily/intraday mixing.
    """
    if df_today is None or len(df_today) < 10:
        return None

    df = df_today.copy()

    df['EMA_9']  = ta.ema(df['Close'], length=9)
    df['EMA_21'] = ta.ema(df['Close'], length=21)
    df['RSI']    = ta.rsi(df['Close'], length=14)
    df['ATR']    = ta.atr(df['High'], df['Low'], df['Close'], length=14)
    df['VWAP']   = ta.vwap(df['High'], df['Low'], df['Close'], df['Volume'])

    bb = ta.bbands(df['Close'], length=20, std=2)
    if bb is not None and not bb.empty:
        df['BBL'] = bb.iloc[:, 0]
        df['BBU'] = bb.iloc[:, 2]

    df.dropna(subset=['RSI', 'EMA_9', 'EMA_21', 'VWAP', 'ATR'], inplace=True)

    # Need at least 3 bars for RSI direction check
    if len(df) < 3:
        return None

    last = df.iloc[-1]
    prev = df.iloc[-2]

    price  = float(last['Close'])
    vwap   = float(last['VWAP'])
    rsi    = float(last['RSI'])
    ema_9  = float(last['EMA_9'])
    ema_21 = float(last['EMA_21'])
    atr    = float(last['ATR'])

    score   = 0
    reasons = []

    # A. VWAP position — primary intraday direction filter
    above_vwap = price > vwap
    if above_vwap:
        score += 3
        reasons.append(f"✅ Price Above VWAP (${vwap:.2f})")

    # B. 5m RSI — reacts within minutes, not days
    if rsi < 35:
        score += 3
        reasons.append(f"💎 Intraday Deeply Oversold (RSI {rsi:.1f})")
    elif rsi < 45:
        score += 2
        reasons.append(f"📉 Intraday Oversold (RSI {rsi:.1f})")
    elif rsi < 55:
        score += 1
        reasons.append(f"🌊 Intraday Momentum Reset (RSI {rsi:.1f})")

    # C. 5m EMA stack
    if ema_9 > ema_21:
        score += 2
        reasons.append("🚀 5m Bullish EMA Stack (9 > 21)")

    # D. VWAP reclaim — dipped below, now above (bounce confirmation)
    low_last_3 = df['Low'].iloc[-3:].min()
    if low_last_3 < vwap and price > vwap:
        score += 2
        reasons.append("⚡ VWAP Reclaim — Intraday Bounce Confirmed")

    # E. Higher high + higher low on 5m (momentum shift)
    if float(last['High']) > float(prev['High']) and float(last['Low']) > float(prev['Low']):
        score += 1
        reasons.append("📈 5m Higher High + Higher Low")

    # F. 5m Bollinger Lower Band touch
    if 'BBL' in df.columns and not pd.isna(last.get('BBL', float('nan'))):
        bbl_5m = float(last['BBL'])
        if price <= bbl_5m * 1.01:
            score += 2
            reasons.append("🛡️ 5m Bollinger Lower Band Touch")

    # ── G. RSI CURLING UPWARD ─────────────────────────────────────────────────
    # Detects RSI making three consecutive higher readings from an oversold base.
    # Scores the DIRECTION of momentum, not just the current RSI level.
    # RSI cap at 50 prevents flagging stocks that are already overbought.
    # Example: RSI 28 → 32 → 37 = buyers taking control from oversold territory.
    rsi_now   = float(df['RSI'].iloc[-1])
    rsi_prev1 = float(df['RSI'].iloc[-2])
    rsi_prev2 = float(df['RSI'].iloc[-3])

    rsi_curling_up = (rsi_now > rsi_prev1 > rsi_prev2) and (rsi_now < 50)

    if rsi_curling_up:
        score += 2
        reasons.append(
            f"🔄 RSI Curling Up from Oversold "
            f"({rsi_prev2:.0f} → {rsi_prev1:.0f} → {rsi_now:.0f})"
        )

    threshold = DAY_SCORE_THRESHOLD + regime_penalty

    if score < threshold:
        return None

    stop_loss   = price - (atr * 1.5)
    take_profit = price + (atr * 3.0)

    return {
        "score":       score,
        "threshold":   threshold,
        "reasons":     reasons,
        "stop_loss":   stop_loss,
        "take_profit": take_profit,
        "atr":         atr,
        "atr_source":  "5m",
        "vwap":        vwap,
        "rsi":         rsi,
        "ema_21":      ema_21,
        "ema_50":      None,
        "price":       price,
        "is_bullish":  above_vwap,
        "mode":        "DAY TRADE",
    }


# =============================================================================
#  SECTION 7 — SWING ENGINE (Full 1-Year Daily Bars)
# =============================================================================

def run_swing_engine(df_daily: pd.DataFrame, regime_penalty: int) -> dict | None:
    """
    Runs on the full 1-year daily dataset from Stage 1 bulk download.
    EMA-50 is now mathematically valid (~252 bars vs ~43 from resample).
    All comparisons use daily CLOSING prices only — no intraday patching.
    """
    if df_daily is None or len(df_daily) < 50:
        return None

    df = df_daily.copy()

    # Full indicator suite — reliable with 252 bars
    df['EMA_21']  = ta.ema(df['Close'], length=21)
    df['EMA_50']  = ta.ema(df['Close'], length=50)
    df['RSI']     = ta.rsi(df['Close'], length=14)
    df['ATR']     = ta.atr(df['High'], df['Low'], df['Close'], length=14)

    macd = ta.macd(df['Close'])
    if macd is not None:
        hist_cols = [c for c in macd.columns if c.startswith('MACDh')]
        if hist_cols:
            df['MACD_H'] = macd[hist_cols[0]]

    bb = ta.bbands(df['Close'], length=20, std=2)
    if bb is not None and not bb.empty:
        df['BBL']      = bb.iloc[:, 0]
        df['BBM']      = bb.iloc[:, 1]
        df['BBU']      = bb.iloc[:, 2]
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

    score   = 0
    reasons = []

    # A. Daily RSI
    if rsi < 35:
        score += 3
        reasons.append(f"💎 Daily RSI Deeply Oversold ({rsi:.1f})")
    elif rsi < 45:
        score += 2
        reasons.append(f"📉 Daily RSI Oversold ({rsi:.1f})")
    elif rsi < 55:
        score += 1
        reasons.append(f"🌊 Daily Momentum Reset ({rsi:.1f})")

    # B. Daily BB Lower Band touch — price closed at/below band
    if bbl is not None and price <= bbl * 1.01:
        score += 3
        reasons.append(f"🛡️ Closed at Daily BB Lower (${bbl:.2f})")

    # C. 21 EMA — Wick Detection + Support Hold
    daily_low   = float(last['Low'])    # Completed daily candle low — same dataset as ema_21
    daily_close = float(last['Close'])  # Same candle close — no intraday patching

    # Wick check: daily Low touched EMA-21 but Close recovered above it
    # RSI cap (< 65) prevents flagging overbought momentum stocks as dip buys
    wick_to_21 = (daily_low <= ema_21 * 1.005) and (daily_close > ema_21) and (rsi < 65)

    # Support hold: price is currently sitting near EMA-21 (no wick needed)
    near_21 = abs(daily_close - ema_21) / ema_21 < 0.015

    if wick_to_21 and not near_21:
        # Price wicked down and bounced hard away — buying demand already confirmed
        # Strongest setup: support tested AND rejected decisively
        score += 3
        reasons.append(f"⚡ Daily Wick to 21 EMA + Strong Recovery (Low ${daily_low:.2f} → Close ${daily_close:.2f})")
    elif wick_to_21 and near_21:
        # Price wicked and is still close to EMA — bounce just beginning
        # Support tested but not yet confirmed by follow-through
        score += 2
        reasons.append(f"⚡ Daily Wick to 21 EMA — Early Bounce (${ema_21:.2f})")
    elif near_21 and rsi < 55:
        # No wick — price hovering at EMA without having tested below
        # Weakest of the three: support respected but not actively tested
        score += 2
        reasons.append(f"📈 Daily 21 EMA Support Hold (${ema_21:.2f})")

    # D. 50 EMA proximity — now always reliable with 1y data
    near_50 = abs(price - ema_50) / ema_50 < 0.02
    if near_50:
        score += 2
        reasons.append(f"📊 Testing Daily 50 EMA (${ema_50:.2f})")

    # E. Broader trend: price above 50 EMA
    if price > ema_50:
        score += 1
        reasons.append("✅ Price Above Daily 50 EMA (Bullish Structure)")

    # F. MACD momentum
    if macd_h > 0:
        score += 2
        reasons.append("🚀 Daily MACD: Positive Histogram")
    elif macd_h > prev_mh:
        score += 1
        reasons.append("🔄 Daily MACD: Improving Momentum")

    # G. BB squeeze penalty — too narrow means no edge
    if 0 < bb_width < 0.03:
        score -= 2
        reasons.append(f"⚠️ BB Squeeze (width {bb_width:.3f}) — reduced edge")

    # Direction signal for conflict gate
    is_bullish = price > ema_21

    threshold = SWING_SCORE_THRESHOLD + regime_penalty

    if score < threshold:
        return None

    # Support-aware stop loss
    if bbl is not None and price <= bbl * 1.02:
        support = bbl
    elif near_21:
        support = ema_21
    else:
        support = ema_50

    stop_loss   = support - (atr * 0.8)
    take_profit = price   + (atr * 2.5)

    if stop_loss >= price:
        stop_loss = price - atr

    return {
        "score":       score,
        "threshold":   threshold,
        "reasons":     reasons,
        "stop_loss":   stop_loss,
        "take_profit": take_profit,
        "atr":         atr,
        "atr_source":  "Daily",
        "vwap":        None,
        "rsi":         rsi,
        "ema_21":      ema_21,
        "ema_50":      ema_50,
        "price":       price,
        "is_bullish":  is_bullish,
        "mode":        "SWING",
    }


# =============================================================================
#  SECTION 8 — CONFLICT GATE
# =============================================================================

def signals_conflict(day_signal: dict | None, swing_signal: dict | None) -> bool:
    """
    Returns True if both engines fired but disagree on price direction.
    Example: 5m VWAP reclaim (bullish day) inside daily EMA-21 breakdown
    (bearish swing) = bear trap. Suppress the alert.
    Only fires when BOTH engines have a signal — single-engine is not a conflict.
    """
    if day_signal is None or swing_signal is None:
        return False

    return day_signal.get("is_bullish", True) != swing_signal.get("is_bullish", True)


# =============================================================================
#  SECTION 9 — DECISION MATRIX
# =============================================================================

def build_final_signal(day_signal: dict | None, swing_signal: dict | None,
                       rel_vol: float, elapsed_minutes: float) -> dict | None:
    """
    Combines engine outputs into one of three scenarios:

    Scenario A — Both engines pass, same direction (highest conviction)
      Large size. Shows both 5m stop (tight) and daily ATR stop (runner).

    Scenario B — Day Engine only (no daily structure confirmation)
      Small size. Tight 5m ATR stop. Must exit before 3:45 PM.

    Scenario C — Swing Engine only (no intraday confirmation yet)
      Half size. Wait for VWAP reclaim at next open before adding.

    NOTE: Scenario B only reaches here if the ticker passed the swing
    liquidity filter AND was explicitly tagged 'day_only_eligible'
    by the main loop. It is never a silent pass-through.
    """
    day_ok   = day_signal   is not None
    swing_ok = swing_signal is not None

    if not day_ok and not swing_ok:
        return None

    # ── Scenario A: Fully Aligned ────────────────────────────────────────────
    if day_ok and swing_ok:
        sig = swing_signal.copy()
        sig["scenario"]       = "A"
        sig["scenario_label"] = "⚡ SCENARIO A — DAY + SWING ALIGNED"
        sig["size_guidance"]  = "Full Size — Both timeframes confirmed"
        sig["hold_guidance"]  = (
            f"Intraday target: ${day_signal['take_profit']:.2f} (5m ATR × 3). "
            f"Trail remainder with daily ATR stop for multi-day hold."
        )
        sig["day_stop"]   = day_signal["stop_loss"]
        sig["day_target"] = day_signal["take_profit"]
        sig["mode"]       = "DAY TRADE + SWING"
        sig["vwap"]       = day_signal.get("vwap")
        sig["rsi"]        = day_signal.get("rsi")   # Fresher 5m RSI

        day_reasons   = [f"[5m] {r}"    for r in day_signal.get("reasons", [])]
        swing_reasons = [f"[Daily] {r}" for r in swing_signal.get("reasons", [])]
        sig["reasons"] = day_reasons + swing_reasons
        sig["atr_source"] = "Daily (swing) + 5m (day)"
        return sig

    # ── Scenario B: Day Only ─────────────────────────────────────────────────
    if day_ok and not swing_ok:
        sig = day_signal.copy()
        sig["scenario"]       = "B"
        sig["scenario_label"] = "⚡ SCENARIO B — INTRADAY SCALP ONLY"
        sig["size_guidance"]  = "Small Size — No daily structure confirmation"
        sig["hold_guidance"]  = "Must exit before 3:45 PM EST. No overnight."
        sig["mode"]           = "DAY TRADE"
        return sig

    # ── Scenario C: Swing Only ────────────────────────────────────────────────
    if swing_ok and not day_ok:
        sig = swing_signal.copy()
        sig["scenario"]       = "C"
        sig["scenario_label"] = "📅 SCENARIO C — SWING (Awaiting Intraday Confirmation)"
        sig["size_guidance"]  = "Half Size — Add on VWAP reclaim with volume"
        sig["hold_guidance"]  = (
            "Daily structure valid. Best entry: next open or when "
            "5m price reclaims VWAP with 1.5x+ relative volume."
        )
        sig["mode"] = "SWING"
        return sig

    return None


# =============================================================================
#  SECTION 10 — RISK VALIDATOR
# =============================================================================

def validate_risk(signal: dict, mode: str) -> dict | None:
    """
    Two-step check:
    1. Stop must not exceed MAX_STOP_PCT — tighten if it does, flag the alert.
    2. Resulting R/R ratio must meet MIN_RR_RATIO — reject if not.
    """
    price     = signal["price"]
    stop_loss = signal["stop_loss"]
    target    = signal["take_profit"]

    # Normalise mode key for lookup
    mode_key = "DAY TRADE" if mode.startswith("DAY TRADE") else mode
    max_stop = MAX_STOP_PCT.get(mode_key, 0.05)

    actual_pct = (price - stop_loss) / price

    if actual_pct > max_stop:
        signal["stop_loss"]      = price * (1 - max_stop)
        signal["stop_adjusted"]  = True

    risk   = price - signal["stop_loss"]
    reward = target - price

    if risk <= 0:
        return None

    rr = reward / risk
    signal["rr_ratio"] = rr

    if rr < MIN_RR_RATIO:
        print(f"   📉 R/R {rr:.2f} below minimum {MIN_RR_RATIO}. Skipping.")
        return None

    return signal


# =============================================================================
#  SECTION 11 — DISCORD ALERT
# =============================================================================

def send_discord_alert(ticker: str, signal: dict, rel_vol: float,
                       earnings_msg: str, elapsed_minutes: float,
                       regime_bullish: bool = True):
    scenario = signal.get("scenario", "?")
    mode     = signal.get("mode", "UNKNOWN")
    price    = signal["price"]

    color_map  = {"A": 5763719,  "B": 16776960, "C": 3447003}
    rating_map = {"A": "🔥 HIGH CONVICTION", "B": "⚡ INTRADAY SCALP", "C": "📅 SWING SETUP"}

    color  = color_map.get(scenario, 16711680)
    rating = rating_map.get(scenario, "⚠️ ALERT")

    tz        = pytz.timezone('US/Eastern')
    timestamp = datetime.now(tz).strftime('%I:%M %p EST')

    stop_loss   = signal["stop_loss"]
    take_profit = signal["take_profit"]
    rr_ratio    = signal.get("rr_ratio", 0.0)
    stop_pct    = (price - stop_loss)   / price * 100
    target_pct  = (take_profit - price) / price * 100
    risk_dollar = price - stop_loss
    atr_val     = signal.get("atr", 0.0)

    # ── RSI label (full range) ────────────────────────────────────────────────
    rsi_val = signal.get("rsi", 0.0)
    if rsi_val < 30:
        rsi_label = "🔴 Deeply Oversold"
    elif rsi_val < 45:
        rsi_label = "🟠 Oversold"
    elif rsi_val < 55:
        rsi_label = "🟡 Neutral"
    elif rsi_val < 65:
        rsi_label = "🟢 Bullish"
    else:
        rsi_label = "⚪ Extended"

    # ── EMA distances ─────────────────────────────────────────────────────────
    ema_21 = signal.get("ema_21", 0.0)
    ema_50 = signal.get("ema_50")

    ema_21_pct  = (price - ema_21) / ema_21 * 100 if ema_21 else None
    ema_21_dir  = "above" if (ema_21_pct or 0) >= 0 else "below"
    ema_21_str  = (f"${ema_21:.2f} ({abs(ema_21_pct):.1f}% {ema_21_dir})"
                   if ema_21_pct is not None else "N/A")

    ema_50_pct  = (price - ema_50) / ema_50 * 100 if ema_50 else None
    ema_50_dir  = "above" if (ema_50_pct or 0) >= 0 else "below"
    ema_50_str  = (f"${ema_50:.2f} ({abs(ema_50_pct):.1f}% {ema_50_dir})"
                   if ema_50_pct is not None else "—")

    # ── VWAP relationship ─────────────────────────────────────────────────────
    vwap = signal.get("vwap")
    if vwap:
        vwap_pct = (price - vwap) / vwap * 100
        vwap_dir = "above" if vwap_pct >= 0 else "below"
        vwap_str = f"${vwap:.2f} ({abs(vwap_pct):.1f}% {vwap_dir})"
    else:
        vwap_str = "N/A"

    # ── Volume label ──────────────────────────────────────────────────────────
    vol_dir   = "Buying" if signal.get("is_bullish") else "Selling"
    vol_label = "🔥 Heavy" if rel_vol > 2.0 else "💪 Strong" if rel_vol > 1.2 else "😐 Normal"
    vol_str   = f"{rel_vol:.1f}x · {vol_label} {vol_dir}"

    # ── Session label ─────────────────────────────────────────────────────────
    if elapsed_minutes < 20:
        session_label = "⏰ Opening"
    elif elapsed_minutes > 360:
        session_label = "🕒 Late Session"
    else:
        session_label = "✅ Normal Hours"

    regime_label = "🟢 Bullish Market" if regime_bullish else "🔴 Bearish Market"

    # ── Score context ─────────────────────────────────────────────────────────
    score     = signal.get("score", 0)
    threshold = signal.get("threshold", 5)
    score_str = f"**{score}** / min {threshold} · +{score - threshold} pts above"

    # ── Description — compact header only ────────────────────────────────────
    desc = f"*{timestamp} · {regime_label} · {session_label}*\n"
    desc += f"Score: {score_str}\n"

    if earnings_msg:
        desc += f"\n{earnings_msg}\n"

    if signal.get("stop_adjusted"):
        mode_key = "DAY TRADE" if mode.startswith("DAY TRADE") else mode
        pct      = MAX_STOP_PCT.get(mode_key, 0.05) * 100
        desc += f"\n⚠️ *Stop auto-tightened to {pct:.0f}% max*\n"

    # ── Inline fields — 3-column grid layout ─────────────────────────────────
    fields = []

    # Row 1: Trade Plan
    fields.append({"name": "📥 Entry",            "value": f"`${price:.2f}`",                          "inline": True})
    fields.append({"name": "🎯 Target",           "value": f"`${take_profit:.2f}` (+{target_pct:.1f}%)", "inline": True})
    fields.append({"name": "🛑 Stop",             "value": f"`${stop_loss:.2f}` (−{stop_pct:.1f}%)",   "inline": True})

    # Row 2: Risk metrics
    fields.append({"name": "⚖️ R/R",              "value": f"`1 : {rr_ratio:.2f}`",                    "inline": True})
    fields.append({"name": "💵 Risk / Share",      "value": f"`${risk_dollar:.2f}`",                    "inline": True})
    fields.append({"name": f"📊 ATR ({signal['atr_source']})", "value": f"`${atr_val:.2f}`",           "inline": True})

    # Row 3: Technicals
    fields.append({"name": "📈 RSI",              "value": f"`{rsi_val:.1f}` {rsi_label}",             "inline": True})
    fields.append({"name": "📍 VWAP",             "value": f"`{vwap_str}`",                            "inline": True})
    fields.append({"name": "📦 Volume",           "value": f"`{vol_str}`",                             "inline": True})

    # Row 4: EMA Key Levels (full width)
    if ema_50:
        ema_field_val = f"21 EMA · {ema_21_str}\n50 EMA · {ema_50_str}"
    else:
        ema_field_val = f"21 EMA · {ema_21_str}"
    fields.append({"name": "📐 Key Levels",       "value": ema_field_val,                              "inline": False})

    # Scenario A intraday plan (full width)
    if scenario == "A" and "day_stop" in signal:
        intraday_val = (f"Day Stop · `${signal['day_stop']:.2f}` (5m ATR × 1.5)\n"
                        f"Day Target · `${signal['day_target']:.2f}` (5m ATR × 3)")
        fields.append({"name": "📌 Intraday Scale-Out Plan", "value": intraday_val, "inline": False})

    # Signal Reasons (full width)
    reasons_str = "\n".join(f"• {r}" for r in signal.get("reasons", []))
    fields.append({"name": "📝 Signal Reasons",   "value": reasons_str or "—",                         "inline": False})

    # Links (full width)
    fields.append({"name": "🔗 Links",            "value": f"[Yahoo Finance](https://finance.yahoo.com/quote/{ticker})", "inline": False})

    payload = {
        "content": f"🚨 **{ticker}** · **{mode}** · {timestamp}",
        "embeds": [{
            "title":       f"{rating} — {ticker}",
            "description": desc,
            "color":       color,
            "fields":      fields,
            "footer": {
                "text": f"Alert Bot v3.0 · ATR Source: {signal['atr_source']}"
            },
        }],
    }

    if not WEBHOOK_URL:
        print(f"   ❌ DISCORD_URL missing. Would have alerted: {ticker} [{mode}]")
        return

    try:
        resp = requests.post(WEBHOOK_URL, json=payload, timeout=10)
        resp.raise_for_status()
        print(f"   ✅ Alert sent: {ticker} [{mode}] Scenario {scenario}")
    except requests.exceptions.HTTPError as e:
        print(f"   ❌ HTTP error for {ticker}: {e}")
    except requests.exceptions.Timeout:
        print(f"   ❌ Timeout sending alert for {ticker}")
    except Exception as e:
        print(f"   ❌ Discord error for {ticker}: {e}")


# =============================================================================
#  SECTION 12 — MAIN LOOP (THREE-STAGE FUNNEL)
# =============================================================================

def check_market():
    tz  = pytz.timezone('US/Eastern')
    now = datetime.now(tz)

    print(f"\n{'='*62}")
    print(f"  ALERT BOT v3.0 — {now.strftime('%Y-%m-%d %I:%M %p EST')}")
    print(f"{'='*62}")

    elapsed_minutes = get_market_minutes_elapsed()
    print(f"🕒 Market Minutes Elapsed: {elapsed_minutes:.0f}/390")

    # ── Time-of-day penalty ──────────────────────────────────────────────────
    time_penalty = 0
    if elapsed_minutes < 30:
        time_penalty += 1
        print("⏰ Opening 30 min — thresholds +1 (noisy open)")
    if now.weekday() == 4 and elapsed_minutes > 270:
        time_penalty += 1
        print("📅 Late Friday — thresholds +1 (weekend risk)")

    # ════════════════════════════════════════════════════════════════════════
    #  STAGE 1: BULK DAILY DOWNLOAD + REGIME CHECK
    #  One API call for all tickers. VTI extracted for regime check.
    # ════════════════════════════════════════════════════════════════════════
    bulk_data = fetch_bulk_daily(TICKERS)

    if bulk_data.empty:
        print("❌ Critical: Bulk download empty. Exiting.")
        return

    # VTI Regime Check — extracted from same bulk pull, no extra API call
    regime_penalty = time_penalty
    regime_bullish = True   # Passed to alert for display — default bullish
    try:
        vti_df = extract_ticker_daily(bulk_data, 'VTI')
        if vti_df is not None and len(vti_df) >= 200:
            vti_sma   = ta.sma(vti_df['Close'], length=200).iloc[-1]
            vti_price = float(vti_df['Close'].iloc[-1])
            if vti_price < vti_sma:
                regime_penalty += 1
                regime_bullish  = False
                print(f"⚠️ Regime: BEARISH (VTI ${vti_price:.2f} < 200 SMA ${vti_sma:.2f}) → penalty +1")
            else:
                print(f"✅ Regime: BULLISH (VTI ${vti_price:.2f} > 200 SMA ${vti_sma:.2f})")
        else:
            print("⚠️ VTI: Insufficient data for 200 SMA — defaulting BULLISH")
    except Exception as e:
        print(f"⚠️ Regime check error: {e} — defaulting BULLISH")

    # ════════════════════════════════════════════════════════════════════════
    #  STAGE 2: SWING FILTER — runs on bulk daily data (no extra API calls)
    #  Two types of candidates advance to Stage 3:
    #    Type 1: swing_signal is valid (Scenario C or A possible)
    #    Type 2: swing_signal is None BUT daily structure is bullish enough
    #            for a pure day trade (Scenario B). Explicitly flagged.
    # ════════════════════════════════════════════════════════════════════════
    scan_tickers = [t for t in TICKERS if t != 'VTI']
    print(f"\n🔍 STAGE 2: Swing filter on {len(scan_tickers)} tickers...")

    # Each entry: (ticker, swing_signal_or_None, day_only_eligible: bool)
    swing_candidates: list[tuple[str, dict | None, bool]] = []

    for ticker in scan_tickers:
        try:
            df_daily = extract_ticker_daily(bulk_data, ticker)
            if df_daily is None:
                continue

            # Liquidity check on daily data (before any engine work)
            if not passes_liquidity_filter(df_daily, "SWING"):
                continue

            # Run Swing Engine on full 1-year daily bars
            swing_signal = run_swing_engine(df_daily, regime_penalty)

            if swing_signal is not None:
                # Has a valid swing setup — advance for Scenarios A or C
                swing_candidates.append((ticker, swing_signal, False))

            else:
                # No swing signal — check if bullish structure exists for day-only play.
                # Explicitly tagged so the decision matrix knows it's Scenario B only.
                # Requires BOTH: price > EMA-50 AND RSI not overbought.
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
                            # Also needs day-trade liquidity
                            if passes_liquidity_filter(df_daily, "DAY TRADE"):
                                swing_candidates.append((ticker, None, True))
                except Exception:
                    pass

        except Exception as e:
            print(f"   ⚠️ [{ticker}] Stage 2 error: {e}")

    type1 = sum(1 for _, s, _ in swing_candidates if s is not None)
    type2 = sum(1 for _, s, d in swing_candidates if s is None and d)
    print(f"   ✅ Funnel: {type1} swing setups + {type2} day-only eligible = "
          f"{len(swing_candidates)}/{len(scan_tickers)} advance to Stage 3")

    # ════════════════════════════════════════════════════════════════════════
    #  STAGE 3: TARGETED INTRADAY — 5m fetch ONLY for Stage 2 survivors
    # ════════════════════════════════════════════════════════════════════════
    print(f"\n⚡ STAGE 3: Targeted intraday analysis for {len(swing_candidates)} tickers...")

    for ticker, swing_signal, day_only_eligible in swing_candidates:
        try:
            print(f"\n── {ticker} {'(day-only)' if day_only_eligible else ''} ─────────────")

            # ── State machine check — DISABLED (allow repeat alerts) ────────
            # Re-enable this block to suppress re-alerts on active setups.
            # daily_close = float(extract_ticker_daily(bulk_data, ticker)['Close'].iloc[-1])
            # prelim_state = check_and_update_state(ticker, daily_close)
            # if prelim_state in ("SUPPRESS_TRIGGERED", "SUPPRESS_INVALIDATED"):
            #     print(f"   🔒 State machine suppressed ({prelim_state})")
            #     continue

            # ── Targeted 5m fetch ────────────────────────────────────────────
            df_intraday = fetch_targeted_intraday(ticker)
            if df_intraday is None:
                print(f"   ⚠️ No intraday data available.")
                continue

            # Isolate today's 5m bars
            today_date = now.date()
            df_today   = df_intraday[df_intraday.index.date == today_date].copy()

            # ── Live state machine check — DISABLED (allow repeat alerts) ───
            # Re-enable this block to detect intraday stop hits between scans.
            # if not df_today.empty:
            #     live_price   = float(df_today['Close'].iloc[-1])
            #     state_action = check_and_update_state(ticker, live_price)
            #     if state_action in ("SUPPRESS_TRIGGERED", "SUPPRESS_INVALIDATED"):
            #         print(f"   🔒 Live state check suppressed ({state_action})")
            #         continue
            # else:
            #     print(f"   ⚠️ No today bars in intraday data (market closed or pre-open).")
            if df_today.empty:
                print(f"   ⚠️ No today bars in intraday data (market closed or pre-open).")

            # ── Day Engine (5m bars) ─────────────────────────────────────────
            day_signal = run_day_engine(df_today, regime_penalty)
            day_status = (f"Score {day_signal['score']}/{day_signal['threshold']} ✅"
                         if day_signal else "❌ Below threshold")
            print(f"   Day Engine:   {day_status}")

            # ── Enforce day-only constraint ───────────────────────────────────
            # If this ticker is day-only eligible (no swing signal),
            # suppress Scenario C entirely — only Scenario B is valid here.
            if day_only_eligible and day_signal is None:
                print(f"   ➖ Day-only ticker: no day signal. Skipping.")
                continue

            # ── Swing signal status ──────────────────────────────────────────
            swing_status = (f"Score {swing_signal['score']}/{swing_signal['threshold']} ✅"
                           if swing_signal else "N/A (day-only eligible)")
            print(f"   Swing Engine: {swing_status}")

            # ── Conflict gate ─────────────────────────────────────────────────
            if signals_conflict(day_signal, swing_signal):
                print(f"   ⚔️ Direction conflict — suppressed.")
                continue

            # ── Relative volume (from already-fetched 5m data) ───────────────
            rel_vol = calculate_relative_volume(df_intraday)

            # ── Decision matrix ───────────────────────────────────────────────
            final_signal = build_final_signal(day_signal, swing_signal,
                                              rel_vol, elapsed_minutes)
            if final_signal is None:
                print(f"   ➖ No qualifying scenario.")
                continue

            scenario = final_signal["scenario"]
            mode     = final_signal["mode"]
            print(f"   🎯 Scenario {scenario} | Mode: {mode}")

            # ── Cooldown check — DISABLED (allow repeat alerts) ─────────────
            # Re-enable this block to enforce per-ticker time-based suppression.
            # if is_on_cooldown(ticker, mode):
            #     continue

            # ── Risk validation ───────────────────────────────────────────────
            final_signal = validate_risk(final_signal, mode)
            if final_signal is None:
                continue

            print(f"   📊 R/R {final_signal['rr_ratio']:.2f} | "
                  f"Stop ${final_signal['stop_loss']:.2f} | "
                  f"Target ${final_signal['take_profit']:.2f}")

            # ── Earnings check (full implementation, not a placeholder) ───────
            has_earnings, earnings_msg = get_earnings_warning(ticker)
            if has_earnings:
                final_signal["score"] -= 2
                print(f"   ⚠️ Earnings within {EARNINGS_WARNING_DAYS}d — score docked 2pts")

                # Re-check threshold after penalty
                base_threshold = (DAY_SCORE_THRESHOLD if "DAY" in mode
                                  else SWING_SCORE_THRESHOLD)
                if final_signal["score"] < base_threshold + regime_penalty:
                    print(f"   ⚠️ Score below threshold after earnings penalty. Skipping.")
                    continue
            else:
                earnings_msg = ""

            # ── FIRE THE ALERT ────────────────────────────────────────────────
            send_discord_alert(
                ticker          = ticker,
                signal          = final_signal,
                rel_vol         = rel_vol,
                earnings_msg    = earnings_msg,
                elapsed_minutes = elapsed_minutes,
                regime_bullish  = regime_bullish,
            )

            # State machine + cooldown write — DISABLED (allow repeat alerts)
            # Re-enable these two calls to track alert state between scan cycles.
            # update_setup_state(
            #     ticker    = ticker,
            #     new_state = "TRIGGERED",
            #     stop_loss = final_signal["stop_loss"],
            #     mode      = mode,
            # )
            # set_cooldown(ticker, mode)

        except Exception as e:
            print(f"   ❌ Error on {ticker}: {e}")
            import traceback
            traceback.print_exc()

    print(f"\n{'='*62}")
    print(f"  Scan complete — {datetime.now(tz).strftime('%I:%M %p EST')}")
    print(f"{'='*62}\n")


# =============================================================================
#  ENTRY POINT
# =============================================================================

if __name__ == "__main__":
    check_market()
