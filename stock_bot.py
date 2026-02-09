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

    # Since we removed Trend (200 EMA), we rely purely on Value (RSI)
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
            # We only need 1y of data now (since 200 EMA is gone)
            df = yf.download(ticker, period="1y", interval="1d", progress=False)
            
            if df.empty:
                print(f"Skipping {ticker}: No data found.")
                continue

            # Calculate Indicators
            df['EMA_50'] = ta.ema(df['Close'], length=50)
            df['RSI'] = ta.rsi(df['Close'], length=14)
            
            # Get latest row
            latest = df.iloc[-1]
            
            # Check for valid data
            if pd.isna(latest['EMA_50']) or pd.isna(latest['RSI']):
                print(f"Skipping {ticker}: Not enough data.")
                continue

            price = float(latest['Close'])
            ema_50 = float(latest['EMA_50'])
            rsi = float(latest['RSI'])

            # --- TRIGGER LOGIC ---
            # 1. Price is within 2% of the 50 EMA
            is_near_support = abs(price - ema_50) <= (ema_50 * 0.02)
            
            # 2. RSI is healthy (Below 60)
            is_good_rsi = rsi < 60

            if is_near_support and is_good_rsi:
                score, reasons = calculate_confidence(rsi)
                print(f"TRIGGER: {ticker} | Price: {price:.2f} | RSI: {rsi:.1f}")
                send_discord_alert(ticker, price, ema_50, rsi, score, reasons)
            else:
                print(f"{ticker}: ${price:.2f} | RSI: {rsi:.1f} (No setup)")

        except Exception as e:
            print(f"Error checking {ticker}: {e}")

if __name__ == "__main__":
    check_market()
