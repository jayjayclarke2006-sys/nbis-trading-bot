import os
import time
from datetime import datetime, time as dtime
import pytz
import requests
import pandas as pd
import MetaTrader5 as mt5
from dotenv import load_dotenv

load_dotenv()

# =========================
# ENV VARIABLES
# =========================
SYMBOL = os.getenv("SYMBOL", "XAUUSD")
LOT = float(os.getenv("LOT", "0.10"))
TZ = pytz.timezone(os.getenv("BOT_TIMEZONE", "Europe/London"))

MT5_LOGIN = int(os.getenv("MT5_LOGIN"))
MT5_PASSWORD = os.getenv("MT5_PASSWORD")
MT5_SERVER = os.getenv("MT5_SERVER")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

RR = 2.0
MAGIC = 30001

# =========================
# SESSIONS
# =========================
SESSIONS = [
    {"name": "London 09:30", "mark_time": dtime(9, 30), "close_time": dtime(14, 30)},
    {"name": "New York 16:00", "mark_time": dtime(16, 0), "close_time": None},
    {"name": "Asia +1h", "mark_time": dtime(1, 0), "close_time": None},
]

state = {
    s["name"]: {
        "marked_date": None,
        "high": None,
        "low": None,
        "traded": False,
    }
    for s in SESSIONS
}

# =========================
# TELEGRAM
# =========================
def send_telegram(msg):
    if not TELEGRAM_BOT_TOKEN:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": msg})


# =========================
# MT5 CONNECTION
# =========================
def connect_mt5():
    if not mt5.initialize(login=MT5_LOGIN, password=MT5_PASSWORD, server=MT5_SERVER):
        raise RuntimeError(mt5.last_error())

    if not mt5.symbol_select(SYMBOL, True):
        raise RuntimeError(f"Symbol not found: {SYMBOL}")

    send_telegram(f"✅ Connected to MT5 ({SYMBOL})")


# =========================
# DATA
# =========================
def get_rates(tf, count=10):
    rates = mt5.copy_rates_from_pos(SYMBOL, tf, 0, count)
    if rates is None:
        return None
    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df["time"], unit="s", utc=True).dt.tz_convert(TZ)
    return df


# =========================
# MARK 30M RANGE
# =========================
def mark_range(session_name):
    df = get_rates(mt5.TIMEFRAME_M30, 3)
    if df is None or len(df) < 2:
        return

    candle = df.iloc[-2]

    state[session_name]["high"] = float(candle["high"])
    state[session_name]["low"] = float(candle["low"])
    state[session_name]["traded"] = False
    state[session_name]["marked_date"] = datetime.now(TZ).date()

    send_telegram(
        f"📌 {session_name}\nHigh: {candle['high']}\nLow: {candle['low']}"
    )


# =========================
# CHECK BREAKOUT
# =========================
def check_breakout(session_name):
    if state[session_name]["traded"]:
        return

    high = state[session_name]["high"]
    low = state[session_name]["low"]

    if high is None:
        return

    df = get_rates(mt5.TIMEFRAME_M5, 3)
    if df is None or len(df) < 2:
        return

    candle = df.iloc[-2]
    close = float(candle["close"])

    buffer = 0.2  # gold noise filter

    if close > high + buffer:
        place_trade("BUY", session_name)

    elif close < low - buffer:
        place_trade("SELL", session_name)


# =========================
# PLACE TRADE
# =========================
def place_trade(direction, session_name):
    tick = mt5.symbol_info_tick(SYMBOL)

    spread = tick.ask - tick.bid
    if spread > 0.5:
        send_telegram(f"⚠️ Spread too high: {spread}")
        return

    high = state[session_name]["high"]
    low = state[session_name]["low"]

    if direction == "BUY":
        price = tick.ask
        sl = low
        risk = price - sl
        tp = price + risk * RR
        order_type = mt5.ORDER_TYPE_BUY
    else:
        price = tick.bid
        sl = high
        risk = sl - price
        tp = price - risk * RR
        order_type = mt5.ORDER_TYPE_SELL

    if risk <= 0:
        return

    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": SYMBOL,
        "volume": LOT,
        "type": order_type,
        "price": price,
        "sl": sl,
        "tp": tp,
        "deviation": 20,
        "magic": MAGIC,
        "comment": session_name,
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }

    result = mt5.order_send(request)

    if result and result.retcode == mt5.TRADE_RETCODE_DONE:
        state[session_name]["traded"] = True

        send_telegram(
            f"🚀 {direction} {session_name}\nEntry: {price}\nSL: {sl}\nTP: {tp}"
        )
    else:
        send_telegram(f"❌ Trade failed {result}")


# =========================
# CLOSE POSITION
# =========================
def close_all():
    positions = mt5.positions_get(symbol=SYMBOL)
    if positions is None:
        return

    for p in positions:
        tick = mt5.symbol_info_tick(SYMBOL)

        if p.type == mt5.POSITION_TYPE_BUY:
            order_type = mt5.ORDER_TYPE_SELL
            price = tick.bid
        else:
            order_type = mt5.ORDER_TYPE_BUY
            price = tick.ask

        mt5.order_send({
            "action": mt5.TRADE_ACTION_DEAL,
            "position": p.ticket,
            "symbol": SYMBOL,
            "volume": p.volume,
            "type": order_type,
            "price": price,
            "deviation": 20,
            "magic": MAGIC,
        })

    send_telegram("🔒 Positions closed")


# =========================
# MAIN LOOP
# =========================
def main():
    connect_mt5()

    while True:
        now = datetime.now(TZ)

        for s in SESSIONS:
            name = s["name"]

            if now.hour == s["mark_time"].hour and now.minute == s["mark_time"].minute:
                if state[name]["marked_date"] != now.date():
                    mark_range(name)

            if state[name]["marked_date"] == now.date():
                check_breakout(name)

            if s["close_time"]:
                if now.hour == s["close_time"].hour and now.minute == s["close_time"].minute:
                    close_all()

        time.sleep(10)


if __name__ == "__main__":
    main()


       
