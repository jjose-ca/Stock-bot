import yfinance as yf
import pandas_ta as ta
import requests
import os
import pandas as pd
import numpy as np

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

# --- 1. ENHANCED SCORING ENGINE ---
def calculate_confidence(rsi, price, bbl, macd_h, volume, vol_avg):
    score = 0
    reasons = []

    # A. RSI (Value) - Max 4 pts
    if rsi < 30:
        score += 4
        reasons.append("💎 **Value:** Deeply Oversold (RSI < 30)")
    elif rsi < 45:
        score += 2
        reasons.append("📉 **Value:** Oversold (RSI < 45)")

    # B. Bollinger Bands (Support) - Max 3 pts
    # If price is within 1% of Lower Band
    if price <= bbl * 1.01: 
        score += 3
        reasons.append("🛡️ **Support:** Touching Lower Bollinger Band")
    
    # C. MACD (Momentum) - Max 2 pts
    # If Histogram is positive or turning up
    if macd_h > 0:
        score += 2
        reasons.append("🚀 **Momentum:** MACD Histogram Positive")
    elif macd_h > -0.1: # Almost turning positive
        score += 1
        reasons.append("🔄 **Momentum:** MACD Turning Up")

    # D. Volume (Conviction) - Max 1 pt
    if volume > vol_avg:
        score += 1
        reasons.append("📊 **Volume:** High Buying Interest")

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
        return # Don't spam discord with weak signals

    reasons_text = "\n".join(reasons)
    
    # Calculate Risk/Reward Ratio
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
    
    for ticker in TICKERS:
        try:
            # Download Data
            df = yf.download(ticker, period="6mo", interval="1d", progress=False)
            
            if df.empty: continue

            # Fix Multi-Index Columns (YFinance Update Fix)
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)

            # --- CALCULATE INDICATORS ---
            # 1. EMAs
            df['EMA_50'] = ta.ema(df['Close'], length=50)
            
            # 2. RSI
            df['RSI'] = ta.rsi(df['Close'], length=14)
            
            # 3. MACD (12, 26, 9)
            macd = ta.macd(df['Close'])
            df['MACD_H'] = macd['MACDh_12_26_9'] # Histogram
            
            # 4. Bollinger Bands (20, 2)
            bb = ta.bbands(df['Close'], length=20, std=2)
            df['BBL'] = bb['BBL_20_2.0'] # Lower Band
            
            # 5. Volume SMA (20)
            df['VOL_AVG'] = ta.sma(df['Volume'], length=20)
            
            # 6. ATR (For Stop Loss)
            df['ATR'] = ta.atr(df['High'], df['Low'], df['Close'], length=14)

            # Get Latest Data Point
            last = df.iloc[-1]
            price = float(last['Close'])
            rsi = float(last['RSI'])
            ema_50 = float(last['EMA_50'])
            bbl = float(last['BBL'])
            macd_h = float(last['MACD_H'])
            volume = float(last['Volume'])
            vol_avg = float(last['VOL_AVG'])
            atr = float(last['ATR'])

            # --- TRIGGER LOGIC ---
            # We want EITHER:
            # A) Price is near 50 EMA (Trend Pullback)
            # B) Price is near Lower Bollinger Band (Mean Reversion)
            
            near_ema = abs(price - ema_50) <= (ema_50 * 0.02)
            near_bb = abs(price - bbl) <= (bbl * 0.015)
            
            if (near_ema or near_bb) and rsi < 60:
                
                # Calculate Suggested Trade Parameters
                stop_loss = price - (atr * 1.5) # Standard 1.5x ATR Stop
                take_profit = price + (atr * 3.0) # 2:1 Reward Target
                
                # Get Score
                score, reasons = calculate_confidence(rsi, price, bbl, macd_h, volume, vol_avg)
                
                print(f"Checking {ticker}: Score {score}/10")
                send_discord_alert(ticker, price, rsi, stop_loss, take_profit, score, reasons)

        except Exception as e:
            print(f"Error checking {ticker}: {e}")

if __name__ == "__main__":
    check_market()
