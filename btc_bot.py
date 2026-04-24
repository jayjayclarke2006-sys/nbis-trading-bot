import os
import time
import requests
import pandas as pd
import yfinance as yf

# =========================
# ENV
# =========================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
TWELVEDATA_API_KEY = os.getenv("TWELVEDATA_API_KEY")

# =========================
# CONFIG
# =========================
CHECK_INTERVAL = 60
HEARTBEAT_SECONDS = 1800
COOLDOWN_SECONDS = 600
DEBUG_MODE = True

MIN_ENTRY_SCORE = 75
FULL_SIZE_SCORE = 85

# =========================
# ASSETS
# =========================
ASSETS = {
    "BTC": {
        "name": "BTC",
        "binance": "BTCUSDT",
        "yfinance": "BTC-USD",
    },
    "GOLD": {
        "name": "GOLD",
        "td": "XAU/USD",
        "yfinance": "GC=F",
    },
}

# =========================
# CONFIG (NO MISSING KEYS)
# =========================
CFG = {
    "BTC": {
        "SL": 2.8, "TP": 5.5, "TRAIL": 2.5,
        "BE": 1.6, "PARTIAL": 2.4,
        "VOL": 0.0008, "EMA_DIST": 0.006,
        "LONG_RSI": 68, "SHORT_RSI": 32,
        "BREAK_L": 1.001, "BREAK_S": 0.999,
        "PULL_L": 1.0015, "PULL_S": 0.9985,
    },
    "GOLD": {
        "SL": 1.8, "TP": 3.8, "TRAIL": 2.0,
        "BE": 1.3, "PARTIAL": 1.8,
        "VOL": 0.00015, "EMA_DIST": 0.004,
        "LONG_RSI": 65, "SHORT_RSI": 35,
        "BREAK_L": 1.0005, "BREAK_S": 0.9995,
        "PULL_L": 1.0012, "PULL_S": 0.9988,
    },
}

# =========================
# STATE
# =========================
STATE = {
    k: {
        "IN_TRADE": False,
        "SIDE": None,
        "ENTRY": 0,
        "SL": 0,
        "TP": 0,
        "LAST_HEARTBEAT": 0,
    }
    for k in ASSETS
}

# =========================
# TELEGRAM
# =========================
def send(msg):
    try:
        if not TELEGRAM_TOKEN:
            print(msg)
            return
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": msg},
            timeout=10,
        )
    except:
        print("Telegram fail")

# =========================
# DATA (BTC FIXED)
# =========================
def get_binance(symbol, interval):
    try:
        r = requests.get(
            "https://api.binance.com/api/v3/klines",
            params={"symbol": symbol, "interval": interval, "limit": 200},
            timeout=10,
        )
        data = r.json()
        df = pd.DataFrame(data)[[1,2,3,4,5]]
        df.columns = ["open","high","low","close","volume"]
        df = df.astype(float)
        return df
    except:
        return pd.DataFrame()

def get_yf(ticker, interval):
    try:
        df = yf.download(ticker, period="7d", interval=interval, progress=False)
        df.columns = [c.lower() for c in df.columns]
        return df[["open","high","low","close","volume"]]
    except:
        return pd.DataFrame()

def get_data(asset, interval):
    if asset == "BTC":
        df = get_binance(ASSETS["BTC"]["binance"], interval)
        if not df.empty:
            return df, "BINANCE"
        df = get_yf(ASSETS["BTC"]["yfinance"], interval)
        if not df.empty:
            return df, "YFINANCE"
    else:
        df = get_yf(ASSETS["GOLD"]["yfinance"], interval)
        if not df.empty:
            return df, "YFINANCE"
    return pd.DataFrame(), "NONE"

# =========================
# INDICATORS
# =========================
def add_indicators(df):
    if df.empty or len(df) < 50:
        return pd.DataFrame()
    df["ema9"] = df["close"].ewm(span=9).mean()
    df["ema21"] = df["close"].ewm(span=21).mean()
    df["ema50"] = df["close"].ewm(span=50).mean()
    df["rsi"] = df["close"].pct_change().rolling(14).mean() * 100
    df["atr"] = (df["high"] - df["low"]).rolling(14).mean()
    df["hh"] = df["high"].rolling(10).max().shift(1)
    df["ll"] = df["low"].rolling(10).min().shift(1)
    df.dropna(inplace=True)
    return df

# =========================
# TREND
# =========================
def trend(df):
    r = df.iloc[-1]
    if r["ema9"] > r["ema21"]:
        return "BULLISH"
    if r["ema9"] < r["ema21"]:
        return "BEARISH"
    return "CHOPPY"

# =========================
# SIGNAL
# =========================
def get_signal(asset):
    df, feed = get_data(asset, "1m")
    df = add_indicators(df)
    if df.empty:
        return None, feed

    r = df.iloc[-1]
    price = r["close"]

    t = trend(df)

    long = (
        t == "BULLISH"
        and price > r["ema9"]
        and r["rsi"] < CFG[asset]["LONG_RSI"]
        and price > r["hh"] * CFG[asset]["BREAK_L"]
    )

    short = (
        t == "BEARISH"
        and price < r["ema9"]
        and r["rsi"] > CFG[asset]["SHORT_RSI"]
        and price < r["ll"] * CFG[asset]["BREAK_S"]
    )

    return {
        "price": price,
        "atr": r["atr"],
        "long": long,
        "short": short,
        "trend": t,
    }, feed

# =========================
# HEARTBEAT
# =========================
def heartbeat(asset, sig, feed):
    s = STATE[asset]
    if time.time() - s["LAST_HEARTBEAT"] < HEARTBEAT_SECONDS:
        return

    if sig is None:
        send(f"💓 {asset} HEARTBEAT\nStatus: NO DATA\nFeed: {feed}")
    else:
        send(
            f"💓 {asset} HEARTBEAT\n"
            f"Price: {sig['price']:.2f}\n"
            f"Trend: {sig['trend']}\n"
            f"In trade: {s['IN_TRADE']}\n"
            f"Feed: {feed}"
        )

    s["LAST_HEARTBEAT"] = time.time()

# =========================
# TRADE
# =========================
def start_trade(asset, side, sig):
    s = STATE[asset]
    cfg = CFG[asset]

    price = sig["price"]
    atr = sig["atr"]

    if side == "LONG":
        sl = price - atr * cfg["SL"]
        tp = price + atr * cfg["TP"]
    else:
        sl = price + atr * cfg["SL"]
        tp = price - atr * cfg["TP"]

    s["IN_TRADE"] = True
    s["SIDE"] = side
    s["ENTRY"] = price
    s["SL"] = sl
    s["TP"] = tp

    send(
        f"{'🚀' if side=='LONG' else '📉'} {asset} {side} ENTRY\n"
        f"Price: {price:.2f}\nSL: {sl:.2f}\nTP: {tp:.2f}"
    )

# =========================
# MANAGE
# =========================
def manage(asset, sig):
    s = STATE[asset]
    price = sig["price"]

    if s["SIDE"] == "LONG":
        if price <= s["SL"]:
            send(f"❌ {asset} STOP HIT")
            s["IN_TRADE"] = False
        if price >= s["TP"]:
            send(f"🎯 {asset} TP HIT")
            s["IN_TRADE"] = False

    if s["SIDE"] == "SHORT":
        if price >= s["SL"]:
            send(f"❌ {asset} STOP HIT")
            s["IN_TRADE"] = False
        if price <= s["TP"]:
            send(f"🎯 {asset} TP HIT")
            s["IN_TRADE"] = False

# =========================
# MAIN
# =========================
def run():
    send("🔥 BTC + GOLD BOT LIVE 🔥")

    while True:
        try:
            for asset in ASSETS:

                sig, feed = get_signal(asset)
                heartbeat(asset, sig, feed)

                if sig is None:
                    continue

                if STATE[asset]["IN_TRADE"]:
                    manage(asset, sig)
                else:
                    if sig["long"]:
                        start_trade(asset, "LONG", sig)
                    elif sig["short"]:
                        start_trade(asset, "SHORT", sig)

            time.sleep(CHECK_INTERVAL)

        except Exception as e:
            send(f"BOT ERROR: {e}")
            time.sleep(10)

if __name__ == "__main__":
    run()
