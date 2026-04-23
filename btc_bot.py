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
HEARTBEAT_SECONDS = 300
COOLDOWN_SECONDS = 600
DEBUG = True

# =========================
# ASSETS
# =========================
ASSETS = {
    "BTC": {"name": "BTC", "binance": "BTCUSDT", "yf": "BTC-USD"},
    "GOLD": {"name": "GOLD", "yf": "GC=F"},
}

# =========================
# CONFIG (FIXED + COMPLETE)
# =========================
CFG = {
    "BTC": {
        "SL": 2.0,
        "TP": 4.5,
        "BE": 1.3,
        "PARTIAL": 2.0,
        "TRAIL": 2.2,
        "VOL": 0.0008,
        "EMA_DIST": 0.007,
        "RSI_HIGH": 70,
        "RSI_LOW": 30,
        "PULLBACK_LONG": 1.002,
        "PULLBACK_SHORT": 0.998,
    },
    "GOLD": {
        "SL": 1.3,
        "TP": 3.0,
        "BE": 1.0,
        "PARTIAL": 1.5,
        "TRAIL": 1.6,
        "VOL": 0.00015,
        "EMA_DIST": 0.0045,
        "RSI_HIGH": 66,
        "RSI_LOW": 34,
        "PULLBACK_LONG": 1.0015,
        "PULLBACK_SHORT": 0.9985,
    },
}

# =========================
# STATE
# =========================
STATE = {
    k: {
        "in_trade": False,
        "side": None,
        "entry": 0,
        "sl": 0,
        "tp": 0,
        "last_hb": 0,
    }
    for k in ASSETS
}

# =========================
# TELEGRAM
# =========================
def send(msg):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        print(msg)
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": msg},
            timeout=10,
        )
    except:
        pass

# =========================
# DATA (BULLETPROOF)
# =========================
def get_binance(symbol):
    try:
        url = "https://api.binance.com/api/v3/klines"
        r = requests.get(url, params={"symbol": symbol, "interval": "1m", "limit": 200}, timeout=10)
        d = r.json()
        df = pd.DataFrame(d)[[1,2,3,4,5]]
        df.columns = ["open","high","low","close","volume"]
        df = df.astype(float)
        return df
    except:
        return pd.DataFrame()

def get_yf(ticker):
    try:
        df = yf.download(ticker, period="7d", interval="1m", progress=False)
        if df.empty:
            return df
        df.columns = [c.lower() for c in df.columns]
        return df[["open","high","low","close","volume"]]
    except:
        return pd.DataFrame()

def get_data(asset):
    if asset == "BTC":
        df = get_binance(ASSETS[asset]["binance"])
        if not df.empty:
            return df, "BINANCE"

    df = get_yf(ASSETS[asset]["yf"])
    if not df.empty:
        return df, "YFINANCE"

    return pd.DataFrame(), "NONE"

# =========================
# INDICATORS
# =========================
def ema(df, n): return df["close"].ewm(span=n).mean()

def rsi(df):
    d = df["close"].diff()
    up = d.clip(lower=0).rolling(14).mean()
    down = (-d.clip(upper=0)).rolling(14).mean()
    return 100 - (100/(1+up/down))

def atr(df):
    tr = pd.concat([
        df["high"]-df["low"],
        (df["high"]-df["close"].shift()).abs(),
        (df["low"]-df["close"].shift()).abs()
    ], axis=1).max(axis=1)
    return tr.rolling(14).mean()

def add(df):
    if len(df)<30: return pd.DataFrame()
    df = df.copy()
    df["ema9"]=ema(df,9)
    df["ema21"]=ema(df,21)
    df["rsi"]=rsi(df)
    df["atr"]=atr(df)
    df.dropna(inplace=True)
    return df

# =========================
# LOGIC
# =========================
def trend(df):
    r=df.iloc[-1]
    if r.ema9>r.ema21: return "BULL"
    if r.ema9<r.ema21: return "BEAR"
    return "CHOP"

def entry_ok(asset,df):
    r=df.iloc[-1]
    cfg=CFG[asset]
    if r.rsi>cfg["RSI_HIGH"] or r.rsi<cfg["RSI_LOW"]:
        return False
    if abs(r.close-r.ema9)/r.close>cfg["EMA_DIST"]:
        return False
    return True

# =========================
# TRADE
# =========================
def enter(asset,side,price,atrv):
    cfg=CFG[asset]
    st=STATE[asset]

    st["in_trade"]=True
    st["side"]=side
    st["entry"]=price

    if side=="LONG":
        st["sl"]=price-atrv*cfg["SL"]
        st["tp"]=price+atrv*cfg["TP"]
        send(f"🚀 {asset} LONG\nPrice: {price:.2f}\nSL: {st['sl']:.2f}\nTP: {st['tp']:.2f}")
    else:
        st["sl"]=price+atrv*cfg["SL"]
        st["tp"]=price-atrv*cfg["TP"]
        send(f"📉 {asset} SHORT\nPrice: {price:.2f}\nSL: {st['sl']:.2f}\nTP: {st['tp']:.2f}")

def manage(asset,price):
    st=STATE[asset]
    if not st["in_trade"]: return

    if st["side"]=="LONG":
        if price<=st["sl"]:
            send(f"❌ {asset} SL HIT")
            st["in_trade"]=False
        elif price>=st["tp"]:
            send(f"🎯 {asset} TP HIT")
            st["in_trade"]=False

    else:
        if price>=st["sl"]:
            send(f"❌ {asset} SL HIT")
            st["in_trade"]=False
        elif price<=st["tp"]:
            send(f"🎯 {asset} TP HIT")
            st["in_trade"]=False

# =========================
# HEARTBEAT
# =========================
def heartbeat(asset,price,trendv,feed):
    st=STATE[asset]
    if time.time()-st["last_hb"]<HEARTBEAT_SECONDS:
        return
    if price is None:
        send(f"💓 {asset}\nNO DATA\nFeed: {feed}")
    else:
        send(f"💓 {asset}\nPrice: {price:.2f}\nTrend: {trendv}\nFeed: {feed}")
    st["last_hb"]=time.time()

# =========================
# LOOP
# =========================
def run():
    send("🔥 BTC + GOLD BOT LIVE 🔥")

    while True:
        try:
            for asset in ASSETS:
                df,feed=get_data(asset)
                df=add(df)

                if df.empty:
                    heartbeat(asset,None,"NONE",feed)
                    continue

                price=float(df.iloc[-1].close)
                atrv=float(df.iloc[-1].atr)
                tr=trend(df)

                heartbeat(asset,price,tr,feed)

                if not STATE[asset]["in_trade"]:
                    if entry_ok(asset,df):
                        if tr=="BULL":
                            enter(asset,"LONG",price,atrv)
                        elif tr=="BEAR":
                            enter(asset,"SHORT",price,atrv)
                else:
                    manage(asset,price)

            time.sleep(CHECK_INTERVAL)

        except Exception as e:
            send(f"BOT ERROR: {e}")
            time.sleep(10)

if __name__=="__main__":
    run()
