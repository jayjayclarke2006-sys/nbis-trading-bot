from flask import Flask
import threading
import time
import os
from datetime import datetime, timedelta, timezone

import requests
import yfinance as yf
import pandas as pd
import numpy as np

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest, GetOrdersRequest
from alpaca.trading.enums import OrderSide, TimeInForce, QueryOrderStatus

app = Flask(__name__)

API_KEY = os.getenv("APCA_API_KEY_ID")
SECRET_KEY = os.getenv("APCA_API_SECRET_KEY")
BASE_URL = os.getenv("APCA_API_BASE_URL", "https://paper-api.alpaca.markets")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

SYMBOL = "NBIS"

RISK_PER_TRADE = 0.01
STOP_LOSS_PCT = 0.03
TAKE_PROFIT_PCT = 0.06
RSI_PERIOD = 14
EMA_FAST = 50
EMA_SLOW = 200
BREAKOUT_LOOKBACK = 20
RUN_INTERVAL_SECONDS = 300
COOLDOWN_MINUTES = 60

last_trade_time = None

client = TradingClient(API_KEY, SECRET_KEY, paper=True)

def send_telegram(message: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram not configured")
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
    }

    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print(f"Telegram error: {e}")

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

def fetch_data(symbol: str) -> pd.DataFrame:
    df = yf.download(symbol, period="6mo", interval="1h", progress=False, auto_adjust=False)

    if df is None or df.empty:
        return pd.DataFrame()

    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)

    df = df.rename(columns=str.title)
    required = ["Open", "High", "Low", "Close", "Volume"]
    missing = [c for c in required if c not in df.columns]
    if missing:
        print(f"Missing columns: {missing}")
        return pd.DataFrame()

    df = df[required].dropna().copy()
    return df

def prepare_indicators(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty or len(df) < 250:
        return pd.DataFrame()

    df["ema_fast"] = ema(df["Close"], EMA_FAST)
    df["ema_slow"] = ema(df["Close"], EMA_SLOW)
    df["rsi"] = rsi(df["Close"], RSI_PERIOD)
    df["breakout_high"] = df["High"].rolling(BREAKOUT_LOOKBACK).max().shift(1)
    df.dropna(inplace=True)
    return df

def in_cooldown() -> bool:
    global last_trade_time
    if last_trade_time is None:
        return False
    return datetime.now(timezone.utc) - last_trade_time < timedelta(minutes=COOLDOWN_MINUTES)

def get_position_qty(symbol: str) -> int:
    try:
        positions = client.get_all_positions()
        for p in positions:
            if p.symbol == symbol:
                return int(float(p.qty))
    except Exception as e:
        print(f"Position check error: {e}")
    return 0

def has_open_orders(symbol: str) -> bool:
    try:
        req = GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=[symbol])
        orders = client.get_orders(filter=req)
        return len(orders) > 0
    except Exception as e:
        print(f"Open order check error: {e}")
        return False

def calculate_qty(price: float, equity: float) -> int:
    risk_amount = equity * RISK_PER_TRADE
    risk_per_share = price * STOP_LOSS_PCT

    if risk_per_share <= 0:
        return 0

    qty = int(risk_amount // risk_per_share)
    max_affordable = int(equity // price)
    return max(0, min(qty, max_affordable))

def place_buy_order(symbol: str, qty: int) -> None:
    order = MarketOrderRequest(
        symbol=symbol,
        qty=qty,
        side=OrderSide.BUY,
        time_in_force=TimeInForce.DAY
    )
    client.submit_order(order_data=order)

def place_sell_order(symbol: str, qty: int) -> None:
    order = MarketOrderRequest(
        symbol=symbol,
        qty=qty,
        side=OrderSide.SELL,
        time_in_force=TimeInForce.DAY
    )
    client.submit_order(order_data=order)

def should_buy(df: pd.DataFrame):
    if df is None or df.empty or len(df) < 2:
        return False, "Not enough data"

    row = df.iloc[-1]
    prev = df.iloc[-2]

    trend_ok = row["ema_fast"] > row["ema_slow"]
    rsi_ok = row["rsi"] > 55
    breakout_ok = row["Close"] > row["breakout_high"]
    rising_now = row["Close"] > prev["Close"]

    if not trend_ok:
        return False, "Trend filter failed"
    if not rsi_ok:
        return False, "RSI filter failed"
    if not breakout_ok:
        return False, "Breakout filter failed"
    if not rising_now:
        return False, "Price momentum failed"

    return True, "Buy signal confirmed"

def should_sell(entry_price: float, current_price: float):
    stop_price = entry_price * (1 - STOP_LOSS_PCT)
    take_profit_price = entry_price * (1 + TAKE_PROFIT_PCT)

    if current_price <= stop_price:
        return True, f"Stop loss hit at {current_price:.2f}"
    if current_price >= take_profit_price:
        return True, f"Take profit hit at {current_price:.2f}"

    return False, "Hold"

def get_avg_entry_price(symbol: str):
    try:
        positions = client.get_all_positions()
        for p in positions:
            if p.symbol == symbol:
                return float(p.avg_entry_price)
    except Exception as e:
        print(f"Entry price error: {e}")
    return None

def run_strategy() -> None:
    global last_trade_time

    print("Fetching data...")
    df = fetch_data(SYMBOL)
    df = prepare_indicators(df)

    if df is None or df.empty or len(df) < 2:
        print("Not enough clean data, skipping...")
        return

    latest_price = float(df.iloc[-1]["Close"])
    print(f"Latest {SYMBOL} price: {latest_price:.2f}")

    qty_held = get_position_qty(SYMBOL)
    open_orders = has_open_orders(SYMBOL)

    if open_orders:
        print("Open orders already exist, skipping...")
        return

    if qty_held > 0:
        entry_price = get_avg_entry_price(SYMBOL)
        if entry_price is None:
            print("Could not get entry price, skipping sell check")
            return

        sell_signal, sell_reason = should_sell(entry_price, latest_price)
        if sell_signal:
            place_sell_order(SYMBOL, qty_held)
            msg = f"SELL {SYMBOL} | Qty: {qty_held} | Reason: {sell_reason}"
            print(msg)
            send_telegram(msg)
            last_trade_time = datetime.now(timezone.utc)
        else:
            print(f"Holding position | Entry: {entry_price:.2f} | Current: {latest_price:.2f}")
        return

    if in_cooldown():
        print("Cooldown active, skipping new entry")
        return

    buy_signal, buy_reason = should_buy(df)
    if not buy_signal:
        print(f"No buy: {buy_reason}")
        return

    try:
        account = client.get_account()
        equity = float(account.equity)
    except Exception as e:
        print(f"Account fetch error: {e}")
        return

    qty = calculate_qty(latest_price, equity)
    if qty <= 0:
        print("Calculated quantity is zero, skipping...")
        return

    place_buy_order(SYMBOL, qty)
    msg = (
        f"BUY {SYMBOL} | Qty: {qty} | Price: {latest_price:.2f} | "
        f"SL: {latest_price * (1 - STOP_LOSS_PCT):.2f} | "
        f"TP: {latest_price * (1 + TAKE_PROFIT_PCT):.2f}"
    )
    print(msg)
    send_telegram(msg)
    last_trade_time = datetime.now(timezone.utc)

def bot_loop():
    print("Bot loop started")
    send_telegram("NBIS bot is live on Render")

    while True:
        try:
            run_strategy()
        except Exception as e:
            msg = f"Bot error: {e}"
            print(msg)
            send_telegram(msg)

        time.sleep(RUN_INTERVAL_SECONDS)

@app.route("/")
def home():
    return "Bot is running"

if __name__ == "__main__":
    threading.Thread(target=bot_loop, daemon=True).start()
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
