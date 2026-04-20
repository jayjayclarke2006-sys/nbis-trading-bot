import os
import time
import requests
import pandas as pd
import numpy as np

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID") or os.getenv("CHAT_ID")

IN_TRADE = False
TRADE_SIDE = None
ENTRY_PRICE = 0.0
STOP_LOSS = 0.0
TAKE_PROFIT = 0.0

# =========================
# TELEGRAM
# =========================
def send(msg):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": CHAT_ID, "text": msg}, timeout=10)
    except:
        print(msg)

# =========================
# DATA
# =========================
def get_klines(interval):
    try:
        url = f"https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval={interval}&limit=120"
        data = requests.get(url, timeout=10).json()

        # ✅ FIX: protect against bad API response
        if not isinstance(data, list) or len(data) == 0:
            return pd.DataFrame()

        df = pd.DataFrame(data, columns=[
            "time","open","high","low","close","volume",
            "ct","qav","nt","tbv","tqv","ignore"
        ])

        for col in ["open","high","low","close","volume"]:
            df[col] = df[col].astype(float)

        return df

    except:
        return pd.DataFrame()

# =========================
# INDICATORS
# =========================
def ema(df, span):
    return df["close"].ewm(span=span).mean()

def rsi(df, period=14):
    delta = df["close"].diff()
    gain = (delta.where(delta > 0, 0)).rolling(period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(period).mean()
    rs = gain / loss
    return 100 - (100 / (1 + rs))

def atr(df, period=14):
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - df["close"].shift()).abs(),
        (df["low"] - df["close"].shift()).abs()
    ], axis=1).max(axis=1)
    return tr.rolling(period).mean()

def add_indicators(df):
    if df.empty:
        return df

    df["ema9"] = ema(df, 9)
    df["ema21"] = ema(df, 21)
    df["ema50"] = ema(df, 50)
    df["rsi"] = rsi(df)
    df["atr"] = atr(df)
    df.dropna(inplace=True)
    return df

# =========================
# SIGNAL ENGINE
# =========================
def get_signal():
    df1 = add_indicators(get_klines("1m"))
    df5 = add_indicators(get_klines("5m"))

    # ✅ FIX: prevent crash if not enough data
    if df1.empty or df5.empty or len(df1) < 50 or len(df5) < 50:
        return None

    price = df1.iloc[-1]["close"]
    atr_val = df1.iloc[-1]["atr"]

    # SIMPLE SCORING (clean + stable)
    long_score = 0
    short_score = 0

    if df5.iloc[-1]["ema9"] > df5.iloc[-1]["ema21"]:
        long_score += 30
    else:
        short_score += 30

    if df1.iloc[-1]["rsi"] > 55:
        long_score += 20
    if df1.iloc[-1]["rsi"] < 45:
        short_score += 20

    # SNIPER
    sniper_long = df1.iloc[-2]["ema9"] < df1.iloc[-2]["ema21"] and df1.iloc[-1]["ema9"] > df1.iloc[-1]["ema21"]
    sniper_short = df1.iloc[-2]["ema9"] > df1.iloc[-2]["ema21"] and df1.iloc[-1]["ema9"] < df1.iloc[-1]["ema21"]

    # BREAKOUT
    breakout_long = price > df1["high"].rolling(10).max().iloc[-2]
    breakout_short = price < df1["low"].rolling(10).min().iloc[-2]

    return {
        "price": price,
        "atr": atr_val,
        "long_score": long_score,
        "short_score": short_score,
        "sniper_long": sniper_long,
        "sniper_short": sniper_short,
        "breakout_long": breakout_long,
        "breakout_short": breakout_short
    }

# =========================
# BOT LOOP
# =========================
def run():
    global IN_TRADE, TRADE_SIDE, ENTRY_PRICE, STOP_LOSS, TAKE_PROFIT

    send("🔥 BTC ELITE SNIPER V3 LIVE 🔥")

    while True:
        try:
            sig = get_signal()

            # ✅ FIX: skip if no data yet
            if sig is None:
                time.sleep(10)
                continue

            price = sig["price"]

            # ENTRY
            if not IN_TRADE:

                if sig["long_score"] >= 60 and (sig["sniper_long"] or sig["breakout_long"]):
                    IN_TRADE = True
                    TRADE_SIDE = "LONG"
                    ENTRY_PRICE = price
                    STOP_LOSS = price - sig["atr"] * 1.5
                    TAKE_PROFIT = price + sig["atr"] * 3

                    send(f"""
🚀 BTC LONG ENTRY

Price: {price}
Score: {sig['long_score']}

SL: {round(STOP_LOSS,2)}
TP: {round(TAKE_PROFIT,2)}
""")

                elif sig["short_score"] >= 60 and (sig["sniper_short"] or sig["breakout_short"]):
                    IN_TRADE = True
                    TRADE_SIDE = "SHORT"
                    ENTRY_PRICE = price
                    STOP_LOSS = price + sig["atr"] * 1.5
                    TAKE_PROFIT = price - sig["atr"] * 3

                    send(f"""
📉 BTC SHORT ENTRY

Price: {price}
Score: {sig['short_score']}

SL: {round(STOP_LOSS,2)}
TP: {round(TAKE_PROFIT,2)}
""")

            # TRADE MANAGEMENT
            else:
                if TRADE_SIDE == "LONG":
                    if price <= STOP_LOSS:
                        send(f"❌ STOP LOSS HIT {price}")
                        IN_TRADE = False
                    elif price >= TAKE_PROFIT:
                        send(f"🎯 TAKE PROFIT HIT {price}")
                        IN_TRADE = False

                if TRADE_SIDE == "SHORT":
                    if price >= STOP_LOSS:
                        send(f"❌ STOP LOSS HIT {price}")
                        IN_TRADE = False
                    elif price <= TAKE_PROFIT:
                        send(f"🎯 TAKE PROFIT HIT {price}")
                        IN_TRADE = False

            time.sleep(60)

        except Exception as e:
            send(f"BTC BOT ERROR: {e}")
            time.sleep(10)

# =========================
# RUN
# =========================
if __name__ == "__main__":
    run()
