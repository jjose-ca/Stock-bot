import yfinance as yf
import pandas_ta as ta
import requests
import os
import pandas as pd

# --- CONFIGURATION ---
TICKERS = [
    'VFV.TO',   # Safe Base
    'SOXQ',     # Semiconductors (Growth)
    'XLY',      # Consumer Discretionary
    'NVDA.NE',  # Nvidia (CAD Hedged)
    'TSLA.NE',  # Tesla (CAD Hedged)
    'HUT.TO',   # Crypto Mining
    'PLTR',     # Aggressive Swing
    'SOFI',     # Fintech
    'CCL'       # Recovery Play
]

BENCHMARK_TICKER = "SPY"
WEBHOOK_URL = os.getenv('DISCORD_URL')

# --- 1. CONFIDENCE SCORING FUNCTION ---
def calculate_confidence(price, ema_50, ema_200, rsi):
    """Calculates a score from 0-10 based on Trend and Value"""
    score = 0
    reasons = []

    # Criterion A: The Trend (Are we in a Bull Market?)
    if price > ema_200:
        score += 5  # Increased weight since we removed Precision
        reasons.append("✅ **Trend:** Bullish (Above 200 EMA)")
    else:
        reasons.append("⚠️ **Trend:** Bearish (Below 200 EMA)")

    # Criterion B: The RSI (Is it cheap?)
    if rsi < 35:
        score += 5
        reasons.append("💎 **Value:** Deeply Oversold (RSI < 35)")
    elif rsi < 45:
        score += 3
        reasons.append("📉 **Value:** Oversold (RSI < 45)")
    elif rsi > 60:
        score -= 2
        reasons.append("🛑 **Risk:** Overbought (RSI > 60)")
    
    # Cap score at 10
    if score > 10: score = 10
    
    return score, reasons

def get_data(ticker):
    """Downloads data and calculates indicators"""
    try:
        df = yf.download(ticker, period="1y", interval="1d", progress=False)
        
        if df.empty:
            return None, None, None, None

        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
            
        close_data = df['Close']
        if isinstance(close_data, pd.DataFrame):
            close_data = close_data.iloc[:, 0]

        df['EMA_50'] = ta.ema(close_data, length=50)
        df['EMA_200'] = ta.ema(close_data, length=200)
        df['RSI'] = ta.rsi(close_data, length=14)

        def get_scalar(series):
            val = series.iloc[-1]
            if isinstance(val, pd.Series):
                val = val.iloc[0]
            return float(val)

        return (get_scalar(close_data), 
                get_scalar(df['EMA_50']), 
                get_scalar(df['EMA_200']), 
                get_scalar(df['RSI']))

    except Exception as e:
        print(f"Error fetching {ticker}: {e}")
        return None, None, None, None

def send_discord_alert(ticker, price, ema, ema200, rsi, status_msg, reason_str, color):
    if not WEBHOOK_URL:
        return

    # Yahoo Finance Link
    news_link = f"https://finance.yahoo.com/quote/{ticker}"
    
    currency = "CAD" if ".TO" in ticker or ".NE" in ticker else "USD"
    
    embed = {
        "title": f"🚨 ACTION SIGNAL: {ticker}",
        "description": f"**Status:** {status_msg}\n\n{reason_str}",
        "color": color, 
        "fields": [
            {"name": "Price", "value": f"${price:.2f} {currency}", "inline": True},
            {"name": "RSI", "value": f"{rsi:.1f}", "inline": True},
            {"name": "50 EMA", "value": f"${ema:.2f}", "inline": True},
            {"name": "200 EMA", "value": f"${ema200:.2f}", "inline": True},
            {"name": "Research", "value": f"[View Chart & News on Yahoo]({news_link})", "inline": False}
        ],
        "footer": {"text": "Bot running via GitHub Actions"}
    }
    
    try:
        requests.post(WEBHOOK_URL, json={"content": f"**{ticker} ALERT**", "embeds": [embed]})
        print(f"--> Alert sent for {ticker}!")
    except Exception as e:
        print(f"Failed to send alert: {e}")

def check_market():
    print("--- 🚀 STARTING BOT RUN 🚀 ---")
    
    # 1. CHECK SPY (Context)
    spy_price, spy_ema, spy_ema200, spy_rsi = get_data(BENCHMARK_TICKER)
    if spy_price:
        if spy_price > spy_ema:
            print(f"MARKET STATUS: Bullish (SPY > 50 EMA)")
        else:
            print(f"MARKET STATUS: Caution (SPY < 50 EMA)")

    print("-" * 30)

    # 2. CHECK YOUR STOCKS
    for ticker in TICKERS:
        price, ema, ema200, rsi = get_data(ticker)

        if price is None:
            continue

        # --- CALCULATE SCORE ---
        confidence, reasons = calculate_confidence(price, ema, ema200, rsi)
        reason_str = "\n".join(reasons)
        full_note = f"**Confidence Score: {confidence}/10**"

        # --- DECISION LOGIC ---
        
        # Trigger A: Standard Bounce (Near 50 EMA)
        if abs(price - ema) <= (ema * 0.02):
            print(f"!!! MATCH: {ticker} (Score: {confidence}) !!!")
            
            status_msg = "✅ **Standard Buy:** Bouncing off 50 EMA support."
            
            if confidence >= 7:
                color = 5763719   # Green
            elif confidence >= 5:
                color = 16776960  # Yellow
            else:
                color = 15158332  # Red
                
            send_discord_alert(ticker, price, ema, ema200, rsi, status_msg, full_note + "\n" + reason_str, color)

        # Trigger B: Deep Value (Near 200 EMA)
        elif abs(price - ema200) <= (ema200 * 0.02):
            print(f"!!! DEEP VALUE MATCH: {ticker} !!!")
            status_msg = "💰 **DEEP VALUE PLAY:** Stock crashed to 200 EMA Floor!"
            send_discord_alert(ticker, price, ema, ema200, rsi, status_msg, full_note + "\n" + reason_str, 10181046) # Purple

        else:
            print(f"{ticker}: ${price:.2f} (Score: {confidence}/10) - No Setup")

    print("--- ✅ RUN COMPLETE ---")

if __name__ == "__main__":
    check_market()
