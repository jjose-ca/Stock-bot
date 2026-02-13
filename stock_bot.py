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

# --- HELPER: INTRADAY METRICS (RVAT + VWAP) ---
def get_intraday_metrics(ticker):
    """
    Calculates Relative Volume (RVAT) and Intraday VWAP.
    Returns: (rel_vol, vwap_val)
    """
    try:
        time.sleep(1) # Safety delay to avoid rate limiting
        # Download 5 days of 5m data
        df = yf.download(ticker, period="5d", interval="5m", progress=False)
        
        if df.empty or len(df) < 20: return 1.0, None

        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        df['Volume'] = pd.to_numeric(df['Volume'], errors='coerce')
        df['Close'] = pd.to_numeric(df['Close'], errors='coerce')
        df['High'] = pd.to_numeric(df['High'], errors='coerce')
        df['Low'] = pd.to_numeric(df['Low'], errors='coerce')
        df.dropna(subset=['Volume', 'Close'], inplace=True)

        # 1. Calculate Intraday VWAP (Manual Calc for current day)
        current_date = df.index[-1].date()
        today_df = df[df.index.date == current_date].copy()
        
        if not today_df.empty:
            today_df['TP'] = (today_df['High'] + today_df['Low'] + today_df['Close']) / 3
            today_df['PV'] = today_df['TP'] * today_df['Volume']
            vwap_series = today_df['PV'].cumsum() / today_df['Volume'].cumsum()
            vwap_val = vwap_series.iloc[-1]
        else:
            vwap_val = None

        # 2. Calculate RVAT
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

        if history_only.empty: return 1.0, vwap_val

        avg_vol = history_only['Volume'].mean()
        if avg_vol == 0 or pd.isna(avg_vol): return 1.0, vwap_val

        rel_vol = check_vol / avg_vol
        return rel_vol, vwap_val

    except Exception as e:
        print(f"⚠️ Intraday calc failed for {ticker}: {e}")
        return 1.0, None

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
            return True, f"⚠️ **EARNINGS WARNING:** Report in {days_until} days ({date_str})"
        
        return False, ""

    except Exception as e:
        return False, ""

# --- 1. ENHANCED SCORING ENGINE (PRO LOGIC) ---
def calculate_confidence(rsi, price, open_price, day_high, day_low, bbl, ema_50, sma_200, atr, macd_h, prev_macd_h, rel_vol, vwap_val, elapsed_minutes):
    score = 0
    reasons = []

    # --- A. VALUE (RSI + TREND FILTER) ---
    # Logic: Only award max points if we are in an UPTREND (Price > 200 SMA)
    # If below 200 SMA, we penalize the "Dip Buy" because it might be a crash.
    is_uptrend = price > sma_200
    
    if rsi < 35:
        if is_uptrend:
            score += 4
            reasons.append("💎 Deep Value (RSI < 35 + Uptrend)")
        else:
            score += 2 # Penalize for being against long-term trend
            reasons.append("⚠️ Deep Oversold (Downtrend Risk)")
    elif rsi < 45: 
        score += 3 if is_uptrend else 2
        reasons.append("📉 Oversold (RSI < 45)")
    elif rsi < 55:
        score += 2
        reasons.append("🌊 Momentum Reset (RSI < 55)")

    # --- B. DYNAMIC SUPPORT (ATR) ---
    # Logic: Use ATR for distance instead of fixed 2%
    # "Touch Zone" is within 0.5 ATR
    dist_to_ema = abs(price - ema_50)
    atr_threshold = 0.5 * atr 
    
    if price <= bbl * 1.01: 
        score += 3
        reasons.append("🛡️ Touching Lower Bollinger Band")
        
    if dist_to_ema <= atr_threshold:
        score += 2
        reasons.append(f"📈 Riding 50-Day Trendline (Within {dist_to_ema/atr:.1f} ATR)")

    # --- C. MOMENTUM (MACD) ---
    if macd_h > 0:
        score += 2
        reasons.append("🚀 Positive Momentum (Green Histogram)")
    elif macd_h > prev_macd_h: 
        score += 1
        reasons.append("🔄 Improving Momentum")

    # --- D. VOLUME (VWAP CHECK) ---
    # Logic: Use VWAP to confirm institutional flow
    is_bullish_vol = False
    method = "Price Action"

    if elapsed_minutes < 30:
        is_bullish_vol = price > open_price
        method = "Green Candle"
    else:
        if vwap_val is not None:
            is_bullish_vol = price >= vwap_val
            method = "VWAP"
        else:
            # Fallback if VWAP calc fails
            midpoint = (day_high + day_low) / 2
            is_bullish_vol = price >= midpoint
            method = "Upper Range"

    if rel_vol > 1.2:
        if is_bullish_vol:
            score += 1
            if rel_vol > 2.0: score += 1
            reasons.append(f"🟢 High Buying Pressure ({rel_vol:.1f}x via {method})")
        else:
            reasons.append(f"🔴 Selling Pressure ({rel_vol:.1f}x via {method})")

    return score, reasons

# --- 2. ALERT FUNCTION (VISUAL POLISH UPGRADE) ---
def send_discord_alert(ticker, price, rsi, ema_50, stop_loss, take_profit, score, reasons, threshold, rel_vol, earnings_msg):
    # 1. Determine Color & Rating
    if score >= 8:
        color = 5763719  # Green (Strong Buy)
        rating = "🔥 STRONG BUY"
    elif score >= 5:
        color = 16776960 # Yellow (Moderate Watch)
        rating = "⚠️ MODERATE WATCH"
    else:
        return 

    # 2. Get Timestamp
    tz = pytz.timezone('US/Eastern')
    timestamp = datetime.now(tz).strftime('%I:%M %p EST')

    # 3. Calculate Percentages for Trade Plan
    stop_pct = ((stop_loss - price) / price) * 100
    target_pct = ((take_profit - price) / price) * 100
    risk_reward = abs(target_pct / stop_pct)
    
    # 4. Determine Status Strings
    rsi_status = "Oversold" if rsi < 35 else ("Weak" if rsi < 45 else "Neutral")
    trend_status = "Above" if price > ema_50 else "Below"
    
    # --- LOGIC FIX START: Detect Volume Direction ---
    vol_dir = "Neutral"
    for r in reasons:
        if "Buying Pressure" in r:
            vol_dir = "Buying"
            break
        elif "Selling Pressure" in r:
            vol_dir = "Selling"
            break
            
    vol_status = "Normal"
    if rel_vol > 2.0: vol_status = f"Heavy {vol_dir}"
    elif rel_vol > 1.2: vol_status = f"Strong {vol_dir}"
    # --- LOGIC FIX END ---

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
        # UPDATED: Download 1 year ("1y") to ensure 200 SMA calculation is valid
        bulk_data = yf.download(TICKERS, period="1y", interval="1d", group_by='ticker', progress=False)
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
            df['SMA_200'] = ta.sma(df['Close'], length=200) #
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
            
            df['ATR'] = ta.atr(df['High'], df['Low'], df['Close'], length=14) #

            # --- GET VALUES ---
            if len(df) < 200: continue # Need 200 candles for SMA 200
            
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
            sma_200 = float(last['SMA_200']) # New
            bbl = float(last['BBL'])
            atr = float(last['ATR']) # New
            
            macd_h = float(last['MACD_H'])
            prev_macd_h = float(prev['MACD_H'])

            # --- TRIGGER LOGIC (UPDATED PRE-FILTER) ---
            # Use ATR for pre-filtering too (1.0 ATR buffer)
            dist_to_ema = abs(price - ema_50)
            
            # Interest Trigger: Near 50 EMA OR Deep Value
            if (dist_to_ema <= 1.0 * atr) or (rsi < 55):
                
                # 1. Check Volume & VWAP (Wait 1s to be polite)
                rel_vol, vwap_val = get_intraday_metrics(ticker)

                # 2. Check Earnings (NEW - Only check if setup is good)
                has_earnings_risk, earnings_msg = get_earnings_warning(ticker)

                # Stops based on ATR
                stop_loss = price - (atr * 1.5)
                take_profit = price + (atr * 2.0) 
                
                # 3. Score (PRO LOGIC)
                score, reasons = calculate_confidence(
                    rsi, price, open_price, day_high, day_low, bbl, 
                    ema_50, sma_200, atr, # Passing new indicators
                    macd_h, prev_macd_h, rel_vol, vwap_val, elapsed_minutes
                )
                
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
