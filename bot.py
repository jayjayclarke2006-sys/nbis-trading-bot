import os
import time
import threading
from typing import Dict

import requests
import yfinance as yf
import pandas as pd
import numpy as np
import alpaca_trade_api as tradeapi
from flask import Flask

# =========================
# ENV
# =========================
API_KEY = os.getenv("APCA_API_KEY_ID")
SECRET_KEY = os.getenv("APCA_API_SECRET_KEY")
BASE_URL = os.getenv("APCA_API_BASE_URL")  # https://paper-api.alpaca.markets

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# =========================
# SETTINGS
# =========================
SYMBOLS = ["NBIS", "WULF", "IREN", "CIFR"]

CHECK_INTERVAL = 300  # 5 minutes
MAX_TOTAL_POSITIONS = 3
MAX_POSITION_PCT = 0.25   # max 25% of equity in one stock
RISK_PER_TRADE = 0.01     # 1% risk model

TAKE_PROFIT_PCT = 0.06    # +6%
STOP_LOSS_PCT = 0.03      # -3%
TRAILING_STOP_PCT = 0.025 # 2.5% below highest seen
EARLY_EXIT_RSI = 65

RSI_MIN = 55
RSI_MAX = 72
BREAKOUT_LOOKBACK = 10
VOLUME_LOOKBACK = 20
EMA_FAST = 20
EMA_SLOW = 50

BUY_COOLDOWN_SECONDS = 900   # 15 min anti-spam
SELL_COOLDOWN_SECONDS = 300  # 5 min anti-spam

# =========================
# APP / API
# =========================
app = Flask(__name__)

api = tradeapi.REST(
    API_KEY,
    SECRET_KEY,
    BASE_URL,
    api_version="v2"
)

# highest price seen since entry for trailing stop
highest_seen: Dict[str, float] = {}

# anti-spam memory
last_buy_time: Dict[str, float] = {}
last_sell_time: Dict[str, float] = {}

# =========================
# TELEGRAM
# =========================
def send_telegram(message: str) -> None:
    try:
        if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
            print("Telegram not configured")
            return

        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data={"chat_id": TELEGRAM_CHAT_ID, "text": message},
            timeout=10,
        )
    except Exception as e:
        print("Telegram error:", e)

# =========================
# INDICATORS
# =========================
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

# =========================
# DATA
# =========================
def get_data(symbol: str) -> pd.DataFrame | None:
    try:
        print(f"Fetching {symbol}...")

        df = yf.download(
            symbol,
            period="5d",
            interval="5m",
            progress=False,
            auto_adjust=False,
        )

        if df is None or df.empty:
            print(f"{symbol}: no data")
            return None

        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        df.columns = [str(c) for c in df.columns]

        required = ["Open", "High", "Low", "Close", "Volume"]
        missing = [c for c in required if c not in df.columns]
        if missing:
            print(f"{symbol}: missing columns {missing}")
            return None

        df = df[required].copy()

        for col in required:
            df[col] = pd.to_numeric(df[col], errors="coerce")

        df["ema_fast"] = ema(df["Close"], EMA_FAST)
        df["ema_slow"] = ema(df["Close"], EMA_SLOW)
        df["rsi"] = rsi(df["Close"], 14)
        df["avg_volume"] = df["Volume"].rolling(VOLUME_LOOKBACK).mean()
        df["recent_high"] = df["High"].rolling(BREAKOUT_LOOKBACK).max().shift(1)

        df.dropna(inplace=True)

        if len(df) < max(VOLUME_LOOKBACK + 1, BREAKOUT_LOOKBACK + 1, EMA_SLOW + 1):
            print(f"{symbol}: not enough cleaned data")
            return None

        return df

    except Exception as e:
        print(f"{symbol} data error:", e)
        if "Rate limited" in str(e):
            print("Rate limited, sleeping 30s...")
            time.sleep(30)
        return None

# =========================
# BROKER HELPERS
# =========================
def get_equity() -> float | None:
    try:
        account = api.get_account()
        return float(account.equity)
    except Exception as e:
        print("Equity error:", e)
        return None

def get_positions_dict() -> Dict[str, object]:
    try:
        return {p.symbol: p for p in api.list_positions()}
    except Exception as e:
        print("Positions error:", e)
        return {}

def get_open_orders_dict() -> Dict[str, list]:
    try:
        orders = api.list_orders(status="open")
        out: Dict[str, list] = {}
        for order in orders:
            out.setdefault(order.symbol, []).append(order)
        return out
    except Exception as e:
        print("Open orders error:", e)
        return {}

def count_open_positions(positions: Dict[str, object]) -> int:
    return len(positions)

def get_position_value(symbol: str, current_price: float, positions: Dict[str, object]) -> float:
    if symbol not in positions:
        return 0.0
    try:
        qty = float(positions[symbol].qty)
        return qty * current_price
    except Exception:
        return 0.0

def calc_qty(price: float, equity: float) -> int:
    risk_amount = equity * RISK_PER_TRADE
    risk_per_share = price * STOP_LOSS_PCT
    if risk_per_share <= 0:
        return 0
    qty = int(risk_amount // risk_per_share)
    return max(0, qty)

def in_buy_cooldown(symbol: str) -> bool:
    ts = last_buy_time.get(symbol)
    return ts is not None and (time.time() - ts) < BUY_COOLDOWN_SECONDS

def in_sell_cooldown(symbol: str) -> bool:
    ts = last_sell_time.get(symbol)
    return ts is not None and (time.time() - ts) < SELL_COOLDOWN_SECONDS

# =========================
# BUY LOGIC
# =========================
def try_buy(symbol: str, df: pd.DataFrame, positions: Dict[str, object], open_orders: Dict[str, list]) -> None:
    # anti-spam: skip if any open order already exists for symbol
    if symbol in open_orders and len(open_orders[symbol]) > 0:
        print(f"{symbol}: open order already pending, skipping")
        return

    # anti-spam: cooldown after recent buy
    if in_buy_cooldown(symbol):
        print(f"{symbol}: buy cooldown active, skipping")
        return

    price = float(df["Close"].iloc[-1])
    prev_close = float(df["Close"].iloc[-2])
    ema_fast_val = float(df["ema_fast"].iloc[-1])
    ema_slow_val = float(df["ema_slow"].iloc[-1])
    rsi_val = float(df["rsi"].iloc[-1])
    volume_now = float(df["Volume"].iloc[-1])
    avg_volume = float(df["avg_volume"].iloc[-1])
    recent_high = float(df["recent_high"].iloc[-1])

    trend = ema_fast_val > ema_slow_val
    momentum = RSI_MIN <= rsi_val <= RSI_MAX
    rising = price > prev_close
    breakout = price > recent_high
    strong_volume = volume_now > avg_volume

    print(
        f"{symbol} | Price={price:.2f} Prev={prev_close:.2f} "
        f"EMA{EMA_FAST}={ema_fast_val:.2f} EMA{EMA_SLOW}={ema_slow_val:.2f} "
        f"RSI={rsi_val:.2f} Vol={volume_now:.0f}/{avg_volume:.0f} "
        f"Trend={trend} Momentum={momentum} Rising={rising} Breakout={breakout} StrongVol={strong_volume}"
    )

    if not (trend and momentum and rising and breakout and strong_volume):
        return

    equity = get_equity()
    if equity is None:
        return

    open_positions = count_open_positions(positions)

    # allow stacking only in already-held names; cap totally new names
    if symbol not in positions and open_positions >= MAX_TOTAL_POSITIONS:
        print(f"{symbol}: max total positions reached")
        return

    current_position_value = get_position_value(symbol, price, positions)
    max_allowed_value = equity * MAX_POSITION_PCT
    remaining_value = max_allowed_value - current_position_value

    print(
        f"{symbol} | CurrentPosValue={current_position_value:.2f} "
        f"MaxAllowed={max_allowed_value:.2f} Remaining={remaining_value:.2f}"
    )

    if remaining_value <= price:
        print(f"{symbol}: position cap reached")
        return

    qty_risk = calc_qty(price, equity)
    qty_cap = int(remaining_value // price)
    qty = min(qty_risk, qty_cap)

    if qty <= 0:
        print(f"{symbol}: qty <= 0")
        return

    try:
        api.submit_order(
            symbol=symbol,
            qty=qty,
            side="buy",
            type="market",
            time_in_force="day",
        )
        last_buy_time[symbol] = time.time()
        highest_seen[symbol] = price
        msg = f"BUY {symbol} @ {price:.2f} | Qty {qty}"
        print(msg)
        send_telegram(msg)
    except Exception as e:
        print(f"{symbol} buy error:", e)

# =========================
# SELL LOGIC
# =========================
def try_sell(position, df: pd.DataFrame, open_orders: Dict[str, list]) -> None:
    symbol = position.symbol

    # anti-spam: skip if any open order already exists for symbol
    if symbol in open_orders and len(open_orders[symbol]) > 0:
        print(f"{symbol}: open order already pending, skipping sell")
        return

    # anti-spam: cooldown after recent sell
    if in_sell_cooldown(symbol):
        print(f"{symbol}: sell cooldown active, skipping")
        return

    qty = int(float(position.qty))
    entry = float(position.avg_entry_price)
    price = float(df["Close"].iloc[-1])
    prev_close = float(df["Close"].iloc[-2])
    rsi_val = float(df["rsi"].iloc[-1])

    if symbol not in highest_seen:
        highest_seen[symbol] = price
    highest_seen[symbol] = max(highest_seen[symbol], price)

    stop_price = entry * (1 - STOP_LOSS_PCT)
    tp_price = entry * (1 + TAKE_PROFIT_PCT)
    trailing_stop_price = highest_seen[symbol] * (1 - TRAILING_STOP_PCT)

    hit_stop = price <= stop_price
    hit_tp = price >= tp_price
    hit_trailing = price <= trailing_stop_price and highest_seen[symbol] > entry
    hit_early_exit = (rsi_val > EARLY_EXIT_RSI) and (price < prev_close)

    print(
        f"{symbol} SELL CHECK | Entry={entry:.2f} Price={price:.2f} "
        f"TP={tp_price:.2f} SL={stop_price:.2f} "
        f"TrailHigh={highest_seen[symbol]:.2f} TrailStop={trailing_stop_price:.2f} "
        f"RSI={rsi_val:.2f}"
    )

    reason = None
    if hit_tp:
        reason = "TAKE PROFIT"
    elif hit_stop:
        reason = "STOP LOSS"
    elif hit_trailing:
        reason = "TRAILING STOP"
    elif hit_early_exit:
        reason = "EARLY EXIT"

    if not reason:
        return

    try:
        api.submit_order(
            symbol=symbol,
            qty=qty,
            side="sell",
            type="market",
            time_in_force="day",
        )
        last_sell_time[symbol] = time.time()
        msg = f"{reason} {symbol} @ {price:.2f} | Qty {qty}"
        print(msg)
        send_telegram(msg)
        highest_seen.pop(symbol, None)
    except Exception as e:
        print(f"{symbol} sell error:", e)

# =========================
# MAIN LOOP
# =========================
def run_bot() -> None:
    print("Bot loop started")
    send_telegram("Bot is live 🚀")

    while True:
        try:
            print("\n=== NEW CYCLE ===")

            positions = get_positions_dict()
            open_orders = get_open_orders_dict()

            for symbol in SYMBOLS:
                try:
                    df = get_data(symbol)
                    if df is None:
                        time.sleep(2)
                        continue

                    # refresh open orders each symbol loop
                    open_orders = get_open_orders_dict()

                    if symbol in positions:
                        try_sell(positions[symbol], df, open_orders)
                    else:
                        try_buy(symbol, df, positions, open_orders)

                    time.sleep(2)

                except Exception as e:
                    print(f"Loop error for {symbol}:", e)
                    time.sleep(2)

            print(f"\nSleeping {CHECK_INTERVAL}s...\n")
            time.sleep(CHECK_INTERVAL)

        except Exception as e:
            print("Main loop error:", e)
            time.sleep(CHECK_INTERVAL)

# =========================
# RENDER KEEP-ALIVE
# =========================
@app.route("/")
def home():
    return "Bot is running"

def run_web():
    app.run(host="0.0.0.0", port=10000)

# =========================
# START
# =========================
if __name__ == "__main__":
    threading.Thread(target=run_bot, daemon=True).start()
    run_web()
