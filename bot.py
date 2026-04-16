import os
import time
import threading
from datetime import datetime
from typing import Dict, Optional, Set

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
# CONFIG
# =========================
SYMBOLS = ["NBIS", "WULF", "IREN", "CIFR"]
MARKET_SYMBOLS = ["SPY", "QQQ"]

CHECK_INTERVAL = 300  # 5 min

# Exposure
MAX_TOTAL_POSITIONS = 4
MAX_TOTAL_EXPOSURE_PCT = 0.28   # 28% total account exposure
MAX_POSITION_PCT = 0.07         # 7% max per stock

# Risk
RISK_PER_TRADE = 0.0075         # 0.75% account risk per trade
BASE_DAILY_MAX_LOSS_PCT = 0.02  # 2% base cap
MAX_DAILY_MAX_LOSS_PCT = 0.05   # never more than 5%
YESTERDAY_GAIN_GIVEBACK_PCT = 0.50  # can give back 50% of yesterday gains max

# Entry filters
EMA_FAST = 20
EMA_SLOW = 50
RSI_PERIOD = 14
ATR_PERIOD = 14
VOLUME_LOOKBACK = 20
BREAKOUT_LOOKBACK = 10
RSI_MIN = 55
RSI_MAX = 70
RVOL_MIN = 1.5
BREAKOUT_BUFFER_PCT = 0.0025
MAX_CHASE_FROM_BREAKOUT_PCT = 0.015
MAX_EXT_FROM_EMA_FAST_PCT = 0.02

# Exits
HARD_STOP_PCT = 0.025
FAIL_FAST_BARS = 3
FAIL_FAST_LOSS_PCT = 0.01
FIRST_TARGET_R = 1.5
FINAL_TARGET_R = 3.0
TRAILING_STOP_ATR_MULT = 2.0
EARLY_EXIT_RSI = 64
WEAK_HOLD_BARS = 6
WEAK_HOLD_LOSS_PCT = 0.0125

# Cooldowns
BUY_COOLDOWN_SECONDS = 1800
SELL_COOLDOWN_SECONDS = 300

# Daily summary
REPORT_HOUR = 21
REPORT_MINUTE = 0

# =========================
# APP / API
# =========================
app = Flask(__name__)

api = tradeapi.REST(
    API_KEY,
    SECRET_KEY,
    BASE_URL,
    api_version="v2",
)

# =========================
# STATE
# =========================
cycle_traded: Set[str] = set()
last_buy_time: Dict[str, float] = {}
last_sell_time: Dict[str, float] = {}

highest_seen: Dict[str, float] = {}

trade_state: Dict[str, Dict[str, float]] = {}
# {
#   symbol: {
#       "entry_price": float,
#       "entry_time": float,
#       "risk_per_share": float,
#       "initial_qty": int,
#       "partial_taken": 0 or 1,
#       "break_even_active": 0 or 1,
#   }
# }

# daily stats
daily_realized_pnl = 0.0
daily_closed_trades = 0
daily_wins = 0
daily_losses = 0
yesterday_realized_pnl = 0.0
last_report_date = None
current_trade_day = None

# =========================
# TELEGRAM
# =========================
def send_telegram(msg: str) -> None:
    try:
        if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
            print("Telegram not configured")
            return

        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data={"chat_id": TELEGRAM_CHAT_ID, "text": msg},
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

def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    hl = df["High"] - df["Low"]
    hc = (df["High"] - df["Close"].shift()).abs()
    lc = (df["Low"] - df["Close"].shift()).abs()
    tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    return tr.rolling(period).mean()

# =========================
# DATA
# =========================
def download_data(symbol: str, period: str = "10d", interval: str = "5m") -> Optional[pd.DataFrame]:
    try:
        df = yf.download(
            symbol,
            period=period,
            interval=interval,
            progress=False,
            auto_adjust=False,
        )

        if df is None or df.empty:
            return None

        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        df.columns = [str(c) for c in df.columns]
        needed = ["Open", "High", "Low", "Close", "Volume"]
        if any(c not in df.columns for c in needed):
            return None

        df = df[needed].copy()
        for c in needed:
            df[c] = pd.to_numeric(df[c], errors="coerce")

        df.dropna(inplace=True)
        return df
    except Exception as e:
        print(f"{symbol} download error:", e)
        if "Rate limited" in str(e):
            time.sleep(20)
        return None

def build_signal_frame(df_5m: pd.DataFrame) -> Optional[pd.DataFrame]:
    df = df_5m.copy()

    df["ema_fast"] = ema(df["Close"], EMA_FAST)
    df["ema_slow"] = ema(df["Close"], EMA_SLOW)
    df["rsi"] = rsi(df["Close"], RSI_PERIOD)
    df["atr"] = atr(df, ATR_PERIOD)
    df["avg_volume"] = df["Volume"].rolling(VOLUME_LOOKBACK).mean()
    df["recent_high"] = df["High"].rolling(BREAKOUT_LOOKBACK).max().shift(1)

    df.dropna(inplace=True)
    if len(df) < max(EMA_SLOW + 5, VOLUME_LOOKBACK + 5, BREAKOUT_LOOKBACK + 5):
        return None
    return df

# =========================
# BROKER HELPERS
# =========================
def get_account_equity() -> Optional[float]:
    try:
        return float(api.get_account().equity)
    except Exception as e:
        print("Account equity error:", e)
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

def get_open_exposure_value(positions: Dict[str, object]) -> float:
    total = 0.0
    for p in positions.values():
        try:
            total += abs(float(p.market_value))
        except Exception:
            pass
    return total

def get_symbol_exposure_value(symbol: str, positions: Dict[str, object]) -> float:
    if symbol not in positions:
        return 0.0
    try:
        return abs(float(positions[symbol].market_value))
    except Exception:
        return 0.0

def in_buy_cooldown(symbol: str) -> bool:
    ts = last_buy_time.get(symbol)
    return ts is not None and (time.time() - ts) < BUY_COOLDOWN_SECONDS

def in_sell_cooldown(symbol: str) -> bool:
    ts = last_sell_time.get(symbol)
    return ts is not None and (time.time() - ts) < SELL_COOLDOWN_SECONDS

def get_filled_avg_price(order_id: str) -> Optional[float]:
    try:
        time.sleep(2)
        o = api.get_order(order_id)
        if o.filled_avg_price:
            return float(o.filled_avg_price)
    except Exception as e:
        print("Fill fetch error:", e)
    return None

# =========================
# DAILY LOSS CONTROL
# =========================
def rollover_day_if_needed() -> None:
    global current_trade_day, yesterday_realized_pnl
    global daily_realized_pnl, daily_closed_trades, daily_wins, daily_losses

    today = datetime.now().date()
    if current_trade_day is None:
        current_trade_day = today
        return

    if today != current_trade_day:
        yesterday_realized_pnl = daily_realized_pnl
        daily_realized_pnl = 0.0
        daily_closed_trades = 0
        daily_wins = 0
        daily_losses = 0
        current_trade_day = today

def get_dynamic_daily_loss_cap(equity: float) -> float:
    base = equity * BASE_DAILY_MAX_LOSS_PCT
    gain_based = max(0.0, yesterday_realized_pnl) * YESTERDAY_GAIN_GIVEBACK_PCT
    cap = max(base, gain_based)
    cap = min(cap, equity * MAX_DAILY_MAX_LOSS_PCT)
    return cap

def daily_loss_limit_hit(equity: float) -> bool:
    cap = get_dynamic_daily_loss_cap(equity)
    return daily_realized_pnl <= -cap

# =========================
# MARKET FILTER
# =========================
def market_is_healthy() -> bool:
    try:
        passes = 0
        checked = 0

        for symbol in MARKET_SYMBOLS:
            df = download_data(symbol, period="10d", interval="15m")
            if df is None or len(df) < 60:
                continue

            checked += 1
            df["ema_fast"] = ema(df["Close"], EMA_FAST)
            df["ema_slow"] = ema(df["Close"], EMA_SLOW)

            close_now = float(df["Close"].iloc[-1])
            fast_now = float(df["ema_fast"].iloc[-1])
            slow_now = float(df["ema_slow"].iloc[-1])

            if close_now > fast_now and fast_now > slow_now:
                passes += 1

        if checked == 0:
            return True

        return passes >= 1
    except Exception as e:
        print("Market filter error:", e)
        return True

# =========================
# ENTRY QUALITY SCORE
# =========================
def score_setup(df: pd.DataFrame) -> int:
    row = df.iloc[-1]
    prev = df.iloc[-2]

    score = 0

    if float(row["ema_fast"]) > float(row["ema_slow"]):
        score += 25

    if 58 <= float(row["rsi"]) <= 68:
        score += 20
    elif 55 <= float(row["rsi"]) <= 70:
        score += 10

    rvol = float(row["Volume"]) / max(float(row["avg_volume"]), 1.0)
    if rvol >= 2.0:
        score += 25
    elif rvol >= 1.5:
        score += 15

    breakout_level = float(row["recent_high"]) * (1 + BREAKOUT_BUFFER_PCT)
    if float(row["Close"]) > breakout_level:
        score += 20

    if float(row["Close"]) > float(prev["Close"]):
        score += 10

    return score

# =========================
# NEW HYBRID PULLBACK LOGIC
# =========================
def is_pullback_entry(df: pd.DataFrame) -> bool:
    try:
        ema9 = df["Close"].ewm(span=9).mean()
        ema20 = df["Close"].ewm(span=20).mean()

        trend_up = float(ema9.iloc[-1]) > float(ema20.iloc[-1])

        recent_high = float(df["High"].rolling(10).max().iloc[-2])
        current_price = float(df["Close"].iloc[-1])
        prev_close = float(df["Close"].iloc[-2])
        ema_fast_now = float(df["ema_fast"].iloc[-1])
        rsi_now = float(df["rsi"].iloc[-1])
        avg_volume = float(df["avg_volume"].iloc[-1])
        current_volume = float(df["Volume"].iloc[-1])

        # pullback into strong trend
        pullback = current_price < recent_high * 0.98

        # bounce confirmation
        bounce = current_price > prev_close

        # not too weak / not too overextended
        not_stretched = abs((current_price - ema_fast_now) / max(ema_fast_now, 1e-9)) <= 0.025
        rsi_ok = 50 <= rsi_now <= 67
        volume_ok = current_volume >= avg_volume * 1.1

        return trend_up and pullback and bounce and not_stretched and rsi_ok and volume_ok

    except Exception as e:
        print("Pullback check error:", e)
        return False

# =========================
# ENTRY LOGIC
# =========================
def try_enter(symbol: str, df: pd.DataFrame, positions: Dict[str, object], open_orders: Dict[str, list], equity: float) -> None:
    global cycle_traded, trade_state, highest_seen

    if symbol in cycle_traded:
        return
    if symbol in positions:
        return
    if symbol in open_orders and open_orders[symbol]:
        return
    if in_buy_cooldown(symbol):
        return
    if len(positions) >= MAX_TOTAL_POSITIONS:
        return
    if daily_loss_limit_hit(equity):
        print("Daily loss limit hit. No new entries.")
        return
    if not market_is_healthy():
        print("Market filter blocked new longs.")
        return

    row = df.iloc[-1]
    prev = df.iloc[-2]

    price = float(row["Close"])
    prev_close = float(prev["Close"])
    ema_fast_now = float(row["ema_fast"])
    ema_slow_now = float(row["ema_slow"])
    rsi_now = float(row["rsi"])
    atr_now = float(row["atr"])
    recent_high = float(row["recent_high"])
    avg_volume = float(row["avg_volume"])
    volume_now = float(row["Volume"])

    breakout_level = recent_high * (1 + BREAKOUT_BUFFER_PCT)
    chase_pct = (price - breakout_level) / breakout_level if breakout_level > 0 else 0
    ema_extension_pct = (price - ema_fast_now) / ema_fast_now if ema_fast_now > 0 else 0
    rvol = volume_now / max(avg_volume, 1.0)

    trend_ok = ema_fast_now > ema_slow_now
    momentum_ok = RSI_MIN <= rsi_now <= RSI_MAX
    breakout_ok = price > breakout_level
    volume_ok = rvol >= RVOL_MIN
    candle_ok = price > prev_close
    not_chasing = chase_pct <= MAX_CHASE_FROM_BREAKOUT_PCT
    not_stretched = ema_extension_pct <= MAX_EXT_FROM_EMA_FAST_PCT

    breakout_condition = (
        trend_ok and momentum_ok and breakout_ok and volume_ok and candle_ok and not_chasing and not_stretched
    )

    pullback_condition = is_pullback_entry(df)

    if not (breakout_condition or pullback_condition):
        return

    setup_score = score_setup(df)
    if breakout_condition and setup_score < 70:
        print(f"{symbol}: breakout score too low ({setup_score})")
        return

    total_exposure = get_open_exposure_value(positions)
    symbol_exposure = get_symbol_exposure_value(symbol, positions)

    max_total_exposure = equity * MAX_TOTAL_EXPOSURE_PCT
    max_symbol_exposure = equity * MAX_POSITION_PCT

    total_remaining = max_total_exposure - total_exposure
    symbol_remaining = max_symbol_exposure - symbol_exposure

    if total_remaining <= price or symbol_remaining <= price:
        print(f"{symbol}: exposure cap reached")
        return

    stop_price = min(price * (1 - HARD_STOP_PCT), price - atr_now)
    risk_per_share = price - stop_price

    if risk_per_share <= 0:
        return

    risk_dollars = equity * RISK_PER_TRADE
    qty_by_risk = int(risk_dollars // risk_per_share)
    qty_by_symbol_cap = int(symbol_remaining // price)
    qty_by_total_cap = int(total_remaining // price)

    qty = min(qty_by_risk, qty_by_symbol_cap, qty_by_total_cap)

    if qty <= 0:
        return

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
        trade_state[symbol] = {
            "entry_price": actual_fill,
            "entry_time": time.time(),
            "risk_per_share": risk_per_share,
            "initial_qty": qty,
            "partial_taken": 0,
            "break_even_active": 0,
        }

        entry_type = "PULLBACK" if pullback_condition and not breakout_condition else "BREAKOUT"
        if breakout_condition and pullback_condition:
            entry_type = "HYBRID"

        send_telegram(
            f"ENTRY {symbol} ({entry_type})\n"
            f"Price: ${actual_fill:.2f}\n"
            f"Qty: {qty}\n"
            f"Exposure: ${actual_fill * qty:.2f}\n"
            f"Setup score: {setup_score}"
        )

    except Exception as e:
        print(f"{symbol} entry error:", e)

# =========================
# EXIT LOGIC
# =========================
def try_manage_position(symbol: str, position, df: pd.DataFrame, open_orders: Dict[str, list]) -> None:
    global cycle_traded, daily_realized_pnl, daily_closed_trades, daily_wins, daily_losses
    global trade_state, highest_seen

    if symbol in cycle_traded:
        return
    if symbol in open_orders and open_orders[symbol]:
        return
    if in_sell_cooldown(symbol):
        return

    row = df.iloc[-1]
    prev = df.iloc[-2]

    price = float(row["Close"])
    prev_close = float(prev["Close"])
    rsi_now = float(row["rsi"])
    atr_now = float(row["atr"])
    recent_high = float(row["recent_high"])

    qty = int(float(position.qty))
    entry = float(position.avg_entry_price)

    if symbol not in highest_seen:
        highest_seen[symbol] = price
    highest_seen[symbol] = max(highest_seen[symbol], price)

    state = trade_state.get(symbol, {
        "entry_price": entry,
        "entry_time": time.time(),
        "risk_per_share": max(entry * HARD_STOP_PCT, 0.01),
        "initial_qty": qty,
        "partial_taken": 0,
        "break_even_active": 0,
    })

    pnl_pct_now = (price - entry) / entry
    risk_per_share = state["risk_per_share"]
    current_r = (price - entry) / risk_per_share if risk_per_share > 0 else 0

    hard_stop = entry * (1 - HARD_STOP_PCT)
    trailing_stop = highest_seen[symbol] - (atr_now * TRAILING_STOP_ATR_MULT)
    break_even_stop = entry

    bars_in_trade_est = max(1, int((time.time() - state["entry_time"]) // CHECK_INTERVAL))

    # 1. Partial profits
    if state["partial_taken"] == 0 and current_r >= FIRST_TARGET_R and qty >= 2:
        sell_qty = max(1, qty // 2)

        try:
            order = api.submit_order(
                symbol=symbol,
                qty=sell_qty,
                side="sell",
                type="market",
                time_in_force="day",
            )
            cycle_traded.add(symbol)
            last_sell_time[symbol] = time.time()

            fill_price = get_filled_avg_price(order.id)
            actual_fill = fill_price if fill_price is not None else price

            pnl_dollars = (actual_fill - entry) * sell_qty
            pnl_pct = ((actual_fill - entry) / entry) * 100

            daily_realized_pnl += pnl_dollars
            daily_closed_trades += 1
            if pnl_dollars >= 0:
                daily_wins += 1
            else:
                daily_losses += 1

            state["partial_taken"] = 1
            state["break_even_active"] = 1
            trade_state[symbol] = state

            send_telegram(
                f"PARTIAL PROFIT {symbol}\n"
                f"Price: ${actual_fill:.2f}\n"
                f"Qty sold: {sell_qty}\n"
                f"Trade P/L: ${pnl_dollars:.2f} ({pnl_pct:.2f}%)\n"
                f"Today P/L: ${daily_realized_pnl:.2f}"
            )
            return

        except Exception as e:
            print(f"{symbol} partial exit error:", e)
            return

    # 2. Full exit logic
    reason = None

    if price <= hard_stop:
        reason = "HARD STOP"
    elif state["break_even_active"] == 1 and price <= break_even_stop:
        reason = "BREAK-EVEN STOP"
    elif current_r >= FINAL_TARGET_R:
        reason = "FINAL TARGET"
    elif highest_seen[symbol] > entry and price <= trailing_stop:
        reason = "TRAILING STOP"
    elif price < recent_high and pnl_pct_now < 0:
        reason = "BREAKOUT FAIL"
    elif bars_in_trade_est <= FAIL_FAST_BARS and pnl_pct_now <= -FAIL_FAST_LOSS_PCT:
        reason = "FAIL FAST"
    elif bars_in_trade_est >= WEAK_HOLD_BARS and pnl_pct_now <= -WEAK_HOLD_LOSS_PCT:
        reason = "WEAK HOLD EXIT"
    elif rsi_now > EARLY_EXIT_RSI and price < prev_close:
        reason = "EARLY EXIT"

    if reason is None:
        return

    try:
        order = api.submit_order(
            symbol=symbol,
            qty=qty,
            side="sell",
            type="market",
            time_in_force="day",
        )

        cycle_traded.add(symbol)
        last_sell_time[symbol] = time.time()

        fill_price = get_filled_avg_price(order.id)
        actual_fill = fill_price if fill_price is not None else price

        pnl_dollars = (actual_fill - entry) * qty
        pnl_pct = ((actual_fill - entry) / entry) * 100

        daily_realized_pnl += pnl_dollars
        daily_closed_trades += 1
        if pnl_dollars >= 0:
            daily_wins += 1
        else:
            daily_losses += 1

        send_telegram(
            f"EXIT {symbol} - {reason}\n"
            f"Price: ${actual_fill:.2f}\n"
            f"Qty sold: {qty}\n"
            f"Trade P/L: ${pnl_dollars:.2f} ({pnl_pct:.2f}%)\n"
            f"Today P/L: ${daily_realized_pnl:.2f}"
        )

        trade_state.pop(symbol, None)
        highest_seen.pop(symbol, None)

    except Exception as e:
        print(f"{symbol} exit error:", e)

# =========================
# DAILY SUMMARY
# =========================
def maybe_send_daily_summary() -> None:
    global last_report_date

    now = datetime.now()
    today = now.date()

    if now.hour == REPORT_HOUR and now.minute >= REPORT_MINUTE:
        if last_report_date != today:
            send_telegram(
                f"Daily Summary 📊\n"
                f"Closed trades: {daily_closed_trades}\n"
                f"Wins: {daily_wins}\n"
                f"Losses: {daily_losses}\n"
                f"Realized P/L: ${daily_realized_pnl:.2f}\n"
                f"Yesterday P/L: ${yesterday_realized_pnl:.2f}"
            )
            last_report_date = today

# =========================
# MAIN LOOP
# =========================
def run_bot() -> None:
    global cycle_traded

    send_telegram("Elite stock bot is live 🚀")

    while True:
        try:
            rollover_day_if_needed()
            cycle_traded = set()

            equity = get_account_equity()
            if equity is None:
                time.sleep(CHECK_INTERVAL)
                continue

            positions = get_positions_dict()
            open_orders = get_open_orders_dict()

            for symbol in SYMBOLS:
                try:
                    raw_df = download_data(symbol, period="10d", interval="5m")
                    if raw_df is None:
                        time.sleep(2)
                        continue

                    df = build_signal_frame(raw_df)
                    if df is None:
                        time.sleep(2)
                        continue

                    positions = get_positions_dict()
                    open_orders = get_open_orders_dict()

                    if symbol in positions:
                        try_manage_position(symbol, positions[symbol], df, open_orders)
                    else:
                        try_enter(symbol, df, positions, open_orders, equity)

                    time.sleep(2)

                except Exception as e:
                    print(f"{symbol} loop error:", e)
                    time.sleep(2)

            maybe_send_daily_summary()
            time.sleep(CHECK_INTERVAL)

        except Exception as e:
            print("Main loop error:", e)
            time.sleep(CHECK_INTERVAL)

# =========================
# WEB
# =========================
@app.route("/")
def home():
    return "Elite stock bot is running"

if __name__ == "__main__":
    threading.Thread(target=run_bot, daemon=True).start()
    app.run(host="0.0.0.0", port=10000)
