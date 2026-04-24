# =========================
# IMPORTS
# =========================
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

MIN_SCORE = 70
FULL_SCORE = 85

# =========================
# ASSETS
# =========================
ASSETS = {
    "BTC": {
        "name": "BTC",
        "binance": "BTCUSDT",
        "yf": "BTC-USD",
    },
    "GOLD": {
        "name": "GOLD",
        "yf": "GC=F",
        "td": "XAU/USD",
    },
}

# =========================
# CONFIG PER ASSET
# =========================
CFG = {
    "BTC": {"SL": 2.6, "TP": 5.2},
    "GOLD": {"SL": 1.6, "TP": 3.2},
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
        "LAST_TRADE": 0,
    } for k in ASSETS
}

# =========================
# TELEGRAM (BULLETPROOF)
# =========================
def send(msg):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        print(msg)
        return

    for _ in range(5):
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                json={"chat_id": CHAT_ID, "text": msg},
                timeout=10,
            )
            if r.status_code == 200:
                return
        except:
            pass
        time.sleep(2)

# =========================
# DATA FEEDS
# =========================
def get_binance(symbol):
    try:
        r = requests.get(
            "https://api.binance.com/api/v3/klines",
            params={"symbol": symbol, "interval": "1m", "limit": 500},
            timeout=10,
        )
        data = r.json()
        df = pd.DataFrame(data)[[1,2,3,4,5]]
        df.columns = ["open","high","low","close","volume"]
        return df.astype(float)
    except:
        return pd.DataFrame()

def get_yf(symbol):
    try:
        df = yf.download(symbol, period="7d", interval="1m", progress=False)
        df.columns = [c.lower() for c in df.columns]
        return df[["open","high","low","close","volume"]]
    except:
        return pd.DataFrame()

def get_data(asset):
    if asset == "BTC":
        df = get_binance(ASSETS["BTC"]["binance"])
        if not df.empty:
            return df, "BINANCE"

    df = get_yf(ASSETS[asset]["yf"])
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
        return "BULL"
    if r["ema9"] < r["ema21"]:
        return "BEAR"
    return "CHOP"

# =========================
# SIGNAL
# =========================
def get_signal(asset):
    df, feed = get_data(asset)
    df = add_indicators(df)

    if df.empty:
        return None, feed

    r = df.iloc[-1]
    t = trend(df)

    long = (
        t == "BULL"
        and r["close"] > r["hh"]
        and r["rsi"] < 70
    )

    short = (
        t == "BEAR"
        and r["close"] < r["ll"]
        and r["rsi"] > 30
    )

    return {
        "price": r["close"],
        "atr": r["atr"],
        "trend": t,
        "long": long,
        "short": short,
    }, feed

# =========================
# HEARTBEAT
# =========================
def heartbeat(asset, sig, feed):
    s = STATE[asset]

    if time.time() - s["LAST_HEARTBEAT"] < HEARTBEAT_SECONDS:
        return

    if sig:
        send(f"💓 {asset} | {sig['trend']} | {sig['price']:.2f}")
    else:
        send(f"💓 {asset} | NO DATA")

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

    s.update({
        "IN_TRADE": True,
        "SIDE": side,
        "ENTRY": price,
        "SL": sl,
        "TP": tp,
        "LAST_TRADE": time.time(),
    })

    send(f"{asset} {side}\nEntry:{price:.2f}\nSL:{sl:.2f}\nTP:{tp:.2f}")

# =========================
# MAIN
# =========================
def run():
    time.sleep(5)
    send("🔥 BTC + GOLD BOT LIVE 🔥")

    while True:
        try:
            for asset in ASSETS:
                sig, feed = get_signal(asset)

                heartbeat(asset, sig, feed)

                if sig is None:
                    continue

                if STATE[asset]["IN_TRADE"]:
                    continue

                if time.time() - STATE[asset]["LAST_TRADE"] < COOLDOWN_SECONDS:
                    continue

                if sig["long"]:
                    start_trade(asset, "LONG", sig)
                elif sig["short"]:
                    start_trade(asset, "SHORT", sig)

            time.sleep(CHECK_INTERVAL)

        except Exception as e:
            send(f"ERROR: {e}")
            time.sleep(10)

if __name__ == "__main__":
    run()
