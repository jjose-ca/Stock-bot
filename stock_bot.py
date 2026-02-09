import yfinance as yf
import pandas_ta as ta
import requests
import os
import time

# --- CONFIGURATION ---
# Watchlist: VFV.TO (Canada) plus top US stocks
TICKERS = ['VFV.TO', 'AAPL', 'TSLA', 'MSFT', 'AMD']
BENCHMARK_TICKER = "SPY"  # US S&P 500 ETF (The "Truth" source)

# WEBHOOK: Get this from your GitHub Secrets
WEBHOOK_URL = os.getenv('DISCORD_URL')

def get_data(ticker):
    """Downloads data and calculates 50 EMA and RSI"""
    try:
        # Download last 6 months of data
        df = yf.download(ticker, period="6mo", interval="1d", progress=False)
        
        if df.empty:
            return None, None, None
        
        # Calculate Indicators
        # 1. 50-day Exponential Moving Average
        df['EMA_50'] = ta.ema(df['Close'], length=50)
        # 2. RSI (Relative Strength Index) - 14 day standard
        df['RSI'] = ta.rsi(df['Close'], length=14)

        # Get the most recent values (the last row)
        current_price = df['Close'].iloc[-1]
        current_ema = df['EMA_50'].iloc[-1]
        current_rsi = df['RSI'].iloc[-1]
        
        return current_price, current_ema, current_rsi

    except Exception as e:
        print(f"Error fetching {ticker}: {e}")
        return None, None, None

def send_discord_alert(ticker, price, ema, rsi, note, color):
    """Sends the fancy alert to your Discord channel"""
    if not WEBHOOK_URL:
        print("Error: No Discord URL found. Check GitHub Secrets.")
        return

    currency = "CAD" if ".TO" in ticker else "USD"
    news_link = f"https://finance.yahoo.com/quote/{ticker}/news"
    
    # Structure the message
    data = {
        "content": f"🚨 **SWING ALERT: {ticker}**",
        "embeds": [
            {
                "title": f"{ticker} is at 50 EMA Support",
                "description": f"**Status:** {note}\nThe price has pulled back to the 50-day trend line.",
                "color": color, # Green or Orange
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
    
    # 1. CHECK THE SPY (Benchmark)
    # We use this to see if the whole market is down, or just VFV.
    spy_price, spy_ema, spy_rsi = get_data(BENCHMARK_TICKER)
    spy_is_down = False
    
    if spy_price:
        # If SPY is within 1.5% of its 50 EMA, the market is dipping.
        spy_threshold = spy_ema * 0.015
        if abs(spy_price - spy_ema) <= spy_threshold:
            spy_is_down = True
            print(f"BENCHMARK: SPY is at support (${spy_price:.2f}). Market is dipping.")
        else:
            print(f"BENCHMARK: SPY is healthy (${spy_price:.2f}). No general crash.")

    print("-" * 30)

    # 2. CHECK YOUR STOCKS
    for ticker in TICKERS:
        price, ema, rsi = get_data(ticker)

        if price is None:
            continue

        # SETTINGS:
        # A) Price must be within 2% of the 50 EMA (Close enough to touch)
        threshold = ema * 0.02
        # B) RSI must be below 55 (Not overbought)
        rsi_limit = 55

        # Check conditions
        is_near_ema = abs(price - ema) <= threshold
        is_cool_rsi = rsi < rsi_limit

        print(f"Checking {ticker}: ${price:.2f} | EMA: ${ema:.2f} | RSI: {rsi:.1f}")

        if is_near_ema and is_cool_rsi:
            print(f"!!! MATCH FOUND: {ticker} !!!")
            
            # DECISION LOGIC FOR ALERT COLOR
            alert_note = ""
            alert_color = 0
            
            if ticker == "VFV.TO":
                # Special Logic for Canada ETF
                if spy_is_down:
                    alert_note = "✅ **STRONG BUY:** US Market (SPY) confirms this dip."
                    alert_color = 5814783  # Green
                else:
                    alert_note = "⚠️ **CAUTION:** VFV is down, but SPY is not. Likely CAD currency fluctuation."
                    alert_color = 16753920 # Orange
            else:
                # Standard Logic for US Stocks
                alert_note = "✅ Price is at support and RSI is cool."
                alert_color = 5814783  # Green

            send_discord_alert(ticker, price, ema, rsi, alert_note, alert_color)
            
    print("--- ✅ RUN COMPLETE ---")

if __name__ == "__main__":
    check_market()
