import yfinance as yf
import pandas_ta as ta
import requests
import os
import pandas as pd
from datetime import datetime

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
    'CCL',      # Recovery Play
    'NFLX'      # High Volatility Swing
]

BENCHMARK_TICKER = "SPY"
WEBHOOK_URL = os.getenv('DISCORD_URL')

# --- 1. EARNINGS CHECKER ---
def get_earnings_warning(ticker_symbol):
    """Returns a warning string ONLY if earnings are within 5 days. Otherwise returns None."""
    try:
        stock = yf.Ticker(ticker_symbol)
        cal = stock.calendar
        
        # Check if calendar exists and has earnings date
        if cal and 'Earnings Date' in cal:
            earnings_list = cal['Earnings Date']
            if earnings_list:
                next_date = earnings_list[0] # Usually a datetime object
                
                # Calculate days remaining
                days_left = (next_date.date() - datetime.now().date()).days
                
                # LOGIC: Only return text if within danger zone (0 to 5 days)
                if 0 <= days_left <= 5:
                    return f"⚠️ {next_date.strftime('%b %d')} ({days_left} days left)"
    except Exception as e:
        pass
        
    return None # Return Nothing if safe

# --- 2. CONFIDENCE SCORING ---
def calculate_confidence(price, ema_50, ema_200, rsi):
    score = 0
    reasons = []

    # Trend (Bull Market?)
    if price > ema_200:
        score += 5
        reasons.append("✅ **Trend:** Bullish (Above 200 EMA)")
    else:
        reasons.append("⚠️ **Trend:** Bearish (Below 200 EMA)")

    # RSI (Value?)
    if rsi < 35:
        score += 5
        reasons.append("💎 **Value:** Deeply Oversold (RSI < 35)")
    elif rsi < 45:
        score += 3
        reasons.append("📉 **Value:** Oversold (RSI < 45)")
    elif rsi > 60:
        score -= 2
        reasons.append("🛑 **Risk:** Overbought (RSI > 60)")
    
    if score > 10: score = 10
    return score, reasons

# --- 3. DATA FETCHING ---
def get_data(ticker):
    try:
        df = yf.download(ticker, period="1y", interval="1d", progress=False)
        if df.empty: return None, None, None, None

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
            if isinstance(val, pd.Series): val = val.iloc[0]
            return float(val)

        return (get_scalar(close_data), get_scalar(df['EMA_50']), 
                get_scalar(df['EMA_200']), get_scalar(df['RSI']))

    except Exception:
        return None, None, None, None

# --- 4. DISCORD ALERT ---
def send_discord_alert(ticker, price, ema, ema200, rsi, status_msg, reason_str, color, earnings_warning=None):
    if not WEBHOOK_URL: return

    news_link = f"https://finance.yahoo.com/quote/{ticker}"
    currency = "CAD" if ".TO" in ticker or ".NE" in ticker else "USD"
    
    # Base Fields
    fields = [
        {"name": "Price", "value": f"${price:.2f} {currency}", "inline": True},
        {"name": "RSI", "value": f"{rsi:.1f}", "inline": True},
        {"name": "50 EMA", "value": f"${ema:.2f}", "inline": True},
        {"name": "200 EMA", "value": f"${ema200:.2f}", "inline": True},
    ]

    # CONDITIONAL FIELD: Only add Earnings if it exists (meaning it's dangerous)
    if earnings_warning:
        fields.insert(1, {"name": "🚨 EARNINGS WARNING", "value": earnings_warning, "inline": True})

    # Add Research Link at the end
    fields.append({"name": "Research", "value": f"[View Chart & News on Yahoo]({news_link})", "inline": False})

    embed = {
        "title": f"🚨 ACTION SIGNAL: {ticker}",
        "description": f"**Status:** {status_msg}\n\n{reason_str}",
        "color": color, 
        "fields": fields,
        "footer": {"text": "Bot running via GitHub Actions"}
    }
    
    try:
        requests.post(WEBHOOK_URL, json={"content": f"**{ticker} ALERT**", "embeds": [embed]})
        print(f"--> Alert sent for {ticker}!")
    except Exception as e:
        print(f"Failed to send alert: {e}")

# --- 5. MAIN LOOP ---
def check_market():
    print("--- 🚀 STARTING BOT RUN 🚀 ---")
    
    # Check SPY
    spy_price, spy_ema, spy_ema200, spy_rsi = get_data(BENCHMARK_TICKER)
    if spy_price:
        if spy_price > spy_ema: print(f"MARKET STATUS: Bullish (SPY > 50 EMA)")
        else: print(f"MARKET STATUS: Caution (SPY < 50 EMA)")
    print("-" * 30)

    for ticker in TICKERS:
        price, ema, ema200, rsi = get_data(ticker)
        if price is None: continue

        # Calculate Score
        confidence, reasons = calculate_confidence(price, ema, ema200, rsi)
        reason_str = "\n".join(reasons)
        full_note = f"**Confidence Score: {confidence}/10**"

        # Check Earnings (Only returns value if <= 5 days)
        earnings_warning = get_earnings_warning(ticker)

        # TRIGGER A: Standard Bounce (Near 50 EMA)
        if abs(price - ema) <= (ema * 0.02):
            print(f"!!! MATCH: {ticker} (Score: {confidence}) !!!")
            status_msg = "✅ **Standard Buy:** Bouncing off 50 EMA support."
            
            if confidence >= 7: color = 5763719   # Green
            elif confidence >= 5: color = 16776960 # Yellow
            else: color = 15158332 # Red
                
            send_discord_alert(ticker, price, ema, ema200, rsi, status_msg, full_note + "\n" + reason_str, color, earnings_warning)

        # TRIGGER B: Deep Value (Near 200 EMA)
        elif abs(price - ema200) <= (ema200 * 0.02):
            print(f"!!! DEEP VALUE MATCH: {ticker} !!!")
            status_msg = "💰 **DEEP VALUE PLAY:** Stock crashed to 200 EMA Floor!"
            send_discord_alert(ticker, price, ema, ema200, rsi, status_msg, full_note + "\n" + reason_str, 10181046, earnings_warning)

        else:
            print(f"{ticker}: ${price:.2f} (Score: {confidence}/10) - No Setup")

    print("--- ✅ RUN COMPLETE ---")

if __name__ == "__main__":
    check_market()
