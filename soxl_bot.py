"""
=============================================================================
  SOXL ALERT BOT v1.0 — Manual Execution Radar
=============================================================================

ARCHITECTURE & PURPOSE:
  Standalone alert system for SOXL (3x Semiconductors).
  Runs independently of the TQQQ automated execution bot.
  Scans daily bars for high-conviction mean-reversion setups.
  Sends Discord alerts formatted for manual Wealthsimple execution.

SIGNAL PATHS:
  Tier 1 (High Conviction):
    Path A: RSI Ladder — three tranches as RSI deepens:
      Tranche 1: RSI < 40 -> deploy $33, target 3.5x ATR
      Tranche 2: RSI < 32 -> deploy $33, target 4.0x ATR
      Tranche 3: RSI < 25 -> deploy $34, target 5.0x ATR
      Total max: $100. Each fires only if prior tranche is OPEN.
    Path D: RSI 35-45 + pivot low reversal (higher low + green close)

  Tier 2 (Medium Conviction — deploy 5% of portfolio):
    Path E: 21 EMA Bounce — price wicks 21 EMA + green close + RSI 35-50
    Path F: MACD Cross — histogram crosses zero with both lines below zero

  Path B (Additive Scoring): DISABLED — backtested -6.16% expectancy on SOXL.

SELL ALERTS (Piece 3):
  RSI_OB    - RSI > 68: momentum exhausted
  RSI_CROSS - RSI crosses above 55 from below: mean reversion complete
  EMA_LOSS  - Price drops below 21 EMA after being above it
  MACD_PEAK - MACD histogram peaked and turning down at positive levels

TRADE OUTCOME TRACKER (Piece 2):
  Every alert logged to soxl_trade_log.json.
  Open trades checked against daily OHLC on every run.
  Discord summary sent whenever trades resolve or positions are open.

VALIDATED PARAMETERS (soxl_backtest.py, 5yr walk-forward):
  Stop mult:  2.5x ATR (T1), 1.5x ATR (T2)
  Target mult:3.5x ATR (T1), 3.0x ATR (T2)
  RSI-A:      < 40
  Path B:     disabled

HOW TO RUN:
  python soxl_bot.py           # normal run
  python soxl_bot.py --force   # bypass entry window (testing)
  python soxl_bot.py --dry-run # skip trade log dedup (testing)
=============================================================================
"""

import os
import sys
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

try:
    import mplfinance as mpf
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    CHART_AVAILABLE = True
except ImportError:
    CHART_AVAILABLE = False
    print("mplfinance not installed - chart images disabled.")

# =============================================================================
#  SECTION 1 - CONFIGURATION
# =============================================================================

TICKERS_USD = ["VTI", "SOXL"]
TICKERS_CAD = []

DISCORD_WEBHOOK_URL = os.getenv("DISCORD_URL")

# Portfolio sizing
# PORTFOLIO_VALUE is the baseline for sizing only.
# At 10% Tier 1: $1,000 x 10% = $100 per trade (TFSA fractional target).
PORTFOLIO_VALUE    = 1000.0
TIER1_POSITION_PCT = 10.0
TIER2_POSITION_PCT = 5.0

# Backtested SOXL parameters (soxl_backtest.py 5yr walk-forward confirmed)
RSI_PATH_A            = 40    # Wider than TQQQ's 35 - semis capitulate harder
SWING_ATR_STOP_MULT   = 2.5   # Tier 1 stop:   support - (ATR x 2.5)
SWING_ATR_TARGET_MULT = 3.5   # Tier 1 target: entry   + (ATR x 3.5)
TIER2_ATR_STOP_MULT   = 1.5   # Tier 2 stop
TIER2_ATR_TARGET_MULT = 3.0   # Tier 2 target

TIER1_HOLD_DAYS = 10
TIER2_HOLD_DAYS = 5

# Risk gates
SWING_SCORE_THRESHOLD = 6
BASE_MAX_STOP_PCT     = 0.10   # 10% floor
ABSOLUTE_MAX_STOP_PCT = 0.20   # 20% ceiling - raised for SOXL wide ATR
MIN_RR_RATIO          = 1.1

# Entry window - buy signals only fire after 12:30pm ET
ENTRY_WINDOW_START_MIN = 180
ENTRY_WINDOW_END_MIN   = 390

# Earnings
EARNINGS_WARNING_DAYS  = 7
EARNINGS_SCORE_PENALTY = 2

# Trade log - persisted in repo, survives between GitHub Actions runs
TRADE_LOG_FILE        = "soxl_trade_log.json"
EARNINGS_CACHE_FILE   = "soxl_earnings_cache.json"
OUTCOME_CHECK_DAYS    = 12
OUTCOME_DISCORD_DAILY = True

# Sell alert thresholds
SOXL_RSI_OVERBOUGHT = 68     # Lower than TQQQ's 70 - SOXL moves faster
SOXL_TRAIL_PCT      = 7.0    # Alert if price drops >7% from running high

# RSI Ladder tranches — three tranches deployed as RSI deepens
# Each fires only if prior tranche is OPEN in the trade log.
# Total max deployment across all three: $100.
LADDER_TRANCHES = [
    {"tranche": 1, "rsi_below": 40, "deploy_pct": 3.3,
     "target_mult": 3.5, "stop_mult": 2.5, "label": "T1-Ladder1"},
    {"tranche": 2, "rsi_below": 32, "deploy_pct": 3.3,
     "target_mult": 4.0, "stop_mult": 2.5, "label": "T1-Ladder2"},
    {"tranche": 3, "rsi_below": 25, "deploy_pct": 3.4,
     "target_mult": 5.0, "stop_mult": 2.5, "label": "T1-Ladder3"},
]
LADDER_HOLD_DAYS = 10

# Discord colors
COLOR_GREEN  = 5763719
COLOR_YELLOW = 16776960
COLOR_BLUE   = 3447003
COLOR_RED    = 15548997
COLOR_ORANGE = 16744272

TIMEZONE = "US/Eastern"

# =============================================================================
#  SECTION 2 - DATA FETCHING AND REGIME
# =============================================================================

def _flatten(df):
    if isinstance(df.columns, pd.MultiIndex):
        ohlcv = {"Open", "High", "Low", "Close", "Volume"}
        for i in range(df.columns.nlevels):
            level_vals = df.columns.get_level_values(i)
            if ohlcv.intersection(set(level_vals)):
                df.columns = level_vals
                return df
        df.columns = df.columns.get_level_values(0)
    return df


def fetch_bulk_daily(tickers):
    try:
        df = yf.download(
            tickers, period="2y", interval="1d",
            group_by="ticker", auto_adjust=True,
            progress=False, multi_level_index=False,
        )
        return df
    except Exception as e:
        print(f"Bulk download failed: {e}")
        return pd.DataFrame()


def extract_ticker_daily(bulk_data, ticker):
    REQUIRED = {"Open", "High", "Low", "Close", "Volume"}
    try:
        if isinstance(bulk_data.columns, pd.MultiIndex):
            ticker_level = None
            for i in range(bulk_data.columns.nlevels):
                if ticker in bulk_data.columns.get_level_values(i):
                    ticker_level = i
                    break
            if ticker_level is None:
                return None
            df = bulk_data.xs(ticker, level=ticker_level, axis=1).copy()
        else:
            df = bulk_data.copy()
            df = _flatten(df)
            if REQUIRED - set(df.columns):
                return None
        df = _flatten(df)
        df.dropna(subset=["Close"], inplace=True)
        return df if not df.empty else None
    except Exception:
        return None


def check_market_regime(bulk_data):
    """Returns (penalty, is_bullish). +1 penalty when VTI below 200 SMA."""
    try:
        vti = extract_ticker_daily(bulk_data, "VTI")
        if vti is None or len(vti) < 200:
            return 0, True
        sma200    = ta.sma(vti["Close"], length=200).iloc[-1]
        vti_price = float(vti["Close"].iloc[-1])
        if vti_price < sma200:
            print(f"   Regime: BEARISH (VTI ${vti_price:.2f} < 200SMA ${sma200:.2f}) +1")
            return 1, False
        print(f"   Regime: BULLISH (VTI ${vti_price:.2f} > 200SMA ${sma200:.2f})")
        return 0, True
    except Exception:
        return 0, True

# =============================================================================
#  SECTION 3 - SWING TRADE ENGINE (SOXL TUNED)
# =============================================================================


def get_active_ladder_tranche(rsi):
    """
    Returns the deepest LADDER_TRANCHE whose RSI threshold is met
    AND which has not yet been opened in the trade log.
    Returns None if all qualifying tranches are already open.

    RSI 38: only T1 qualifies. Returns T1 if not open, else None.
    RSI 30: T1+T2 qualify. If T1 open, returns T2. If both open, None.
    RSI 23: all qualify. Returns deepest available.
    Requires prior tranche to be open before firing T2 or T3.
    """
    trades = load_trade_log()
    open_labels = {
        t.get("ladder_label")
        for t in trades
        if t["status"] == "OPEN" and t.get("ladder_label")
    }
    for tranche in sorted(LADDER_TRANCHES, key=lambda x: -x["tranche"]):
        if rsi >= tranche["rsi_below"]:
            continue
        if tranche["label"] in open_labels:
            continue
        if tranche["tranche"] > 1:
            prev_label = next(
                t["label"] for t in LADDER_TRANCHES
                if t["tranche"] == tranche["tranche"] - 1
            )
            if prev_label not in open_labels:
                continue
        return tranche
    return None

def run_swing_engine(df_daily, total_penalty, ticker="SOXL"):
    if df_daily is None or len(df_daily) < 50:
        return None

    df = df_daily.copy()
    df["EMA_21"]  = ta.ema(df["Close"], length=21)
    df["EMA_50"]  = ta.ema(df["Close"], length=50)
    df["EMA_200"] = ta.ema(df["Close"], length=200)
    df["RSI"]     = ta.rsi(df["Close"], length=14)
    df["ATR"]     = ta.atr(df["High"], df["Low"], df["Close"], length=14)

    macd = ta.macd(df["Close"])
    if macd is not None:
        hist_cols   = [c for c in macd.columns if c.startswith("MACDh")]
        line_cols   = [c for c in macd.columns if c.startswith("MACD_") or
                       (c.startswith("MACD") and not c.startswith("MACDh")
                        and not c.startswith("MACDs"))]
        signal_cols = [c for c in macd.columns if c.startswith("MACDs")]
        if hist_cols:   df["MACD_H"] = macd[hist_cols[0]]
        if line_cols:   df["MACD"]   = macd[line_cols[0]]
        if signal_cols: df["MACDs"]  = macd[signal_cols[0]]

    # Write indicators back for charting
    for col in ["EMA_21", "EMA_50", "EMA_200", "RSI", "ATR", "MACD_H"]:
        if col in df.columns:
            df_daily[col] = df[col]

    df.dropna(subset=["RSI", "EMA_50", "ATR"], inplace=True)
    if len(df) < 3:
        return None

    scored = df.iloc[-1]
    prev   = df.iloc[-2]

    price    = float(scored["Close"])
    entry_price = price
    ema_21   = float(scored["EMA_21"])
    ema_50   = float(scored["EMA_50"])
    ema_200  = float(scored["EMA_200"]) if not pd.isna(scored.get("EMA_200", float("nan"))) else None
    rsi      = float(scored["RSI"])
    atr      = float(scored["ATR"])
    macd_h   = float(scored["MACD_H"]) if "MACD_H" in df.columns else 0.0
    prev_mh  = float(prev["MACD_H"])   if "MACD_H" in df.columns else 0.0
    macd_line= float(scored["MACD"])   if "MACD"   in df.columns else macd_h
    macd_sig = float(scored["MACDs"])  if "MACDs"  in df.columns else 0.0
    bar_open  = float(scored["Open"])
    bar_high  = float(scored["High"])
    bar_low   = float(scored["Low"])
    bar_close = float(scored["Close"])

    threshold = SWING_SCORE_THRESHOLD + total_penalty

    # ── PATH A - DEEP OVERSOLD BYPASS WITH RSI LADDER ───────────────────
    # RSI < 40 triggers ladder evaluation. Which tranche fires depends on:
    #   (a) how deep the RSI is (which thresholds are breached)
    #   (b) which tranches are already OPEN in the trade log
    # Each tranche has its own sizing and target multiplier.
    # Deeper RSI = larger target (bigger snap-back historically).
    if rsi < RSI_PATH_A:
        # Support anchor — same logic for all tranches
        if ema_200 is not None and price > ema_200:
            ema200_gap = (price - ema_200) / price
            if ema200_gap > 0.10:
                support        = entry_price
                support_source = "Volatility Stop (200 EMA >10% distant)"
            else:
                support        = ema_200
                support_source = "200 EMA"
        elif price > ema_50:
            support        = ema_50
            support_source = "50 EMA"
        elif price > ema_21:
            support        = ema_21
            support_source = "21 EMA"
        else:
            support        = entry_price
            support_source = "Volatility Stop (below all EMAs)"

        active_tranche = get_active_ladder_tranche(rsi)
        if active_tranche is None:
            print(f"   [{ticker}] PATH A RSI {rsi:.1f} - all qualifying tranches OPEN")
        else:
            t_num   = active_tranche["tranche"]
            t_mult  = active_tranche["target_mult"]
            s_mult  = active_tranche["stop_mult"]
            t_pct   = active_tranche["deploy_pct"]
            t_label = active_tranche["label"]

            stop_loss   = support - (atr * s_mult)
            if stop_loss >= entry_price:
                stop_loss = entry_price - atr
            take_profit = entry_price + (atr * t_mult)

            reasons = [
                f"RSI Ladder Tranche {t_num}/3 - RSI {rsi:.1f} < {active_tranche['rsi_below']}",
                f"Support anchor: {support_source} ${support:.2f}",
                f"Target: {t_mult}x ATR — wider for deeper capitulation",
                f"Deploy: ${PORTFOLIO_VALUE * t_pct / 100:.0f} ({t_pct}% of baseline)",
            ]
            print(f"   [{ticker}] PATH A Ladder T{t_num}/3 RSI={rsi:.1f} "
                  f"target={t_mult}x deploy={t_pct}%")
            return {
                "price": entry_price, "entry_price": entry_price,
                "stop_loss": round(stop_loss, 2), "take_profit": round(take_profit, 2),
                "score": 6, "threshold": threshold,
                "atr": round(atr, 4), "atr_source": "Daily",
                "ema_21": round(ema_21, 2), "ema_50": round(ema_50, 2),
                "ema_200": round(ema_200, 2) if ema_200 else None,
                "rsi": round(rsi, 1), "macd_h": round(macd_h, 4),
                "support": round(support, 2), "support_source": support_source,
                "path": "A", "tier": 1, "hold_days": LADDER_HOLD_DAYS,
                "position_size_pct": t_pct,
                "ladder_tranche": t_num,
                "ladder_label":   t_label,
                "reasons": reasons, "mode": "SWING",
            }

    # ── PATH D - PIVOT LOW REVERSAL ──────────────────────────────────────
    # Dead zone + RSI 35-45 + higher low + green close + RSI turning up.
    # RSI ceiling 45 (vs TQQQ 50) - tighter for SOXL.
    prev_rsi = float(prev["RSI"]) if not pd.isna(prev.get("RSI", float("nan"))) else rsi
    if (
        price < ema_21 and price < ema_50 and
        35 <= rsi < 45 and
        bar_low > float(prev["Low"]) and
        bar_close > bar_open and
        rsi > prev_rsi
    ):
        support     = float(prev["Low"])
        stop_loss   = support - (atr * SWING_ATR_STOP_MULT)
        if stop_loss >= entry_price:
            stop_loss = entry_price - atr
        take_profit = entry_price + (atr * SWING_ATR_TARGET_MULT)
        reasons = [
            f"Path D - Pivot Low Reversal",
            f"Higher Low (${bar_low:.2f} > ${float(prev['Low']):.2f}) + Green Close",
            f"RSI Turning Up {prev_rsi:.1f} to {rsi:.1f}",
        ]
        print(f"   [{ticker}] PATH D - RSI {rsi:.1f} HL=${bar_low:.2f}>${float(prev['Low']):.2f}")
        return {
            "price": entry_price, "entry_price": entry_price,
            "stop_loss": round(stop_loss, 2), "take_profit": round(take_profit, 2),
            "score": 6, "threshold": threshold,
            "atr": round(atr, 4), "atr_source": "Daily",
            "ema_21": round(ema_21, 2), "ema_50": round(ema_50, 2),
            "ema_200": round(ema_200, 2) if ema_200 else None,
            "rsi": round(rsi, 1), "macd_h": round(macd_h, 4),
            "support": round(support, 2), "support_source": "Prior Day Pivot Low",
            "path": "D", "tier": 1, "hold_days": TIER1_HOLD_DAYS,
            "position_size_pct": TIER1_POSITION_PCT,
            "reasons": reasons, "mode": "SWING",
        }

    # ── PATH E - 21 EMA BOUNCE (TIER 2) ──────────────────────────────────
    # RSI ceiling 50 (backtest confirmed no edge above 50 on SOXL).
    if (
        bar_low <= ema_21 * 1.015 and
        bar_close > ema_21 and
        bar_close > bar_open and
        35 <= rsi <= 50 and
        price > ema_50
    ):
        support     = min(bar_low, float(prev["Low"]))
        stop_loss   = support - (atr * TIER2_ATR_STOP_MULT)
        if stop_loss >= entry_price:
            stop_loss = entry_price - atr
        take_profit = entry_price + (atr * TIER2_ATR_TARGET_MULT)
        reasons = [
            f"Path E - 21 EMA Bounce Wick",
            f"Wick to EMA ${ema_21:.2f} (Low ${bar_low:.2f}) + Green Close",
            f"Price above 50 EMA ${ema_50:.2f} - uptrend intact",
        ]
        print(f"   [{ticker}] PATH E - RSI {rsi:.1f} wick=${bar_low:.2f} ema21=${ema_21:.2f}")
        return {
            "price": entry_price, "entry_price": entry_price,
            "stop_loss": round(stop_loss, 2), "take_profit": round(take_profit, 2),
            "score": 4, "threshold": threshold,
            "atr": round(atr, 4), "atr_source": "Daily",
            "ema_21": round(ema_21, 2), "ema_50": round(ema_50, 2),
            "ema_200": round(ema_200, 2) if ema_200 else None,
            "rsi": round(rsi, 1), "macd_h": round(macd_h, 4),
            "support": round(support, 2), "support_source": "21 EMA Bounce Wick",
            "path": "E", "tier": 2, "hold_days": TIER2_HOLD_DAYS,
            "position_size_pct": TIER2_POSITION_PCT,
            "reasons": reasons, "mode": "SWING",
        }

    # ── PATH F - MACD CROSS (TIER 2) ─────────────────────────────────────
    # Histogram crosses zero while both line and signal still below zero.
    macd_cross      = prev_mh < 0 and macd_h >= 0
    both_below_zero = macd_line < 0 and macd_sig < 0
    macd_rsi_ok     = rsi < 60
    macd_trend_ok   = price > ema_21
    macd_green      = bar_close > bar_open

    if macd_cross and both_below_zero and macd_rsi_ok and macd_trend_ok and macd_green:
        support     = float(prev["Low"])
        stop_loss   = support - (atr * TIER2_ATR_STOP_MULT)
        if stop_loss >= entry_price:
            stop_loss = entry_price - atr
        take_profit = entry_price + (atr * TIER2_ATR_TARGET_MULT)
        reasons = [
            f"Path F - MACD Cross",
            f"Histogram crossed zero: {prev_mh:.3f} to {macd_h:.3f}",
            f"Both MACD lines still below zero - early recovery",
        ]
        print(f"   [{ticker}] PATH F - RSI {rsi:.1f} macd={prev_mh:.3f}>{macd_h:.3f}")
        return {
            "price": entry_price, "entry_price": entry_price,
            "stop_loss": round(stop_loss, 2), "take_profit": round(take_profit, 2),
            "score": 4, "threshold": threshold,
            "atr": round(atr, 4), "atr_source": "Daily",
            "ema_21": round(ema_21, 2), "ema_50": round(ema_50, 2),
            "ema_200": round(ema_200, 2) if ema_200 else None,
            "rsi": round(rsi, 1), "macd_h": round(macd_h, 4),
            "support": round(support, 2), "support_source": "Prior Day Low",
            "path": "F", "tier": 2, "hold_days": TIER2_HOLD_DAYS,
            "position_size_pct": TIER2_POSITION_PCT,
            "reasons": reasons, "mode": "SWING",
        }

    # ── PATH B - DISABLED ON SOXL ─────────────────────────────────────────
    # Backtest: 32 trades, 20% WR, -6.16% expectancy over 4.8 years.
    # RSI 47-60 at entry (not oversold) - fires on momentum dips that become
    # sector flushes on a 3x semiconductor ETF.
    return None


# =============================================================================
#  SECTION 4 - RISK VALIDATOR
# =============================================================================

def validate_risk(signal):
    price     = signal["price"]
    stop_loss = signal["stop_loss"]
    target    = signal["take_profit"]
    atr       = signal.get("atr", 0)

    atr_pct          = atr / price if price > 0 else 0
    dynamic_max_stop = max(BASE_MAX_STOP_PCT, min(atr_pct * 3.5, ABSOLUTE_MAX_STOP_PCT))
    actual_pct       = (price - stop_loss) / price

    if actual_pct > dynamic_max_stop:
        print(f"   [SOXL] Stop too wide ({actual_pct*100:.1f}% > "
              f"{dynamic_max_stop*100:.1f}%). Rejected.")
        return None

    risk = price - stop_loss
    if risk <= 0:
        return None

    structural_rr = round((target - price) / risk, 2)
    if structural_rr < MIN_RR_RATIO:
        print(f"   [SOXL] R/R {structural_rr:.2f} below minimum. Rejected.")
        return None

    signal["rr_ratio"] = structural_rr
    return signal


# =============================================================================
#  SECTION 5 - POSITION SIZING
# =============================================================================

def calculate_position_size(score, threshold, price, atr, tier=1):
    base       = TIER1_POSITION_PCT if tier == 1 else TIER2_POSITION_PCT
    margin     = max(score - threshold, 0)
    conviction = min(margin * 1.0, 4.0)
    atr_pct    = (atr / price) * 100 if price > 0 else 4.0

    if atr_pct <= 3.0:
        vol_factor = 1.0
    elif atr_pct <= 6.0:
        vol_factor = 1.0 - ((atr_pct - 3.0) / 3.0) * 0.4
    else:
        vol_factor = 0.6

    pct           = round(min((base + conviction) * vol_factor, 15.0), 1)
    dollar_amount = round(PORTFOLIO_VALUE * pct / 100, 2)
    label = (f"Wealthsimple Fractional Order: **${dollar_amount:,.2f}** "
             f"({pct}% of ${PORTFOLIO_VALUE:,.0f} baseline)")
    return {"pct": pct, "dollar": dollar_amount, "label": label}


# =============================================================================
#  SECTION 6 - TRADE LOG (PIECE 2 - OUTCOME TRACKER)
# =============================================================================

def load_trade_log():
    try:
        p = Path(TRADE_LOG_FILE)
        if p.exists():
            with open(p) as f:
                return json.load(f)
    except Exception as e:
        print(f"Trade log load failed: {e}")
    return []


def save_trade_log(trades):
    try:
        with open(TRADE_LOG_FILE, "w") as f:
            json.dump(trades, f, indent=2)
    except Exception as e:
        print(f"Trade log save failed: {e}")


def log_new_trade(signal):
    """Logs a new SOXL alert. Skips if an OPEN trade already exists."""
    try:
        trades = load_trade_log()

        dry_run = globals().get("DRY_RUN", False)
        if not dry_run:
            ladder_label = signal.get("ladder_label")
            if ladder_label:
                # Per-tranche dedup — T1 open does not block T2
                if any(t["status"] == "OPEN" and t.get("ladder_label") == ladder_label
                       for t in trades):
                    print(f"   Ladder {ladder_label} already OPEN - skipping")
                    return
            else:
                # Non-ladder paths: one open trade at a time
                if any(t["status"] == "OPEN" and not t.get("ladder_label")
                       for t in trades):
                    print(f"   SOXL already has an OPEN trade - skipping duplicate log")
                    return

        tz  = pytz.timezone(TIMEZONE)
        now = datetime.now(tz)
        trade = {
            "id":             f"SOXL_{now.strftime('%Y%m%d_%H%M')}",
            "ticker":         "SOXL",
            "alert_date":     now.strftime("%Y-%m-%d"),
            "alert_time":     now.strftime("%H:%M ET"),
            "path":           signal.get("path", "?"),
            "tier":           int(signal.get("tier", 1)),
            "entry":          float(signal["price"]),
            "stop_loss":      float(signal["stop_loss"]),
            "take_profit":    float(signal["take_profit"]),
            "rr_ratio":       float(signal.get("rr_ratio", 0)),
            "rsi":            float(signal.get("rsi", 0)),
            "atr":            float(signal.get("atr", 0)),
            "ema_21":         float(signal.get("ema_21", 0)),
            "ema_50":         float(signal.get("ema_50", 0)),
            "support":        float(signal.get("support", 0)),
            "support_source": str(signal.get("support_source", "")),
            "reasons":        [str(r) for r in signal.get("reasons", [])],
            "ladder_tranche": signal.get("ladder_tranche", None),
            "ladder_label":   signal.get("ladder_label",   None),
            "status":         "OPEN",
            "outcome_date":   None,
            "outcome_pct":    None,
            "max_price":      float(signal["price"]),
            "min_price":      float(signal["price"]),
        }
        trades.append(trade)
        save_trade_log(trades)
        print(f"   Trade logged: {trade['id']}")
    except Exception as e:
        print(f"   Trade log error: {e}")


def check_open_trades(bulk_data):
    """
    Checks all OPEN trades against latest daily OHLC.
    Walks bars chronologically - first event (target or stop) wins.
    Returns list of newly resolved trades for Discord summary.
    """
    trades   = load_trade_log()
    resolved = []
    tz       = pytz.timezone(TIMEZONE)
    today    = datetime.now(tz).date()
    changed  = False

    open_count = len([t for t in trades if t["status"] == "OPEN"])
    print(f"   Checking {open_count} open trade(s)...")

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
                    (trade["max_price"] - trade["entry"]) / trade["entry"] * 100, 2
                )
                resolved.append(trade)
                changed = True
                print(f"   SOXL EXPIRED after {days_open}d (max ${trade['max_price']:.2f})")
                continue

            df = extract_ticker_daily(bulk_data, "SOXL")
            if df is None or df.empty:
                continue

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
            latest = float(df_window["Close"].iloc[-1])
            changed = True

            outcome_found = None
            outcome_date  = None

            for bar_date, bar in df_window.iterrows():
                bar_high = float(bar["High"])
                bar_low  = float(bar["Low"])
                trade["max_price"] = float(max(trade.get("max_price", entry), bar_high))
                trade["min_price"] = float(min(trade.get("min_price", entry), bar_low))

                if bar_low <= stop and bar_high >= target:
                    outcome_found = "WON"
                    outcome_date  = str(bar_date.date() if hasattr(bar_date, "date") else bar_date)
                    break
                elif bar_high >= target:
                    outcome_found = "WON"
                    outcome_date  = str(bar_date.date() if hasattr(bar_date, "date") else bar_date)
                    break
                elif bar_low <= stop:
                    outcome_found = "LOST"
                    outcome_date  = str(bar_date.date() if hasattr(bar_date, "date") else bar_date)
                    break

            if outcome_found == "WON":
                trade["status"]       = "WON"
                trade["outcome_date"] = outcome_date
                trade["outcome_pct"]  = round((target - entry) / entry * 100, 2)
                resolved.append(trade)
                print(f"   SOXL WON - target ${target:.2f} hit on {outcome_date}")
            elif outcome_found == "LOST":
                trade["status"]       = "LOST"
                trade["outcome_date"] = outcome_date
                trade["outcome_pct"]  = round((stop - entry) / entry * 100, 2)
                resolved.append(trade)
                print(f"   SOXL LOST - stop ${stop:.2f} hit on {outcome_date}")
            else:
                pct_to_tgt  = (target - latest) / entry * 100
                pct_to_stop = (latest - stop)   / entry * 100
                print(f"   SOXL OPEN - close ${latest:.2f} | "
                      f"+{pct_to_tgt:.1f}% to target | -{pct_to_stop:.1f}% to stop")

        except Exception as e:
            print(f"   Outcome check error: {e}")

    if changed:
        save_trade_log(trades)

    return resolved


def send_outcome_summary(resolved):
    """Sends Discord embed showing open positions and newly resolved trades."""
    if not DISCORD_WEBHOOK_URL:
        return
    try:
        trades   = load_trade_log()
        open_tr  = [t for t in trades if t["status"] == "OPEN"]
        won_tr   = [t for t in trades if t["status"] == "WON"]
        lost_tr  = [t for t in trades if t["status"] == "LOST"]
        expired  = [t for t in trades if t["status"] == "EXPIRED"]

        total_closed = len(won_tr) + len(lost_tr)
        win_rate     = (len(won_tr) / total_closed * 100) if total_closed > 0 else 0
        avg_win      = (sum(t["outcome_pct"] for t in won_tr)  / len(won_tr))  if won_tr  else 0
        avg_loss     = (sum(t["outcome_pct"] for t in lost_tr) / len(lost_tr)) if lost_tr else 0

        desc  = f"**Win Rate:** `{win_rate:.0f}%` "
        desc += f"({len(won_tr)}W / {len(lost_tr)}L / {len(expired)} expired)\n"
        desc += f"**Avg Win:** `+{avg_win:.1f}%` | **Avg Loss:** `{avg_loss:.1f}%`\n"

        if open_tr:
            desc += f"\n**Open Positions ({len(open_tr)})**\n"
            for t in open_tr[-5:]:
                days = (datetime.now(pytz.timezone(TIMEZONE)).date() -
                        datetime.strptime(t["alert_date"], "%Y-%m-%d").date()).days
                desc += (f"- SOXL Path {t['path']} | "
                         f"Entry ${t['entry']:.2f} | "
                         f"Target ${t['take_profit']:.2f} | "
                         f"Stop ${t['stop_loss']:.2f} | "
                         f"Day {days}/{t.get('hold_days', TIER1_HOLD_DAYS)}\n")

        if resolved:
            desc += f"\n**Just Resolved ({len(resolved)})**\n"
            for t in resolved:
                icon = "WIN" if t["status"] == "WON" else ("LOSS" if t["status"] == "LOST" else "EXPIRED")
                desc += (f"- {icon} SOXL Path {t['path']} | "
                         f"{t['outcome_pct']:+.1f}% | entry ${t['entry']:.2f}\n")

        payload = {"embeds": [{
            "title":       "SOXL Trade Tracker",
            "description": desc[:4096],
            "color":       COLOR_GREEN if win_rate >= 50 else COLOR_RED,
            "footer":      {"text": f"SOXL Alert Bot v1.0 | {len(trades)} total trades"},
        }]}
        _post_discord(payload)
        print(f"   Outcome summary sent ({len(open_tr)} open, {len(resolved)} resolved)")
    except Exception as e:
        print(f"Outcome summary error: {e}")


# =============================================================================
#  SECTION 7 - SELL ALERTS (PIECE 3)
# =============================================================================

def fetch_soxl_live_price():
    try:
        p = yf.Ticker("SOXL").fast_info.get("last_price")
        if p is not None:
            return float(p)
    except Exception as e:
        print(f"   Live price fetch failed: {e}")
    return None


def check_live_sell_alerts(bulk_data):
    """
    Technical sell signal runner. Four exit conditions:
      RSI_OB    - RSI > 68 (momentum exhausted)
      RSI_CROSS - RSI crosses above 55 from below (recovery complete)
      EMA_LOSS  - Price drops below 21 EMA after being above
      MACD_PEAK - Histogram peaked and turning down while positive

    Updates max_price watermark on open trades.
    Labels signals NEW vs STILL ACTIVE within same day via /tmp.
    """
    trades      = load_trade_log()
    open_trades = [t for t in trades if t["status"] == "OPEN"]
    print(f"   Checking sell conditions ({len(open_trades)} open)...")

    live_price = fetch_soxl_live_price()
    if live_price is None:
        print("   Could not fetch live price - sell check skipped.")
        return
    print(f"   SOXL live: ${live_price:.2f}")

    # Update max_price watermark
    changed = False
    for trade in trades:
        if trade["status"] != "OPEN":
            continue
        if live_price > trade.get("max_price", trade["entry"]):
            trade["max_price"] = float(live_price)
            changed = True
    if changed:
        save_trade_log(trades)

    # Build indicators from bulk data
    try:
        df = extract_ticker_daily(bulk_data, "SOXL")
        if df is None or df.empty:
            return
        df = df.copy()
        df["RSI"]    = ta.rsi(df["Close"], length=14)
        df["EMA_21"] = ta.ema(df["Close"], length=21)
        macd_df = ta.macd(df["Close"])
        if macd_df is not None:
            hist_col = [c for c in macd_df.columns if c.startswith("MACDh")]
            if hist_col:
                df["MACD_H"] = macd_df[hist_col[0]]
        df.dropna(subset=["RSI", "EMA_21"], inplace=True)
        if len(df) < 3:
            return

        today  = df.iloc[-1]
        prev   = df.iloc[-2]
        prev2  = df.iloc[-3]

        rsi_today  = float(today["RSI"])
        rsi_prev   = float(prev["RSI"])
        ema_21     = float(today["EMA_21"])
        prev_close = float(prev["Close"])
        macd_today = float(today.get("MACD_H", 0)) if "MACD_H" in df.columns else None
        macd_prev  = float(prev.get("MACD_H", 0))  if "MACD_H" in df.columns else None
        macd_prev2 = float(prev2.get("MACD_H", 0)) if "MACD_H" in df.columns else None

    except Exception as e:
        print(f"   Sell data failed: {e}")
        return

    # Session tracking - NEW vs STILL ACTIVE within same day
    sell_session_file = Path("/tmp/soxl_sell_session.json")
    try:
        session_fired = (json.loads(sell_session_file.read_text())
                         if sell_session_file.exists() else {})
    except Exception:
        session_fired = {}

    def _send(reason, title, desc, color):
        already = session_fired.get(reason, False)
        label   = "STILL ACTIVE" if already else "NEW"
        payload = {"embeds": [{
            "title":       f"{label} - {title}",
            "description": desc,
            "color":       color,
            "footer":      {"text": "SOXL Alert Bot v1.0 | Sell Signal"},
        }]}
        _post_discord(payload)
        session_fired[reason] = True
        try:
            sell_session_file.write_text(json.dumps(session_fired))
        except Exception:
            pass
        print(f"   [{label}] Sell {reason} RSI={rsi_today:.1f} price=${live_price:.2f}")

    # SIGNAL 1: RSI OVERBOUGHT
    if rsi_today > SOXL_RSI_OVERBOUGHT:
        _send(
            reason = "RSI_OB",
            title  = "SELL SIGNAL - SOXL RSI Overbought",
            color  = COLOR_RED,
            desc   = (
                f"RSI(14) reached **{rsi_today:.1f}** - momentum exhausted.\n\n"
                f"**Consider selling your SOXL position.**\n\n"
                f"- Live Price: `${live_price:.2f}`\n"
                f"- RSI Today: `{rsi_today:.1f}` (threshold: {SOXL_RSI_OVERBOUGHT})\n"
                f"- RSI Yesterday: `{rsi_prev:.1f}`\n"
                f"- 21 EMA: `${ema_21:.2f}`\n\n"
                f"_Technical signal - you decide whether to act._"
            )
        )

    # SIGNAL 2: RSI RECOVERY COMPLETE
    elif rsi_today > 55 and rsi_prev < 45:
        _send(
            reason = "RSI_CROSS",
            title  = "SELL SIGNAL - SOXL Mean Reversion Complete",
            color  = COLOR_ORANGE,
            desc   = (
                f"RSI recovered from oversold: **{rsi_prev:.1f} to {rsi_today:.1f}**\n\n"
                f"The mean-reversion trade has played out.\n\n"
                f"- Live Price: `${live_price:.2f}`\n"
                f"- RSI Today: `{rsi_today:.1f}` (crossed above 55)\n"
                f"- RSI Yesterday: `{rsi_prev:.1f}` (was below 45)\n\n"
                f"_Consider booking profits._"
            )
        )

    # SIGNAL 3: 21 EMA LOST
    was_above = prev_close > float(prev["EMA_21"])
    now_below = live_price < ema_21
    if was_above and now_below:
        drop_pct = (prev_close - live_price) / prev_close * 100
        _send(
            reason = "EMA_LOSS",
            title  = "SELL SIGNAL - SOXL 21 EMA Lost",
            color  = COLOR_YELLOW,
            desc   = (
                f"Price dropped below 21 EMA - short-term uptrend broken.\n\n"
                f"- Live Price: `${live_price:.2f}` (below 21 EMA `${ema_21:.2f}`)\n"
                f"- Yesterday Close: `${prev_close:.2f}` (was above EMA)\n"
                f"- Drop: `{drop_pct:.1f}%` from yesterday\n"
                f"- RSI: `{rsi_today:.1f}`\n\n"
                f"_Losing the 21 EMA in a semiconductor selloff often signals more "
                f"downside. Consider tightening stop or exiting Tier 2._"
            )
        )

    # SIGNAL 4: MACD HISTOGRAM PEAK
    if macd_today is not None and macd_prev is not None and macd_prev2 is not None:
        macd_peaked = (
            macd_prev > macd_prev2 and
            macd_today < macd_prev and
            macd_prev > 0
        )
        if macd_peaked:
            _send(
                reason = "MACD_PEAK",
                title  = "SELL SIGNAL - SOXL MACD Momentum Fading",
                color  = COLOR_BLUE,
                desc   = (
                    f"MACD histogram peaked and turning down.\n\n"
                    f"- Live Price: `${live_price:.2f}`\n"
                    f"- MACD: `{macd_prev2:.3f}` to `{macd_prev:.3f}` (peak) "
                    f"to `{macd_today:.3f}` (turning down)\n"
                    f"- RSI: `{rsi_today:.1f}`\n\n"
                    f"_Early warning - consider partial profit-taking or "
                    f"tightening your stop._"
                )
            )


# =============================================================================
#  SECTION 8 - DISCORD
# =============================================================================

def _post_discord(payload):
    if not DISCORD_WEBHOOK_URL:
        print("DISCORD_URL not set - skipping.")
        return
    try:
        r = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=10)
        if not r.ok:
            print(f"Discord error: {r.status_code} {r.text[:200]}")
        time.sleep(0.5)
    except Exception as e:
        print(f"Discord error: {e}")


def send_setup_alert(signal, elapsed_minutes, regime_bullish):
    """Sends buy signal embed to Discord."""
    if not DISCORD_WEBHOOK_URL:
        return

    et_now  = datetime.now(pytz.timezone(TIMEZONE))
    price   = signal["price"]
    stop    = signal["stop_loss"]
    target  = signal["take_profit"]
    rr      = signal.get("rr_ratio", 0.0)
    tier    = signal.get("tier", 1)
    path    = signal.get("path", "?")
    rsi_val = signal.get("rsi", 0.0)
    atr_val = signal.get("atr", 0.0)

    stop_pct = (price - stop)   / price * 100
    tgt_pct  = (target - price) / price * 100

    pos_c = calculate_position_size(
        signal["score"], signal["threshold"], price, atr_val, tier
    )

    tier_label = "TIER 1 - HIGH CONVICTION" if tier == 1 else "TIER 2 - MEDIUM CONVICTION"
    color      = COLOR_RED if tier == 1 else COLOR_YELLOW
    path_name  = {"A": "Deep Oversold RSI", "D": "Pivot Low Reversal",
                  "E": "21 EMA Bounce", "F": "MACD Cross"}.get(path, path)
    regime_str = "Bullish" if regime_bullish else "Bearish"

    rsi_label = ("Deeply Oversold" if rsi_val < 30 else
                 "Oversold"        if rsi_val < 45 else
                 "Neutral"         if rsi_val < 55 else "Bullish")

    desc  = f"*{et_now.strftime('%I:%M %p ET')} | Regime: {regime_str}*\n\n"
    desc += f"**{tier_label}**\n"
    desc += f"Signal: **{path_name}** (Path {path})\n"
    desc += f"Position: {pos_c['label']}\n"
    desc += f"Hold: **{signal.get('hold_days', TIER1_HOLD_DAYS)} trading days**\n\n"

    desc += "**Trade Plan**\n"
    desc += f"- Entry:  `${price:.2f}`\n"
    desc += f"- Target: `${target:.2f}` (+{tgt_pct:.1f}%)\n"
    desc += f"- Stop:   `${stop:.2f}` (-{stop_pct:.1f}%)\n"
    desc += f"- R/R:    `1:{rr:.2f}`\n"
    desc += f"- ATR:    `${atr_val:.2f}`\n\n"

    desc += "**Technicals**\n"
    desc += f"- RSI:        `{rsi_val:.1f}` ({rsi_label})\n"
    desc += f"- 21 EMA:     `${signal.get('ema_21', 0):.2f}`\n"
    desc += f"- 50 EMA:     `${signal.get('ema_50', 0):.2f}`\n"
    desc += f"- Stop Anchor: {signal.get('support_source', '-')}\n\n"

    desc += "**Why This Signal**\n"
    for r in signal.get("reasons", []):
        desc += f"- {r}\n"

    desc += (f"\n[TradingView](https://www.tradingview.com/chart/?symbol=SOXL) | "
             f"[Yahoo Finance](https://finance.yahoo.com/quote/SOXL)")

    if len(desc) > 4096:
        desc = desc[:4050] + "\n...(trimmed)"

    payload = {
        "content": f"**ACTION REQUIRED: SOXL MANUAL ENTRY | Path {path} | ${price:.2f}**",
        "embeds": [{
            "title":       f"NEW SIGNAL - SOXL | {path_name} | Hold {signal.get('hold_days',10)}d",
            "description": desc,
            "color":       color,
            "footer":      {"text": f"SOXL Alert Bot v1.0 | Path {path} | Tier {tier}"},
        }],
    }
    _post_discord(payload)
    print(f"   Buy alert sent: SOXL Path {path} Tier {tier} @ ${price:.2f}")


# =============================================================================
#  SECTION 9 - MAIN EXECUTION LOOP
# =============================================================================

def check_market():
    tz     = pytz.timezone(TIMEZONE)
    et_now = datetime.now(tz)

    print(f"\n{'='*60}")
    print(f"  SOXL Alert Bot v1.1 RSI Ladder - {et_now.strftime('%A %b %d %Y %I:%M %p ET')}")
    print(f"{'='*60}\n")

    mkt_open    = et_now.replace(hour=9, minute=30, second=0, microsecond=0)
    elapsed_min = max((et_now - mkt_open).total_seconds() / 60.0, 0.0)

    in_entry_window = ENTRY_WINDOW_START_MIN <= elapsed_min < ENTRY_WINDOW_END_MIN
    force_override  = globals().get("FORCE_RUN", False)

    # Download data (one call covers SOXL + VTI)
    print("Downloading SOXL + VTI daily data...")
    bulk_data = fetch_bulk_daily(TICKERS_USD)
    if bulk_data is None or bulk_data.empty:
        print("Data download failed. Aborting.")
        return

    regime_penalty, regime_bullish = check_market_regime(bulk_data)
    total_penalty = regime_penalty

    # PIECE 2: Outcome tracking (runs every run regardless of time)
    print("\nChecking open trade outcomes...")
    resolved = check_open_trades(bulk_data)

    # PIECE 3: Sell alerts (runs every run regardless of time)
    print("\nChecking sell conditions...")
    check_live_sell_alerts(bulk_data)

    # Outcome Discord summary
    if OUTCOME_DISCORD_DAILY:
        open_trades = [t for t in load_trade_log() if t["status"] == "OPEN"]
        if resolved or open_trades:
            send_outcome_summary(resolved)

    # Buy signal scan - only within entry window
    if not in_entry_window and not force_override:
        print(f"\nOutside entry window (elapsed {elapsed_min:.0f} min) - buy scan skipped.")
        return

    print(f"\nScanning SOXL for buy setups...")
    df_daily = extract_ticker_daily(bulk_data, "SOXL")
    if df_daily is None:
        print("No SOXL data.")
        return

    signal = run_swing_engine(df_daily, total_penalty, ticker="SOXL")
    if signal is None:
        print(f"   No setups found. | {et_now.strftime('%I:%M %p ET')}")
        return

    signal = validate_risk(signal)
    if signal is None:
        return

    print(f"   R/R {signal['rr_ratio']:.2f} | Stop ${signal['stop_loss']:.2f} | "
          f"Target ${signal['take_profit']:.2f}")

    send_setup_alert(signal, elapsed_min, regime_bullish)
    log_new_trade(signal)

    print(f"\nScan complete | {et_now.strftime('%I:%M %p ET')}\n")


# =============================================================================
#  ENTRY POINT
# =============================================================================

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="SOXL Alert Bot v1.0")
    parser.add_argument("--force",   action="store_true",
                        help="Bypass entry window gate (testing)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Skip trade log dedup (testing)")
    args = parser.parse_args()

    DRY_RUN   = args.dry_run
    FORCE_RUN = args.force

    if DRY_RUN:
        print("DRY RUN - trade log dedup disabled")
    if FORCE_RUN:
        print("--force active - entry window bypassed")

    check_market()
