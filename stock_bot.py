import yfinance as yf
import pandas_ta as ta
import requests
import os
import pandas as pd
import numpy as np
import time
from datetime import datetime, timedelta
import pytz

# --- CONFIGURATION ---
TICKERS = [
    # --- SAFE FOUNDATION ---
    'VFV.TO', 'ZSP.TO', 'XEF.TO', 'VTI',
    # --- SECTOR ETFS ---
    'SOXQ', 'XLY',
    # --- CANADIAN GROWTH ---
    'HUT.TO',
    # --- US SWINGS ---
    'PLTR', 'SOFI', 'SHOP', 'CCL', 'AMD', 'TSLA', 'HOOD', 'NVDA', 'AAPL', 'MSFT', 'NFLX', 'ORCL', 'MARA'
]

WEBHOOK_URL = os.getenv('DISCORD_URL')

# --- HELPER: GET MARKET TIME ---
def get_market_minutes_elapsed():
    """Returns minutes elapsed since 9:30 AM EST today. Returns 390 if market is closed."""
    tz = pytz.timezone('US/Eastern')
    now = datetime.now(tz)
    market_open = now.replace(hour=9, minute=30, second=0, microsecond=0)
    
    if now < market_open:
        return 0
    
    diff = (now - market_open).total_seconds() / 60
    return min(diff, 390)

# --- HELPER: RELATIVE VOLUME (RVAT) ---
def get_relative_volume(ticker):
    """Calculates Relative Volume at Time (RVAT)."""
    try:
        time.sleep(1) # Safety delay to avoid rate limiting
        df = yf.download(ticker, period="5d", interval="5m", progress=False)
        
        if df.empty or len(df) < 10: return 1.0 

        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        df['Volume'] = pd.to_numeric(df['Volume'], errors='coerce')
        df.dropna(subset=['Volume'], inplace=True)

        df['time_slot'] = df.index.time
        df['date'] = df.index.date 

        # USE LAST COMPLETED CANDLE
        last_completed_bar = df.iloc[-2]
        check_time = last_completed_bar.name.time()
        check_date = last_completed_bar.name.date()
        check_vol = float(last_completed_bar['Volume'])

        # Filter: Same time of day, excluding TODAY (check_date)
        historical_at_time = df[df['time_slot'] == check_time]
        history_only = historical_at_time[historical_at_time['date'] != check_date]

        if history_only.empty: return 1.0

        avg_vol = history_only['Volume'].mean()
        if avg_vol == 0 or pd.isna(avg_vol): return 1.0

        return check_vol / avg_vol

    except Exception as e:
        print(f"⚠️ Volume calc failed for {ticker}: {e}")
        return 1.0

# --- HELPER: EARNINGS CHECK (NEW) ---
def get_earnings_warning(ticker):
    """
    Checks if earnings are within the next 7 days.
    Returns: (is_risky: bool, message: str)
    """
    try:
        # Note: calling yf.Ticker() is a separate request from .download()
        stock = yf.Ticker(ticker)
        cal = stock.calendar
        
        if cal is None:
            return False, ""
            
        earnings_date = None
        
        # Handle different yfinance return types (dict vs dataframe)
        if isinstance(cal, dict) and 'Earnings Date' in cal:
             earnings_date = cal['Earnings Date'][0]
        elif isinstance(cal, pd.DataFrame) and 'Earnings Date' in cal.columns:
             earnings_date = cal.iloc[0]['Earnings Date']
        elif isinstance(cal, pd.DataFrame) and not cal.empty:
             # Fallback for some weird dataframe structures
             return False, ""

        if earnings_date is None:
            return False, ""

        # Convert to date object
        earnings_date = pd.to_datetime(earnings_date).date()
        today = datetime.now().date()
        
        days_until = (earnings_date - today).days
        
        # Risk Window: 0 to 7 days
        if 0 <= days_until <= 7:
            # Format date for cleaner display (e.g. "Feb 16")
            date_str = earnings_date.strftime('%b %d')
            return True, f"Report in {days_until} Days ({date_str})"
        
        return False, ""

    except Exception as e:
        return False, ""

# --- 1. ENHANCED SCORING ENGINE ---
def calculate_confidence(rsi, price, open_price, day_high, day_low, bbl, ema_50, macd_h, prev_macd_h, rel_vol, elapsed_minutes):
    score = 0
    reasons = []

    # A. RSI
    if rsi < 35:
        score += 4
        reasons.append("💎 Deep Value (RSI < 35)")
    elif rsi < 45: 
        score += 3
        reasons.append("📉 Oversold (RSI < 45)")
    elif rsi < 55:
        score += 2
        reasons.append("🌊 Momentum Reset (RSI < 55)")

    # B. SUPPORT LEVELS
    if price <= bbl * 1.01: 
        score += 3
        reasons.append("🛡️ Touching Lower Bollinger Band")
    if abs(price - ema_50) <= (ema_50 * 0.02):
        score += 2
        reasons.append("📈 Riding 50-Day Trendline")

    # C. MACD
    if macd_h > 0:
        score += 2
        reasons.append("🚀 Positive Momentum (Green Histogram)")
    elif macd_h > prev_macd_h: 
        score += 1
        reasons.append("🔄 Improving Momentum")

    # D. VOLUME HYBRID
    if elapsed_minutes < 30:
        is_bullish = price > open_price
        method = "Green Candle"
    else:
        midpoint = (day_high + day_low) / 2
        is_bullish = price >= midpoint
        method = "Upper Range"

    if rel_vol > 1.2:
        if is_bullish:
            score += 1
            if rel_vol > 2.0: score += 1
            reasons.append(f"🟢 High Buying Pressure ({rel_vol:.1f}x)")
        else:
            reasons.append(f"🔴 Selling Pressure ({rel_vol:.1f}x)")

    return score, reasons

# --- 2. ALERT FUNCTION (PRO FORMAT) ---
def send_discord_alert(ticker, price, rsi, ema_50, stop_loss, take_profit, score, reasons, threshold, rel_vol, earnings_msg):
    # 1. Determine Color & Rating
    if score >= 8:
        color = 5763719  # Green (Strong Buy)
        rating = "STRONG BUY"
    elif score >= 5:
        color = 16776960 # Yellow (Moderate Watch)
        rating = "MODERATE WATCH"
    else:
        return 

    # 2. Get Timestamp
    tz = pytz.timezone('US/Eastern')
    timestamp = datetime.now(tz).strftime('%I:%M %p EST')

    # 3. Calculate Percentages for Trade Plan
    stop_pct = ((stop_loss - price) / price) * 100
    target_pct = ((take_profit - price) / price) * 100
    
    # 4. Determine Status Strings
    rsi_status = "Oversold" if rsi < 35 else ("Weak" if rsi < 45 else "Neutral")
    trend_status = "Above" if price > ema_50 else "Below"
    
    vol_status = "Normal"
    if rel_vol > 2.0: vol_status = "Heavy Buying"
    elif rel_vol > 1.2: vol_status = "Strong Buying"

    # 5. Format the Description
    description = f"*Triggered at {timestamp}*\n\n"
    
    if earnings_msg:
        description += f"⚠️ **EARNINGS WARNING:** {earnings_msg}\n\n"
    
    # Trade Plan Section
    description += (
        f"📊 **Trade Plan**\n"
        f"• **Entry:** `${price:.2f}`\n"
        f"• **Target:** `${take_profit:.2f}` (+{target_pct:.1f}%) 🎯\n"
        f"• **Stop:** `${stop_loss:.2f}` ({stop_pct:.1f}%) 🛑\n\n"
    )

    # Technicals Section
    description += (
        f"📉 **Technicals**\n"
        f"• **RSI:** `{rsi:.1f}` ({rsi_status})\n"
        f"• **Trend:** {trend_status} 50 EMA ( `${ema_50:.2f}` )\n"
        f"• **Volume:** `{rel_vol:.1f}x` ({vol_status})\n\n"
    )

    # Analysis Section (Clean Bullets)
    description += "📝 **Analysis**\n"
    for r in reasons:
        # Check if emoji exists, if not add a default bullet
        if not any(char in r for char in ["💎", "📉", "🌊", "🛡️", "📈", "🚀", "🔄", "🟢", "🔴"]):
            description += f"• {r}\n"
        else:
            description += f"• {r}\n"

    # 6. Construct the Payload
    data = {
        "content": f"🚨 **SWING ALERT: {ticker}** <@YourID>", # Replace <@YourID> with actual ID if needed
        "embeds": [
            {
                "title": f"🔥 {rating}: {ticker} (Score: {score}/10)",
                "description": description,
                "color": color,
                "fields": [
                    {
                        "name": "🔗 Links", 
                        "value": f"[Yahoo Finance](https://finance.yahoo.com/quote/{ticker})", # TradingView Removed
                        "inline": False
                    }
                ],
                "footer": {"text": "Bot Triggered via GitHub Actions"}
            }
        ]
    }
    
    try:
        requests.post(WEBHOOK_URL, json=data)
    except Exception as e:
        print(f"Failed to send Discord alert: {e}")

# --- 3. MAIN LOOP ---
def check_market():
    print(f"Checking {len(TICKERS)} tickers via Bulk Download...")
    elapsed_minutes = get_market_minutes_elapsed()
    print(f"🕒 Market Minutes Elapsed: {elapsed_minutes:.0f}/390")
    
    try:
        bulk_data = yf.download(TICKERS, period="6mo", interval="1d", group_by='ticker', progress=False)
    except Exception as e:
        print(f"Critical Error: Bulk download failed - {e}")
        return

    for ticker in TICKERS:
        try:
            try:
                df = bulk_data[ticker].copy()
            except KeyError:
                print(f"⚠️ No data found for {ticker}")
                continue

            if df['Close'].isnull().all(): continue
            df.dropna(subset=['Close'], inplace=True)

            # --- CALCULATE INDICATORS ---
            df['EMA_50'] = ta.ema(df['Close'], length=50)
            df['RSI'] = ta.rsi(df['Close'], length=14)
            
            macd = ta.macd(df['Close'])
            if macd is not None:
                df['MACD_H'] = macd.iloc[:, 1]
            else:
                continue

            bb = ta.bbands(df['Close'], length=20, std=2)
            if bb is not None and not bb.empty:
                df['BBL'] = bb.iloc[:, 0]
            else:
                df['BBL'] = pd.NA
            
            df['ATR'] = ta.atr(df['High'], df['Low'], df['Close'], length=14)

            # --- GET VALUES ---
            if len(df) < 50: continue 
            
            last = df.iloc[-1]
            
            # --- 🛡️ GHOST CANDLE FIX 🛡️ ---
            tz = pytz.timezone('US/Eastern')
            today_date = datetime.now(tz).date()
            candle_date = last.name.date()
            
            if elapsed_minutes > 20 and candle_date != today_date:
                continue

            prev = df.iloc[-2]
            if pd.isna(last['BBL']) or pd.isna(last['EMA_50']): continue

            price = float(last['Close'])
            open_price = float(last['Open']) 
            day_high = float(last['High'])
            day_low = float(last['Low'])

            rsi = float(last['RSI'])
            ema_50 = float(last['EMA_50'])
            bbl = float(last['BBL'])
            
            macd_h = float(last['MACD_H'])
            prev_macd_h = float(prev['MACD_H'])
            atr = float(last['ATR'])

            # --- TRIGGER LOGIC ---
            near_ema = abs(price - ema_50) <= (ema_50 * 0.02)
            near_bb = abs(price - bbl) <= (bbl * 0.015)
            
            if (near_ema or near_bb) and rsi < 55:
                
                # 1. Check Volume (Wait 1s to be polite)
                rel_vol = get_relative_volume(ticker)

                # 2. Check Earnings (NEW - Only check if setup is good)
                has_earnings_risk, earnings_msg = get_earnings_warning(ticker)

                stop_loss = price - (atr * 1.5)
                take_profit = price + (atr * 2.0) 
                
                # 3. Score
                score, reasons = calculate_confidence(rsi, price, open_price, day_high, day_low, bbl, ema_50, macd_h, prev_macd_h, rel_vol, elapsed_minutes)
                
                # 4. Apply Earnings Penalty
                if has_earnings_risk:
                    score -= 2 # Penalize risky setups
                    # Note: We pass earnings_msg explicitly to the alert function now

                # --- TIME & FRIDAY THRESHOLD ---
                min_score_needed = 5 
                if elapsed_minutes < 60: min_score_needed = 7 
                
                is_friday = datetime.now(tz).weekday() == 4
                if is_friday and elapsed_minutes > 270: min_score_needed += 1

                print(f"🔎 Checking {ticker}: Score {score}/10 (RVAT: {rel_vol:.2f}x)")
                
                if score >= min_score_needed:
                    # UPDATED CALL: Passing 'earnings_msg' to the new visual alert function
                    send_discord_alert(ticker, price, rsi, ema_50, stop_loss, take_profit, score, reasons, min_score_needed, rel_vol, earnings_msg)

        except Exception as e:
            print(f"Error processing {ticker}: {e}")

if __name__ == "__main__":
    check_market()
