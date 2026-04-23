import os
import time
import requests
import pandas as pd
import yfinance as yf

# =========================
# ENV
# =========================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID") or os.getenv("TELEGRAM_CHAT_ID")
TWELVEDATA_API_KEY = os.getenv("TWELVEDATA_API_KEY")

CHECK_INTERVAL = 60
HEARTBEAT_SECONDS = 300
COOLDOWN_SECONDS = 600
DEBUG_MODE = True

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
            timeout=10
        )
    except:
        pass

# =========================
# DATA (BULLETPROOF)
# =========================
def get_binance(symbol, interval):
    try:
        r = requests.get("https://api.binance.com/api/v3/klines",
                         params={"symbol": symbol, "interval": interval, "limit": 200},
                         timeout=10)
        data = r.json()
        if not isinstance(data, list):
            return pd.DataFrame()

        df = pd.DataFrame(data)
        df = df[[1,2,3,4,5]]
        df.columns = ["open","high","low","close","volume"]
        df = df.astype(float)
        return df
    except:
        return pd.DataFrame()

def get_twelve(symbol, interval):
    try:
        if not TWELVEDATA_API_KEY:
            return pd.DataFrame()

        r = requests.get("https://api.twelvedata.com/time_series", params={
            "symbol": symbol,
            "interval": {"1m":"1min","5m":"5min","15m":"15min"}[interval],
            "apikey": TWELVEDATA_API_KEY,
            "outputsize": 200
        }, timeout=10)

        data = r.json()
        if "values" not in data:
            return pd.DataFrame()

        df = pd.DataFrame(data["values"]).iloc[::-1]
        df = df.astype(float)
        return df[["open","high","low","close","volume"]]
    except:
        return pd.DataFrame()

def get_yf(symbol, interval):
    try:
        df = yf.download(symbol, period="7d", interval=interval, progress=False)
        if df.empty:
            return pd.DataFrame()
        df.columns = [c.lower() for c in df.columns]
        return df[["open","high","low","close","volume"]]
    except:
        return pd.DataFrame()

def get_coingecko():
    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/coins/bitcoin/market_chart",
            params={"vs_currency":"usd","days":"1"},
            timeout=10
        )
        data = r.json()["prices"]
        df = pd.DataFrame(data, columns=["t","price"])
        df["open"]=df["price"]
        df["high"]=df["price"]
        df["low"]=df["price"]
        df["close"]=df["price"]
        df["volume"]=1
        return df[["open","high","low","close","volume"]]
    except:
        return pd.DataFrame()

def get_klines(asset, interval):
    if asset == "BTC":
        df = get_binance("BTCUSDT", interval)
        if not df.empty: return df, "BINANCE"

        df = get_twelve("BTC/USD", interval)
        if not df.empty: return df, "TWELVEDATA"

        df = get_yf("BTC-USD", interval)
        if not df.empty: return df, "YFINANCE"

        df = get_coingecko()
        if not df.empty: return df, "COINGECKO"

        return pd.DataFrame(), "NONE"

    if asset == "GOLD":
        df = get_twelve("XAU/USD", interval)
        if not df.empty: return df, "TWELVEDATA"

        df = get_yf("GC=F", interval)
        if not df.empty: return df, "YFINANCE"

        return pd.DataFrame(), "NONE"

# =========================
# INDICATORS
# =========================
def ema(df,n): return df["close"].ewm(span=n).mean()

def rsi(df):
    delta = df["close"].diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = -delta.clip(upper=0).rolling(14).mean()
    rs = gain/loss
    return 100-(100/(1+rs))

def add(df):
    if df.empty: return df
    df["ema9"]=ema(df,9)
    df["ema21"]=ema(df,21)
    df["ema50"]=ema(df,50)
    df["rsi"]=rsi(df)
    return df.dropna()

# =========================
# TREND + HTF
# =========================
def trend(df1,df5):
    r1=df1.iloc[-1]; r5=df5.iloc[-1]
    if r5.ema9>r5.ema21 and r1.ema9>r1.ema21: return "BULLISH"
    if r5.ema9<r5.ema21 and r1.ema9<r1.ema21: return "BEARISH"
    return "CHOPPY"

def htf(df15):
    r=df15.iloc[-1]
    if r.ema9>r.ema21>r.ema50: return "STRONG_BULL"
    if r.ema9<r.ema21<r.ema50: return "STRONG_BEAR"
    if r.ema9>r.ema21: return "BULL"
    if r.ema9<r.ema21: return "BEAR"
    return "NEUTRAL"

# =========================
# HEARTBEAT
# =========================
last_hb = {"BTC":0,"GOLD":0}

def heartbeat(asset, df, src):
    now=time.time()

    if now-last_hb[asset] < HEARTBEAT_SECONDS:
        return

    if df.empty:
        send(f"💓 {asset} HEARTBEAT\nStatus: NO DATA\nFeed: {src}")
    else:
        r=df.iloc[-1]
        send(
            f"💓 {asset} HEARTBEAT\n\n"
            f"Price: {round(r.close,2)}\n"
            f"RSI: {round(r.rsi,1)}\n"
            f"Trend: {trend(df,df)}\n"
            f"Feed: {src}"
        )

    last_hb[asset]=now

# =========================
# MAIN
# =========================
def run():
    send("🔥 BTC + GOLD BOT LIVE 🔥")

    while True:
        try:
            for asset in ["BTC","GOLD"]:
                df1,src1 = get_klines(asset,"1m")
                df5,_ = get_klines(asset,"5m")
                df15,_ = get_klines(asset,"15m")

                df1=add(df1)
                df5=add(df5)
                df15=add(df15)

                heartbeat(asset, df1, src1)

                if df1.empty or df5.empty or df15.empty:
                    continue

                t = trend(df1,df5)
                bias = htf(df15)

                if DEBUG_MODE:
                    print(asset,t,bias,src1)

                r=df1.iloc[-1]

                # ===== ENTRY FILTER (HIGH QUALITY ONLY) =====
                if t=="BULLISH" and bias in ["BULL","STRONG_BULL"]:
                    if r.close>r.ema9 and r.rsi>50:
                        send(f"🚀 {asset} LONG ENTRY\nPrice: {round(r.close,2)}")

                if t=="BEARISH" and bias in ["BEAR","STRONG_BEAR"]:
                    if r.close<r.ema9 and r.rsi<50:
                        send(f"📉 {asset} SHORT ENTRY\nPrice: {round(r.close,2)}")

            time.sleep(CHECK_INTERVAL)

        except Exception as e:
            send(f"ERROR: {e}")
            time.sleep(10)

if __name__=="__main__":
    run()
