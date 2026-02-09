import yfinance as yf
import pandas_ta as ta
import requests
import os
import pandas as pd

# --- CONFIGURATION ---
TICKERS = [
    # --- The Safe Foundation ---
    'VFV.TO',   # Vanguard S&P 500 (Canadian)

    # --- Sector ETFs ---
    'SOXQ',     # Semiconductors (Nvidia/AMD)
    'XLY',      # Consumer Discretionary (Amazon/Tesla)

    # --- High Volatility / CAD Hedged ---
    'NVDA.NE',  # Nvidia (CAD Hedged)
    'TSLA.NE',  # Tesla (CAD Hedged)
    'HUT.TO',   # Hut 8 Mining (Crypto)

    # --- US Aggressive Swings ---
    'PLTR',     # Palantir (AI)
    'SOFI',     # SoFi (Fintech)
    'CCL'       # Carnival Cruise (Recovery)
]

BENCHMARK_TICKER = "SPY"

# WEBHOOK: Get this from your GitHub Secrets
WEBHOOK_URL = os.getenv('DISCORD_URL')

def get_data(ticker):
    """Downloads data and calculates 50 EMA, 200 EMA, and RSI"""
    try:
        # Download data
        # Note: We need more history (1y) to calculate the 200 EMA correctly
        df = yf.download(ticker, period="1y", interval="1d", progress=False)
        
        if df.empty:
            return None, None, None, None

        # --- THE FIX: FLATTEN WEIRD COLUMNS ---
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
            
        close_data = df['Close']
        if isinstance(close_data, pd.DataFrame):
            close_data = close_data.iloc[:, 0]

        # --- CALCULATE INDICATORS ---
        df['EMA_50'] = ta.ema(close_data, length=50)
        df['EMA_200'] = ta.ema(close_data, length=200) # <--- NEW: 200 EMA
        df['RSI'] = ta.rsi(close_data, length=14)

        # --- SAFE EXTRACT ---
        def get_scalar(series):
            val = series.iloc[-1]
            if isinstance(val, pd.Series):
                val = val.iloc[0]
            return float(val)

        current_price = get_scalar(close_data)
        current_ema = get_scalar(df['EMA_50'])
        current_ema200 = get_scalar(df['EMA_200']) # <--- NEW
        current_rsi = get_scalar(df['RSI'])
        
        return current_price, current_ema, current_ema200, current_rsi

    except Exception as e:
        print(f"Error fetching {ticker}: {e}")
        return None, None, None, None

def send_discord_alert(ticker, price, ema, ema200, rsi, note, color):
    if not WEBHOOK_URL:
        print("Error: No Webhook URL found.")
        return

    currency = "CAD" if ".TO" in ticker or ".NE" in ticker else "USD"
    news_link = f"https://finance.yahoo.com/quote/{ticker}/news"
    
    data = {
        "content": f"🚨 **SWING ALERT: {ticker}**",
        "embeds": [
            {
                "title": f"Action Signal: {ticker}",
                "description": f"**Status:** {note}\n{ticker} has hit a key support level.",
                "color": color, 
                "fields": [
                    {"name": "Current Price", "value": f"${price:.2f} {currency}", "inline": True},
                    {"name": "50 EMA (Trend)", "value": f"${ema:.2f}", "inline": True},
                    {"name": "200 EMA (Floor)", "value": f"${ema200:.2f}", "inline": True},
                    {"name": "RSI Strength", "value": f"{rsi:.1f}", "inline": True},
                    {"name": "Action", "value": f"[Check News on Yahoo]({news_link})"}
                ],
                "footer": {"text": "Bot running via GitHub Actions"}
            }
        ]
    }
    
    try:
        requests.post(WEBHOOK_URL, json=data)
        print(f"--> Alert sent for {ticker}!")
    except Exception as e:
        print(f"Failed to send alert: {e}")

def calculate_confidence(price, ema_50, ema_200, rsi):
    score = 0
    reasons = []

    # Criterion 1: The Trend (Is it in a long-term bull market?)
    if price > ema_200:
        score += 3
        reasons.append("✅ Above 200 EMA (Bull Trend)")
    else:
        reasons.append("⚠️ Below 200 EMA (Bear Trend)")

    # Criterion 2: The Setup Quality (How close to the line?)
    # If it's literally touching the line (within 0.5%), that's better than being 2% away
    distance = abs(price - ema_50) / price
    if distance < 0.005: # Less than 0.5% away
        score += 3
        reasons.append("🎯 Perfect Touch (<0.5% dist)")
    elif distance < 0.015:
        score += 1
        reasons.append("OK Proximity (~1.5% dist)")

    # Criterion 3: The RSI (How cheap is it?)
    if rsi < 35:
        score += 4
        reasons.append("💎 Deeply Oversold (RSI < 35)")
    elif rsi < 45:
        score += 2
        reasons.append("📉 Oversold (RSI < 45)")
    
    return score, reasons

def check_market():
    print("--- 🚀 STARTING BOT RUN 🚀 ---")
    
    # 1. CHECK BENCHMARK (SPY)
    spy_price, spy_ema, spy_ema200, spy_rsi = get_data(BENCHMARK_TICKER)
    spy_is_down = False
    
    if spy_price is not None:
        if spy_price < spy_ema:
            spy_is_down = True
            print(f"BENCHMARK: SPY is below 50 EMA (${spy_price:.2f}). Market is weak.")
        else:
            print(f"BENCHMARK: SPY is healthy (${spy_price:.2f}).")
    else:
        print("Warning: Could not fetch SPY data.")

    print("-" * 30)

    # 2. CHECK YOUR STOCKS
    for ticker in TICKERS:
        price, ema, ema200, rsi = get_data(ticker)

        if price is None:
            continue

        rsi_limit = 55
        
        # --- NEW LOGIC START ---
        
        # Scenario 1: Standard Swing (Price bouncing off 50 EMA)
        # Logic: Price is close to 50 EMA (+/- 2%) AND Price is ABOVE 50 EMA
        if abs(price - ema) <= (ema * 0.02) and price > ema and rsi < rsi_limit:
            print(f"!!! MATCH: {ticker} at 50 EMA !!!")
            alert_note = "✅ **Standard Buy:** Bouncing off 50 EMA support."
            alert_color = 5814783  # Green
            send_discord_alert(ticker, price, ema, ema200, rsi, alert_note, alert_color)

        # Scenario 2: Deep Value (Price crashed to 200 EMA)
        # Logic: Price is close to 200 EMA (+/- 2%)
        elif abs(price - ema200) <= (ema200 * 0.02) and rsi < 40: # Stricter RSI for deep dips
            print(f"!!! MATCH: {ticker} at 200 EMA !!!")
            alert_note = "💰 **DEEP VALUE:** Stock crashed to 200 EMA support!"
            alert_color = 16776960 # Gold
            send_discord_alert(ticker, price, ema, ema200, rsi, alert_note, alert_color)
            
        # Scenario 3: Broken Trend (No Alert)
        else:
            print(f"Checking {ticker}: ${price:.2f} | 50 EMA: ${ema:.2f} | 200 EMA: ${ema200:.2f}")
            if price < ema and price > ema200:
                print(f"   -> Status: In 'No Man's Land' (Falling Knife). Waiting.")
            
    print("--- ✅ RUN COMPLETE ---")

if __name__ == "__main__":
    check_market()
