import os
import time
import threading
from datetime import datetime
from typing import Dict, Set, Optional

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

CHECK_INTERVAL = 300  # 5 min

MAX_TOTAL_POSITIONS = 3
MAX_POSITION_PCT = 0.25
RISK_PER_TRADE = 0.008

# Long settings
TAKE_PROFIT_PCT = 0.06
STOP_LOSS_PCT = 0.025
TRAILING_STOP_PCT = 0.02
EARLY_EXIT_RSI = 62

# Short settings
ENABLE_SHORTS = True
SHORT_TAKE_PROFIT_PCT = 0.05
SHORT_STOP_LOSS_PCT = 0.02
SHORT_TRAILING_STOP_PCT = 0.02
SHORT_EARLY_EXIT_RSI = 38

# Signal filters
RSI_MIN = 55
RSI_MAX = 70
SHORT_RSI_MAX = 45

BREAKOUT_LOOKBACK = 10
VOLUME_LOOKBACK = 20
EMA_FAST = 20
EMA_SLOW = 50

BREAKOUT_BUFFER_PCT = 0.0025
BREAKDOWN_BUFFER_PCT = 0.0025

BUY_COOLDOWN_SECONDS = 1800
SELL_COOLDOWN_SECONDS = 300

REPORT_HOUR = 21
REPORT_MINUTE = 0

# Risk extras
FAIL_FAST_BARS = 3
FAIL_FAST_LOSS_PCT = 0.01
MAX_RED_HOLD_BARS = 6
MAX_RED_HOLD_LOSS_PCT = 0.015

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

# =========================
# STATE
# =========================
highest_seen: Dict[str, float] = {}
lowest_seen: Dict[str, float] = {}
last_buy_time: Dict[str, float] = {}
last_sell_time: Dict[str, float] = {}
cycle_traded: Set[str] = set()

entry_info: Dict[str, Dict[str, float]] = {}

daily_realized_pnl = 0.0
daily_wins = 0
daily_losses = 0
daily_trades_closed = 0
last_report_date = None

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
def get_data(symbol: str) -> Optional[pd.DataFrame]:
    try:
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
        if any(c not in df.columns for c in required):
            print(f"{symbol}: missing columns")
            return None

        df = df[required].copy()

        for col in required:
            df[col] = pd.to_numeric(df[col], errors="coerce")

        df["ema_fast"] = ema(df["Close"], EMA_FAST)
        df["ema_slow"] = ema(df["Close"], EMA_SLOW)
        df["rsi"] = rsi(df["Close"], 14)
        df["avg_volume"] = df["Volume"].rolling(VOLUME_LOOKBACK).mean()
        df["recent_high"] = df["High"].rolling(BREAKOUT_LOOKBACK).max().shift(1)
        df["recent_low"] = df["Low"].rolling(BREAKOUT_LOOKBACK).min().shift(1)

        df.dropna(inplace=True)

        needed = max(VOLUME_LOOKBACK + 1, BREAKOUT_LOOKBACK + 2, EMA_SLOW + 1)
        if len(df) < needed:
            print(f"{symbol}: not enough cleaned data")
            return None

        return df

    except Exception as e:
        print(f"{symbol} data error:", e)
        if "Rate limited" in str(e):
            time.sleep(30)
        return None

# =========================
# HELPERS
# =========================
def get_equity() -> Optional[float]:
    try:
        return float(api.get_account().equity)
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

def get_position_value(symbol: str, current_price: float, positions: Dict[str, object]) -> float:
    if symbol not in positions:
        return 0.0
    try:
        return abs(float(positions[symbol].qty)) * current_price
    except Exception:
        return 0.0

def calc_qty(price: float, equity: float, stop_loss_pct: float) -> int:
    risk_amount = equity * RISK_PER_TRADE
    risk_per_share = price * stop_loss_pct
    if risk_per_share <= 0:
        return 0
    return max(0, int(risk_amount // risk_per_share))

def in_buy_cooldown(symbol: str) -> bool:
    ts = last_buy_time.get(symbol)
    return ts is not None and (time.time() - ts) < BUY_COOLDOWN_SECONDS

def in_sell_cooldown(symbol: str) -> bool:
    ts = last_sell_time.get(symbol)
    return ts is not None and (time.time() - ts) < SELL_COOLDOWN_SECONDS

def breakout_is_valid(df: pd.DataFrame) -> bool:
    recent_high = float(df["recent_high"].iloc[-1])
    breakout_level = recent_high * (1 + BREAKOUT_BUFFER_PCT)
    last_close = float(df["Close"].iloc[-1])
    prev_close = float(df["Close"].iloc[-2])
    return last_close > breakout_level and prev_close > recent_high

def breakdown_is_valid(df: pd.DataFrame) -> bool:
    recent_low = float(df["recent_low"].iloc[-1])
    breakdown_level = recent_low * (1 - BREAKDOWN_BUFFER_PCT)
    last_close = float(df["Close"].iloc[-1])
    prev_close = float(df["Close"].iloc[-2])
    return last_close < breakdown_level and prev_close < recent_low

def get_filled_avg_price(order_id: str) -> Optional[float]:
    try:
        time.sleep(2)
        order = api.get_order(order_id)
        if order.filled_avg_price:
            return float(order.filled_avg_price)
    except Exception as e:
        print("Fill fetch error:", e)
    return None

# =========================
# LONG ENTRY
# =========================
def try_long_entry(symbol: str, df: pd.DataFrame, positions: Dict[str, object], open_orders: Dict[str, list]) -> bool:
    global cycle_traded

    if symbol in cycle_traded:
        return False
    if symbol in open_orders and open_orders[symbol]:
        return False
    if symbol in positions:
        return False
    if in_buy_cooldown(symbol):
        return False
    if len(positions) >= MAX_TOTAL_POSITIONS:
        return False

    price = float(df["Close"].iloc[-1])
    prev_close = float(df["Close"].iloc[-2])
    ema_fast_val = float(df["ema_fast"].iloc[-1])
    ema_slow_val = float(df["ema_slow"].iloc[-1])
    rsi_val = float(df["rsi"].iloc[-1])
    volume_now = float(df["Volume"].iloc[-1])
    avg_volume = float(df["avg_volume"].iloc[-1])

    trend = ema_fast_val > ema_slow_val
    momentum = RSI_MIN <= rsi_val <= RSI_MAX
    rising = price > prev_close
    strong_volume = volume_now > avg_volume
    valid_breakout = breakout_is_valid(df)

    if not (trend and momentum and rising and strong_volume and valid_breakout):
        return False

    equity = get_equity()
    if equity is None:
        return False

    max_allowed_value = equity * MAX_POSITION_PCT
    qty_risk = calc_qty(price, equity, STOP_LOSS_PCT)
    qty_cap = int(max_allowed_value // price)
    qty = min(qty_risk, qty_cap)

    if qty <= 0:
        return False

    try:
        order = api.submit_order(
            symbol=symbol,
            qty=qty,
            side="buy",
            type="market",
            time_in_force="day",
        )

        cycle_traded.add(symbol)
        last_buy_time[symbol] = time.time()

        fill_price = get_filled_avg_price(order.id)
        actual_fill = fill_price if fill_price is not None else price

        highest_seen[symbol] = actual_fill
        lowest_seen[symbol] = actual_fill
        entry_info[symbol] = {
            "price": actual_fill,
            "qty": qty,
            "entry_time": time.time(),
            "direction": "long",
        }

        send_telegram(f"BUY {symbol} @ {actual_fill:.2f} | Qty {qty}")
        return True

    except Exception as e:
        print(f"{symbol} long buy error:", e)
        return False

# =========================
# SHORT ENTRY
# =========================
def try_short_entry(symbol: str, df: pd.DataFrame, positions: Dict[str, object], open_orders: Dict[str, list]) -> bool:
    global cycle_traded

    if not ENABLE_SHORTS:
        return False
    if symbol in cycle_traded:
        return False
    if symbol in open_orders and open_orders[symbol]:
        return False
    if symbol in positions:
        return False
    if in_buy_cooldown(symbol):
        return False
    if len(positions) >= MAX_TOTAL_POSITIONS:
        return False

    price = float(df["Close"].iloc[-1])
    prev_close = float(df["Close"].iloc[-2])
    ema_fast_val = float(df["ema_fast"].iloc[-1])
    ema_slow_val = float(df["ema_slow"].iloc[-1])
    rsi_val = float(df["rsi"].iloc[-1])
    volume_now = float(df["Volume"].iloc[-1])
    avg_volume = float(df["avg_volume"].iloc[-1])

    bearish_trend = ema_fast_val < ema_slow_val
    weak_rsi = rsi_val < SHORT_RSI_MAX
    falling = price < prev_close
    strong_volume = volume_now > avg_volume
    valid_breakdown = breakdown_is_valid(df)

    if not (bearish_trend and weak_rsi and falling and strong_volume and valid_breakdown):
        return False

    equity = get_equity()
    if equity is None:
        return False

    max_allowed_value = equity * MAX_POSITION_PCT
    qty_risk = calc_qty(price, equity, SHORT_STOP_LOSS_PCT)
    qty_cap = int(max_allowed_value // price)
    qty = min(qty_risk, qty_cap)

    if qty <= 0:
        return False

    try:
        order = api.submit_order(
            symbol=symbol,
            qty=qty,
            side="sell",
            type="market",
            time_in_force="day",
        )

        cycle_traded.add(symbol)
        last_buy_time[symbol] = time.time()

        fill_price = get_filled_avg_price(order.id)
        actual_fill = fill_price if fill_price is not None else price

        highest_seen[symbol] = actual_fill
        lowest_seen[symbol] = actual_fill
        entry_info[symbol] = {
            "price": actual_fill,
            "qty": qty,
            "entry_time": time.time(),
            "direction": "short",
        }

        send_telegram(f"SHORT {symbol} @ {actual_fill:.2f} | Qty {qty}")
        return True

    except Exception as e:
        print(f"{symbol} short entry error:", e)
        return False

# =========================
# EXIT LOGIC
# =========================
def try_exit(position, df: pd.DataFrame, open_orders: Dict[str, list]) -> None:
    global cycle_traded, daily_realized_pnl, daily_wins, daily_losses, daily_trades_closed

    symbol = position.symbol

    if symbol in cycle_traded:
        return
    if symbol in open_orders and open_orders[symbol]:
        return
    if in_sell_cooldown(symbol):
        return

    qty_float = float(position.qty)
    qty = int(abs(qty_float))
    entry = float(position.avg_entry_price)
    price = float(df["Close"].iloc[-1])
    prev_close = float(df["Close"].iloc[-2])
    recent_high = float(df["recent_high"].iloc[-1])
    recent_low = float(df["recent_low"].iloc[-1])
    rsi_val = float(df["rsi"].iloc[-1])

    is_short = qty_float < 0

    if symbol not in highest_seen:
        highest_seen[symbol] = price
    if symbol not in lowest_seen:
        lowest_seen[symbol] = price

    highest_seen[symbol] = max(highest_seen[symbol], price)
    lowest_seen[symbol] = min(lowest_seen[symbol], price)

    if is_short:
        pnl_pct_now = (entry - price) / entry
        stop_price = entry * (1 + SHORT_STOP_LOSS_PCT)
        tp_price = entry * (1 - SHORT_TAKE_PROFIT_PCT)
        trailing_stop_price = lowest_seen[symbol] * (1 + SHORT_TRAILING_STOP_PCT)
    else:
        pnl_pct_now = (price - entry) / entry
        stop_price = entry * (1 - STOP_LOSS_PCT)
        tp_price = entry * (1 + TAKE_PROFIT_PCT)
        trailing_stop_price = highest_seen[symbol] * (1 - TRAILING_STOP_PCT)

    reason = None

    if is_short:
        if price >= stop_price:
            reason = "HARD STOP"
        elif price <= tp_price:
            reason = "TAKE PROFIT"
        elif lowest_seen[symbol] < entry and price >= trailing_stop_price:
            reason = "TRAILING STOP"
        elif price > recent_low and pnl_pct_now < 0:
            reason = "BREAKDOWN FAIL"
        elif symbol in entry_info:
            entry_seconds = time.time() - entry_info[symbol]["entry_time"]
            if entry_seconds <= FAIL_FAST_BARS * CHECK_INTERVAL and pnl_pct_now <= -FAIL_FAST_LOSS_PCT:
                reason = "FAIL FAST"
        if reason is None and symbol in entry_info:
            entry_seconds = time.time() - entry_info[symbol]["entry_time"]
            if entry_seconds >= MAX_RED_HOLD_BARS * CHECK_INTERVAL and pnl_pct_now <= -MAX_RED_HOLD_LOSS_PCT:
                reason = "WEAK HOLD EXIT"
        if reason is None and rsi_val < SHORT_EARLY_EXIT_RSI and price > prev_close:
            reason = "EARLY EXIT"
    else:
        if price <= stop_price:
            reason = "HARD STOP"
        elif price >= tp_price:
            reason = "TAKE PROFIT"
        elif highest_seen[symbol] > entry and price <= trailing_stop_price:
            reason = "TRAILING STOP"
        elif price < recent_high and pnl_pct_now < 0:
            reason = "BREAKOUT FAIL"
        elif symbol in entry_info:
            entry_seconds = time.time() - entry_info[symbol]["entry_time"]
            if entry_seconds <= FAIL_FAST_BARS * CHECK_INTERVAL and pnl_pct_now <= -FAIL_FAST_LOSS_PCT:
                reason = "FAIL FAST"
        if reason is None and symbol in entry_info:
            entry_seconds = time.time() - entry_info[symbol]["entry_time"]
            if entry_seconds >= MAX_RED_HOLD_BARS * CHECK_INTERVAL and pnl_pct_now <= -MAX_RED_HOLD_LOSS_PCT:
                reason = "WEAK HOLD EXIT"
        if reason is None and rsi_val > EARLY_EXIT_RSI and price < prev_close:
            reason = "EARLY EXIT"

    if reason is None:
        return

    side_to_exit = "buy" if is_short else "sell"

    try:
        order = api.submit_order(
            symbol=symbol,
            qty=qty,
            side=side_to_exit,
            type="market",
            time_in_force="day",
        )

        cycle_traded.add(symbol)
        last_sell_time[symbol] = time.time()

        fill_price = get_filled_avg_price(order.id)
        actual_fill = fill_price if fill_price is not None else price

        if is_short:
            pnl_dollars = (entry - actual_fill) * qty
            pnl_pct = ((entry - actual_fill) / entry) * 100
            action = "COVER"
        else:
            pnl_dollars = (actual_fill - entry) * qty
            pnl_pct = ((actual_fill - entry) / entry) * 100
            action = "SELL"

        daily_realized_pnl += pnl_dollars
        daily_trades_closed += 1
        if pnl_dollars >= 0:
            daily_wins += 1
        else:
            daily_losses += 1

        send_telegram(
            f"{reason} {symbol} {action} @ {actual_fill:.2f} | Qty {qty}\n"
            f"P/L: ${pnl_dollars:.2f} ({pnl_pct:.2f}%)"
        )

        highest_seen.pop(symbol, None)
        lowest_seen.pop(symbol, None)
        entry_info.pop(symbol, None)

    except Exception as e:
        print(f"{symbol} exit error:", e)

# =========================
# DAILY SUMMARY
# =========================
def maybe_send_daily_report() -> None:
    global last_report_date
    global daily_realized_pnl, daily_wins, daily_losses, daily_trades_closed

    now = datetime.now()
    today = now.date()

    if now.hour == REPORT_HOUR and now.minute >= REPORT_MINUTE:
        if last_report_date != today:
            send_telegram(
                f"Daily Summary 📊\n"
                f"Closed trades: {daily_trades_closed}\n"
                f"Wins: {daily_wins}\n"
                f"Losses: {daily_losses}\n"
                f"Realized P/L: ${daily_realized_pnl:.2f}"
            )
            last_report_date = today
            daily_realized_pnl = 0.0
            daily_wins = 0
            daily_losses = 0
            daily_trades_closed = 0

# =========================
# MAIN LOOP
# =========================
def run_bot() -> None:
    global cycle_traded
    send_telegram("Bot is live 🚀")

    while True:
        try:
            cycle_traded = set()

            positions = get_positions_dict()
            open_orders = get_open_orders_dict()

            for symbol in SYMBOLS:
                try:
                    df = get_data(symbol)
                    if df is None:
                        time.sleep(2)
                        continue

                    open_orders = get_open_orders_dict()
                    positions = get_positions_dict()

                    if symbol in positions:
                        try_exit(positions[symbol], df, open_orders)
                    else:
                        entered_long = try_long_entry(symbol, df, positions, open_orders)
                        if not entered_long:
                            open_orders = get_open_orders_dict()
                            positions = get_positions_dict()
                            try_short_entry(symbol, df, positions, open_orders)

                    time.sleep(2)

                except Exception as e:
                    print(f"{symbol} loop error:", e)
                    time.sleep(2)

            maybe_send_daily_report()
            time.sleep(CHECK_INTERVAL)

        except Exception as e:
            print("Main loop error:", e)
            time.sleep(CHECK_INTERVAL)

# =========================
# RENDER WEB
# =========================
@app.route("/")
def home():
    return "Bot is running"

if __name__ == "__main__":
    threading.Thread(target=run_bot, daemon=True).start()
    app.run(host="0.0.0.0", port=10000)
