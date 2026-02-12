import yfinance as yf
import pandas_ta as ta
import requests
import os
import pandas as pd
import numpy as np
from datetime import datetime
import pytz

# --- CONFIGURATION ---
TICKERS = [
    # --- SAFE FOUNDATION ---
    'VFV.TO', 'ZSP.TO', 'XEF.TO', 'VTI',
    # --- SECTOR ETFS ---
    'SOXQ', 'XLY',
    # --- CANADIAN GROWTH ---
    'HUT.TO',
    # --- US SWINGS ---
    'PLTR', 'SOFI', 'SHOP', 'CCL', 'AMD', 'TSLA', 'HOOD', 'NVDA', 'AAPL', 'MSFT', 'NFLX', 'ORCL', 'MARA'
]

WEBHOOK_URL = os.getenv('DISCORD_URL')

# --- HELPER: GET MARKET TIME ---
def get_market_minutes_elapsed():
    """Returns minutes elapsed since 9:30 AM EST today. Returns 390 if market is closed."""
    tz = pytz.timezone('US/Eastern')
    now = datetime.now(tz)
    market_open = now.replace(hour=9, minute=30, second=0, microsecond=0)
    
    if now < market_open:
        return 0
    
    diff = (now - market_open).total_seconds() / 60
    return min(diff, 390)

# --- HELPER: RELATIVE VOLUME (RVAT) ---
def get_relative_volume(ticker):
    """
    Calculates Relative Volume at Time (RVAT).
    Compares the LAST COMPLETED 5-min candle volume to the average volume 
    of that same 5-min time slot over the last 5 days.
    """
    try:
        # Fetch 5 days of 5-minute data
        df = yf.download(ticker, period="5d", interval="5m", progress=False)
        
        if df.empty or len(df) < 10:
            return 1.0 

        # ROBUST MULTIINDEX HANDLING
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        # Ensure Volume is numeric and drop NaNs
        df['Volume'] = pd.to_numeric(df['Volume'], errors='coerce')
        df.dropna(subset=['Volume'], inplace=True)

        # HISTORICAL TIME ALIGNMENT
        df['time_slot'] = df.index.time
        df['date'] = df.index.date  # Create date column for filtering

        # --- THE FIX: USE LAST COMPLETED CANDLE ---
        # We use iloc[-2] because iloc[-1] is the current "forming" candle (incomplete volume).
        last_completed_bar = df.iloc[-2]
        check_time = last_completed_bar.name.time()
        check_date = last_completed_bar.name.date() # Capture the date of the candle we are checking
        check_vol = float(last_completed_bar['Volume'])

        # 1. Filter for historical bars at this exact time (e.g., all 10:00 AM bars)
        historical_at_time = df[df['time_slot'] == check_time]

        # 2. THE LOGIC FIX: Filter by DATE, not blind slicing
        # We keep all rows where the date is NOT the check_date.
        # This preserves Yesterday's data while removing the current bar.
        history_only = historical_at_time[historical_at_time['date'] != check_date]

        if history_only.empty:
            return 1.0

        avg_vol = history_only['Volume'].mean()

        if avg_vol == 0 or pd.isna(avg_vol):
            return 1.0

        return check_vol / avg_vol

    except Exception as e:
        print(f"⚠️ Volume calc failed for {ticker}: {e}")
        return 1.0

# --- 1. ENHANCED SCORING ENGINE ---
def calculate_confidence(rsi, price, open_price, day_high, day_low, bbl, ema_50, macd_h, prev_macd_h, rel_vol, elapsed_minutes):
    score = 0
    reasons = []

    # A. RSI (Value) - Gradient Scoring
    if rsi < 35:
        score += 4
        reasons.append("💎 **Value:** Deeply Oversold (RSI < 35)")
    elif rsi < 45: 
        score += 3
        reasons.append("📉 **Value:** Oversold (RSI < 45)")
    elif rsi < 55:
        score += 2
        reasons.append("🌊 **Trend:** Momentum Reset (RSI < 55)")

    # B. SUPPORT LEVELS (Confluence Check)
    # 1. Bollinger Band (Deep Support)
    if price <= bbl * 1.01: 
        score += 3
        reasons.append("🛡️ **Support:** Touching Lower Bollinger Band")
    
    # 2. 50 EMA (Trend Support)
    if abs(price - ema_50) <= (ema_50 * 0.02):
        score += 2
        reasons.append("📈 **Support:** Riding 50-Day Trendline")

    # C. MACD (Momentum)
    if macd_h > 0:
        score += 2
        reasons.append("🚀 **Momentum:** Positive (Green Histogram)")
    elif macd_h > prev_macd_h: 
        score += 1
        reasons.append("🔄 **Momentum:** Improving (Selling Slowing Down)")

    # D. VOLUME: HYBRID DIRECTION CHECK
    # 1. Determine "Bullish" status based on time of day
    if elapsed_minutes < 30:
        # MORNING RULE: Just check if candle is Green (Price > Open)
        is_bullish = price > open_price
        method = "Green Candle"
    else:
        # AFTERNOON RULE: Check if price is in upper half of daily range
        midpoint = (day_high + day_low) / 2
        is_bullish = price >= midpoint
        method = "Upper Range"

    # 2. Score the Volume based on Direction
    if rel_vol > 1.2:
        if is_bullish:
            score += 1
            if rel_vol > 2.0: score += 1 # Bonus for massive surge
            reasons.append(f"🟢 **Vol:** Buying Pressure ({rel_vol:.1f}x via {method})")
        else:
            reasons.append(f"🔴 **Vol:** Selling Pressure ({rel_vol:.1f}x via {method})")

    return score, reasons

# --- 2. ALERT FUNCTION ---
def send_discord_alert(ticker, price, rsi, ema_50, stop_loss, take_profit, score, reasons, threshold, rel_vol):
    if score >= 8:
        color = 5763719  # Green (Strong)
        rating = "🔥 STRONG BUY"
    elif score >= 5:
        color = 16776960 # Yellow (Moderate)
        rating = "⚠️ MODERATE WATCH"
    else:
        return 

    reasons_text = "\n".join(reasons)
    
    data = {
        "content": f"🚨 **SWING ALERT: {ticker}**",
        "embeds": [
            {
                "title": f"{ticker}: {rating} (Score: {score}/10)",
                "description": f"**Analysis:**\n{reasons_text}",
                "color": color,
                "fields": [
                    {"name": "Status", "value": f"Passed Threshold ({threshold}+)", "inline": False},
                    {"name": "Entry Price", "value": f"**${price:.2f}**", "inline": True},
                    {"name": "RSI", "value": f"{rsi:.1f}", "inline": True},
                    {"name": "50 EMA", "value": f"${ema_50:.2f}", "inline": True},
                    {"name": "Vol Strength", "value": f"{rel_vol:.1f}x", "inline": True},
                    {"name": "🛑 Stop Loss", "value": f"${stop_loss:.2f}", "inline": True},
                    {"name": "🎯 Take Profit", "value": f"${take_profit:.2f}", "inline": True},
                    {"name": "Links", "value": f"[Yahoo](https://finance.yahoo.com/quote/{ticker}) | [TradingView](https://www.tradingview.com/symbols/{ticker})", "inline": False}
                ],
                "footer": {"text": "Bot running via GitHub Actions"}
            }
        ]
    }
    try:
        requests.post(WEBHOOK_URL, json=data)
    except Exception as e:
        print(f"Failed to send Discord alert: {e}")

# --- 3. MAIN LOOP ---
def check_market():
    print(f"Checking {len(TICKERS)} tickers via Bulk Download...")
    elapsed_minutes = get_market_minutes_elapsed()
    print(f"🕒 Market Minutes Elapsed: {elapsed_minutes:.0f}/390")
    
    # --- 1. BULK DOWNLOAD ---
    try:
        bulk_data = yf.download(TICKERS, period="6mo", interval="1d", group_by='ticker', progress=False)
    except Exception as e:
        print(f"Critical Error: Bulk download failed - {e}")
        return

    for ticker in TICKERS:
        try:
            # --- 2. EXTRACT DATA ---
            try:
                df = bulk_data[ticker].copy()
            except KeyError:
                print(f"⚠️ No data found for {ticker}")
                continue

            if df['Close'].isnull().all():
                continue
            
            df.dropna(subset=['Close'], inplace=True)

            # --- 3. CALCULATE INDICATORS ---
            df['EMA_50'] = ta.ema(df['Close'], length=50)
            df['RSI'] = ta.rsi(df['Close'], length=14)
            
            macd = ta.macd(df['Close'])
            if macd is not None:
                df['MACD_H'] = macd.iloc[:, 1]
            else:
                continue

            bb = ta.bbands(df['Close'], length=20, std=2)
            if bb is not None and not bb.empty:
                df['BBL'] = bb.iloc[:, 0]
            else:
                df['BBL'] = pd.NA
            
            df['ATR'] = ta.atr(df['High'], df['Low'], df['Close'], length=14)

            # --- 4. GET LATEST VALUES ---
            if len(df) < 50: continue 
            
            last = df.iloc[-1]
            
            # --- 🛡️ GHOST CANDLE FIX 🛡️ ---
            tz = pytz.timezone('US/Eastern')
            today_date = datetime.now(tz).date()
            candle_date = last.name.date()
            
            if elapsed_minutes > 20 and candle_date != today_date:
                continue

            prev = df.iloc[-2]

            if pd.isna(last['BBL']) or pd.isna(last['EMA_50']): continue

            price = float(last['Close'])
            open_price = float(last['Open']) 
            day_high = float(last['High'])
            day_low = float(last['Low'])

            rsi = float(last['RSI'])
            ema_50 = float(last['EMA_50'])
            bbl = float(last['BBL'])
            
            macd_h = float(last['MACD_H'])
            prev_macd_h = float(prev['MACD_H'])
            atr = float(last['ATR'])

            # --- 5. TRIGGER LOGIC (PRE-FILTER) ---
            near_ema = abs(price - ema_50) <= (ema_50 * 0.02)
            near_bb = abs(price - bbl) <= (bbl * 0.015)
            
            if (near_ema or near_bb) and rsi < 55:
                
                # --- 6. DEEP DIVE: RELATIVE VOLUME ---
                rel_vol = get_relative_volume(ticker)

                stop_loss = price - (atr * 1.5)
                take_profit = price + (atr * 2.0) 
                
                # Calculate Score
                score, reasons = calculate_confidence(rsi, price, open_price, day_high, day_low, bbl, ema_50, macd_h, prev_macd_h, rel_vol, elapsed_minutes)
                
                # --- TIME & FRIDAY THRESHOLD LOGIC ---
                min_score_needed = 5 
                
                if elapsed_minutes < 60:
                    min_score_needed = 7 
                
                tz = pytz.timezone('US/Eastern')
                is_friday = datetime.now(tz).weekday() == 4
                if is_friday and elapsed_minutes > 270:
                    min_score_needed += 1

                print(f"🔎 Checking {ticker}: Score {score}/10 (RVAT: {rel_vol:.2f}x)")
                
                if score >= min_score_needed:
                    send_discord_alert(ticker, price, rsi, ema_50, stop_loss, take_profit, score, reasons, min_score_needed, rel_vol)

        except Exception as e:
            print(f"Error processing {ticker}: {e}")

if __name__ == "__main__":
    check_market()
