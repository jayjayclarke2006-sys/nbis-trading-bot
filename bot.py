import math
import pandas as pd
import numpy as np
import yfinance as yf
import os

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest, StopOrderRequest, LimitOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce

# -----------------------
# API KEYS (FROM RENDER ENV)
# -----------------------
API_KEY = os.getenv("APCA_API_KEY_ID")
SECRET_KEY = os.getenv("APCA_API_SECRET_KEY")

SYMBOL = "NBIS"
RISK_PER_TRADE = 0.005

EMA_FAST = 50
EMA_SLOW = 200
RSI_PERIOD = 14
ATR_PERIOD = 14
BREAKOUT_LOOKBACK = 20

STOP_ATR = 2.0
TP_ATR = 3.5

client = TradingClient(API_KEY, SECRET_KEY, paper=True)

# -----------------------
# DATA
# -----------------------
def get_data():
    df = yf.download(SYMBOL, period="6mo", progress=False)
    df = df.rename(columns=str.title)
    return df

# -----------------------
# INDICATORS
# -----------------------
def ema(series, span):
    return series.ewm(span=span, adjust=False).mean()

def rsi(series, period=14):
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(period).mean()
    avg_loss = loss.rolling(period).mean()
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def atr(df, period=14):
    high_low = df["High"] - df["Low"]
    high_close = abs(df["High"] - df["Close"].shift())
    low_close = abs(df["Low"] - df["Close"].shift())
    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    return tr.rolling(period).mean()

# -----------------------
# SIGNAL
# -----------------------
def generate_signal(df, equity):
    df["ema50"] = ema(df["Close"], EMA_FAST)
    df["ema200"] = ema(df["Close"], EMA_SLOW)
    df["rsi"] = rsi(df["Close"])
    df["atr"] = atr(df)

    df.dropna(inplace=True)

if df is None or df.empty or len(df) < 2:
    print("Not enough data, skipping...")
    return None

df.dropna(inplace=True)

if df is None or df.empty or len(df) < 2:
    print("Not enough data yet, skipping...")
    return None

try:
    row = df.iloc[-1]
    prev = df.iloc[-2]
except Exception as e:
    print("Index error:", e)
    return None

    close = row["Close"]

    if not (row["ema50"] > row["ema200"]):
        return None

    if not (prev["rsi"] < 50 and row["rsi"] > 50):
        return None

    if not (close > df["High"].rolling(BREAKOUT_LOOKBACK).max().iloc[-2]):
        return None

    stop = close - STOP_ATR * row["atr"]
    tp = close + TP_ATR * row["atr"]

    risk_per_share = close - stop
    qty = math.floor((equity * RISK_PER_TRADE) / risk_per_share)

    return {
        "qty": qty,
        "stop": stop,
        "tp": tp,
        "price": close
    }

# -----------------------
# EXECUTION
# -----------------------
def run():
    account = client.get_account()
    equity = float(account.equity)

    df = get_data()
    signal = generate_signal(df, equity)

    if signal is None:
        print("No trade signal")
        return

    positions = client.get_all_positions()
    if any(p.symbol == SYMBOL for p in positions):
        print("Already in position")
        return

    qty = signal["qty"]

    if qty <= 0:
        print("Position size too small")
        return

    print(f"BUY {qty} shares of {SYMBOL}")

    client.submit_order(
        MarketOrderRequest(
            symbol=SYMBOL,
            qty=qty,
            side=OrderSide.BUY,
            time_in_force=TimeInForce.DAY
        )
    )

    client.submit_order(
        StopOrderRequest(
            symbol=SYMBOL,
            qty=qty,
            stop_price=round(signal["stop"], 2),
            side=OrderSide.SELL,
            time_in_force=TimeInForce.GTC
        )
    )

    client.submit_order(
        LimitOrderRequest(
            symbol=SYMBOL,
            qty=qty,
            limit_price=round(signal["tp"], 2),
            side=OrderSide.SELL,
            time_in_force=TimeInForce.GTC
        )
    )

    print(f"Stop: {signal['stop']:.2f} | Take Profit: {signal['tp']:.2f}")

# -----------------------
# LOOP (RUN DAILY)
# -----------------------
import 
import time

if __name__ == "__main__":
    print("Starting bot...")

    while True:
        try:
            run()
        except Exception as e:
            print("Error:", e)
time.sleep(60)

