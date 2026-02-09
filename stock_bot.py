import yfinance as yf
import pandas_ta as ta
import requests
import os
import pandas as pd  # <--- Added this to handle the data format

# --- CONFIGURATION ---
TICKERS = [
    'VFV.TO',   # Your Safe Base
    'NVDA.NE',  # Nvidia (CAD Hedged) - High Volatility
    'TSLA.NE',  # Tesla (CAD Hedged) - High Volatility
    'PLTR',     # Palantir (US) - aggressive AI swing
    'SOFI',     # SoFi (US) - cheap fintech
    'CCL',      # Carnival Cruise (US) - recovery play
    'HUT.TO'    # Hut 8 Mining (Canadian Crypto miner) - fast swings
]
BENCHMARK_TICKER = "SPY"

# WEBHOOK: Get this from your GitHub Secrets
WEBHOOK_URL = os.getenv('DISCORD_URL')

def get_data(ticker):
    """Downloads data and strictly forces it into simple numbers"""
    try:
        # Download data
        df = yf.download(ticker, period="6mo", interval="1d", progress=False)
        
        if df.empty:
            return None, None, None

        # --- THE FIX: FLATTEN WEIRD COLUMNS ---
        # If columns are complex (e.g., ('Close', 'VFV.TO')), flatten them to just 'Close'
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
            
        # Ensure 'Close' is a Series, not a 1-column DataFrame
        # (This happens sometimes with recent updates)
        close_data = df['Close']
        if isinstance(close_data, pd.DataFrame):
            close_data = close_data.iloc[:, 0] # Take the first column

        # Calculate Indicators
        df['EMA_50'] = ta.ema(close_data, length=50)
        df['RSI'] = ta.rsi(close_data, length=14)

        # --- SAFE EXTRACT (Force values to be simple floats) ---
        def get_scalar(series):
            val = series.iloc[-1]
            # If it's still a list/series (e.g. from a glitch), dig deeper
            if isinstance(val, pd.Series):
                val = val.iloc[0]
            return float(val)

        current_price = get_scalar(close_data)
        current_ema = get_scalar(df['EMA_50'])
        current_rsi = get_scalar(df['RSI'])
        
        return current_price, current_ema, current_rsi

    except Exception as e:
        print(f"Error fetching {ticker}: {e}")
        return None, None, None

def send_discord_alert(ticker, price, ema, rsi, note, color):
    if not WEBHOOK_URL:
        return

    currency = "CAD" if ".TO" in ticker else "USD"
    news_link = f"https://finance.yahoo.com/quote/{ticker}/news"
    
    data = {
        "content": f"🚨 **SWING ALERT: {ticker}**",
        "embeds": [
            {
                "title": f"{ticker} is at 50 EMA Support",
                "description": f"**Status:** {note}\nThe price has pulled back to the 50-day trend line.",
                "color": color, 
                "fields": [
                    {"name": "Current Price", "value": f"${price:.2f} {currency}", "inline": True},
                    {"name": "50 EMA Level", "value": f"${ema:.2f} {currency}", "inline": True},
                    {"name": "RSI Strength", "value": f"{rsi:.1f}", "inline": True},
                    {"name": "Strategy", "value": "RSI < 55 + Near EMA", "inline": True},
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

def check_market():
    print("--- 🚀 STARTING BOT RUN 🚀 ---")
    
    # 1. CHECK BENCHMARK (SPY)
    spy_price, spy_ema, spy_rsi = get_data(BENCHMARK_TICKER)
    spy_is_down = False
    
    if spy_price is not None:
        spy_threshold = spy_ema * 0.015
        if abs(spy_price - spy_ema) <= spy_threshold:
            spy_is_down = True
            print(f"BENCHMARK: SPY is at support (${spy_price:.2f}). Market is dipping.")
        else:
            print(f"BENCHMARK: SPY is healthy (${spy_price:.2f}). No general crash.")
    else:
        print("Warning: Could not fetch SPY data.")

    print("-" * 30)

    # 2. CHECK YOUR STOCKS
    for ticker in TICKERS:
        price, ema, rsi = get_data(ticker)

        if price is None:
            continue

        threshold = ema * 0.02
        rsi_limit = 55

        # Safe logic using simple floats
        is_near_ema = abs(price - ema) <= threshold
        is_cool_rsi = rsi < rsi_limit

        print(f"Checking {ticker}: ${price:.2f} | EMA: ${ema:.2f} | RSI: {rsi:.1f}")

        if is_near_ema and is_cool_rsi:
            print(f"!!! MATCH FOUND: {ticker} !!!")
            
            alert_note = "✅ Price is at support and RSI is cool."
            alert_color = 5814783  # Green
            
            if ticker == "VFV.TO":
                if spy_is_down:
                    alert_note = "✅ **STRONG BUY:** US Market (SPY) confirms this dip."
                else:
                    alert_note = "⚠️ **CAUTION:** VFV is down, but SPY is not. Likely CAD currency noise."
                    alert_color = 16753920 # Orange

            send_discord_alert(ticker, price, ema, rsi, alert_note, alert_color)
            
    print("--- ✅ RUN COMPLETE ---")

if __name__ == "__main__":
    check_market()
