import yfinance as yf
import pandas_ta as ta
import requests
import os

# --- CONFIGURATION ---
# Added 'VFV.TO' for the Canadian S&P 500 ETF
TICKERS = ['AAPL', 'TSLA', 'NVDA', 'AMD', 'MSFT', 'VFV.TO'] 

# Get the Webhook URL from GitHub Secrets
WEBHOOK_URL = os.getenv('DISCORD_URL')

def send_discord_alert(ticker, price, ema, currency):
    news_link = f"https://finance.yahoo.com/quote/{ticker}/news"
    
    data = {
        "content": "🚨 **SWING TRADE ALERT** 🚨",
        "embeds": [
            {
                "title": f"Buy Signal: {ticker}",
                "description": f"The price has touched the **50 EMA** support level.",
                "color": 5814783,  # Green
                "fields": [
                    {"name": "Current Price", "value": f"${price:.2f} {currency}", "inline": True},
                    {"name": "50 EMA Level", "value": f"${ema:.2f} {currency}", "inline": True},
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
                print(f"No data for {ticker}")
                continue

            # Calculate 50 EMA
            df['EMA_50'] = ta.ema(df['Close'], length=50)
            
            # Get latest values
            current_price = df['Close'].iloc[-1]
            current_ema = df['EMA_50'].iloc[-1]
            
            # Determine Currency (Simple check for Canadian stocks)
            currency = "CAD" if ".TO" in ticker else "USD"

            # Logic: Alert if price is within 1% of the 50 EMA
            threshold = current_ema * 0.01 
            
            print(f"{ticker}: ${current_price:.2f} {currency} (EMA: {current_ema:.2f})")

            if abs(current_price - current_ema) <= threshold:
                print(f"!!! TRIGGER: {ticker} !!!")
                send_discord_alert(ticker, current_price, current_ema, currency)
                
        except Exception as e:
            print(f"Error checking {ticker}: {e}")

if __name__ == "__main__":
    check_market()
