import yfinance as yf
import pandas_ta as ta
import requests
import os

# --- CONFIGURATION ---
# List of stocks you want to watch
TICKERS = ['AAPL', 'TSLA', 'NVDA', 'AMD', 'MSFT'] 

# Get the Webhook URL from GitHub Secrets (Security Best Practice)
WEBHOOK_URL = os.getenv('DISCORD_URL')

def send_discord_alert(ticker, price, ema):
    # Create a link to Yahoo Finance News
    news_link = f"https://finance.yahoo.com/quote/{ticker}/news"
    
    data = {
        "content": "🚨 **SWING TRADE ALERT** 🚨",
        "embeds": [
            {
                "title": f"Buy Signal: {ticker}",
                "description": f"The price has touched the **50 EMA** support level.",
                "color": 5814783,  # Green
                "fields": [
                    {"name": "Current Price", "value": f"${price:.2f}", "inline": True},
                    {"name": "50 EMA Level", "value": f"${ema:.2f}", "inline": True},
                    {"name": "Step 2: Check News", "value": f"[Click here to read news]({news_link})"}
                ],
                "footer": {"text": "Bot running via GitHub Actions"}
            }
        ]
    }
    requests.post(WEBHOOK_URL, json=data)

def check_market():
    print("Checking market data...")
    for ticker in TICKERS:
        try:
            # Get 6 months of data
            df = yf.download(ticker, period="6mo", interval="1d", progress=False)
            
            if df.empty:
                continue

            # Calculate 50 EMA
            df['EMA_50'] = ta.ema(df['Close'], length=50)
            
            # Get latest values
            current_price = df['Close'].iloc[-1]
            current_ema = df['EMA_50'].iloc[-1]

            # Logic: Alert if price is within 1% of the 50 EMA
            # This catches the "bounce" before it fully happens
            threshold = current_ema * 0.01 
            
            print(f"{ticker}: ${current_price:.2f} (EMA: {current_ema:.2f})")

            if abs(current_price - current_ema) <= threshold:
                print(f"!!! TRIGGER: {ticker} !!!")
                send_discord_alert(ticker, current_price, current_ema)
                
        except Exception as e:
            print(f"Error checking {ticker}: {e}")

if __name__ == "__main__":
    check_market()

# --- TEST BLOCK (Delete this later) ---
if __name__ == "__main__":
    # Test the connection immediately
    print("Sending test message...")
    data = {
        "content": "✅ **Bot Connection Test**\nIf you see this, your Discord Webhook is working perfectly!"
    }
    requests.post(WEBHOOK_URL, json=data)
    
    # Run the normal check
    check_market()
