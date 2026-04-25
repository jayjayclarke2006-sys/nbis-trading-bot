import os
import time
import requests
import pandas as pd
import yfinance as yf
from datetime import datetime

# =========================
# TELEGRAM
# =========================
TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

def send(msg):
    print(msg)
    if not TOKEN or not CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": msg},
            timeout=10
        )
    except:
        pass

# =========================
# CONFIG
# =========================
CHECK_INTERVAL = 60
HEARTBEAT = 1800
COOLDOWN = 1800

ASSETS = {
    "BTC": {"binance": "BTCUSDT", "yf": "BTC-USD"},
    "GOLD": {"binance": None, "yf": "GC=F"}
}

STATE = {
    k: {"IN_TRADE": False, "LAST_HEART": 0, "LAST_TRADE": 0}
    for k in ASSETS
}

# =========================
# DATA
# =========================
def get_binance(symbol, interval):
    try:
        r = requests.get(
            "https://api.binance.com/api/v3/klines",
            params={"symbol": symbol, "interval": interval, "limit": 200},
            timeout=10
        )
        data = r.json()
        df = pd.DataFrame(data)
        df = df[[1,2,3,4,5]]
        df.columns = ["open","high","low","close","volume"]
        df = df.astype(float)
        return df
    except:
        return pd.DataFrame()

def get_yf(symbol, interval):
    try:
        df = yf.download(symbol, period="7d", interval=interval, progress=False)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df.columns = [c.lower() for c in df.columns]
        return df[["open","high","low","close","volume"]]
    except:
        return pd.DataFrame()

def get_data(asset, interval):
    if asset == "BTC":
        df = get_binance(ASSETS[asset]["binance"], interval)
        if not df.empty:
            return df
    return get_yf(ASSETS[asset]["yf"], interval)

# =========================
# INDICATORS
# =========================
def add_indicators(df):
    if len(df) < 50:
        return pd.DataFrame()

    df["ema9"] = df["close"].ewm(span=9).mean()
    df["ema21"] = df["close"].ewm(span=21).mean()

    tr = pd.concat([
        df["high"]-df["low"],
        (df["high"]-df["close"].shift()).abs(),
        (df["low"]-df["close"].shift()).abs()
    ], axis=1).max(axis=1)

    df["atr"] = tr.rolling(14).mean()
    df["body"] = abs(df["close"]-df["open"])
    df["upper"] = df["high"] - df[["open","close"]].max(axis=1)
    df["lower"] = df[["open","close"]].min(axis=1) - df["low"]

    return df.dropna()

# =========================
# LOGIC
# =========================
def trend(df5, df15):
    if df15.iloc[-1]["ema9"] > df15.iloc[-1]["ema21"]:
        return "BULL"
    if df15.iloc[-1]["ema9"] < df15.iloc[-1]["ema21"]:
        return "BEAR"
    return "CHOP"

def breakout(df):
    r = df.iloc[-1]
    return r["close"] > df["high"].iloc[-20:-1].max()

def breakdown(df):
    r = df.iloc[-1]
    return r["close"] < df["low"].iloc[-20:-1].min()

def pullback_long(df):
    r = df.iloc[-1]
    return r["low"] <= r["ema21"] and r["close"] > r["open"]

def pullback_short(df):
    r = df.iloc[-1]
    return r["high"] >= r["ema21"] and r["close"] < r["open"]

def candle_long(df):
    r = df.iloc[-1]
    return r["lower"] > r["body"] or r["close"] > df.iloc[-2]["high"]

def candle_short(df):
    r = df.iloc[-1]
    return r["upper"] > r["body"] or r["close"] < df.iloc[-2]["low"]

# =========================
# HEARTBEAT
# =========================
def heartbeat(asset, price=None):
    s = STATE[asset]
    if time.time() - s["LAST_HEART"] < HEARTBEAT:
        return

    if price:
        send(f"💓 {asset} HEARTBEAT\nPrice: ${price:.2f}")
    else:
        send(f"💓 {asset} WAITING DATA")

    s["LAST_HEART"] = time.time()

# =========================
# TRADE
# =========================
def trade(asset, df1, df5, df15):
    s = STATE[asset]

    price = df1.iloc[-1]["close"]
    atr = df1.iloc[-1]["atr"]

    heartbeat(asset, price)

    if s["IN_TRADE"]:
        return

    if time.time() - s["LAST_TRADE"] < COOLDOWN:
        return

    t = trend(df5, df15)

    # LONG
    if (
        t == "BULL"
        and breakout(df1)
        and pullback_long(df1)
        and candle_long(df1)
    ):
        sl = price - atr * 3
        tp = price + atr * 6

        send(f"🚀 {asset} LONG\nPrice: {price}\nSL: {sl}\nTP: {tp}")

        s["IN_TRADE"] = True
        s["LAST_TRADE"] = time.time()

    # SHORT
    elif (
        t == "BEAR"
        and breakdown(df1)
        and pullback_short(df1)
        and candle_short(df1)
    ):
        sl = price + atr * 3
        tp = price - atr * 6

        send(f"📉 {asset} SHORT\nPrice: {price}\nSL: {sl}\nTP: {tp}")

        s["IN_TRADE"] = True
        s["LAST_TRADE"] = time.time()

# =========================
# MAIN
# =========================
def run():
    send("✅ BOT STARTING...")
    send(f"🔥 BTC + GOLD BOT LIVE 🔥\nTime: {time.strftime('%H:%M:%S')}")

    while True:
        try:
            for asset in ASSETS:
                df1 = add_indicators(get_data(asset, "1m"))
                df5 = add_indicators(get_data(asset, "5m"))
                df15 = add_indicators(get_data(asset, "15m"))

                if df1.empty or df5.empty or df15.empty:
                    heartbeat(asset)
                    continue

                trade(asset, df1, df5, df15)

            time.sleep(CHECK_INTERVAL)

        except Exception as e:
            send(f"🚨 ERROR: {e}")
            time.sleep(10)

if __name__ == "__main__":
    run()
