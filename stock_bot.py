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
    # --- MARKET PROXY (For Regime Check) ---
    'VTI', 

    # --- SAFE FOUNDATION (ETFs) ---
    'VFV.TO',   # S&P 500 (CAD Hedged)
    'ZSP.TO',   # S&P 500 (CAD Unhedged)
    'XEF.TO',   # International Markets
    'SPLG',     # S&P 500 (Cheaper alternative to SPY)
    'QQQM',     # Nasdaq 100 (Cheaper alternative to QQQ)

    # --- SECTOR ETFS (Commodities & Industry) ---
    'SOXQ',     # Semiconductors
    'XLY',      # Consumer Discretionary
    'GDX',      # Gold Miners (High Beta to Gold)
    'SIL',      # Silver Miners (High Volatility)
    'XLF',      # Financials (Bank Swings)
    'URA',      # Uranium (Energy Cycle Plays)

    # --- US SWINGS (High Volume / Retail Favorites) ---
    'PLTR', 'SOFI', 'SHOP', 'CCL', 'AMD', 'TSLA', 'HOOD', 'NVDA', 
    'AAPL', 'MSFT', 'NFLX', 'ORCL', 'MARA', 'F', 'LCID', 'DKNG',
    'UBER', 'RIVN', 'CLSK', 'RIOT', 'MSTR', 'PANW', 'ARM', 'SMCI', 'COIN', 

    # --- CANADIAN GROWTH & SWINGS (TSX) ---
    'HUT.TO',   # Bitcoin Miner (High Volatility)
    'BITF.TO',  # Bitfarms (Crypto Swing)
    'CVE.TO',   # Cenovus Energy (Oil Proxy)
    'AC.TO',    # Air Canada (Range Bound / Travel Recovery)
    'MFC.TO',   # Manulife Financial (Defensive Swing)
    'ATD.TO',   # Alimentation Couche-Tard (Defensive Growth)
    'TOU.TO',
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
        
        # FIX 1: Reduced lookback to 35 days (was 60d) to prevent API timeouts/brittleness
        df = yf.download(ticker, period="35d", interval="5m", progress=False)
        
        if df.empty or len(df) < 10: return 1.0 

        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        df['Volume'] = pd.to_numeric(df['Volume'], errors='coerce')
        df.dropna(subset=['Volume'], inplace=True)
        
        # FIX 2: Filter for Regular Market Hours only (9:30 - 16:00)
        # --- TIMEZONE FIX START ---
        # yfinance returns UTC-aware indexes. Must convert to US/Eastern BEFORE filtering time.
        if df.index.tz is None:
            df.index = df.index.tz_localize('UTC') # Assume UTC if naive
        
        df.index = df.index.tz_convert('US/Eastern')
        # --- TIMEZONE FIX END ---
        
        df = df.between_time('09:30', '16:00')

        df['time_slot'] = df.index.time
        df['date'] = df.index.date 

        # USE LAST COMPLETED CANDLE
        if len(df) < 2: return 1.0
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

# --- HELPER: EARNINGS CHECK (FIXED) ---
def get_earnings_warning(ticker):
    """Checks if earnings are within 7 days using robust timezone handling."""
    try:
        time.sleep(1)  # <--- CRITICAL FIX: Prevent Rate Limiting
        stock = yf.Ticker(ticker)
        cal = stock.calendar
        
        if cal is None: return False, ""
            
        earnings_date = None
        # Handle Dictionary return (newer yfinance versions)
        if isinstance(cal, dict) and 'Earnings Date' in cal:
            earnings_date = cal['Earnings Date'][0]
        # Handle DataFrame return (older versions)
        elif isinstance(cal, pd.DataFrame):
            if 'Earnings Date' in cal.columns:
                earnings_date = cal.iloc[0]['Earnings Date']
            elif not cal.empty: # Fallback for index-based dataframes
                earnings_date = cal.iloc[0, 0] 

        if earnings_date is None: return False, ""

        # Normalize to US/Eastern to match market time
        eastern = pytz.timezone('US/Eastern')
        
        # Parse earnings date (handle both datetime and date objects)
        if isinstance(earnings_date, (datetime, pd.Timestamp)):
            earnings_date = pd.to_datetime(earnings_date).replace(tzinfo=eastern).date()
        else:
            # If it's just a raw date object, assume it's correct
            earnings_date = pd.to_datetime(earnings_date).date()

        today = datetime.now(eastern).date()
        days_until = (earnings_date - today).days
        
        if 0 <= days_until <= 7:
            return True, f"⚠️ **EARNINGS WARNING:** Report in {days_until} days ({earnings_date})"
        
        return False, ""

    except Exception as e:
        # print(f"Earnings check error for {ticker}: {e}") # Optional debug
        return False, ""

# --- 1. ENHANCED SCORING ENGINE (UPDATED WITH 9/21 EMA) ---
def calculate_confidence(rsi, price, open_price, day_high, day_low, bbl, bb_width, ema_50, ema_9, ema_21, macd_h, prev_macd_h, rel_vol, elapsed_minutes):
    score = 0
    reasons = []

    # A. RSI (Value)
    if rsi < 35:
        score += 4
        reasons.append("💎 Deep Value (RSI < 35)")
    elif rsi < 45: 
        score += 3
        reasons.append("📉 Oversold (RSI < 45)")
    elif rsi < 55:  
        score += 1 # Reduced score here to prioritize Trend
        reasons.append("🌊 Momentum Reset (RSI < 55)")

    # B. TREND STRUCTURE (9/21 EMA)
    # 1. Momentum Alignment (Trend is Up)
    if ema_9 > ema_21:
        score += 1
        reasons.append("🚀 Bullish Trend (9 > 21 EMA)")
    
    # 2. "Option A" Dip & Bounce Logic
    # Did we touch the 21 EMA today (Low <= 21 EMA) AND are we currently above it?
    # Tolerance of 0.5% used for the "Touch"
    if day_low <= (ema_21 * 1.005) and price > ema_21:
        score += 2
        reasons.append("⚡ 21 EMA Bounce (Perfect Pullback)")

    # C. MAJOR SUPPORT LEVELS
    if price <= bbl * 1.01: 
        score += 3
        reasons.append("🛡️ Touching Lower Bollinger Band")
    
    # 50 EMA Touch (The "GPS" Support)
    if abs(price - ema_50) <= (ema_50 * 0.015):
        score += 2
        reasons.append("📈 Riding 50-Day Trendline")

    # D. MACD
    if macd_h > 0:
        score += 2
        reasons.append("🟢 Positive Momentum")
    elif macd_h > prev_macd_h: 
        score += 1
        reasons.append("🔄 Improving Momentum")
        
    # E. VOLATILITY
    if bb_width > 0.15:
        score += 1
        reasons.append("⚡ High Volatility Expansion")

    # F. VOLUME HYBRID
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

# --- 2. ALERT FUNCTION (VISUAL POLISH UPGRADE) ---
def send_discord_alert(ticker, price, rsi, ema_21, ema_50, stop_loss, take_profit, score, reasons, threshold, rel_vol, earnings_msg, open_price, day_high, day_low, elapsed_minutes):
    # 1. Determine Color & Rating
    if score >= 8:
        color = 5763719  # Green (Strong Buy)
        rating = "🔥 STRONG BUY"
    elif score >= 6:
        color = 16776960 # Yellow (Moderate Watch)
        rating = "⚠️ MODERATE WATCH"
    else:
        return 

    # 2. Get Timestamp
    tz = pytz.timezone('US/Eastern')
    timestamp = datetime.now(tz).strftime('%I:%M %p EST')

    # 3. Calculate Risk/Reward (Clean Math Change)
    risk = price - stop_loss
    reward = take_profit - price
    
    if risk > 0:
        risk_reward = reward / risk
    else:
        risk_reward = 0.0

    stop_pct = (risk / price) * 100
    target_pct = (reward / price) * 100
    
    # 4. Determine Status Strings
    rsi_status = "Oversold" if rsi < 35 else ("Weak" if rsi < 45 else "Neutral")
    trend_status = "Above" if price > ema_50 else "Below"
    
    vol_dir = "Buying" if price >= (day_high + day_low)/2 else "Selling"
    vol_status = "Normal"
    if rel_vol > 2.0: vol_status = f"Heavy {vol_dir}"
    elif rel_vol > 1.2: vol_status = f"Strong {vol_dir}"

    # 5. Format the Description
    description = f"*Triggered at {timestamp}*\n\n"
    
    if earnings_msg:
        description += f"{earnings_msg}\n\n"
    
    # Trade Plan Section
    description += (
        f"📊 **Trade Plan**\n"
        f"• **Entry:** `${price:.2f}`\n"
        f"• **Target:** `${take_profit:.2f}` (+{target_pct:.1f}%) 🎯\n"
        f"• **Stop:** `${stop_loss:.2f}` (-{stop_pct:.1f}%) 🛑\n"
        f"• **Ratio:** `1:{risk_reward:.2f}` ⚖️\n\n"
    )

    # Technicals Section
    description += (
        f"📉 **Technicals**\n"
        f"• **RSI:** `{rsi:.1f}` ({rsi_status})\n"
        f"• **Trend:** 21 EMA (`${ema_21:.2f}`) | 50 EMA (`${ema_50:.2f}`)\n"
        f"• **Volume:** `{rel_vol:.1f}x` ({vol_status})\n\n"
    )

    # Analysis Section (Clean Bullets)
    description += "📝 **Analysis**\n"
    for r in reasons:
        if not any(char in r for char in ["💎", "📉", "🌊", "🛡️", "📈", "🚀", "🔄", "🟢", "🔴"]):
            description += f"• {r}\n"
        else:
            description += f"• {r}\n"

    # 6. Construct the Payload
    data = {
        "content": f"🚨 **SWING ALERT: {ticker}**",
        "embeds": [
            {
                "title": f"🔥 {rating}: {ticker} (Score: {score}/10)",
                "description": description,
                "color": color,
                "fields": [
                    {
                        "name": "🔗 Links", 
                        "value": f"[Yahoo Finance](https://finance.yahoo.com/quote/{ticker})",
                        "inline": False
                    }
                ],
                "footer": {"text": "Bot Triggered via GitHub Actions"}
            }
        ]
    }
    
    if not WEBHOOK_URL:
        print("❌ Error: DISCORD_URL environment variable is missing.")
        return

    try:
        response = requests.post(WEBHOOK_URL, json=data, timeout=10)
        response.raise_for_status()
    except Exception as e:
        print(f"❌ General Error sending alert: {e}")

# --- 3. MAIN LOOP ---
def check_market():
    print(f"Checking {len(TICKERS)} tickers via Bulk Download...")
    elapsed_minutes = get_market_minutes_elapsed()
    print(f"🕒 Market Minutes Elapsed: {elapsed_minutes:.0f}/390")
    
    try:
        bulk_data = yf.download(TICKERS, period="1y", interval="1d", group_by='ticker', progress=False)
        if len(TICKERS) == 1:
            single_ticker = TICKERS[0]
            bulk_data = {single_ticker: bulk_data}
            
    except Exception as e:
        print(f"Critical Error: Bulk download failed - {e}")
        return

    # --- MARKET REGIME CHECK (VTI) ---
    regime_penalty = 0
    try:
        vti_df = bulk_data['VTI'].copy()
        if isinstance(vti_df.columns, pd.MultiIndex):
            vti_df.columns = vti_df.columns.get_level_values(0)
        vti_df.dropna(subset=['Close'], inplace=True)
        vti_df['SMA_200'] = ta.sma(vti_df['Close'], length=200)
        
        if not vti_df.empty and len(vti_df) > 200:
            last_vti = vti_df.iloc[-1]
            if last_vti['Close'] < last_vti['SMA_200']:
                regime_penalty = 1 
                print(f"⚠️ Market Regime: BEARISH (VTI < 200 SMA). Increasing thresholds.")
            else:
                print(f"✅ Market Regime: BULLISH (VTI > 200 SMA).")
    except Exception as e:
        print(f"Market Regime Check Skipped: {e}")

    for ticker in TICKERS:
        if ticker == 'VTI': continue 

        try:
            try:
                df = bulk_data[ticker].copy()
            except KeyError:
                if isinstance(bulk_data.columns, pd.MultiIndex):
                    try:
                          df = bulk_data.xs(ticker, level=1, axis=1)
                    except Exception:
                          continue
                else:
                    continue
            
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)

            if df['Close'].isnull().all(): continue
            df.dropna(subset=['Close'], inplace=True)

            # --- CALCULATE INDICATORS ---
            df['EMA_50'] = ta.ema(df['Close'], length=50)
            df['EMA_21'] = ta.ema(df['Close'], length=21) # New
            df['EMA_9'] = ta.ema(df['Close'], length=9)   # New
            df['RSI'] = ta.rsi(df['Close'], length=14)
            
            # --- MACD ---
            macd = ta.macd(df['Close'])
            if macd is not None:
                hist_cols = [c for c in macd.columns if c.startswith('MACDh')]
                if not hist_cols: continue
                hist_col = hist_cols[0]
                df['MACD_H'] = macd[hist_col]
            else:
                continue

            bb = ta.bbands(df['Close'], length=20, std=2)
            if bb is not None and not bb.empty:
                df['BBL'] = bb.iloc[:, 0]
                df['BBM'] = bb.iloc[:, 1]
                df['BBU'] = bb.iloc[:, 2]
                df['BB_WIDTH'] = (df['BBU'] - df['BBL']) / df['BBM']
            else:
                df['BBL'] = pd.NA
                df['BB_WIDTH'] = 0
            
            df['ATR'] = ta.atr(df['High'], df['Low'], df['Close'], length=14)

            # --- GET VALUES ---
            if len(df) < 50: continue 
            last = df.iloc[-1]
            prev = df.iloc[-2]
            
            # --- 🛡️ GHOST CANDLE FIX 🛡️ ---
            tz = pytz.timezone('US/Eastern')
            today_date = datetime.now(tz).date()
            candle_date = last.name.date()
            if elapsed_minutes > 20 and candle_date != today_date:
                continue

            if pd.isna(last['BBL']) or pd.isna(last['EMA_50']): continue

            price = float(last['Close'])
            open_price = float(last['Open']) 
            day_high = float(last['High'])
            day_low = float(last['Low'])

            rsi = float(last['RSI'])
            ema_50 = float(last['EMA_50'])
            ema_21 = float(last['EMA_21']) # New
            ema_9 = float(last['EMA_9'])   # New
            bbl = float(last['BBL'])
            bb_width = float(last['BB_WIDTH']) 
            
            macd_h = float(last['MACD_H'])
            prev_macd_h = float(prev['MACD_H'])
            atr = float(last['ATR'])

            # --- 🚦 TREND FILTER (Safety First) ---
            # If price is below 50 EMA, we skip it (unless it's a massive capitulation RSI < 25)
            # This prevents buying "cheap" stocks that are crashing.
            if price < ema_50 and rsi > 30:
                continue

            # --- TRIGGER LOGIC ---
            # We alert if near 21 EMA (Momentum) OR near Bollinger Band (Mean Reversion)
            near_21 = abs(price - ema_21) <= (ema_21 * 0.01) # Within 1% of 21 EMA
            near_bb = abs(price - bbl) <= (bbl * 0.015)
            
            if (near_21 or near_bb) and rsi < 60: # RSI threshold raised slightly for Momentum trades
                
                # Pre-Scan with dummy vol
                dummy_vol = 1.0 
                base_score, _ = calculate_confidence(rsi, price, open_price, day_high, day_low, bbl, bb_width, ema_50, ema_9, ema_21, macd_h, prev_macd_h, dummy_vol, elapsed_minutes)
                
                pre_threshold = 3 + regime_penalty
                if (base_score + 2) < pre_threshold:
                    continue

                # --- DEEP DIVE ---
                rel_vol = get_relative_volume(ticker)
                has_earnings_risk, earnings_msg = get_earnings_warning(ticker)

                # --- HYBRID STOP LOSS (Dynamic) ---
                # If we are bouncing off 21 EMA, Stop is below 21 EMA.
                # If we are bouncing off BBL, Stop is below BBL.
                support_level = bbl if near_bb else ema_21
                
                stop_buffer = atr * 0.5
                stop_loss = support_level - stop_buffer
                if stop_loss >= price: stop_loss = price - atr 

                take_profit = price + (atr * 2.0)
                risk_per_share = price - stop_loss
                reward_per_share = take_profit - price
                rr_ratio = reward_per_share / risk_per_share if risk_per_share > 0 else 0.0

                # Final Score
                score, reasons = calculate_confidence(rsi, price, open_price, day_high, day_low, bbl, bb_width, ema_50, ema_9, ema_21, macd_h, prev_macd_h, rel_vol, elapsed_minutes)
                
                if has_earnings_risk: score -= 2

                min_score_needed = 6 
                if elapsed_minutes < 60: min_score_needed = 7 
                
                is_friday = datetime.now(tz).weekday() == 4
                if is_friday and elapsed_minutes > 270: min_score_needed += 1
                min_score_needed += regime_penalty
                
                if rr_ratio < 1.5:
                     print(f"📉 {ticker} Skipped: Poor Risk/Reward ({rr_ratio:.2f})")
                     continue

                print(f"🔎 Checking {ticker}: Score {score}/{min_score_needed} (RVAT: {rel_vol:.2f}x) (RR: {rr_ratio:.2f})")
                
                if score >= min_score_needed:
                    send_discord_alert(ticker, price, rsi, ema_21, ema_50, stop_loss, take_profit, score, reasons, min_score_needed, rel_vol, earnings_msg, open_price, day_high, day_low, elapsed_minutes)

        except Exception as e:
            print(f"Error processing {ticker}: {e}")

if __name__ == "__main__":
    check_market()
