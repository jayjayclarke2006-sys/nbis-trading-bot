import os
import time
import math
import requests
import pandas as pd
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

# ============================================================
# NBIS ALPACA STOCK BOT - CLEAN ALERTS VERSION
# ============================================================

E_CHECK = "\u2705"
E_FIRE = "\U0001F525"
E_WARN = "\u26A0\uFE0F"
E_ROCKET = "\U0001F680"
E_DOWN = "\U0001F4C9"
E_CHART = "\U0001F4CA"
E_SLEEP = "\U0001F634"
E_CROSS = "\u274C"

ALPACA_API_KEY = os.getenv("ALPACA_API_KEY") or os.getenv("APCA_API_KEY_ID") or os.getenv("ALPACA_KEY_ID")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY") or os.getenv("APCA_API_SECRET_KEY") or os.getenv("ALPACA_SECRET")
ALPACA_PAPER = os.getenv("ALPACA_PAPER", "true").lower() in ["1", "true", "yes", "y"]
EXECUTE_ORDERS = os.getenv("EXECUTE_ORDERS", "true" if ALPACA_PAPER else "false").lower() in ["1", "true", "yes", "y"]

ALPACA_TRADE_BASE = "https://paper-api.alpaca.markets" if ALPACA_PAPER else "https://api.alpaca.markets"
ALPACA_DATA_BASE = "https://data.alpaca.markets"
ALPACA_DATA_FEED = os.getenv("ALPACA_DATA_FEED", "iex")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID") or os.getenv("TELEGRAM_CHAT_ID")

HEADERS = {
    "APCA-API-KEY-ID": ALPACA_API_KEY or "",
    "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY or "",
    "Content-Type": "application/json",
}

NY_TZ = ZoneInfo("America/New_York")

CHECK_INTERVAL = 60

WATCHLIST = ["AAPL", "TSLA", "NVDA", "AMD", "META", "MSFT", "AMZN", "SPY", "QQQ"]

RISK_PER_TRADE = 0.005
MAX_POSITION_PCT = 0.12
MAX_TRADES_PER_DAY = 4
ALLOW_SHORTS = os.getenv("ALLOW_SHORTS", "false").lower() in ["1", "true", "yes", "y"]

RR_TARGET = 2.0
ATR_LEN = 14
EMA_FAST = 20
EMA_MID = 50
EMA_SLOW = 200

MIN_ATR_PCT = 0.0030
MIN_VOLUME_MULT = 0.90
MIN_BODY_ATR = 0.20
MAX_BODY_ATR = 2.20
RETEST_BUFFER_ATR = 0.25
PULLBACK_BUFFER_ATR = 0.30

MARKET_OPEN = "09:30"
OPENING_RANGE_END = "10:00"
TRADE_START = "10:00"
LAST_ENTRY_TIME = "15:30"
MARKET_CLOSE = "16:00"
EOD_SUMMARY_TIME = "16:05"

STATE = {
    symbol: {
        "DATE": None,
        "OR_HIGH": None,
        "OR_LOW": None,
        "OR_SET": False,
        "BREAK_SIDE": None,
        "RETEST_DONE": False,
        "TRADED_TODAY": False,
        "IN_POSITION": False,
        "LAST_REASON": "STARTING",
        "LAST_PRICE": None,
        "ORDER_ID": None,
        "ORDER_STATUS_NOTIFIED": None,
        "LAST_SIGNAL_MODEL": None,
    }
    for symbol in WATCHLIST
}

BOT_STATE = {
    "DATE": None,
    "TRADES_TODAY": 0,
    "SENT_KEYS": set(),
    "EOD_SENT": False,
}

def send(msg: str):
    print(msg)
    if not TELEGRAM_TOKEN or not CHAT_ID:
        print("TELEGRAM NOT SET")
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": msg},
            timeout=10,
        )
    except Exception as e:
        print("TELEGRAM ERROR:", e)

def send_once(key: str, msg: str):
    if key in BOT_STATE["SENT_KEYS"]:
        return
    send(msg)
    BOT_STATE["SENT_KEYS"].add(key)

def now_ny() -> datetime:
    return datetime.now(NY_TZ)

def to_minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)

def minute_of_day(dt: datetime) -> int:
    return dt.hour * 60 + dt.minute

def in_window(dt: datetime, start: str, end: str) -> bool:
    m = minute_of_day(dt)
    return to_minutes(start) <= m <= to_minutes(end)

def iso_utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

def reset_daily_state():
    today = now_ny().date()
    if BOT_STATE["DATE"] == today:
        return

    BOT_STATE["DATE"] = today
    BOT_STATE["TRADES_TODAY"] = 0
    BOT_STATE["SENT_KEYS"] = set()
    BOT_STATE["EOD_SENT"] = False

    for sym in WATCHLIST:
        STATE[sym]["DATE"] = today
        STATE[sym]["OR_HIGH"] = None
        STATE[sym]["OR_LOW"] = None
        STATE[sym]["OR_SET"] = False
        STATE[sym]["BREAK_SIDE"] = None
        STATE[sym]["RETEST_DONE"] = False
        STATE[sym]["TRADED_TODAY"] = False
        STATE[sym]["IN_POSITION"] = False
        STATE[sym]["LAST_REASON"] = "NEW_DAY"
        STATE[sym]["LAST_PRICE"] = None
        STATE[sym]["ORDER_ID"] = None
        STATE[sym]["ORDER_STATUS_NOTIFIED"] = None
        STATE[sym]["LAST_SIGNAL_MODEL"] = None

def alpaca_get(path: str, params=None, data_api=False):
    base = ALPACA_DATA_BASE if data_api else ALPACA_TRADE_BASE
    try:
        r = requests.get(f"{base}{path}", headers=HEADERS, params=params or {}, timeout=15)
        if r.status_code >= 400:
            print("ALPACA GET ERROR:", r.status_code, r.text[:500])
            return None
        return r.json()
    except Exception as e:
        print("ALPACA GET EXCEPTION:", e)
        return None

def alpaca_post(path: str, payload: dict):
    try:
        r = requests.post(f"{ALPACA_TRADE_BASE}{path}", headers=HEADERS, json=payload, timeout=15)
        if r.status_code >= 400:
            print("ALPACA POST ERROR:", r.status_code, r.text[:500])
            return None
        return r.json()
    except Exception as e:
        print("ALPACA POST EXCEPTION:", e)
        return None

def get_account():
    return alpaca_get("/v2/account")

def get_positions():
    data = alpaca_get("/v2/positions")
    return data if isinstance(data, list) else []

def get_open_orders():
    data = alpaca_get("/v2/orders", params={"status": "open", "limit": 500})
    return data if isinstance(data, list) else []

def get_order(order_id: str):
    if not order_id or order_id == "DRY_RUN":
        return None
    return alpaca_get(f"/v2/orders/{order_id}")

def has_position(symbol: str) -> bool:
    for p in get_positions():
        if p.get("symbol") == symbol and float(p.get("qty", 0)) != 0:
            return True
    return False

def has_open_order(symbol: str) -> bool:
    for o in get_open_orders():
        if o.get("symbol") == symbol and o.get("status") in ["new", "accepted", "pending_new", "held"]:
            return True
    return False

def bars_to_df(symbol: str, timeframe: str, limit: int = 300) -> pd.DataFrame:
    end = now_ny()
    start = end - timedelta(days=10)
    params = {
        "symbols": symbol,
        "timeframe": timeframe,
        "start": iso_utc(start),
        "end": iso_utc(end),
        "limit": limit,
        "adjustment": "raw",
        "feed": ALPACA_DATA_FEED,
        "sort": "asc",
    }

    data = alpaca_get("/v2/stocks/bars", params=params, data_api=True)
    if not data or "bars" not in data:
        return pd.DataFrame()

    rows = data["bars"].get(symbol, [])
    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)
    df.rename(
        columns={"t": "time", "o": "open", "h": "high", "l": "low", "c": "close", "v": "volume"},
        inplace=True
    )

    needed = ["time", "open", "high", "low", "close", "volume"]
    for col in needed:
        if col not in df.columns:
            return pd.DataFrame()

    df["time"] = pd.to_datetime(df["time"], utc=True).dt.tz_convert(NY_TZ)
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    df.dropna(inplace=True)
    return df[needed].reset_index(drop=True)

def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty or len(df) < EMA_SLOW + 5:
        return pd.DataFrame()

    out = df.copy()
    out["ema20"] = out["close"].ewm(span=EMA_FAST, adjust=False).mean()
    out["ema50"] = out["close"].ewm(span=EMA_MID, adjust=False).mean()
    out["ema200"] = out["close"].ewm(span=EMA_SLOW, adjust=False).mean()

    tr = pd.concat([
        out["high"] - out["low"],
        (out["high"] - out["close"].shift()).abs(),
        (out["low"] - out["close"].shift()).abs(),
    ], axis=1).max(axis=1)

    out["atr"] = tr.rolling(ATR_LEN).mean()
    out["atr_pct"] = out["atr"] / out["close"]
    out["body"] = (out["close"] - out["open"]).abs()
    out["upper_wick"] = out["high"] - out[["open", "close"]].max(axis=1)
    out["lower_wick"] = out[["open", "close"]].min(axis=1) - out["low"]
    out["vol_ma"] = out["volume"].rolling(20).mean()

    out.dropna(inplace=True)
    return out.reset_index(drop=True)

def htf_bias(symbol: str) -> str:
    df = add_indicators(bars_to_df(symbol, "15Min", 400))
    if df.empty:
        return "NONE"

    r = df.iloc[-1]
    p = df.iloc[-2]

    if r["close"] > r["ema50"] > r["ema200"] and r["ema50"] >= p["ema50"]:
        return "BULL"
    if r["close"] < r["ema50"] < r["ema200"] and r["ema50"] <= p["ema50"]:
        return "BEAR"
    if r["close"] > r["ema200"]:
        return "BULL_WEAK"
    if r["close"] < r["ema200"]:
        return "BEAR_WEAK"
    return "CHOP"

def candle_quality(row) -> bool:
    atr = float(row["atr"])
    body = float(row["body"])
    if atr <= 0:
        return False
    body_atr = body / atr
    return MIN_BODY_ATR <= body_atr <= MAX_BODY_ATR

def volume_ok(row) -> bool:
    if float(row["vol_ma"]) <= 0:
        return True
    return float(row["volume"]) >= float(row["vol_ma"]) * MIN_VOLUME_MULT

def not_dead_chop(df: pd.DataFrame) -> bool:
    if len(df) < 20:
        return False
    recent = df.tail(12)
    avg_range = (recent["high"] - recent["low"]).mean()
    atr = float(df.iloc[-1]["atr"])
    if atr <= 0:
        return False
    return avg_range >= atr * 0.55

def strong_rejection(row, side: str) -> bool:
    open_ = float(row["open"])
    high = float(row["high"])
    low = float(row["low"])
    close = float(row["close"])
    body = abs(close - open_)
    if body <= 0:
        return False

    upper = high - max(open_, close)
    lower = min(open_, close) - low

    if side == "LONG":
        return close > open_ and lower >= body * 0.45
    if side == "SHORT":
        return close < open_ and upper >= body * 0.45
    return False

def bias_allows(side: str, bias: str) -> bool:
    if side == "LONG":
        return bias in ["BULL", "BULL_WEAK"]
    if side == "SHORT":
        return ALLOW_SHORTS and bias in ["BEAR", "BEAR_WEAK"]
    return False

def build_opening_range(symbol: str, df5: pd.DataFrame):
    s = STATE[symbol]
    if s["OR_SET"] or df5.empty:
        return

    today = now_ny().date()
    session = df5[df5["time"].dt.date == today]
    if session.empty:
        return

    open_min = to_minutes(MARKET_OPEN)
    or_end_min = to_minutes(OPENING_RANGE_END)
    opening = session[session["time"].apply(lambda x: open_min <= minute_of_day(x) < or_end_min)]

    if opening.empty:
        return
    if minute_of_day(now_ny()) < or_end_min:
        return

    s["OR_HIGH"] = float(opening["high"].max())
    s["OR_LOW"] = float(opening["low"].min())
    s["OR_SET"] = True
    s["LAST_REASON"] = "OPENING_RANGE_SET"

def get_signal(symbol: str):
    s = STATE[symbol]
    df5_raw = bars_to_df(symbol, "5Min", 400)
    df5 = add_indicators(df5_raw)

    if df5.empty:
        s["LAST_REASON"] = "NO_DATA"
        return None

    build_opening_range(symbol, df5)

    r = df5.iloc[-1]
    close = float(r["close"])
    high = float(r["high"])
    low = float(r["low"])
    open_ = float(r["open"])
    atr = float(r["atr"])
    s["LAST_PRICE"] = close

    t = now_ny()

    if not in_window(t, TRADE_START, LAST_ENTRY_TIME):
        s["LAST_REASON"] = "OUTSIDE_TRADE_WINDOW"
        return None
    if not s["OR_SET"]:
        s["LAST_REASON"] = "OPENING_RANGE_NOT_READY"
        return None
    if s["TRADED_TODAY"]:
        s["LAST_REASON"] = "TRADED_TODAY"
        return None
    if BOT_STATE["TRADES_TODAY"] >= MAX_TRADES_PER_DAY:
        s["LAST_REASON"] = "MAX_DAILY_TRADES"
        return None
    if has_position(symbol):
        s["IN_POSITION"] = True
        s["LAST_REASON"] = "ALREADY_IN_POSITION"
        return None
    if has_open_order(symbol):
        s["LAST_REASON"] = "OPEN_ORDER_EXISTS"
        return None

    bias = htf_bias(symbol)
    if bias in ["CHOP", "NONE"]:
        s["LAST_REASON"] = f"HTF_{bias}"
        return None
    if float(r["atr_pct"]) < MIN_ATR_PCT:
        s["LAST_REASON"] = "LOW_VOLATILITY"
        return None
    if not not_dead_chop(df5):
        s["LAST_REASON"] = "CHOP_BLOCK"
        return None
    if not volume_ok(r):
        s["LAST_REASON"] = "LOW_VOLUME"
        return None

    # Model 1: Opening range break + retest
    if s["BREAK_SIDE"] is None:
        if close > s["OR_HIGH"] and bias_allows("LONG", bias) and candle_quality(r):
            s["BREAK_SIDE"] = "LONG"
            s["LAST_REASON"] = "OR_BREAK_LONG_WAIT_RETEST"
            return None

        if close < s["OR_LOW"] and bias_allows("SHORT", bias) and candle_quality(r):
            s["BREAK_SIDE"] = "SHORT"
            s["LAST_REASON"] = "OR_BREAK_SHORT_WAIT_RETEST"
            return None

    if s["BREAK_SIDE"] == "LONG" and not s["RETEST_DONE"]:
        if low <= s["OR_HIGH"] + atr * RETEST_BUFFER_ATR:
            s["RETEST_DONE"] = True
            s["LAST_REASON"] = "LONG_RETEST_HIT"
            return None

    if s["BREAK_SIDE"] == "SHORT" and not s["RETEST_DONE"]:
        if high >= s["OR_LOW"] - atr * RETEST_BUFFER_ATR:
            s["RETEST_DONE"] = True
            s["LAST_REASON"] = "SHORT_RETEST_HIT"
            return None

    if s["BREAK_SIDE"] == "LONG" and s["RETEST_DONE"]:
        if strong_rejection(r, "LONG") and bias_allows("LONG", bias) and candle_quality(r):
            sl = min(low, s["OR_LOW"])
            risk = close - sl
            if risk > 0:
                s["LAST_SIGNAL_MODEL"] = "OR_BREAK_RETEST_LONG"
                return {
                    "symbol": symbol,
                    "side": "buy",
                    "model": "OR_BREAK_RETEST_LONG",
                    "price": close,
                    "sl": sl,
                    "tp": close + risk * RR_TARGET,
                    "bias": bias,
                }

    if s["BREAK_SIDE"] == "SHORT" and s["RETEST_DONE"]:
        if strong_rejection(r, "SHORT") and bias_allows("SHORT", bias) and candle_quality(r):
            sl = max(high, s["OR_HIGH"])
            risk = sl - close
            if risk > 0:
                s["LAST_SIGNAL_MODEL"] = "OR_BREAK_RETEST_SHORT"
                return {
                    "symbol": symbol,
                    "side": "sell",
                    "model": "OR_BREAK_RETEST_SHORT",
                    "price": close,
                    "sl": sl,
                    "tp": close - risk * RR_TARGET,
                    "bias": bias,
                }

    # Model 2: Trend pullback continuation
    if bias_allows("LONG", bias):
        touched_value = low <= float(r["ema20"]) + atr * PULLBACK_BUFFER_ATR
        reclaimed = close > open_ and close > float(r["ema20"])
        if touched_value and reclaimed and strong_rejection(r, "LONG") and candle_quality(r):
            sl = min(low, float(r["ema50"]) - atr * 0.15)
            risk = close - sl
            if risk > 0:
                s["LAST_SIGNAL_MODEL"] = "TREND_PULLBACK_LONG"
                return {
                    "symbol": symbol,
                    "side": "buy",
                    "model": "TREND_PULLBACK_LONG",
                    "price": close,
                    "sl": sl,
                    "tp": close + risk * RR_TARGET,
                    "bias": bias,
                }

    if bias_allows("SHORT", bias):
        touched_value = high >= float(r["ema20"]) - atr * PULLBACK_BUFFER_ATR
        rejected = close < open_ and close < float(r["ema20"])
        if touched_value and rejected and strong_rejection(r, "SHORT") and candle_quality(r):
            sl = max(high, float(r["ema50"]) + atr * 0.15)
            risk = sl - close
            if risk > 0:
                s["LAST_SIGNAL_MODEL"] = "TREND_PULLBACK_SHORT"
                return {
                    "symbol": symbol,
                    "side": "sell",
                    "model": "TREND_PULLBACK_SHORT",
                    "price": close,
                    "sl": sl,
                    "tp": close - risk * RR_TARGET,
                    "bias": bias,
                }

    s["LAST_REASON"] = "NO_SETUP"
    return None

def round_price(x: float) -> float:
    return round(float(x), 2)

def calculate_qty(signal: dict) -> int:
    account = get_account()
    if not account:
        return 0

    equity = float(account.get("equity", 0))
    buying_power = float(account.get("buying_power", 0))
    if equity <= 0 or buying_power <= 0:
        return 0

    risk_cash = equity * RISK_PER_TRADE
    risk_per_share = abs(float(signal["price"]) - float(signal["sl"]))
    if risk_per_share <= 0:
        return 0

    risk_qty = math.floor(risk_cash / risk_per_share)
    max_cash = min(equity, buying_power) * MAX_POSITION_PCT
    cash_qty = math.floor(max_cash / float(signal["price"]))
    return max(min(risk_qty, cash_qty), 0)

def submit_bracket_order(signal: dict):
    symbol = signal["symbol"]
    qty = calculate_qty(signal)

    if qty <= 0:
        return {"ok": False, "reason": "qty calculated as 0", "dry_run": False, "qty": 0}

    if not EXECUTE_ORDERS:
        return {"ok": True, "dry_run": True, "qty": qty, "id": "DRY_RUN"}

    payload = {
        "symbol": symbol,
        "qty": str(qty),
        "side": signal["side"],
        "type": "market",
        "time_in_force": "day",
        "order_class": "bracket",
        "take_profit": {"limit_price": str(round_price(signal["tp"]))},
        "stop_loss": {"stop_price": str(round_price(signal["sl"]))},
    }

    result = alpaca_post("/v2/orders", payload)
    if result:
        return {"ok": True, "dry_run": False, "qty": qty, "id": result.get("id", "UNKNOWN"), "raw": result}

    return {"ok": False, "reason": "alpaca order rejected or request failed", "dry_run": False, "qty": qty}

def handle_signal(signal: dict):
    symbol = signal["symbol"]
    result = submit_bracket_order(signal)

    if not result["ok"]:
        send_once(
            f"{BOT_STATE['DATE']}:{symbol}:ORDER_FAIL",
            f"{E_WARN} {symbol} ORDER FAILED\n\nReason: {result['reason']}"
        )
        return

    STATE[symbol]["TRADED_TODAY"] = True
    STATE[symbol]["ORDER_ID"] = result["id"]
    STATE[symbol]["ORDER_STATUS_NOTIFIED"] = "submitted"
    BOT_STATE["TRADES_TODAY"] += 1

    icon = E_ROCKET if signal["side"] == "buy" else E_DOWN
    mode_text = "PAPER SIGNAL" if result["dry_run"] else "ORDER SENT"

    send_once(
        f"{BOT_STATE['DATE']}:{symbol}:SIGNAL",
        f"{icon} {symbol} {mode_text}\n\n"
        f"Model: {signal['model']}\n"
        f"Side: {signal['side'].upper()}\n"
        f"Bias: {signal['bias']}\n"
        f"Qty: {result['qty']}\n"
        f"Entry ref: ${signal['price']:.2f}\n"
        f"SL: ${signal['sl']:.2f}\n"
        f"TP: ${signal['tp']:.2f}\n"
        f"RR: 1:{RR_TARGET}\n"
        f"Paper: {ALPACA_PAPER}\n"
        f"Execute: {EXECUTE_ORDERS}"
    )

def check_order_updates():
    for symbol in WATCHLIST:
        s = STATE[symbol]
        order_id = s["ORDER_ID"]

        if not order_id or order_id == "DRY_RUN":
            continue

        order = get_order(order_id)
        if not order:
            continue

        status = order.get("status", "unknown")
        if s["ORDER_STATUS_NOTIFIED"] == status:
            continue

        if status in ["filled"]:
            send_once(
                f"{BOT_STATE['DATE']}:{symbol}:FILLED",
                f"{E_CHECK} {symbol} ORDER FILLED\n\n"
                f"Status: {status}\n"
                f"Order ID: {order_id}"
            )

        elif status in ["canceled", "expired", "rejected", "suspended"]:
            send_once(
                f"{BOT_STATE['DATE']}:{symbol}:{status}",
                f"{E_CROSS} {symbol} ORDER {status.upper()}\n\n"
                f"Order ID: {order_id}"
            )

        s["ORDER_STATUS_NOTIFIED"] = status

def end_of_day_summary():
    t = now_ny()
    if minute_of_day(t) < to_minutes(EOD_SUMMARY_TIME):
        return
    if BOT_STATE["EOD_SENT"]:
        return

    positions = get_positions()
    open_orders = get_open_orders()

    reason_counts = {}
    traded_symbols = []

    for symbol in WATCHLIST:
        s = STATE[symbol]
        reason = s["LAST_REASON"]
        reason_counts[reason] = reason_counts.get(reason, 0) + 1
        if s["TRADED_TODAY"]:
            traded_symbols.append(symbol)

    top_reasons = sorted(reason_counts.items(), key=lambda x: x[1], reverse=True)
    top_reason_text = ", ".join([f"{k}={v}" for k, v in top_reasons[:5]]) if top_reasons else "NONE"
    traded_text = ", ".join(traded_symbols) if traded_symbols else "NONE"

    send(
        f"{E_SLEEP} END OF DAY SUMMARY\n\n"
        f"Trades today: {BOT_STATE['TRADES_TODAY']}/{MAX_TRADES_PER_DAY}\n"
        f"Traded symbols: {traded_text}\n"
        f"Open positions: {len(positions)}\n"
        f"Open orders: {len(open_orders)}\n"
        f"Top states: {top_reason_text}\n"
        f"Paper: {ALPACA_PAPER}\n"
        f"Execute: {EXECUTE_ORDERS}"
    )

    BOT_STATE["EOD_SENT"] = True

def startup_check():
    if not ALPACA_API_KEY or not ALPACA_SECRET_KEY:
        send(f"{E_WARN} ALPACA KEYS MISSING\n\nSet ALPACA_API_KEY and ALPACA_SECRET_KEY.")
        return False

    account = get_account()
    if not account:
        send(f"{E_WARN} ALPACA ACCOUNT CHECK FAILED\n\nCheck API keys, paper/live setting, or Alpaca connection.")
        return False

    send(
        f"{E_FIRE} ALPACA STOCK BOT LIVE {E_FIRE}\n\n"
        f"Mode: {'PAPER' if ALPACA_PAPER else 'LIVE'}\n"
        f"Execute orders: {EXECUTE_ORDERS}\n"
        f"Data feed: {ALPACA_DATA_FEED}\n"
        f"Equity: ${float(account.get('equity', 0)):.2f}\n"
        f"Buying Power: ${float(account.get('buying_power', 0)):.2f}\n"
        f"Watchlist: {', '.join(WATCHLIST)}\n\n"
        f"Alerts: CLEAN MODE\n"
        f"- startup\n"
        f"- signal / order sent\n"
        f"- order filled / failed\n"
        f"- end of day summary"
    )
    return True

def run():
    time.sleep(3)
    if not startup_check():
        return

    while True:
        try:
            reset_daily_state()
            t = now_ny()

            if not in_window(t, MARKET_OPEN, MARKET_CLOSE):
                for symbol in WATCHLIST:
                    STATE[symbol]["LAST_REASON"] = "MARKET_CLOSED"
                check_order_updates()
                end_of_day_summary()
                time.sleep(CHECK_INTERVAL)
                continue

            for symbol in WATCHLIST:
                signal = get_signal(symbol)
                if signal:
                    handle_signal(signal)
                time.sleep(1)

            check_order_updates()
            end_of_day_summary()
            time.sleep(CHECK_INTERVAL)

        except Exception as e:
            send(f"{E_WARN} STOCK BOT ERROR:\n{e}")
            time.sleep(15)

if __name__ == "__main__":
    run()
