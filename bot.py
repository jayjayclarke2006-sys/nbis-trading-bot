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

# =====================
# ENV VARIABLES
# =====================
API_KEY = os.getenv("APCA_API_KEY_ID")
SECRET_KEY = os.getenv("APCA_API_SECRET_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

client = TradingClient(API_KEY, SECRET_KEY, paper=True)

# =====================
# SETTINGS
# =====================
SYMBOLS = ["NBIS", "WULF", "IREN", "CIFR"]
RUN_INTERVAL = 600  # 10 mins

RISK_PER_TRADE = 0.01
STOP_LOSS_PCT = 0.03
TAKE_PROFIT_PCT = 0.06

MAX_POSITION_PCT = 0.25   # max 25% of equity in one stock
RSI_MIN = 55
RSI_MAX = 72
BREAKOUT_LOOKBACK = 10
VOLUME_LOOKBACK = 20

# =====================
# TELEGRAM
# =====================
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

# =====================
# INDICATORS
# =====================
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

# =====================
# DATA
# =====================
def get_data(symbol: str) -> pd.DataFrame | None:
    try:
        print(f"Fetching {symbol}...")

        df = yf.download(
            symbol,
            period="3mo",
            interval="1h",
            progress=False,
            auto_adjust=False,
        )

        if df is None or df.empty:
            print("No data")
            return None

        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        df.columns = [str(c) for c in df.columns]

        required = ["Open", "High", "Low", "Close", "Volume"]
        missing = [c for c in required if c not in df.columns]
        if missing:
            print("Missing columns:", missing)
            print("Columns found:", df.columns.tolist())
            return None

        df = df[required].copy()

        for col in required:
            df[col] = pd.to_numeric(df[col], errors="coerce")

        df["ema_fast"] = ema(df["Close"], 50)
        df["ema_slow"] = ema(df["Close"], 200)
        df["rsi"] = rsi(df["Close"], 14)
        df["avg_volume"] = df["Volume"].rolling(VOLUME_LOOKBACK).mean()
        df["recent_high"] = df["High"].rolling(BREAKOUT_LOOKBACK).max().shift(1)

        df.dropna(inplace=True)

        if len(df) < max(2, BREAKOUT_LOOKBACK + 1, VOLUME_LOOKBACK + 1):
            print("Not enough cleaned data")
            return None

        return df

    except Exception as e:
        print("Data error:", e)

        if "Rate limited" in str(e):
            print("Sleeping due to rate limit...")
            time.sleep(30)

        return None

# =====================
# ACCOUNT / POSITION HELPERS
# =====================
def get_equity() -> float | None:
    try:
        account = client.get_account()
        return float(account.equity)
    except Exception as e:
        print("Equity error:", e)
        return None

def get_position_value(symbol: str, current_price: float) -> float:
    try:
        positions = client.get_all_positions()
        for p in positions:
            if p.symbol == symbol:
                qty = float(p.qty)
                return qty * current_price
    except Exception as e:
        print("Position value error:", e)
    return 0.0

def calc_qty(price: float, equity: float) -> int:
    risk_amount = equity * RISK_PER_TRADE
    risk_per_share = price * STOP_LOSS_PCT

    if risk_per_share <= 0:
        return 0

    qty = int(risk_amount // risk_per_share)
    return max(0, qty)

# =====================
# STRATEGY
# =====================
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

                volume_now = float(df["Volume"].iloc[-1])
                avg_volume = float(df["avg_volume"].iloc[-1])
                recent_high = float(df["recent_high"].iloc[-1])

                print(f"Price={price:.2f} Prev={prev_close:.2f}")
                print(f"EMA50={ema_fast_val:.2f} EMA200={ema_slow_val:.2f} RSI={rsi_val:.2f}")
                print(f"Vol={volume_now:.0f} AvgVol={avg_volume:.0f} RecentHigh={recent_high:.2f}")

                trend = ema_fast_val > ema_slow_val
                momentum = RSI_MIN <= rsi_val <= RSI_MAX
                rising = price > prev_close
                breakout = price > recent_high
                strong_volume = volume_now > avg_volume

                print(
                    f"Trend={trend} Momentum={momentum} Rising={rising} "
                    f"Breakout={breakout} StrongVol={strong_volume}"
                )

                # =====================
                # BUY
                # =====================
                if trend and momentum and rising and breakout and strong_volume:
                    print("BUY SIGNAL")

                    equity = get_equity()
                    if equity is None:
                        time.sleep(2)
                        continue

                    current_position_value = get_position_value(symbol, price)
                    max_allowed_value = equity * MAX_POSITION_PCT
                    remaining_value = max_allowed_value - current_position_value

                    print(
                        f"CurrentPosValue={current_position_value:.2f} "
                        f"MaxAllowed={max_allowed_value:.2f} Remaining={remaining_value:.2f}"
                    )

                    if remaining_value <= price:
                        print("Position cap reached, skipping buy")
                    else:
                        qty = calc_qty(price, equity)
                        max_qty_by_cap = int(remaining_value // price)
                        qty = min(qty, max_qty_by_cap)

                        if qty > 0:
                            try:
                                client.submit_order(
                                    order_data=MarketOrderRequest(
                                        symbol=symbol,
                                        qty=qty,
                                        side=OrderSide.BUY,
                                        time_in_force=TimeInForce.DAY,
                                    )
                                )
                                msg = f"BUY {symbol} @ {price:.2f} | Qty {qty}"
                                print(msg)
                                send_telegram(msg)
                            except Exception as e:
                                print("Buy error:", e)
                        else:
                            print("Qty <= 0 after cap/risk checks")

                # =====================
                # SELL
                # =====================
                try:
                    positions = client.get_all_positions()

                    for p in positions:
                        if p.symbol == symbol:
                            entry = float(p.avg_entry_price)
                            qty = int(float(p.qty))

                            stop = entry * (1 - STOP_LOSS_PCT)
                            tp = entry * (1 + TAKE_PROFIT_PCT)

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
                                msg = f"SELL {symbol} @ {price:.2f} | Qty {qty}"
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

# =====================
# WEB ROUTE
# =====================
@app.route("/")
def home():
    return "Bot is running"

# =====================
# START
# =====================
if __name__ == "__main__":
    print("Starting bot...")
    threading.Thread(target=run_bot, daemon=True).start()

    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
