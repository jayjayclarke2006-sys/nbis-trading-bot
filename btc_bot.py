import os
import time
import requests
import pandas as pd
import yfinance as yf

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

CHECK_INTERVAL = 60
HEARTBEAT_SECONDS = 1800
COOLDOWN = 1800

ASSETS = {
    "BTC": {"binance": "BTCUSDT", "yf": "BTC-USD"},
    "GOLD": {"binance": None, "yf": "GC=F"},
}

STATE = {k: {"IN": False, "SIDE": None, "ENTRY": 0, "SL": 0,
             "TP1": 0, "TP2": 0, "BE": False,
             "LAST": 0, "HB": 0} for k in ASSETS}

# ============================================================
# TELEGRAM
# ============================================================
def send(msg):
    print(msg)
    if not TELEGRAM_TOKEN or not CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": msg},
            timeout=10
        )
    except:
        pass

# ============================================================
# DATA
# ============================================================
def get_data(asset, interval):
    try:
        if asset == "BTC":
            r = requests.get(
                "https://api.binance.com/api/v3/klines",
                params={"symbol": "BTCUSDT", "interval": interval, "limit": 200},
                timeout=10
            )
            df = pd.DataFrame(r.json())
            df = df[[1,2,3,4,5]].astype(float)
            df.columns = ["o","h","l","c","v"]
        else:
            df = yf.download("GC=F", period="1d", interval=interval, progress=False)
            df = df.rename(columns={"Open":"o","High":"h","Low":"l","Close":"c","Volume":"v"})
        return df.dropna()
    except:
        return pd.DataFrame()

# ============================================================
# INDICATORS
# ============================================================
def ind(df):
    df["ema9"] = df["c"].ewm(span=9).mean()
    df["ema21"] = df["c"].ewm(span=21).mean()
    df["ema50"] = df["c"].ewm(span=50).mean()
    df["atr"] = (df["h"] - df["l"]).rolling(14).mean()
    return df.dropna()

# ============================================================
# STRUCTURE
# ============================================================
def trend(df):
    r = df.iloc[-1]
    if r["ema9"] > r["ema21"] > r["ema50"]:
        return "BULL"
    if r["ema9"] < r["ema21"] < r["ema50"]:
        return "BEAR"
    return "NONE"

def swing_high(df):
    return df["h"].iloc[-20:-1].max()

def swing_low(df):
    return df["l"].iloc[-20:-1].min()

# ============================================================
# LIQUIDITY SWEEP
# ============================================================
def sweep_high(df):
    r = df.iloc[-1]
    return r["h"] > swing_high(df) and r["c"] < r["h"]

def sweep_low(df):
    r = df.iloc[-1]
    return r["l"] < swing_low(df) and r["c"] > r["l"]

# ============================================================
# ENTRY LOGIC
# ============================================================
def long_entry(df):
    r = df.iloc[-1]
    prev = df.iloc[-2]

    sweep = sweep_low(df)
    bos = r["c"] > swing_high(df)
    retest = r["l"] <= swing_high(df)

    engulf = r["c"] > prev["o"] and r["o"] < prev["c"]

    return sweep and bos and retest and engulf

def short_entry(df):
    r = df.iloc[-1]
    prev = df.iloc[-2]

    sweep = sweep_high(df)
    bos = r["c"] < swing_low(df)
    retest = r["h"] >= swing_low(df)

    engulf = r["c"] < prev["o"] and r["o"] > prev["c"]

    return sweep and bos and retest and engulf

# ============================================================
# ENTRY
# ============================================================
def enter(asset, side, price, atr):
    s = STATE[asset]
    s["IN"] = True
    s["SIDE"] = side
    s["ENTRY"] = price

    if side == "LONG":
        s["SL"] = price - atr * 4
        s["TP1"] = price + atr * 4
        s["TP2"] = price + atr * 8
        icon = "🚀"
    else:
        s["SL"] = price + atr * 4
        s["TP1"] = price - atr * 4
        s["TP2"] = price - atr * 8
        icon = "📉"

    send(
        f"{icon} {asset} {side} ENTRY\n\n"
        f"Smart Money Setup\n"
        f"Price: {price:.2f}\n\n"
        f"SL: {s['SL']:.2f}\n"
        f"TP1: {s['TP1']:.2f}\n"
        f"TP2: {s['TP2']:.2f}"
    )

# ============================================================
# MANAGEMENT
# ============================================================
def manage(asset, price, atr):
    s = STATE[asset]

    if s["SIDE"] == "LONG":
        if not s["BE"] and price >= s["TP1"]:
            s["SL"] = s["ENTRY"]
            s["BE"] = True
            send(f"⚡ {asset} LONG BREAK EVEN")

        if price <= s["SL"]:
            send(f"❌ {asset} LONG STOP {price:.2f}")
            s["IN"] = False

        if price >= s["TP2"]:
            send(f"🎯 {asset} LONG TP HIT {price:.2f}")
            s["IN"] = False

    if s["SIDE"] == "SHORT":
        if not s["BE"] and price <= s["TP1"]:
            s["SL"] = s["ENTRY"]
            s["BE"] = True
            send(f"⚡ {asset} SHORT BREAK EVEN")

        if price >= s["SL"]:
            send(f"❌ {asset} SHORT STOP {price:.2f}")
            s["IN"] = False

        if price <= s["TP2"]:
            send(f"🎯 {asset} SHORT TP HIT {price:.2f}")
            s["IN"] = False

# ============================================================
# HEARTBEAT
# ============================================================
def hb(asset, price):
    s = STATE[asset]
    if time.time() - s["HB"] > HEARTBEAT_SECONDS:
        send(f"💓 {asset} LIVE\nPrice: {price:.2f}")
        s["HB"] = time.time()

# ============================================================
# MAIN
# ============================================================
def run():
    send("🔥 ELITE SMC BOT LIVE 🔥")

    while True:
        try:
            for asset in ASSETS:

                df1 = ind(get_data(asset, "1m"))
                df5 = ind(get_data(asset, "5m"))
                df15 = ind(get_data(asset, "15m"))

                if df1.empty or df5.empty or df15.empty:
                    continue

                price = df1.iloc[-1]["c"]
                atr = df1.iloc[-1]["atr"]

                hb(asset, price)

                if STATE[asset]["IN"]:
                    manage(asset, price, atr)
                    continue

                if time.time() - STATE[asset]["LAST"] < COOLDOWN:
                    continue

                t = trend(df15)

                if t == "BULL" and long_entry(df1):
                    enter(asset, "LONG", price, atr)
                    STATE[asset]["LAST"] = time.time()

                elif t == "BEAR" and short_entry(df1):
                    enter(asset, "SHORT", price, atr)
                    STATE[asset]["LAST"] = time.time()

            time.sleep(CHECK_INTERVAL)

        except Exception as e:
            send(f"🚨 ERROR: {e}")
            time.sleep(5)

if __name__ == "__main__":
    run()
