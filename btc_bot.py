import os
import time
import requests
import pandas as pd
import yfinance as yf

def normalize(df):
    if df is None or df.empty:
        return pd.DataFrame()
    df.columns = [c.lower() for c in df.columns]
    df = df[["open","high","low","close","volume"]]
    df = df.apply(pd.to_numeric, errors="coerce")
    df.dropna(inplace=True)
    return df

def get_binance(symbol, interval):
    try:
        r = requests.get(
            "https://api.binance.com/api/v3/klines",
            params={"symbol": symbol, "interval": interval, "limit": 500},
            timeout=10
        )
        data = r.json()
        if not isinstance(data, list) or len(data) < 50:
            return pd.DataFrame()
        df = pd.DataFrame(data, columns=[
            "time","open","high","low","close","volume",
            "ct","q","t","tb","tq","ig"
        ])
        return normalize(df)
    except:
        return pd.DataFrame()

def get_coinbase(symbol, interval):
    try:
        gran = {"1m":60,"5m":300,"15m":900}[interval]
        r = requests.get(
            f"https://api.exchange.coinbase.com/products/{symbol}/candles",
            params={"granularity": gran},
            timeout=10
        )
        data = r.json()
        if not isinstance(data, list) or len(data) < 30:
            return pd.DataFrame()
        df = pd.DataFrame(data, columns=[
            "time","low","high","open","close","volume"
        ])
        return normalize(df.sort_values("time"))
    except:
        return pd.DataFrame()

def get_yf(symbol, interval):
    try:
        df = yf.download(symbol, period="7d", interval=interval, progress=False)
        if df is None or df.empty:
            return pd.DataFrame()
        return normalize(df)
    except:
        return pd.DataFrame()

def get_klines(asset, interval):
    if asset == "BTC":
        df = get_binance("BTCUSDT", interval)
        if not df.empty:
            return df, "BINANCE"

        df = get_coinbase("BTC-USD", interval)
        if not df.empty:
            return df, "COINBASE"

        df = get_yf("BTC-USD", interval)
        if not df.empty:
            return df, "YFINANCE"

        return pd.DataFrame(), "NO_DATA"
    else:
        df = get_yf("GC=F", interval)
        if not df.empty:
            return df, "YFINANCE"
        return pd.DataFrame(), "NO_DATA"

def run():
    print("BOT STARTED")
    while True:
        for asset in ["BTC","GOLD"]:
            df, feed = get_klines(asset, "1m")
            if df.empty:
                print(asset, "NO DATA", feed)
            else:
                print(asset, "OK", df.iloc[-1]["close"], feed)
        time.sleep(60)

if __name__ == "__main__":
    run()
