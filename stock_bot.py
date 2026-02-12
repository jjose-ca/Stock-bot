import yfinance as yf
import pandas_ta as ta
import requests
import os
import pandas as pd
import numpy as np
from datetime import datetime, time
import pytz

# --- CONFIGURATION ---
TICKERS = [
    # --- SAFE FOUNDATION ---
    'VFV.TO', 'ZSP.TO', 'XEF.TO',   # Canada S&P 500
    'VTI',  # US S&P 500
    
    # --- SECTOR ETFS ---
    'SOXQ', 'XLY',        # Semis & Consumer
   
    # --- CANADIAN GROWTH ---
    'HUT.TO',             # Crypto Miner
    
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
    
    # If before 9:30 AM, return 0
    if now < market_open:
        return 0
    
    # Calculate difference in minutes
    diff = (now - market_open).total_seconds() / 60
    
    # Cap at 390 minutes (Full trading day: 6.5 hours)
    return min(diff, 390)

# --- 1. ENHANCED SCORING ENGINE ---
def calculate_confidence(rsi, price, open_price, bbl, macd_h, prev_macd_h, proj_volume, vol_avg):
    score = 0
    reasons = []

    # A. RSI (Value) - UPDATED TO < 35
    if rsi < 35:
        score += 4
        reasons.append("💎 **Value:** Deeply Oversold (RSI < 35)")
    elif rsi < 50: 
        score += 2
        reasons.append("📉 **Value:** Oversold (RSI < 50)")

    # B. Bollinger Bands (Support)
    if price <= bbl * 1.01: 
        score += 3
        reasons.append("🛡️ **Support:** Touching Lower Bollinger Band")
    
    # C. MACD (Momentum)
    if macd_h > 0:
        score += 2
        reasons.append("🚀 **Momentum:** Positive (Green Histogram)")
    elif macd_h > prev_macd_h: 
        score += 1
        reasons.append("🔄 **Momentum:** Improving (Selling Slowing Down)")

    # D. VOLUME FIX: Projection + Candle Color Check
    # Only reward high volume if the candle is GREEN (Price > Open)
    is_green_candle = price >= open_price
    
    if proj_volume > vol_avg and is_green_candle:
        score += 1
        reasons.append("📊 **Volume:** High Buying Interest (Green Candle)")
    elif proj_volume > vol_avg and not is_green_candle:
        # Penalize or ignore high volume on red days (Panic Selling)
        pass 

    return score, reasons

# --- 2. ALERT FUNCTION ---
def send_discord_alert(ticker, price, rsi, stop_loss, take_profit, score, reasons):
    if score >= 8:
        color = 5763719  # Green (Strong)
        rating = "🔥 STRONG BUY"
    elif score >= 5:
        color = 16776960 # Yellow (Moderate)
        rating = "⚠️ MODERATE WATCH"
    else:
        return 

    reasons_text = "\n".join(reasons)
    
    risk = price - stop_loss
    reward = take_profit - price
    rr_ratio = reward / risk if risk > 0 else 0

    data = {
        "content": f"🚨 **SWING ALERT: {ticker}**",
        "embeds": [
            {
                "title": f"{ticker}: {rating} (Score: {score}/10)",
                "description": f"**Analysis:**\n{reasons_text}",
                "color": color,
                "fields": [
                    {"name": "Entry Price", "value": f"**${price:.2f}**", "inline": True},
                    {"name": "RSI", "value": f"{rsi:.1f}", "inline": True},
                    {"name": "RR Ratio", "value": f"1:{rr_ratio:.1f}", "inline": True},
                    
                    {"name": "🛑 Stop Loss", "value": f"${stop_loss:.2f}", "inline": True},
                    {"name": "🎯 Take Profit", "value": f"${take_profit:.2f}", "inline": True},
                    {"name": "Vol Status", "value": "Normal" if "Volume" not in reasons_text else "High", "inline": True},
                    
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
    print(f"Checking {len(TICKERS)} tickers...")
    elapsed_minutes = get_market_minutes_elapsed()
    print(f"🕒 Market Minutes Elapsed: {elapsed_minutes:.0f}/390")
    
    for ticker in TICKERS:
        try:
            df = yf.download(ticker, period="6mo", interval="1d", progress=False)
            
            if df.empty: continue

            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)

            # --- CALCULATE INDICATORS ---
            df['EMA_50'] = ta.ema(df['Close'], length=50)
            df['RSI'] = ta.rsi(df['Close'], length=14)
            
            # MACD
            macd = ta.macd(df['Close'])
            df['MACD_H'] = macd.iloc[:, 1] 
            
            # Bollinger Bands
            bb = ta.bbands(df['Close'], length=20, std=2)
            if bb is not None and not bb.empty:
                df['BBL'] = bb.iloc[:, 0] 
            else:
                df['BBL'] = pd.NA
            
            df['VOL_AVG'] = ta.sma(df['Volume'], length=20)
            df['ATR'] = ta.atr(df['High'], df['Low'], df['Close'], length=14)

            # --- GET LATEST VALUES ---
            if len(df) < 2: continue
            
            last = df.iloc[-1]
            prev = df.iloc[-2]

            if pd.isna(last['BBL']) or pd.isna(last['EMA_50']): continue

            price = float(last['Close'])
            open_price = float(last['Open']) # Needed for candle color check
            rsi = float(last['RSI'])
            ema_50 = float(last['EMA_50'])
            bbl = float(last['BBL'])
            
            macd_h = float(last['MACD_H'])
            prev_macd_h = float(prev['MACD_H'])
            
            volume = float(last['Volume'])
            vol_avg = float(last['VOL_AVG'])
            atr = float(last['ATR'])

            # --- VOLUME PROJECTION LOGIC ---
            if elapsed_minutes > 15 and elapsed_minutes < 390:
                # If market is open, project what full day volume would look like
                proj_volume = (volume / elapsed_minutes) * 390
            else:
                # If market closed or just started, use raw volume
                proj_volume = volume

            # --- TRIGGER LOGIC ---
            near_ema = abs(price - ema_50) <= (ema_50 * 0.02)
            near_bb = abs(price - bbl) <= (bbl * 0.015)
            
            if (near_ema or near_bb) and rsi < 55:
                
                stop_loss = price - (atr * 1.5)
                take_profit = price + (atr * 3.0) 
                
                # Pass proj_volume and open_price to scoring
                score, reasons = calculate_confidence(rsi, price, open_price, bbl, macd_h, prev_macd_h, proj_volume, vol_avg)
                
                print(f"Checking {ticker}: Score {score}/10")
                send_discord_alert(ticker, price, rsi, stop_loss, take_profit, score, reasons)

        except Exception as e:
            print(f"Error checking {ticker}: {e}")

if __name__ == "__main__":
    check_market()
