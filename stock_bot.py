import yfinance as yf
import pandas_ta as ta
import requests
import os

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
    'PLTR', 'SOFI', 'CCL', 'NFLX', 'AAPL', 'MSFT'
]

WEBHOOK_URL = os.getenv('DISCORD_URL')

# --- 1. CONFIDENCE SCORING (Updated: No Precision) ---
def calculate_confidence(price, ema_200, rsi):
    """Calculates a score from 0-10 based on TREND and VALUE only."""
    score = 0
    reasons = []

    # A) The Trend (50% of Score)
    # Are we in a long-term uptrend?
    if price > ema_200:
        score += 5
        reasons.append("✅ **Trend:** Bullish (Price > 200 EMA)")
    else:
        score -= 3
        reasons.append("⚠️ **Trend:** Bearish (Price < 200 EMA)")

    # B) The Value / RSI (50% of Score)
    if rsi < 35:
        score += 5
        reasons.append("💎 **Value:** Deeply Oversold (RSI < 35)")
    elif rsi < 45:
        score += 3
        reasons.append("📉 **Value:** Oversold (RSI < 45)")
    elif rsi < 55:
        score += 2
        reasons.append("🌊 **Value:** Momentum Dip (RSI < 55)")
    elif rsi > 65:
        score -= 5
        reasons.append("🛑 **Risk:** Overbought (RSI > 65)")

    # Cap score at 10
    final_score = max(0, min(10, score))
    return final_score, reasons

# --- 2. ALERT FUNCTION ---
def send_discord_alert(ticker, price, ema_50, rsi, score, reasons):
    # Color code based on Score
    if score >= 7:
        color = 5814783  # Green (Strong Buy)
        rating = "🔥 STRONG BUY"
    elif score >= 5:
        color = 16776960 # Yellow (Moderate)
        rating = "⚠️ MODERATE BUY"
    else:
        color = 15158332 # Red (Weak)
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
                    # --- THE VALUES YOU REQUESTED ---
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
            # Get 1 year of data for 200 EMA
            df = yf.download(ticker, period="1y", interval="1d", progress=False)
            if df.empty: continue

            # Calculate Indicators
            df['EMA_50'] = ta.ema(df['Close'], length=50)
            df['EMA_200'] = ta.ema(df['Close'], length=200)
            df['RSI'] = ta.rsi(df['Close'], length=14)
            
            # Get latest values
            price = df['Close'].iloc[-1]
            ema_50 = df['EMA_50'].iloc[-1]
            ema_200 = df['EMA_200'].iloc[-1]
            rsi = df['RSI'].iloc[-1]

            # --- TRIGGER LOGIC ---
            
            # 1. Price is within 2% of the 50 EMA (The "Zone")
            is_near_support = abs(price - ema_50) <= (ema_50 * 0.02)
            
            # 2. RSI is healthy (Below 60)
            is_good_rsi = rsi < 60

            if is_near_support and is_good_rsi:
                # Calculate Score (Trend + Value only)
                score, reasons = calculate_confidence(price, ema_200, rsi)
                
                print(f"TRIGGER: {ticker} | Price: {price} | RSI: {rsi}")
                send_discord_alert(ticker, price, ema_50, rsi, score, reasons)
            else:
                print(f"{ticker}: ${price:.2f} | RSI: {rsi:.1f} (No setup)")

        except Exception as e:
            print(f"Error checking {ticker}: {e}")

if __name__ == "__main__":
    check_market()
