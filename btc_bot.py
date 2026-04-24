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

# =========================
# CONFIG
# =========================
CHECK_INTERVAL = 60
HEARTBEAT_SECONDS = 1800
COOLDOWN_SECONDS = 600
DEBUG_MODE = True

MIN_SCORE = 75
FULL_SCORE = 85

# =========================
# ASSETS
# =========================
ASSETS = {
    "BTC": {"binance": "BTCUSDT", "yf": "BTC-USD"},
    "GOLD": {"yf": "GC=F"},
}

# =========================
# CONFIG
# =========================
CFG = {
    "BTC": {
        "SL": 2.8, "TP": 5.5,
        "RSI_L": 68, "RSI_S": 32,
        "BREAK_L": 1.001, "BREAK_S": 0.999,
        "PULL_L": 1.0015, "PULL_S": 0.9985,
    },
    "GOLD": {
        "SL": 1.8, "TP": 3.8,
        "RSI_L": 65, "RSI_S": 35,
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
        "LAST_TRADE": 0,
    } for k in ASSETS
}

# =========================
# TELEGRAM (FIXED)
# =========================
def send(msg):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        print("❌ TELEGRAM:", msg)
        return

    for _ in range(3):
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                json={"chat_id": CHAT_ID, "text": msg},
                timeout=10,
            )
            if r.status_code == 200:
                return
            else:
                print("Telegram fail:", r.text)
        except Exception as e:
            print("Telegram error:", e)
        time.sleep(2)

# =========================
# DATA
# =========================
def get_binance(symbol):
    try:
        r = requests.get(
            "https://api.binance.com/api/v3/klines",
            params={"symbol": symbol, "interval": "1m", "limit": 200},
            timeout=10,
        )
        df = pd.DataFrame(r.json())[[1,2,3,4,5]]
        df.columns = ["open","high","low","close","volume"]
        return df.astype(float)
    except:
        return pd.DataFrame()

def get_yf(ticker):
    try:
        df = yf.download(ticker, period="7d", interval="1m", progress=False)
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
    df["rsi"] = df["close"].pct_change().rolling(14).mean() * 100
    df["atr"] = (df["high"] - df["low"]).rolling(14).mean()
    df["hh"] = df["high"].rolling(10).max().shift(1)
    df["ll"] = df["low"].rolling(10).min().shift(1)
    df.dropna(inplace=True)
    return df

# =========================
# SIGNAL
# =========================
def get_signal(asset):
    df, feed = get_data(asset)
    df = add_indicators(df)

    if df.empty:
        return None, feed

    r = df.iloc[-1]

    trend = "BULLISH" if r["ema9"] > r["ema21"] else "BEARISH"

    long_score = 0
    short_score = 0

    if trend == "BULLISH":
        long_score += 30
    if trend == "BEARISH":
        short_score += 30

    if r["close"] > r["ema9"]:
        long_score += 20
    else:
        short_score += 20

    if 50 < r["rsi"] < CFG[asset]["RSI_L"]:
        long_score += 20

    if CFG[asset]["RSI_S"] < r["rsi"] < 50:
        short_score += 20

    breakout_long = r["close"] > r["hh"] * CFG[asset]["BREAK_L"]
    breakout_short = r["close"] < r["ll"] * CFG[asset]["BREAK_S"]

    pullback_long = r["close"] <= r["ema9"] * CFG[asset]["PULL_L"]
    pullback_short = r["close"] >= r["ema9"] * CFG[asset]["PULL_S"]

    return {
        "price": r["close"],
        "atr": r["atr"],
        "trend": trend,
        "long_score": long_score,
        "short_score": short_score,
        "long_break": breakout_long,
        "short_break": breakout_short,
        "long_pull": pullback_long,
        "short_pull": pullback_short,
    }, feed

# =========================
# HEARTBEAT
# =========================
def heartbeat(asset, sig, feed):
    s = STATE[asset]

    if time.time() - s["LAST_HEARTBEAT"] < HEARTBEAT_SECONDS:
        return

    if sig is None:
        send(f"💓 {asset} HEARTBEAT\nNO DATA\nFeed:{feed}")
    else:
        send(
            f"💓 {asset} HEARTBEAT\n"
            f"Price:{sig['price']:.2f}\n"
            f"Trend:{sig['trend']}\n"
            f"Feed:{feed}"
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

    sl = price - atr * cfg["SL"] if side == "LONG" else price + atr * cfg["SL"]
    tp = price + atr * cfg["TP"] if side == "LONG" else price - atr * cfg["TP"]

    s.update({
        "IN_TRADE": True,
        "SIDE": side,
        "ENTRY": price,
        "SL": sl,
        "TP": tp,
        "LAST_TRADE": time.time(),
    })

    send(
        f"{'🚀' if side=='LONG' else '📉'} {asset} {side}\n"
        f"Entry:{price:.2f}\nSL:{sl:.2f}\nTP:{tp:.2f}"
    )

# =========================
# MANAGE
# =========================
def manage(asset, sig):
    s = STATE[asset]
    p = sig["price"]

    if s["SIDE"] == "LONG":
        if p <= s["SL"]:
            send(f"❌ {asset} STOP")
            s["IN_TRADE"] = False
        elif p >= s["TP"]:
            send(f"🎯 {asset} TP")
            s["IN_TRADE"] = False

    if s["SIDE"] == "SHORT":
        if p >= s["SL"]:
            send(f"❌ {asset} STOP")
            s["IN_TRADE"] = False
        elif p <= s["TP"]:
            send(f"🎯 {asset} TP")
            s["IN_TRADE"] = False

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
                    manage(asset, sig)
                    continue

                if time.time() - STATE[asset]["LAST_TRADE"] < COOLDOWN_SECONDS:
                    continue

                if sig["long_score"] >= MIN_SCORE and (sig["long_break"] or sig["long_pull"]):
                    start_trade(asset, "LONG", sig)

                elif sig["short_score"] >= MIN_SCORE and (sig["short_break"] or sig["short_pull"]):
                    start_trade(asset, "SHORT", sig)

            time.sleep(CHECK_INTERVAL)

        except Exception as e:
            send(f"ERROR: {e}")
            time.sleep(10)

if __name__ == "__main__":
    run()
