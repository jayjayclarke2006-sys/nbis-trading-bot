import os
import time
import requests
import pandas as pd
import yfinance as yf

# ============================================================
# NORMALIZE
# ============================================================
def normalize(df):
    if df is None or df.empty:
        return pd.DataFrame()

    df = df.copy()
    df.columns = [c.lower() for c in df.columns]

    required = ["open","high","low","close"]
    for col in required:
        if col not in df.columns:
            return pd.DataFrame()

    if "volume" not in df.columns:
        df["volume"] = 1

    df = df[["open","high","low","close","volume"]]

    for col in df.columns:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    df.dropna(inplace=True)
    return df.reset_index(drop=True)

# ============================================================
# BINANCE (FIXED)
# ============================================================
def get_binance(symbol, interval):
    try:
        r = requests.get(
            "https://api.binance.com/api/v3/klines",
            params={"symbol": symbol, "interval": interval, "limit": 500},
            timeout=10,
            headers={"User-Agent": "Mozilla/5.0"}
        )

        data = r.json()

        if not isinstance(data, list) or len(data) < 20:
            print("BINANCE FAIL:", data)
            return pd.DataFrame()

        df = pd.DataFrame(data, columns=[
            "time","open","high","low","close","volume",
            "ct","q","t","tb","tq","ig"
        ])

        return normalize(df)

    except Exception as e:
        print("BINANCE ERROR:", e)
        return pd.DataFrame()

# ============================================================
# COINBASE (FIXED)
# ============================================================
def get_coinbase(symbol, interval):
    try:
        gran = {"1m":60,"5m":300,"15m":900}[interval]

        r = requests.get(
            f"https://api.exchange.coinbase.com/products/{symbol}/candles",
            params={"granularity": gran},
            timeout=10
        )

        data = r.json()

        if not isinstance(data, list) or len(data) < 20:
            print("COINBASE FAIL:", data)
            return pd.DataFrame()

        df = pd.DataFrame(data, columns=[
            "time","low","high","open","close","volume"
        ])

        df = df.sort_values("time")
        return normalize(df)

    except Exception as e:
        print("COINBASE ERROR:", e)
        return pd.DataFrame()

# ============================================================
# YFINANCE (LAST RESORT)
# ============================================================
def get_yf(symbol, interval):
    try:
        period = {"1m":"7d","5m":"30d","15m":"60d"}[interval]

        df = yf.download(
            symbol,
            period=period,
            interval=interval,
            progress=False
        )

        if df is None or df.empty:
            return pd.DataFrame()

        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        return normalize(df)

    except Exception as e:
        print("YF ERROR:", e)
        return pd.DataFrame()

# ============================================================
# 🔥 ELITE ROUTING (THIS IS THE REAL FIX)
# ============================================================
def get_klines(asset, interval):

    if asset == "BTC":

        # 1️⃣ TRY BINANCE FIRST
        df = get_binance("BTCUSDT", interval)
        if not df.empty:
            return df, "BINANCE"

        # 2️⃣ FALLBACK → COINBASE
        df = get_coinbase("BTC-USD", interval)
        if not df.empty:
            return df, "COINBASE"

        # 3️⃣ LAST RESORT → YF
        df = get_yf("BTC-USD", interval)
        if not df.empty:
            return df, "YFINANCE"

        return pd.DataFrame(), "NO_DATA"

    # GOLD
    df = get_yf("GC=F", interval)
    if not df.empty:
        return df, "YFINANCE"

    return pd.DataFrame(), "NO_DATA"

# ============================================================
# TEST LOOP
# ============================================================
def run():
    print("✅ BOT STARTED")

    while True:
        for asset in ["BTC","GOLD"]:

            df, feed = get_klines(asset, "1m")

            print(asset, "LEN:", len(df), "FEED:", feed)

            if df.empty:
                print(f"{asset} ❌ NO DATA ({feed})")
            else:
                price = df.iloc[-1]["close"]
                print(f"{asset} ✅ PRICE: {price} ({feed})")

        print("------")
        time.sleep(60)

# ============================================================
if __name__ == "__main__":
    run()
