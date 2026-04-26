import os
import time
import requests
import pandas as pd
import yfinance as yf
from datetime import datetime

# (rest of your file unchanged...)

def get_binance(symbol: str, interval: str, limit: int = 500) -> pd.DataFrame:
    try:
        url = "https://api.binance.com/api/v3/klines"

        params = {
            "symbol": symbol,
            "interval": interval,
            "limit": limit
        }

        headers = {
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json"
        }

        r = requests.get(url, params=params, headers=headers, timeout=10)

        if r.status_code != 200:
            print("BINANCE STATUS ERROR:", r.status_code, r.text)
            return pd.DataFrame()

        data = r.json()

        if not isinstance(data, list) or len(data) < 50:
            print("BINANCE BAD DATA:", data)
            return pd.DataFrame()

        df = pd.DataFrame(data, columns=[
            "time","open","high","low","close","volume",
            "close_time","quote_asset_volume","trades",
            "taker_buy_base","taker_buy_quote","ignore"
        ])

        df["open"] = pd.to_numeric(df["open"], errors="coerce")
        df["high"] = pd.to_numeric(df["high"], errors="coerce")
        df["low"] = pd.to_numeric(df["low"], errors="coerce")
        df["close"] = pd.to_numeric(df["close"], errors="coerce")
        df["volume"] = pd.to_numeric(df["volume"], errors="coerce")

        df = df.dropna()

        if len(df) < 50:
            return pd.DataFrame()

        return df.reset_index(drop=True)

    except Exception as e:
        print("BINANCE ERROR:", e)
        return pd.DataFrame()


def get_klines(asset: str, interval: str):
    if asset == "BTC":
        for _ in range(5):
            df = get_binance("BTCUSDT", interval)
            if not df.empty:
                return df, "BINANCE"
            time.sleep(1)
        return pd.DataFrame(), "BINANCE_FAIL"

    df = get_yf("GC=F", interval)
    if not df.empty:
        return df, "YFINANCE"

    return pd.DataFrame(), "NONE"
