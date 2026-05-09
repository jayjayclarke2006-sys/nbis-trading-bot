import os
import time
import math
import json
import requests
import pandas as pd
import numpy as np
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

# ============================================================
# NBIS ALPACA STOCK BOT - LIVE + WALK-FORWARD + MONTE CARLO
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

WATCHLIST = [
    "AAPL", "TSLA", "NVDA", "AMD", "META", "MSFT",
    "AMZN", "SPY", "QQQ", "NBIS", "WULF", "IREN"
]

RISK_PER_TRADE = 0.005
MAX_POSITION_PCT = 0.12
MAX_TRADES_PER_DAY = 6
ALLOW_SHORTS = os.getenv("ALLOW_SHORTS", "false").lower() in ["1", "true", "yes", "y"]

# Live defaults. Walk-forward can override these per symbol.
RR_TARGET = 1.8
ATR_LEN = 14
EMA_FAST = 20
EMA_MID = 50
EMA_SLOW = 200

MIN_ATR_PCT = 0.0020
MIN_VOLUME_MULT = 0.75
MIN_BODY_ATR = 0.12
MAX_BODY_ATR = 3.00
RETEST_BUFFER_ATR = 0.35
PULLBACK_BUFFER_ATR = 0.45
CHOP_RANGE_MULT = 0.45

MARKET_OPEN = "09:30"
OPENING_RANGE_END = "10:00"
TRADE_START = "10:00"
LAST_ENTRY_TIME = "15:30"
MARKET_CLOSE = "16:00"
EOD_SUMMARY_TIME = "16:05"

RUN_RESEARCH_AT_STARTUP = os.getenv("RUN_RESEARCH_AT_STARTUP", "true").lower() in ["1", "true", "yes", "y"]
RUN_RESEARCH_AFTER_CLOSE = os.getenv("RUN_RESEARCH_AFTER_CLOSE", "true").lower() in ["1", "true", "yes", "y"]
RESEARCH_DAYS = int(os.getenv("RESEARCH_DAYS", "45"))
WF_TRAIN_DAYS = int(os.getenv("WF_TRAIN_DAYS", "15"))
WF_TEST_DAYS = int(os.getenv("WF_TEST_DAYS", "5"))
MC_RUNS = int(os.getenv("MC_RUNS", "500"))
RESEARCH_CACHE_FILE = os.getenv("RESEARCH_CACHE_FILE", "stock_research_cache.json")

STATE = {
    symbol: {
        "DATE": None,
        "OR_HIGH": None,
        "OR_LOW": None,
        "OR_SET": False,
        "BREAK_SIDE": None,
        "BREAK_BAR_INDEX": None,
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
    "RESEARCH_RAN_TODAY": False,
}

RESEARCH_CACHE = {}


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


def load_research_cache():
    global RESEARCH_CACHE
    if not os.path.exists(RESEARCH_CACHE_FILE):
        RESEARCH_CACHE = {}
        return
    try:
        with open(RESEARCH_CACHE_FILE, "r") as f:
            RESEARCH_CACHE = json.load(f)
    except Exception:
        RESEARCH_CACHE = {}


def save_research_cache():
    try:
        with open(RESEARCH_CACHE_FILE, "w") as f:
            json.dump(RESEARCH_CACHE, f, indent=2)
    except Exception as e:
        print("RESEARCH CACHE SAVE ERROR:", e)


def reset_daily_state():
    today = now_ny().date()
    if BOT_STATE["DATE"] == today:
        return

    BOT_STATE["DATE"] = today
    BOT_STATE["TRADES_TODAY"] = 0
    BOT_STATE["SENT_KEYS"] = set()
    BOT_STATE["EOD_SENT"] = False
    BOT_STATE["RESEARCH_RAN_TODAY"] = False

    for sym in WATCHLIST:
        STATE[sym]["DATE"] = today
        STATE[sym]["OR_HIGH"] = None
        STATE[sym]["OR_LOW"] = None
        STATE[sym]["OR_SET"] = False
        STATE[sym]["BREAK_SIDE"] = None
        STATE[sym]["BREAK_BAR_INDEX"] = None
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
        r = requests.get(f"{base}{path}", headers=HEADERS, params=params or {}, timeout=20)
        if r.status_code >= 400:
            print("ALPACA GET ERROR:", r.status_code, r.text[:500])
            return None
        return r.json()
    except Exception as e:
        print("ALPACA GET EXCEPTION:", e)
        return None


def alpaca_post(path: str, payload: dict):
    try:
        r = requests.post(f"{ALPACA_TRADE_BASE}{path}", headers=HEADERS, json=payload, timeout=20)
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


def bars_to_df(symbol: str, timeframe: str, limit: int = 300, days: int = 10) -> pd.DataFrame:
    end = now_ny()
    start = end - timedelta(days=days)
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
    df = df[needed].reset_index(drop=True)
    return df


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


def classify_bias_row(row) -> str:
    if row["close"] > row["ema20"] > row["ema50"]:
        return "BULL"
    if row["close"] < row["ema20"] < row["ema50"]:
        return "BEAR"
    if row["close"] > row["ema50"] and row["close"] > row["ema200"]:
        return "BULL_WEAK"
    if row["close"] < row["ema50"] and row["close"] < row["ema200"]:
        return "BEAR_WEAK"
    return "CHOP"


def htf_bias(symbol: str) -> str:
    df = add_indicators(bars_to_df(symbol, "15Min", 500, 20))
    if df.empty:
        return "NONE"
    return classify_bias_row(df.iloc[-1])


def get_live_params(symbol: str) -> dict:
    cached = RESEARCH_CACHE.get(symbol, {})
    best = cached.get("best_params", {})
    return {
        "rr_target": float(best.get("rr_target", RR_TARGET)),
        "min_atr_pct": float(best.get("min_atr_pct", MIN_ATR_PCT)),
        "min_volume_mult": float(best.get("min_volume_mult", MIN_VOLUME_MULT)),
        "min_body_atr": float(best.get("min_body_atr", MIN_BODY_ATR)),
        "max_body_atr": float(best.get("max_body_atr", MAX_BODY_ATR)),
        "retest_buffer_atr": float(best.get("retest_buffer_atr", RETEST_BUFFER_ATR)),
        "pullback_buffer_atr": float(best.get("pullback_buffer_atr", PULLBACK_BUFFER_ATR)),
        "chop_range_mult": float(best.get("chop_range_mult", CHOP_RANGE_MULT)),
    }


def candle_quality(row, params=None) -> bool:
    p = params or get_live_params("DEFAULT")
    atr = float(row["atr"])
    body = float(row["body"])
    if atr <= 0:
        return False
    body_atr = body / atr
    return p["min_body_atr"] <= body_atr <= p["max_body_atr"]


def volume_ok(row, params=None) -> bool:
    p = params or get_live_params("DEFAULT")
    if float(row["vol_ma"]) <= 0:
        return True
    return float(row["volume"]) >= float(row["vol_ma"]) * p["min_volume_mult"]


def not_dead_chop(df: pd.DataFrame, params=None) -> bool:
    p = params or get_live_params("DEFAULT")
    if len(df) < 20:
        return False
    recent = df.tail(12)
    avg_range = (recent["high"] - recent["low"]).mean()
    atr = float(df.iloc[-1]["atr"])
    if atr <= 0:
        return False
    return avg_range >= atr * p["chop_range_mult"]


def strong_rejection(row, side: str) -> bool:
    open_ = float(row["open"])
    high = float(row["high"])
    low = float(row["low"])
    close = float(row["close"])
    rng = high - low
    body = abs(close - open_)
    if rng <= 0:
        return False

    upper = high - max(open_, close)
    lower = min(open_, close) - low

    if side == "LONG":
        return (close >= open_ and lower >= body * 0.25) or (close >= low + rng * 0.65)
    if side == "SHORT":
        return (close <= open_ and upper >= body * 0.25) or (close <= low + rng * 0.35)
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
    params = get_live_params(symbol)

    df5_raw = bars_to_df(symbol, "5Min", 600, 10)
    df5 = add_indicators(df5_raw)

    if df5.empty:
        s["LAST_REASON"] = "NO_DATA"
        return None

    build_opening_range(symbol, df5)

    r = df5.iloc[-1]
    prev = df5.iloc[-2]
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
    if float(r["atr_pct"]) < params["min_atr_pct"]:
        s["LAST_REASON"] = "LOW_VOLATILITY"
        return None
    if not not_dead_chop(df5, params):
        s["LAST_REASON"] = "CHOP_BLOCK"
        return None
    if not volume_ok(r, params):
        s["LAST_REASON"] = "LOW_VOLUME"
        return None

    # Model 1: Opening range break + retest
    if s["BREAK_SIDE"] is None:
        if close > s["OR_HIGH"] and bias_allows("LONG", bias) and candle_quality(r, params):
            s["BREAK_SIDE"] = "LONG"
            s["BREAK_BAR_INDEX"] = len(df5) - 1
            s["LAST_REASON"] = "OR_BREAK_LONG_WAIT_RETEST"
            # immediate continuation entry if very strong
            if close > s["OR_HIGH"] + atr * 0.20 and strong_rejection(r, "LONG"):
                risk = close - min(low, s["OR_LOW"])
                if risk > 0:
                    s["LAST_SIGNAL_MODEL"] = "OR_BREAK_CONTINUATION_LONG"
                    return {
                        "symbol": symbol,
                        "side": "buy",
                        "model": "OR_BREAK_CONTINUATION_LONG",
                        "price": close,
                        "sl": min(low, s["OR_LOW"]),
                        "tp": close + risk * params["rr_target"],
                        "bias": bias,
                    }
            return None

        if close < s["OR_LOW"] and bias_allows("SHORT", bias) and candle_quality(r, params):
            s["BREAK_SIDE"] = "SHORT"
            s["BREAK_BAR_INDEX"] = len(df5) - 1
            s["LAST_REASON"] = "OR_BREAK_SHORT_WAIT_RETEST"
            if close < s["OR_LOW"] - atr * 0.20 and strong_rejection(r, "SHORT"):
                risk = max(high, s["OR_HIGH"]) - close
                if risk > 0:
                    s["LAST_SIGNAL_MODEL"] = "OR_BREAK_CONTINUATION_SHORT"
                    return {
                        "symbol": symbol,
                        "side": "sell",
                        "model": "OR_BREAK_CONTINUATION_SHORT",
                        "price": close,
                        "sl": max(high, s["OR_HIGH"]),
                        "tp": close - risk * params["rr_target"],
                        "bias": bias,
                    }
            return None

    if s["BREAK_SIDE"] == "LONG" and not s["RETEST_DONE"]:
        if low <= s["OR_HIGH"] + atr * params["retest_buffer_atr"]:
            s["RETEST_DONE"] = True
            s["LAST_REASON"] = "LONG_RETEST_HIT"
            return None

        # allow delayed continuation if within 3 bars of break
        if s["BREAK_BAR_INDEX"] is not None and len(df5) - 1 - s["BREAK_BAR_INDEX"] <= 3:
            if close > s["OR_HIGH"] + atr * 0.25 and prev["close"] > s["OR_HIGH"] and candle_quality(r, params):
                risk = close - min(low, s["OR_HIGH"])
                if risk > 0:
                    s["LAST_SIGNAL_MODEL"] = "OR_BREAK_CONTINUATION_LONG"
                    return {
                        "symbol": symbol,
                        "side": "buy",
                        "model": "OR_BREAK_CONTINUATION_LONG",
                        "price": close,
                        "sl": min(low, s["OR_HIGH"]),
                        "tp": close + risk * params["rr_target"],
                        "bias": bias,
                    }

    if s["BREAK_SIDE"] == "SHORT" and not s["RETEST_DONE"]:
        if high >= s["OR_LOW"] - atr * params["retest_buffer_atr"]:
            s["RETEST_DONE"] = True
            s["LAST_REASON"] = "SHORT_RETEST_HIT"
            return None

        if s["BREAK_BAR_INDEX"] is not None and len(df5) - 1 - s["BREAK_BAR_INDEX"] <= 3:
            if close < s["OR_LOW"] - atr * 0.25 and prev["close"] < s["OR_LOW"] and candle_quality(r, params):
                risk = max(high, s["OR_LOW"]) - close
                if risk > 0:
                    s["LAST_SIGNAL_MODEL"] = "OR_BREAK_CONTINUATION_SHORT"
                    return {
                        "symbol": symbol,
                        "side": "sell",
                        "model": "OR_BREAK_CONTINUATION_SHORT",
                        "price": close,
                        "sl": max(high, s["OR_LOW"]),
                        "tp": close - risk * params["rr_target"],
                        "bias": bias,
                    }

    if s["BREAK_SIDE"] == "LONG" and s["RETEST_DONE"]:
        if strong_rejection(r, "LONG") and bias_allows("LONG", bias) and candle_quality(r, params):
            sl = min(low, s["OR_LOW"], float(r["ema20"]) - atr * 0.10)
            risk = close - sl
            if risk > 0:
                s["LAST_SIGNAL_MODEL"] = "OR_BREAK_RETEST_LONG"
                return {
                    "symbol": symbol,
                    "side": "buy",
                    "model": "OR_BREAK_RETEST_LONG",
                    "price": close,
                    "sl": sl,
                    "tp": close + risk * params["rr_target"],
                    "bias": bias,
                }

    if s["BREAK_SIDE"] == "SHORT" and s["RETEST_DONE"]:
        if strong_rejection(r, "SHORT") and bias_allows("SHORT", bias) and candle_quality(r, params):
            sl = max(high, s["OR_HIGH"], float(r["ema20"]) + atr * 0.10)
            risk = sl - close
            if risk > 0:
                s["LAST_SIGNAL_MODEL"] = "OR_BREAK_RETEST_SHORT"
                return {
                    "symbol": symbol,
                    "side": "sell",
                    "model": "OR_BREAK_RETEST_SHORT",
                    "price": close,
                    "sl": sl,
                    "tp": close - risk * params["rr_target"],
                    "bias": bias,
                }

    # Model 2: Trend pullback continuation
    if bias_allows("LONG", bias):
        touched_value = (
            low <= float(r["ema20"]) + atr * params["pullback_buffer_atr"]
            or low <= float(r["ema50"]) + atr * 0.10
        )
        reclaimed = close > open_ and (close > float(r["ema20"]) or close > prev["high"])
        if touched_value and reclaimed and candle_quality(r, params):
            sl = min(low, float(r["ema50"]) - atr * 0.10)
            risk = close - sl
            if risk > 0:
                s["LAST_SIGNAL_MODEL"] = "TREND_PULLBACK_LONG"
                return {
                    "symbol": symbol,
                    "side": "buy",
                    "model": "TREND_PULLBACK_LONG",
                    "price": close,
                    "sl": sl,
                    "tp": close + risk * params["rr_target"],
                    "bias": bias,
                }

    if bias_allows("SHORT", bias):
        touched_value = (
            high >= float(r["ema20"]) - atr * params["pullback_buffer_atr"]
            or high >= float(r["ema50"]) - atr * 0.10
        )
        rejected = close < open_ and (close < float(r["ema20"]) or close < prev["low"])
        if touched_value and rejected and candle_quality(r, params):
            sl = max(high, float(r["ema50"]) + atr * 0.10)
            risk = sl - close
            if risk > 0:
                s["LAST_SIGNAL_MODEL"] = "TREND_PULLBACK_SHORT"
                return {
                    "symbol": symbol,
                    "side": "sell",
                    "model": "TREND_PULLBACK_SHORT",
                    "price": close,
                    "sl": sl,
                    "tp": close - risk * params["rr_target"],
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
    research = RESEARCH_CACHE.get(symbol, {})
    research_line = ""
    if research:
        research_line = (
            f"\nWF win rate: {research.get('wf_win_rate', 0):.1%}"
            f"\nWF PF: {research.get('wf_profit_factor', 0):.2f}"
            f"\nMC risk of loss: {research.get('mc_risk_of_loss', 0):.1%}"
        )

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
        f"RR: 1:{abs((signal['tp'] - signal['price']) / max(abs(signal['price'] - signal['sl']), 1e-9)):.2f}"
        f"{research_line}\n"
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


# ============================================================
# WALK-FORWARD + MONTE CARLO RESEARCH
# ============================================================

def build_bias_map(df15: pd.DataFrame) -> pd.DataFrame:
    if df15.empty:
        return pd.DataFrame(columns=["time", "bias"])
    d = add_indicators(df15)
    if d.empty:
        return pd.DataFrame(columns=["time", "bias"])
    d["bias"] = d.apply(classify_bias_row, axis=1)
    return d[["time", "bias"]].copy()


def merge_bias(df5: pd.DataFrame, df15: pd.DataFrame) -> pd.DataFrame:
    bias_map = build_bias_map(df15)
    if bias_map.empty:
        df5 = df5.copy()
        df5["bias"] = "NONE"
        return df5
    out = pd.merge_asof(
        df5.sort_values("time"),
        bias_map.sort_values("time"),
        on="time",
        direction="backward",
    )
    out["bias"] = out["bias"].fillna("NONE")
    return out


def research_grid():
    grids = [
        {
            "rr_target": rr,
            "min_atr_pct": atr,
            "min_volume_mult": vol,
            "retest_buffer_atr": retest,
            "pullback_buffer_atr": pullback,
            "min_body_atr": MIN_BODY_ATR,
            "max_body_atr": MAX_BODY_ATR,
            "chop_range_mult": CHOP_RANGE_MULT,
        }
        for rr in [1.5, 1.8, 2.0]
        for atr in [0.0015, 0.0020, 0.0025]
        for vol in [0.70, 0.85]
        for retest in [0.25, 0.35, 0.45]
        for pullback in [0.35, 0.45]
    ]
    return grids


def intraday_backtest(df5_raw: pd.DataFrame, df15_raw: pd.DataFrame, params: dict):
    df5 = add_indicators(df5_raw)
    if df5.empty:
        return []

    df5 = merge_bias(df5, df15_raw)
    trades = []

    unique_days = sorted(pd.Series(df5["time"].dt.date.unique()).tolist())
    for day in unique_days:
        day_df = df5[df5["time"].dt.date == day].reset_index(drop=True)
        if day_df.empty:
            continue

        or_start = to_minutes(MARKET_OPEN)
        or_end = to_minutes(OPENING_RANGE_END)
        trade_start = to_minutes(TRADE_START)
        last_entry = to_minutes(LAST_ENTRY_TIME)

        opening = day_df[day_df["time"].apply(lambda x: or_start <= minute_of_day(x) < or_end)]
        if opening.empty:
            continue

        or_high = float(opening["high"].max())
        or_low = float(opening["low"].min())

        break_side = None
        break_idx = None
        retest_done = False
        traded = False

        for i in range(1, len(day_df)):
            if traded:
                break

            row = day_df.iloc[i]
            prev = day_df.iloc[i - 1]
            minute = minute_of_day(row["time"])

            if minute < trade_start or minute > last_entry:
                continue

            bias = row["bias"]
            if bias in ["NONE", "CHOP"]:
                continue

            if float(row["atr_pct"]) < params["min_atr_pct"]:
                continue
            if not not_dead_chop(day_df.iloc[max(0, i-20):i+1], params):
                continue
            if not volume_ok(row, params):
                continue

            close = float(row["close"])
            open_ = float(row["open"])
            high = float(row["high"])
            low = float(row["low"])
            atr = float(row["atr"])

            if break_side is None:
                if close > or_high and bias_allows("LONG", bias) and candle_quality(row, params):
                    break_side = "LONG"
                    break_idx = i
                    if close > or_high + atr * 0.20 and strong_rejection(row, "LONG"):
                        sl = min(low, or_low)
                        risk = close - sl
                        if risk > 0:
                            trades.append(simulate_exit(day_df, i, "LONG", close, sl, close + risk * params["rr_target"]))
                            traded = True
                            break
                    continue

                if close < or_low and bias_allows("SHORT", bias) and candle_quality(row, params):
                    break_side = "SHORT"
                    break_idx = i
                    if close < or_low - atr * 0.20 and strong_rejection(row, "SHORT"):
                        sl = max(high, or_high)
                        risk = sl - close
                        if risk > 0:
                            trades.append(simulate_exit(day_df, i, "SHORT", close, sl, close - risk * params["rr_target"]))
                            traded = True
                            break
                    continue

            if break_side == "LONG" and not retest_done:
                if low <= or_high + atr * params["retest_buffer_atr"]:
                    retest_done = True
                    continue
                if break_idx is not None and i - break_idx <= 3:
                    if close > or_high + atr * 0.25 and prev["close"] > or_high and candle_quality(row, params):
                        sl = min(low, or_high)
                        risk = close - sl
                        if risk > 0:
                            trades.append(simulate_exit(day_df, i, "LONG", close, sl, close + risk * params["rr_target"]))
                            traded = True
                            break

            if break_side == "SHORT" and not retest_done:
                if high >= or_low - atr * params["retest_buffer_atr"]:
                    retest_done = True
                    continue
                if break_idx is not None and i - break_idx <= 3:
                    if close < or_low - atr * 0.25 and prev["close"] < or_low and candle_quality(row, params):
                        sl = max(high, or_low)
                        risk = sl - close
                        if risk > 0:
                            trades.append(simulate_exit(day_df, i, "SHORT", close, sl, close - risk * params["rr_target"]))
                            traded = True
                            break

            if break_side == "LONG" and retest_done:
                if strong_rejection(row, "LONG") and bias_allows("LONG", bias) and candle_quality(row, params):
                    sl = min(low, or_low, float(row["ema20"]) - atr * 0.10)
                    risk = close - sl
                    if risk > 0:
                        trades.append(simulate_exit(day_df, i, "LONG", close, sl, close + risk * params["rr_target"]))
                        traded = True
                        break

            if break_side == "SHORT" and retest_done:
                if strong_rejection(row, "SHORT") and bias_allows("SHORT", bias) and candle_quality(row, params):
                    sl = max(high, or_high, float(row["ema20"]) + atr * 0.10)
                    risk = sl - close
                    if risk > 0:
                        trades.append(simulate_exit(day_df, i, "SHORT", close, sl, close - risk * params["rr_target"]))
                        traded = True
                        break

            if bias_allows("LONG", bias):
                touched_value = low <= float(row["ema20"]) + atr * params["pullback_buffer_atr"] or low <= float(row["ema50"]) + atr * 0.10
                reclaimed = close > open_ and (close > float(row["ema20"]) or close > float(prev["high"]))
                if touched_value and reclaimed and candle_quality(row, params):
                    sl = min(low, float(row["ema50"]) - atr * 0.10)
                    risk = close - sl
                    if risk > 0:
                        trades.append(simulate_exit(day_df, i, "LONG", close, sl, close + risk * params["rr_target"]))
                        traded = True
                        break

            if bias_allows("SHORT", bias):
                touched_value = high >= float(row["ema20"]) - atr * params["pullback_buffer_atr"] or high >= float(row["ema50"]) - atr * 0.10
                rejected = close < open_ and (close < float(row["ema20"]) or close < float(prev["low"]))
                if touched_value and rejected and candle_quality(row, params):
                    sl = max(high, float(row["ema50"]) + atr * 0.10)
                    risk = sl - close
                    if risk > 0:
                        trades.append(simulate_exit(day_df, i, "SHORT", close, sl, close - risk * params["rr_target"]))
                        traded = True
                        break

    return [t for t in trades if t is not None]


def simulate_exit(day_df: pd.DataFrame, entry_idx: int, side: str, entry: float, sl: float, tp: float):
    risk = abs(entry - sl)
    if risk <= 0:
        return None

    for j in range(entry_idx + 1, len(day_df)):
        row = day_df.iloc[j]
        high = float(row["high"])
        low = float(row["low"])

        if side == "LONG":
            if low <= sl:
                return {"r": -1.0, "entry": entry, "exit": sl, "side": side, "time": str(day_df.iloc[entry_idx]["time"])}
            if high >= tp:
                return {"r": (tp - entry) / risk, "entry": entry, "exit": tp, "side": side, "time": str(day_df.iloc[entry_idx]["time"])}
        else:
            if high >= sl:
                return {"r": -1.0, "entry": entry, "exit": sl, "side": side, "time": str(day_df.iloc[entry_idx]["time"])}
            if low <= tp:
                return {"r": (entry - tp) / risk, "entry": entry, "exit": tp, "side": side, "time": str(day_df.iloc[entry_idx]["time"])}

    close = float(day_df.iloc[-1]["close"])
    r_mult = ((close - entry) / risk) if side == "LONG" else ((entry - close) / risk)
    return {"r": float(r_mult), "entry": entry, "exit": close, "side": side, "time": str(day_df.iloc[entry_idx]["time"])}


def trade_stats(trades):
    if not trades:
        return None
    rs = np.array([t["r"] for t in trades], dtype=float)
    wins = rs[rs > 0]
    losses = rs[rs <= 0]
    pf = wins.sum() / abs(losses.sum()) if len(losses) and abs(losses.sum()) > 0 else float("inf")
    return {
        "trades": int(len(rs)),
        "win_rate": float((rs > 0).mean()),
        "expectancy": float(rs.mean()),
        "profit_factor": float(pf if np.isfinite(pf) else 999.0),
        "total_r": float(rs.sum()),
        "max_dd_r": float(max_drawdown_from_r(rs)),
    }


def max_drawdown_from_r(rs):
    equity = np.cumsum(rs)
    peak = np.maximum.accumulate(equity)
    dd = equity - peak
    return dd.min() if len(dd) else 0.0


def optimize_symbol(train5: pd.DataFrame, train15: pd.DataFrame):
    best_params = None
    best_score = -1e9
    best_stats = None

    for params in research_grid():
        trades = intraday_backtest(train5, train15, params)
        stats = trade_stats(trades)
        if not stats or stats["trades"] < 4:
            continue

        score = (
            stats["expectancy"] * 0.45
            + stats["profit_factor"] * 0.30
            + stats["win_rate"] * 0.15
            + max(stats["max_dd_r"], -10) * 0.10
        )

        if score > best_score:
            best_score = score
            best_params = params
            best_stats = stats

    return best_params, best_stats


def walk_forward_symbol(symbol: str, days: int = RESEARCH_DAYS):
    df5 = bars_to_df(symbol, "5Min", limit=5000, days=days)
    df15 = bars_to_df(symbol, "15Min", limit=3000, days=days)

    if df5.empty or df15.empty:
        return None

    trade_dates = sorted(pd.Series(df5["time"].dt.date.unique()).tolist())
    if len(trade_dates) < WF_TRAIN_DAYS + WF_TEST_DAYS + 2:
        return None

    reports = []
    test_trades_all = []

    start_idx = 0
    while start_idx + WF_TRAIN_DAYS + WF_TEST_DAYS <= len(trade_dates):
        train_days = trade_dates[start_idx:start_idx + WF_TRAIN_DAYS]
        test_days = trade_dates[start_idx + WF_TRAIN_DAYS:start_idx + WF_TRAIN_DAYS + WF_TEST_DAYS]

        train5 = df5[df5["time"].dt.date.isin(train_days)].reset_index(drop=True)
        train15 = df15[df15["time"].dt.date.isin(train_days)].reset_index(drop=True)
        test5 = df5[df5["time"].dt.date.isin(test_days)].reset_index(drop=True)
        test15 = df15[df15["time"].dt.date.isin(test_days)].reset_index(drop=True)

        params, train_stats = optimize_symbol(train5, train15)
        if not params:
            start_idx += WF_TEST_DAYS
            continue

        test_trades = intraday_backtest(test5, test15, params)
        test_stats = trade_stats(test_trades)
        if test_stats:
            reports.append({
                "train_start": str(train_days[0]),
                "train_end": str(train_days[-1]),
                "test_start": str(test_days[0]),
                "test_end": str(test_days[-1]),
                **params,
                **test_stats
            })
            test_trades_all.extend(test_trades)

        start_idx += WF_TEST_DAYS

    if not reports:
        return None

    report_df = pd.DataFrame(reports)
    overall = trade_stats(test_trades_all)
    return {
        "report": report_df.to_dict(orient="records"),
        "best_params": {
            "rr_target": float(report_df.iloc[-1]["rr_target"]),
            "min_atr_pct": float(report_df.iloc[-1]["min_atr_pct"]),
            "min_volume_mult": float(report_df.iloc[-1]["min_volume_mult"]),
            "retest_buffer_atr": float(report_df.iloc[-1]["retest_buffer_atr"]),
            "pullback_buffer_atr": float(report_df.iloc[-1]["pullback_buffer_atr"]),
            "min_body_atr": MIN_BODY_ATR,
            "max_body_atr": MAX_BODY_ATR,
            "chop_range_mult": CHOP_RANGE_MULT,
        },
        "wf_win_rate": float(overall["win_rate"]),
        "wf_profit_factor": float(overall["profit_factor"]),
        "wf_expectancy_r": float(overall["expectancy"]),
        "wf_total_r": float(overall["total_r"]),
        "wf_trades": int(overall["trades"]),
        "test_trades": test_trades_all,
    }


def monte_carlo_r(trades, runs: int = MC_RUNS):
    if not trades:
        return None

    rs = np.array([t["r"] for t in trades], dtype=float)
    finals = []
    drawdowns = []

    for _ in range(runs):
        sampled = np.random.choice(rs, size=len(rs), replace=True)
        equity = np.cumsum(sampled)
        finals.append(equity[-1])
        peak = np.maximum.accumulate(equity)
        drawdowns.append(float((equity - peak).min()))

    finals = np.array(finals)
    drawdowns = np.array(drawdowns)

    return {
        "mc_median_r": float(np.median(finals)),
        "mc_worst_5pct_r": float(np.quantile(finals, 0.05)),
        "mc_best_95pct_r": float(np.quantile(finals, 0.95)),
        "mc_worst_dd_r": float(np.quantile(drawdowns, 0.05)),
        "mc_risk_of_loss": float((finals < 0).mean()),
    }


def run_research():
    load_research_cache()
    summary_lines = []

    for symbol in WATCHLIST:
        try:
            result = walk_forward_symbol(symbol, RESEARCH_DAYS)
            if not result:
                summary_lines.append(f"{symbol}: no research result")
                continue

            mc = monte_carlo_r(result["test_trades"], MC_RUNS)
            merged = {k: v for k, v in result.items() if k != "test_trades"}
            if mc:
                merged.update(mc)

            RESEARCH_CACHE[symbol] = merged
            summary_lines.append(
                f"{symbol}: WF WR={merged.get('wf_win_rate', 0):.1%}, "
                f"PF={merged.get('wf_profit_factor', 0):.2f}, "
                f"MC loss={merged.get('mc_risk_of_loss', 0):.1%}"
            )
        except Exception as e:
            summary_lines.append(f"{symbol}: research error {e}")

    save_research_cache()

    send(
        f"{E_CHART} WALK-FORWARD / MONTE CARLO UPDATED\n\n" +
        "\n".join(summary_lines[:20])
    )


def maybe_run_research_after_close():
    if not RUN_RESEARCH_AFTER_CLOSE:
        return
    t = now_ny()
    if minute_of_day(t) < to_minutes(EOD_SUMMARY_TIME):
        return
    if BOT_STATE["RESEARCH_RAN_TODAY"]:
        return
    run_research()
    BOT_STATE["RESEARCH_RAN_TODAY"] = True


def startup_check():
    if not ALPACA_API_KEY or not ALPACA_SECRET_KEY:
        send(f"{E_WARN} ALPACA KEYS MISSING\n\nSet ALPACA_API_KEY and ALPACA_SECRET_KEY.")
        return False

    account = get_account()
    if not account:
        send(f"{E_WARN} ALPACA ACCOUNT CHECK FAILED\n\nCheck API keys, paper/live setting, or Alpaca connection.")
        return False

    load_research_cache()

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
        f"- end of day summary\n"
        f"- walk-forward / monte carlo summary"
    )
    return True


def run():
    time.sleep(3)
    if not startup_check():
        return

    if RUN_RESEARCH_AT_STARTUP:
        try:
            run_research()
            BOT_STATE["RESEARCH_RAN_TODAY"] = True
        except Exception as e:
            send(f"{E_WARN} STARTUP RESEARCH ERROR:\n{e}")

    while True:
        try:
            reset_daily_state()
            t = now_ny()

            if not in_window(t, MARKET_OPEN, MARKET_CLOSE):
                for symbol in WATCHLIST:
                    STATE[symbol]["LAST_REASON"] = "MARKET_CLOSED"
                check_order_updates()
                end_of_day_summary()
                maybe_run_research_after_close()
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
