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
    Compares the current 5-min volume to the average volume of the 
    same 5-min time slot over the last 5 days.
    """
    try:
        # Fetch 5 days of 5-minute data
        df = yf.download(ticker, period="5d", interval="5m", progress=False)
        
        if df.empty or len(df) < 10:
            return 1.0 

        # ROBUST MULTIINDEX HANDLING
        # Fixes issues where yfinance returns (Price, Ticker) tuples as columns
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        # Ensure Volume is numeric and drop NaNs (Crucial for calculations)
        df['Volume'] = pd.to_numeric(df['Volume'], errors='coerce')
        df.dropna(subset=['Volume'], inplace=True)

        # HISTORICAL TIME ALIGNMENT
        # We extract just the time component (e.g., 14:30:00) to align days
        df['time_slot'] = df.index.time

        # Get the current (latest) bar details
        current_bar = df.iloc[-1]
        current_time = current_bar.name.time()
        current_vol = float(current_bar['Volume'])

        # Filter for all historical bars that share this exact time slot
        # We exclude the very last row (current bar) to ensure we compare against history
        historical_at_time = df[df['time_slot'] == current_time].iloc[:-1]

        if historical_at_time.empty:
            return 1.0

        avg_vol = historical_at_time['Volume'].mean()

        if avg_vol == 0 or pd.isna(avg_vol):
            return 1.0

        return current_vol / avg_vol

    except Exception as e:
        print(f"⚠️ Volume calc failed for {ticker}: {e}")
        return 1.0

# --- 1. ENHANCED SCORING ENGINE ---
def calculate_confidence(rsi, price, open_price, bbl, ema_50, macd_h, prev_macd_h, rel_vol):
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

    # D. VOLUME: Relative Volume Check
    is_green_candle = price >= open_price
    
    # If volume is 1.5x (150%) of normal for this time of day
    if rel_vol > 1.5 and is_green_candle:
        score += 2
        reasons.append(f"📊 **Volume:** Surge ({rel_vol:.1f}x normal for this time)")
    elif rel_vol > 1.2 and is_green_candle:
        score += 1
        reasons.append(f"📊 **Volume:** Above Average ({rel_vol:.1f}x)")

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
    # Downloads 6mo of data for ALL tickers in ONE request.
    # group_by='ticker' makes it easy to access data via data['AAPL']
    try:
        bulk_data = yf.download(TICKERS, period="6mo", interval="1d", group_by='ticker', progress=False)
    except Exception as e:
        print(f"Critical Error: Bulk download failed - {e}")
        return

    for ticker in TICKERS:
        try:
            # --- 2. EXTRACT DATA ---
            # We copy the dataframe to avoid SettingWithCopy warnings
            # If only 1 ticker is in list, yf structure is different, but for list > 1 it works like this:
            try:
                df = bulk_data[ticker].copy()
            except KeyError:
                # Handle case where ticker failed to download
                print(f"⚠️ No data found for {ticker}")
                continue

            # Check for empty data (NaNs) which happens if a ticker is delisted or errored
            if df['Close'].isnull().all():
                continue
            
            # Drop NaN rows (e.g., holidays where some markets were open and others closed)
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
            if len(df) < 50: continue # Ensure enough data for EMA
            
            last = df.iloc[-1]
            prev = df.iloc[-2]

            if pd.isna(last['BBL']) or pd.isna(last['EMA_50']): continue

            price = float(last['Close'])
            open_price = float(last['Open']) 
            rsi = float(last['RSI'])
            ema_50 = float(last['EMA_50'])
            bbl = float(last['BBL'])
            
            macd_h = float(last['MACD_H'])
            prev_macd_h = float(prev['MACD_H'])
            atr = float(last['ATR'])

            # --- 5. TRIGGER LOGIC (PRE-FILTER) ---
            # We check Price Structure FIRST to avoid wasting API calls on Volume checks
            near_ema = abs(price - ema_50) <= (ema_50 * 0.02)
            near_bb = abs(price - bbl) <= (bbl * 0.015)
            
            if (near_ema or near_bb) and rsi < 55:
                
                # --- 6. DEEP DIVE: RELATIVE VOLUME ---
                # Only now do we make a specific API call for this ticker
                rel_vol = get_relative_volume(ticker)

                stop_loss = price - (atr * 1.5)
                take_profit = price + (atr * 2.0) 
                
                # Calculate Score
                score, reasons = calculate_confidence(rsi, price, open_price, bbl, ema_50, macd_h, prev_macd_h, rel_vol)
                
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
