from flask import Flask
import time
import os
import requests
import yfinance as yf
import pandas as pd
import numpy as np
import threading

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce

app = Flask(__name__)

API_KEY = os.getenv("APCA_API_KEY_ID")
SECRET_KEY = os.getenv("APCA_API_SECRET_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

client = TradingClient(API_KEY, SECRET_KEY, paper=True)

SYMBOLS = ["NBIS", "WULF", "IREN", "CIFR"]
RUN_INTERVAL = 600


def send_telegram(msg: str) -> None:
    try:
        if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                json={"chat_id": TELEGRAM_CHAT_ID, "text": msg},
                timeout=10,
            )
    except Exception as e:
        print("Telegram failed:", e)


def ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(period).mean()
    avg_loss = loss.rolling(period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def get_data(symbol: str) -> pd.DataFrame | None:
    try:
        print(f"Fetching {symbol}...")

        df = yf.download(symbol, period="3mo", interval="1h", progress=False, auto_adjust=False)

        if df is None or df.empty:
            print("No data")
            return None

        # Flatten MultiIndex columns if Yahoo returns them
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        # Force plain string column names
        df.columns = [str(c) for c in df.columns]

        required = ["Open", "High", "Low", "Close", "Volume"]
        missing = [c for c in required if c not in df.columns]
        if missing:
            print("Missing columns:", missing)
            print("Columns found:", df.columns.tolist())
            return None

        # Keep only the columns we need
        df = df[required].copy()

        # Force numeric data
        for col in required:
            df[col] = pd.to_numeric(df[col], errors="coerce")

        df["ema_fast"] = ema(df["Close"], 50)
        df["ema_slow"] = ema(df["Close"], 200)
        df["rsi"] = rsi(df["Close"], 14)

        df.dropna(inplace=True)

        if len(df) < 2:
            print("Not enough cleaned data")
            return None

        return df

    except Exception as e:
        print("Data error:", e)
        if "Rate limited" in str(e):
            print("Sleeping due to rate limit...")
            time.sleep(30)
        return None


def run_bot() -> None:
    print("Bot loop started")
    send_telegram("Bot is live 🚀")

    while True:
        print("\n=== NEW CYCLE ===")

        for symbol in SYMBOLS:
            try:
                print(f"\nChecking {symbol}")
                df = get_data(symbol)

                if df is None:
                    time.sleep(2)
                    continue

                price = float(df["Close"].iloc[-1])
                prev_close = float(df["Close"].iloc[-2])
                ema_fast_val = float(df["ema_fast"].iloc[-1])
                ema_slow_val = float(df["ema_slow"].iloc[-1])
                rsi_val = float(df["rsi"].iloc[-1])

                print(f"Price={price} Prev={prev_close}")
                print(f"EMA50={ema_fast_val} EMA200={ema_slow_val} RSI={rsi_val}")

                trend = ema_fast_val > ema_slow_val
                momentum = rsi_val > 55
                rising = price > prev_close

                print(f"Trend={trend} Momentum={momentum} Rising={rising}")

                if trend and momentum and rising:
                    print("BUY SIGNAL")

                    try:
                        account = client.get_account()
                        equity = float(account.equity)
                        qty = int((equity * 0.01) / (price * 0.03))

                        if qty > 0:
                            client.submit_order(
                                order_data=MarketOrderRequest(
                                    symbol=symbol,
                                    qty=qty,
                                    side=OrderSide.BUY,
                                    time_in_force=TimeInForce.DAY,
                                )
                            )
                            msg = f"BUY {symbol} @ {price}"
                            print(msg)
                            send_telegram(msg)

                    except Exception as e:
                        print("Buy error:", e)

                try:
                    positions = client.get_all_positions()

                    for p in positions:
                        if p.symbol == symbol:
                            entry = float(p.avg_entry_price)
                            qty = int(float(p.qty))
                            stop = entry * 0.97
                            tp = entry * 1.06

                            if price <= stop or price >= tp:
                                print("SELL SIGNAL")

                                client.submit_order(
                                    order_data=MarketOrderRequest(
                                        symbol=symbol,
                                        qty=qty,
                                        side=OrderSide.SELL,
                                        time_in_force=TimeInForce.DAY,
                                    )
                                )
                                msg = f"SELL {symbol} @ {price}"
                                print(msg)
                                send_telegram(msg)

                except Exception as e:
                    print("Sell error:", e)

                time.sleep(2)

            except Exception as e:
                print(f"Loop error for {symbol}:", e)
                time.sleep(2)

        print(f"\nSleeping {RUN_INTERVAL}s...\n")
        time.sleep(RUN_INTERVAL)


@app.route("/")
def home():
    return "Bot is running"


if __name__ == "__main__":
    print("Starting bot...")
    threading.Thread(target=run_bot, daemon=True).start()
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
