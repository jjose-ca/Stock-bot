import yfinance as yf
import pandas_ta as ta
import requests
import os
import pandas as pd

# --- CONFIGURATION ---
TICKERS = [
    # --- SAFE FOUNDATION ---
    'VFV.TO', 'ZSP.TO',   # Canada S&P 500
    'SPY', 'IVV', 'VTI',  # US S&P 500
    
    # --- SECTOR ETFS ---
    'SOXQ', 'XLY',        # Semis & Consumer
    'TLT',                # Bonds (Hedge)

    # --- CANADIAN GROWTH ---
    'NVDA.NE', 'TSLA.NE', # Nvidia & Tesla (CAD Hedged)
    'HUT.TO',             # Crypto Miner
    
    # --- US SWINGS ---
    'PLTR', 'SOFI', 'CCL', 'AMD', 'AAPL', 'MSFT', 'NFLX'
]

WEBHOOK_URL = os.getenv('DISCORD_URL')

# --- 1. CONFIDENCE SCORING (RSI ONLY) ---
def calculate_confidence(rsi):
    score = 0
    reasons = []

    if rsi < 35:
        score = 10
        reasons.append("💎 **Value:** Deeply Oversold (RSI < 35)")
    elif rsi < 45:
        score = 8
        reasons.append("📉 **Value:** Oversold (RSI < 45)")
    elif rsi < 55:
        score = 6
        reasons.append("🌊 **Value:** Momentum Dip (RSI < 55)")
    elif rsi >= 55:
        score = 4
        reasons.append("⚠️ **Value:** Neutral/High (RSI > 55)")

    return score, reasons

# --- 2. ALERT FUNCTION ---
def send_discord_alert(ticker, price, ema_50, rsi, score, reasons):
    if score >= 8:
        color = 5814783  # Green
        rating = "🔥 STRONG BUY"
    elif score >= 6:
        color = 16776960 # Yellow
        rating = "⚠️ MODERATE BUY"
    else:
        color = 15158332 # Red
        rating = "🚫 WEAK SETUP"

    news_link = f"https://finance.yahoo.com/quote/{ticker}/news"
    reasons_text = "\n".join(reasons)
    
    data = {
        "content": f"🚨 **SWING ALERT: {ticker}**",
        "embeds": [
            {
                "title": f"{ticker}: {rating} (Score: {score}/10)",
                "description": f"**Analysis:**\n{reasons_text}",
                "color": color,
                "fields": [
                    {"name": "Current Price", "value": f"**${price:.2f}**", "inline": True},
                    {"name": "50 EMA Target", "value": f"${ema_50:.2f}", "inline": True},
                    {"name": "RSI Level", "value": f"**{rsi:.1f}**", "inline": True},
                    {"name": "Action", "value": f"👉 [**Check News**]({news_link})", "inline": False}
                ],
                "footer": {"text": "Bot running via GitHub Actions"}
            }
        ]
    }
    requests.post(WEBHOOK_URL, json=data)

# --- 3. MAIN LOOP ---
def check_market():
    print(f"Checking {len(TICKERS)} tickers...")
    
    for ticker in TICKERS:
        try:
            # Download data
            df = yf.download(ticker, period="1y", interval="1d", progress=False)
            
            if df.empty:
                print(f"Skipping {ticker}: No data found.")
                continue

            # --- THE FIX: FLATTEN MULTI-LEVEL COLUMNS ---
            # Yahoo sometimes returns columns like ('Close', 'VFV.TO')
            # This forces them to just be 'Close'
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)

            # Calculate Indicators
            df['EMA_50'] = ta.ema(df['Close'], length=50)
            df['RSI'] = ta.rsi(df['Close'], length=14)
            
            # Use .iloc[-1] to grab the last row, then force it to be a single float
            try:
                # We access the column, take the last value, and convert to float
                # This kills the "Series" error
                price = float(df['Close'].iloc[-1])
                ema_50 = float(df['EMA_50'].iloc[-1])
                rsi = float(df['RSI'].iloc[-1])
            except (IndexError, ValueError):
                print(f"Skipping {ticker}: Data error.")
                continue

            # --- TRIGGER LOGIC ---
            
            # 1. Price is within 2% of the 50 EMA
            diff = abs(price - ema_50)
            threshold = ema_50 * 0.02
            is_near_support = diff <= threshold
            
            # 2. RSI is healthy (Below 60)
            is_good_rsi = rsi < 60

            if is_near_support and is_good_rsi:
                score, reasons = calculate_confidence(rsi)
                print(f"!!! TRIGGER: {ticker} | Price: {price:.2f} | RSI: {rsi:.1f}")
                send_discord_alert(ticker, price, ema_50, rsi, score, reasons)
            else:
                print(f"{ticker}: ${price:.2f} | RSI: {rsi:.1f} (No setup)")

        except Exception as e:
            print(f"Error checking {ticker}: {e}")

if __name__ == "__main__":
    check_market()
