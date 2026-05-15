import os
import time
import math
import json
import requests
import pandas as pd
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

# ============================================================
# STOCK MERGED AUTO-TRADER
# - old stable execution backbone
# - multi-setup selection
# - soft edge weighting from research profile
# - auto-trades best candidate
# ============================================================

E_CHECK = "\u2705"
E_FIRE = "\U0001F525"
E_WARN = "\u26A0\uFE0F"
E_ROCKET = "\U0001F680"
E_DOWN = "\U0001F4C9"
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
EDGE_PROFILE_FILE = os.getenv("STOCK_EDGE_PROFILE_FILE", "stock_edge_profile.json")

HEADERS = {
    "APCA-API-KEY-ID": ALPACA_API_KEY or "",
    "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY or "",
    "Content-Type": "application/json",
}

NY_TZ = ZoneInfo("America/New_York")
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "45"))

WATCHLIST = ["AAPL", "TSLA", "NVDA", "AMD", "META", "MSFT", "AMZN", "SPY", "QQQ", "NBIS", "WULF", "IREN"]

RISK_PER_TRADE = float(os.getenv("RISK_PER_TRADE", "0.005"))
MAX_POSITION_PCT = float(os.getenv("MAX_POSITION_PCT", "0.12"))
MAX_TRADES_PER_DAY = int(os.getenv("MAX_TRADES_PER_DAY", "4"))
MAX_NEW_ORDERS_PER_SCAN = int(os.getenv("MAX_NEW_ORDERS_PER_SCAN", "1"))
ONE_POSITION_AT_A_TIME = os.getenv("ONE_POSITION_AT_A_TIME", "false").lower() in ["1", "true", "yes", "y"]
PROTECT_UNCOVERED_POSITIONS = os.getenv("PROTECT_UNCOVERED_POSITIONS", "true").lower() in ["1", "true", "yes", "y"]
AUTO_CLOSE_UNPROTECTED_PAPER = os.getenv("AUTO_CLOSE_UNPROTECTED_PAPER", "true").lower() in ["1", "true", "yes", "y"]
AUTO_CLOSE_UNPROTECTED_LIVE = os.getenv("AUTO_CLOSE_UNPROTECTED_LIVE", "false").lower() in ["1", "true", "yes", "y"]
MAX_ENTRY_DRIFT_PCT = float(os.getenv("MAX_ENTRY_DRIFT_PCT", "0.0075"))
MAX_DATA_STALE_MINUTES_5M = int(os.getenv("MAX_DATA_STALE_MINUTES_5M", "20"))
MAX_DATA_STALE_MINUTES_15M = int(os.getenv("MAX_DATA_STALE_MINUTES_15M", "45"))
ALLOW_SHORTS = os.getenv("ALLOW_SHORTS", "false").lower() in ["1", "true", "yes", "y"]

RR_TARGET = float(os.getenv("RR_TARGET", "1.6"))
ATR_LEN = 14
EMA_FAST = 20
EMA_MID = 50
EMA_SLOW = 200

MIN_ATR_PCT = float(os.getenv("MIN_ATR_PCT", "0.0015"))
MIN_VOLUME_MULT = float(os.getenv("MIN_VOLUME_MULT", "0.60"))
MIN_BODY_ATR = float(os.getenv("MIN_BODY_ATR", "0.12"))
MAX_BODY_ATR = float(os.getenv("MAX_BODY_ATR", "2.80"))
RETEST_BUFFER_ATR = float(os.getenv("RETEST_BUFFER_ATR", "0.45"))
PULLBACK_BUFFER_ATR = float(os.getenv("PULLBACK_BUFFER_ATR", "0.55"))
MIN_SCORE_TO_TRADE = float(os.getenv("MIN_SCORE_TO_TRADE", "54"))

MARKET_OPEN = "09:30"
OPENING_RANGE_END = "10:00"
TRADE_START = "09:45"
LAST_ENTRY_TIME = "15:40"
MARKET_CLOSE = "16:00"
EOD_SUMMARY_TIME = "16:05"

STATE = {
    symbol: {
        "DATE": None,
        "OR_HIGH": None,
        "OR_LOW": None,
        "OR_SET": False,
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

BOT_STATE = {"DATE": None, "TRADES_TODAY": 0, "SENT_KEYS": set(), "EOD_SENT": False}
_BAR_CACHE = {}
_HTF_CACHE = {"stamp": None, "bias": {}}


# ============================================================
# HELPERS
# ============================================================

def load_json(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r") as f:
            return json.load(f)
    except Exception:
        return default


def send(msg: str):
    print(msg)
    if not TELEGRAM_TOKEN or not CHAT_ID:
        print("TELEGRAM NOT SET")
        return
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", json={"chat_id": CHAT_ID, "text": msg}, timeout=10)
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


# ============================================================
# RESET
# ============================================================

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
        STATE[sym]["TRADED_TODAY"] = False
        STATE[sym]["IN_POSITION"] = False
        STATE[sym]["LAST_REASON"] = "NEW_DAY"
        STATE[sym]["LAST_PRICE"] = None
        STATE[sym]["ORDER_ID"] = None
        STATE[sym]["ORDER_STATUS_NOTIFIED"] = None
        STATE[sym]["LAST_SIGNAL_MODEL"] = None

    _BAR_CACHE.clear()
    _HTF_CACHE["stamp"] = None
    _HTF_CACHE["bias"] = {}


# ============================================================
# ALPACA
# ============================================================

def alpaca_get(path: str, params=None, data_api=False):
    base = ALPACA_DATA_BASE if data_api else ALPACA_TRADE_BASE
    try:
        r = requests.get(f"{base}{path}", headers=HEADERS, params=params or {}, timeout=20)
        if r.status_code >= 400:
            print("ALPACA GET ERROR:", r.status_code, r.text[:400])
            return None
        return r.json()
    except Exception as e:
        print("ALPACA GET EXCEPTION:", e)
        return None


def alpaca_post(path: str, payload: dict):
    try:
        r = requests.post(f"{ALPACA_TRADE_BASE}{path}", headers=HEADERS, json=payload, timeout=20)

        if r.status_code >= 400:
            error_text = r.text[:1000]
            print("ALPACA POST ERROR:", r.status_code, error_text)
            return {
                "_ok": False,
                "_status_code": r.status_code,
                "_error": error_text,
                "_payload": payload,
            }

        data = r.json()
        if isinstance(data, dict):
            data["_ok"] = True
        return data

    except Exception as e:
        print("ALPACA POST EXCEPTION:", e)
        return {
            "_ok": False,
            "_status_code": "EXCEPTION",
            "_error": str(e),
            "_payload": payload,
        }



def alpaca_delete(path: str):
    try:
        r = requests.delete(f"{ALPACA_TRADE_BASE}{path}", headers=HEADERS, timeout=20)

        if r.status_code >= 400:
            error_text = r.text[:1000]
            print("ALPACA DELETE ERROR:", r.status_code, error_text)
            return {
                "_ok": False,
                "_status_code": r.status_code,
                "_error": error_text,
            }

        try:
            data = r.json()
        except Exception:
            data = {}

        if isinstance(data, dict):
            data["_ok"] = True
        return data

    except Exception as e:
        print("ALPACA DELETE EXCEPTION:", e)
        return {
            "_ok": False,
            "_status_code": "EXCEPTION",
            "_error": str(e),
        }


def close_position_market(symbol: str):
    return alpaca_delete(f"/v2/positions/{symbol}")

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


def get_latest_price(symbol: str):
    """
    Get a live-ish Alpaca reference price before submitting a market bracket.
    This prevents stale candle signals from creating invalid TP/SL brackets.
    """
    try:
        trade = alpaca_get(
            f"/v2/stocks/{symbol}/trades/latest",
            params={"feed": ALPACA_DATA_FEED},
            data_api=True,
        )
        price = None
        if isinstance(trade, dict):
            price = trade.get("trade", {}).get("p")

        if price is not None:
            return float(price)

        quote = alpaca_get(
            f"/v2/stocks/{symbol}/quotes/latest",
            params={"feed": ALPACA_DATA_FEED},
            data_api=True,
        )
        if isinstance(quote, dict):
            q = quote.get("quote", {})
            bid = q.get("bp")
            ask = q.get("ap")
            if bid is not None and ask is not None and float(bid) > 0 and float(ask) > 0:
                return (float(bid) + float(ask)) / 2
            if ask is not None and float(ask) > 0:
                return float(ask)
            if bid is not None and float(bid) > 0:
                return float(bid)

    except Exception as e:
        print(f"{symbol} latest price error:", e)

    return None


# ============================================================
# DATA
# ============================================================

def bars_to_df(symbol: str, timeframe: str, limit: int = 400) -> pd.DataFrame:
    """
    Fetch recent Alpaca bars safely.

    Important fix:
    The old version asked for 10 days of bars with sort='asc' and limit=400.
    Alpaca can return the FIRST 400 bars from that window, not the newest 400.
    That made signals use stale prices and caused bracket-order rejections.

    This version requests a larger window/limit, then keeps the newest rows.
    """
    cache_key = (symbol, timeframe, limit, now_ny().strftime("%Y-%m-%d %H:%M"))
    if cache_key in _BAR_CACHE:
        return _BAR_CACHE[cache_key].copy()

    end = now_ny()

    if timeframe == "5Min":
        start = end - timedelta(days=8)
    elif timeframe == "15Min":
        start = end - timedelta(days=25)
    else:
        start = end - timedelta(days=30)

    request_limit = max(1000, min(10000, limit * 5))

    params = {
        "symbols": symbol,
        "timeframe": timeframe,
        "start": iso_utc(start),
        "end": iso_utc(end),
        "limit": request_limit,
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
    df.rename(columns={"t": "time", "o": "open", "h": "high", "l": "low", "c": "close", "v": "volume"}, inplace=True)

    needed = ["time", "open", "high", "low", "close", "volume"]
    for col in needed:
        if col not in df.columns:
            return pd.DataFrame()

    df["time"] = pd.to_datetime(df["time"], utc=True).dt.tz_convert(NY_TZ)
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    df.dropna(inplace=True)
    df = df[needed].sort_values("time").tail(limit).reset_index(drop=True)

    if df.empty:
        return pd.DataFrame()

    # Freshness guard. During market hours, stale bars are dangerous.
    last_bar_time = df.iloc[-1]["time"]
    age_minutes = (now_ny() - last_bar_time).total_seconds() / 60

    if timeframe == "5Min" and in_window(now_ny(), MARKET_OPEN, MARKET_CLOSE):
        if age_minutes > MAX_DATA_STALE_MINUTES_5M:
            print(f"{symbol} {timeframe} stale data blocked. Last bar {last_bar_time}, age {age_minutes:.1f} min")
            return pd.DataFrame()

    if timeframe == "15Min" and in_window(now_ny(), MARKET_OPEN, MARKET_CLOSE):
        if age_minutes > MAX_DATA_STALE_MINUTES_15M:
            print(f"{symbol} {timeframe} stale data blocked. Last bar {last_bar_time}, age {age_minutes:.1f} min")
            return pd.DataFrame()

    _BAR_CACHE[cache_key] = df.copy()
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


def htf_bias(symbol: str) -> str:
    stamp = now_ny().strftime("%Y-%m-%d %H:%M")
    if _HTF_CACHE["stamp"] == stamp and symbol in _HTF_CACHE["bias"]:
        return _HTF_CACHE["bias"][symbol]

    df = add_indicators(bars_to_df(symbol, "15Min", 400))
    if df.empty:
        bias = "NONE"
    else:
        r = df.iloc[-1]
        p = df.iloc[-2]
        if r["close"] > r["ema50"] > r["ema200"] and r["ema50"] >= p["ema50"]:
            bias = "BULL"
        elif r["close"] < r["ema50"] < r["ema200"] and r["ema50"] <= p["ema50"]:
            bias = "BEAR"
        elif r["close"] > r["ema200"]:
            bias = "BULL_WEAK"
        elif r["close"] < r["ema200"]:
            bias = "BEAR_WEAK"
        else:
            bias = "CHOP"

    _HTF_CACHE["stamp"] = stamp
    _HTF_CACHE["bias"][symbol] = bias
    return bias


# ============================================================
# STRUCTURE
# ============================================================

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
    return avg_range >= atr * 0.40


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
        return (close > open_ and lower >= body * 0.25) or (close > open_ and close > (high + low) / 2)
    if side == "SHORT":
        return (close < open_ and upper >= body * 0.25) or (close < open_ and close < (high + low) / 2)
    return False


def bullish_pin(row) -> bool:
    open_ = float(row["open"])
    high = float(row["high"])
    low = float(row["low"])
    close = float(row["close"])
    body = abs(close - open_)
    if body <= 0:
        return False
    upper = high - max(open_, close)
    lower = min(open_, close) - low
    return close > open_ and lower >= body * 1.5 and upper <= body * 1.2


def bearish_pin(row) -> bool:
    open_ = float(row["open"])
    high = float(row["high"])
    low = float(row["low"])
    close = float(row["close"])
    body = abs(close - open_)
    if body <= 0:
        return False
    upper = high - max(open_, close)
    lower = min(open_, close) - low
    return close < open_ and upper >= body * 1.5 and lower <= body * 1.2


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

    if opening.empty or minute_of_day(now_ny()) < or_end_min:
        return

    s["OR_HIGH"] = float(opening["high"].max())
    s["OR_LOW"] = float(opening["low"].min())
    s["OR_SET"] = True
    s["LAST_REASON"] = "OPENING_RANGE_SET"


def score_candidate(side: str, bias: str, row, bonus: float) -> float:
    score = 0.0
    close = float(row["close"])
    ema20 = float(row["ema20"])
    ema50 = float(row["ema50"])
    ema200 = float(row["ema200"])

    if side == "LONG":
        if close > ema20: score += 8
        if ema20 > ema50: score += 10
        if ema50 > ema200: score += 12
        if bias == "BULL": score += 18
        elif bias == "BULL_WEAK": score += 8
    else:
        if close < ema20: score += 8
        if ema20 < ema50: score += 10
        if ema50 < ema200: score += 12
        if bias == "BEAR": score += 18
        elif bias == "BEAR_WEAK": score += 8

    if float(row["atr_pct"]) >= MIN_ATR_PCT: score += 8
    if volume_ok(row): score += 8
    if candle_quality(row): score += 8

    atr = max(float(row["atr"]), 1e-9)
    adx_like = abs(float(row["ema20"]) - float(row["ema50"])) / atr
    score += min(adx_like * 6, 12)

    score += bonus
    return round(score, 1)


def make_candidate(symbol: str, side: str, model: str, bias: str, row, entry: float, sl: float, tp: float, bonus: float, reason: str):
    risk = abs(entry - sl)
    if risk <= 0:
        return None
    return {
        "symbol": symbol,
        "side": "buy" if side == "LONG" else "sell",
        "direction": side,
        "model": model,
        "bias": bias,
        "price": float(entry),
        "sl": float(sl),
        "tp": float(tp),
        "score": score_candidate(side, bias, row, bonus),
        "reason": reason,
    }


def time_bucket(ts):
    ts = pd.Timestamp(ts)
    minute = (ts.minute // 15) * 15
    return f"{ts.hour:02d}:{minute:02d}"


def price_zone(symbol, row):
    s = STATE[symbol]
    close = float(row["close"])
    ema20 = float(row["ema20"])
    ema50 = float(row["ema50"])

    if s["OR_SET"] and close > s["OR_HIGH"]:
        return "above_or"
    if s["OR_SET"] and close < s["OR_LOW"]:
        return "below_or"
    if close > ema20 > ema50:
        return "above_ema_stack"
    if close < ema20 < ema50:
        return "below_ema_stack"
    return "mixed"


def edge_adjustment(signal, symbol, row):
    profile = load_json(EDGE_PROFILE_FILE, {})
    buckets = profile.get("buckets", {})
    bucket = time_bucket(now_ny())
    zone = price_zone(symbol, row)

    keys = [
        f"{symbol}|{signal['model']}|{signal['direction']}|{bucket}|{zone}",
        f"{symbol}|{signal['model']}|{signal['direction']}|{bucket}|ALL",
        f"ALL|{signal['model']}|{signal['direction']}|{bucket}|ALL",
        f"ALL|{signal['model']}|{signal['direction']}|ALL|ALL",
    ]

    chosen = None
    for k in keys:
        info = buckets.get(k)
        if info and info.get("trades", 0) >= 10:
            chosen = info
            break

    if not chosen:
        signal["edge_note"] = "no_profile"
        return signal

    expectancy_r = float(chosen.get("expectancy_r", 0))
    win_rate = float(chosen.get("win_rate", 0))
    trades = int(chosen.get("trades", 0))

    # Boost-only edge weighting.
    # Research can help a setup rank higher, but it cannot suppress a live setup yet.
    adj = max(0, min(8, expectancy_r * 4 + (win_rate - 0.5) * 8))
    signal["score"] = round(signal["score"] + adj, 1)
    signal["edge_note"] = f"expR={expectancy_r:.2f}, wr={win_rate:.1%}, n={trades}"
    return signal


# ============================================================
# MODELS
# ============================================================

def detect_or_breakout_continuation(symbol: str, df5: pd.DataFrame, row, bias: str):
    s = STATE[symbol]
    if not s["OR_SET"]:
        return None

    close = float(row["close"])
    low = float(row["low"])
    high = float(row["high"])
    open_ = float(row["open"])

    if close > s["OR_HIGH"] and bias_allows("LONG", bias) and candle_quality(row) and close > open_:
        sl = min(low, s["OR_LOW"])
        risk = close - sl
        if risk > 0:
            return make_candidate(symbol, "LONG", "OR_BREAKOUT_CONTINUATION", bias, row, close, sl, close + risk * RR_TARGET, 18, f"Opening range breakout continuation above {s['OR_HIGH']:.2f}")

    if close < s["OR_LOW"] and bias_allows("SHORT", bias) and candle_quality(row) and close < open_:
        sl = max(high, s["OR_HIGH"])
        risk = sl - close
        if risk > 0:
            return make_candidate(symbol, "SHORT", "OR_BREAKOUT_CONTINUATION", bias, row, close, sl, close - risk * RR_TARGET, 18, f"Opening range breakdown continuation below {s['OR_LOW']:.2f}")

    return None


def detect_or_retest_rejection(symbol: str, df5: pd.DataFrame, row, bias: str):
    s = STATE[symbol]
    if not s["OR_SET"]:
        return None

    close = float(row["close"])
    low = float(row["low"])
    high = float(row["high"])
    atr = float(row["atr"])
    recent = df5.tail(10)
    bull_broke = (recent["close"] > s["OR_HIGH"]).any()
    bear_broke = (recent["close"] < s["OR_LOW"]).any()

    if bull_broke and low <= s["OR_HIGH"] + atr * RETEST_BUFFER_ATR and close > s["OR_HIGH"] and strong_rejection(row, "LONG") and bias_allows("LONG", bias):
        sl = min(low, s["OR_LOW"])
        risk = close - sl
        if risk > 0:
            return make_candidate(symbol, "LONG", "OR_RETEST_REJECTION", bias, row, close, sl, close + risk * RR_TARGET, 22, f"Retest/rejection of opening range high {s['OR_HIGH']:.2f}")

    if bear_broke and high >= s["OR_LOW"] - atr * RETEST_BUFFER_ATR and close < s["OR_LOW"] and strong_rejection(row, "SHORT") and bias_allows("SHORT", bias):
        sl = max(high, s["OR_HIGH"])
        risk = sl - close
        if risk > 0:
            return make_candidate(symbol, "SHORT", "OR_RETEST_REJECTION", bias, row, close, sl, close - risk * RR_TARGET, 22, f"Retest/rejection of opening range low {s['OR_LOW']:.2f}")

    return None


def detect_trend_pullback(symbol: str, df5: pd.DataFrame, row, bias: str):
    close = float(row["close"])
    low = float(row["low"])
    high = float(row["high"])
    open_ = float(row["open"])
    atr = float(row["atr"])
    ema20 = float(row["ema20"])
    ema50 = float(row["ema50"])

    if bias_allows("LONG", bias):
        touched_value = low <= ema20 + atr * PULLBACK_BUFFER_ATR or low <= ema50 + atr * 0.20
        reclaimed = close > open_ and close > ema20
        if touched_value and reclaimed and strong_rejection(row, "LONG") and candle_quality(row):
            sl = min(low, ema50 - atr * 0.10)
            risk = close - sl
            if risk > 0:
                return make_candidate(symbol, "LONG", "TREND_PULLBACK", bias, row, close, sl, close + risk * RR_TARGET, 16, "Pullback into EMA zone and bullish reclaim")

    if bias_allows("SHORT", bias):
        touched_value = high >= ema20 - atr * PULLBACK_BUFFER_ATR or high >= ema50 - atr * 0.20
        rejected = close < open_ and close < ema20
        if touched_value and rejected and strong_rejection(row, "SHORT") and candle_quality(row):
            sl = max(high, ema50 + atr * 0.10)
            risk = sl - close
            if risk > 0:
                return make_candidate(symbol, "SHORT", "TREND_PULLBACK", bias, row, close, sl, close - risk * RR_TARGET, 16, "Pullback into EMA zone and bearish rejection")

    return None


def detect_liquidity_sweep_reversal(symbol: str, df5: pd.DataFrame, row, bias: str):
    i = len(df5) - 1
    if i < 10:
        return None

    recent8 = df5.iloc[i - 8:i]
    close = float(row["close"])
    low = float(row["low"])
    high = float(row["high"])

    if len(recent8) < 6:
        return None

    swing_low = float(recent8["low"].min())
    swing_high = float(recent8["high"].max())

    if low < swing_low and close > swing_low and bullish_pin(row) and bias != "BEAR":
        sl = low
        risk = close - sl
        if risk > 0:
            return make_candidate(symbol, "LONG", "LIQUIDITY_SWEEP_REVERSAL", bias, row, close, sl, close + risk * RR_TARGET, 14, "Downside liquidity sweep and bullish reversal candle")

    if high > swing_high and close < swing_high and bearish_pin(row) and bias != "BULL" and ALLOW_SHORTS:
        sl = high
        risk = sl - close
        if risk > 0:
            return make_candidate(symbol, "SHORT", "LIQUIDITY_SWEEP_REVERSAL", bias, row, close, sl, close - risk * RR_TARGET, 14, "Upside liquidity sweep and bearish reversal candle")

    return None


def detect_range_rejection(symbol: str, df5: pd.DataFrame, row, bias: str):
    if len(df5) < 25:
        return None

    recent = df5.tail(20)
    range_high = float(recent["high"].max())
    range_low = float(recent["low"].min())
    close = float(row["close"])
    low = float(row["low"])
    high = float(row["high"])

    if low <= range_low and close > range_low and bullish_pin(row) and bias != "BEAR":
        sl = low
        risk = close - sl
        if risk > 0:
            return make_candidate(symbol, "LONG", "RANGE_REJECTION", bias, row, close, sl, close + risk * RR_TARGET, 10, f"Range-low rejection near {range_low:.2f}")

    if high >= range_high and close < range_high and bearish_pin(row) and bias != "BULL" and ALLOW_SHORTS:
        sl = high
        risk = sl - close
        if risk > 0:
            return make_candidate(symbol, "SHORT", "RANGE_REJECTION", bias, row, close, sl, close - risk * RR_TARGET, 10, f"Range-high rejection near {range_high:.2f}")

    return None


def detect_compression_breakout(symbol: str, df5: pd.DataFrame, row, bias: str):
    if len(df5) < 20:
        return None

    recent = df5.tail(12)
    width = float(recent["high"].max() - recent["low"].min())
    atr = float(row["atr"])
    close = float(row["close"])
    low = float(row["low"])
    high = float(row["high"])

    if atr <= 0:
        return None

    high_level = float(recent["high"].max())
    low_level = float(recent["low"].min())

    if width <= atr * 2.4 and close > high_level and bias_allows("LONG", bias):
        sl = low
        risk = close - sl
        if risk > 0:
            return make_candidate(symbol, "LONG", "COMPRESSION_BREAKOUT", bias, row, close, sl, close + risk * RR_TARGET, 15, "Compression resolved upward")

    if width <= atr * 2.4 and close < low_level and bias_allows("SHORT", bias):
        sl = high
        risk = sl - close
        if risk > 0:
            return make_candidate(symbol, "SHORT", "COMPRESSION_BREAKOUT", bias, row, close, sl, close - risk * RR_TARGET, 15, "Compression resolved downward")

    return None


def detect_ema_reclaim(symbol: str, df5: pd.DataFrame, row, bias: str):
    if len(df5) < 2:
        return None

    prev = df5.iloc[-2]
    close = float(row["close"])
    high = float(row["high"])
    low = float(row["low"])
    ema20 = float(row["ema20"])

    if prev["close"] < prev["ema20"] and close > ema20 and close > row["open"] and bias_allows("LONG", bias):
        sl = min(low, ema20)
        risk = close - sl
        if risk > 0:
            return make_candidate(symbol, "LONG", "EMA_RECLAIM", bias, row, close, sl, close + risk * RR_TARGET, 9, "Price reclaimed EMA20 in bullish context")

    if prev["close"] > prev["ema20"] and close < ema20 and close < row["open"] and bias_allows("SHORT", bias):
        sl = max(high, ema20)
        risk = sl - close
        if risk > 0:
            return make_candidate(symbol, "SHORT", "EMA_RECLAIM", bias, row, close, sl, close - risk * RR_TARGET, 9, "Price lost EMA20 in bearish context")

    return None


DETECTORS = [
    detect_or_breakout_continuation,
    detect_or_retest_rejection,
    detect_trend_pullback,
    detect_liquidity_sweep_reversal,
    detect_range_rejection,
    detect_compression_breakout,
    detect_ema_reclaim,
]


def get_candidates(symbol: str):
    s = STATE[symbol]
    df5_raw = bars_to_df(symbol, "5Min", 400)
    df5 = add_indicators(df5_raw)

    if df5.empty:
        s["LAST_REASON"] = "NO_DATA"
        return []

    build_opening_range(symbol, df5)

    row = df5.iloc[-1]
    close = float(row["close"])
    s["LAST_PRICE"] = close

    t = now_ny()
    if not in_window(t, TRADE_START, LAST_ENTRY_TIME):
        s["LAST_REASON"] = "OUTSIDE_TRADE_WINDOW"; return []
    # Opening-range models need OR_SET, but trend/reclaim/range/compression setups do not.
    # So do not globally block here. The OR-specific detectors check OR_SET themselves.
    if not s["OR_SET"]:
        s["LAST_REASON"] = "OPENING_RANGE_NOT_READY_NON_OR_MODELS_ALLOWED"
    if s["TRADED_TODAY"]:
        s["LAST_REASON"] = "TRADED_TODAY"; return []
    if BOT_STATE["TRADES_TODAY"] >= MAX_TRADES_PER_DAY:
        s["LAST_REASON"] = "MAX_DAILY_TRADES"; return []
    if has_position(symbol):
        s["IN_POSITION"] = True; s["LAST_REASON"] = "ALREADY_IN_POSITION"; return []
    if has_open_order(symbol):
        s["LAST_REASON"] = "OPEN_ORDER_EXISTS"; return []

    bias = htf_bias(symbol)

    # Do not globally block CHOP.
    # CHOP should still allow reversal/range/sweep setups, while trend/breakout models
    # remain controlled by bias_allows().
    if bias == "NONE":
        s["LAST_REASON"] = "HTF_NONE"; return []

    if float(row["atr_pct"]) < MIN_ATR_PCT:
        s["LAST_REASON"] = "LOW_VOLATILITY"; return []
    if not not_dead_chop(df5):
        s["LAST_REASON"] = "CHOP_BLOCK"; return []
    if not volume_ok(row):
        s["LAST_REASON"] = "LOW_VOLUME"; return []

    candidates = []
    for fn in DETECTORS:
        try:
            c = fn(symbol, df5, row, bias)
            if c:
                candidates.append(edge_adjustment(c, symbol, row))
        except Exception as e:
            print(f"{symbol} detector {fn.__name__} error:", e)

    if not candidates:
        s["LAST_REASON"] = "NO_SETUP"
        return []

    candidates = sorted(candidates, key=lambda x: x["score"], reverse=True)

    for c in candidates:
        print(
            f"{symbol} candidate {c['model']} {c['direction']} "
            f"score={c['score']} bias={c['bias']} edge={c.get('edge_note', 'none')}"
        )

    s["LAST_SIGNAL_MODEL"] = candidates[0]["model"]
    return candidates


# ============================================================
# ORDER FLOW
# ============================================================

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


def round_price(x: float) -> float:
    return round(float(x), 2)


def submit_bracket_order(signal: dict):
    symbol = signal["symbol"]
    side = signal["side"]

    original_entry_ref = float(signal["price"])
    live_price = get_latest_price(symbol)

    if live_price is None or live_price <= 0:
        return {
            "ok": False,
            "reason": "could not get latest Alpaca price before order submit",
            "dry_run": False,
            "qty": 0,
        }

    drift = abs(live_price - original_entry_ref) / max(original_entry_ref, 1e-9)

    if drift > MAX_ENTRY_DRIFT_PCT:
        return {
            "ok": False,
            "reason": (
                f"live price drift too large. Signal entry_ref=${original_entry_ref:.2f}, "
                f"latest Alpaca price=${live_price:.2f}, drift={drift:.2%}. "
                f"Order blocked to prevent stale-data bracket rejection."
            ),
            "dry_run": False,
            "qty": 0,
        }

    # Re-anchor the bracket around the latest Alpaca price.
    # This stops Alpaca rejecting take_profit/stop_loss because its base_price
    # differs slightly from the candle close.
    signal["price"] = float(live_price)

    sl = float(signal["sl"])

    if side == "buy":
        risk = live_price - sl
        if risk <= 0:
            return {
                "ok": False,
                "reason": f"invalid buy risk after live price check: live=${live_price:.2f}, sl=${sl:.2f}",
                "dry_run": False,
                "qty": 0,
            }
        signal["tp"] = live_price + risk * RR_TARGET

    elif side == "sell":
        risk = sl - live_price
        if risk <= 0:
            return {
                "ok": False,
                "reason": f"invalid sell risk after live price check: live=${live_price:.2f}, sl=${sl:.2f}",
                "dry_run": False,
                "qty": 0,
            }
        signal["tp"] = live_price - risk * RR_TARGET

    qty = calculate_qty(signal)

    if qty <= 0:
        return {
            "ok": False,
            "reason": "qty calculated as 0. This usually means risk per share is too large, buying power is too low, or position cap is too small.",
            "dry_run": False,
            "qty": 0,
        }

    entry_ref = float(signal["price"])
    sl = round_price(signal["sl"])
    tp = round_price(signal["tp"])

    # Alpaca market bracket validation uses its own base_price.
    # Keep TP/SL safely on the correct side of live reference.
    if side == "buy":
        min_tp = round_price(entry_ref + 0.02)
        max_sl = round_price(entry_ref - 0.02)
        tp = max(tp, min_tp)
        sl = min(sl, max_sl)

    if side == "sell":
        max_tp = round_price(entry_ref - 0.02)
        min_sl = round_price(entry_ref + 0.02)
        tp = min(tp, max_tp)
        sl = max(sl, min_sl)

    signal["sl"] = sl
    signal["tp"] = tp

    if not EXECUTE_ORDERS:
        return {"ok": True, "dry_run": True, "qty": qty, "id": "DRY_RUN"}

    payload = {
        "symbol": symbol,
        "qty": str(qty),
        "side": side,
        "type": "market",
        "time_in_force": "day",
        "order_class": "bracket",
        "take_profit": {"limit_price": str(tp)},
        "stop_loss": {"stop_price": str(sl)},
    }

    result = alpaca_post("/v2/orders", payload)

    if isinstance(result, dict) and result.get("_ok") is False:
        status = result.get("_status_code", "unknown")
        error = result.get("_error", "unknown error")
        return {
            "ok": False,
            "reason": f"Alpaca rejected order. Status: {status}. Error: {error}",
            "dry_run": False,
            "qty": qty,
            "payload": payload,
        }

    if result:
        return {"ok": True, "dry_run": False, "qty": qty, "id": result.get("id", "UNKNOWN"), "raw": result}

    return {
        "ok": False,
        "reason": "Alpaca order rejected or request failed, but no error body was returned.",
        "dry_run": False,
        "qty": qty,
        "payload": payload,
    }

def handle_signal(signal: dict):
    symbol = signal["symbol"]
    result = submit_bracket_order(signal)

    if not result["ok"]:
        send_once(
            f"{BOT_STATE['DATE']}:{symbol}:ORDER_FAIL:{int(time.time())}",
            f"{E_WARN} {symbol} ORDER FAILED\n\n"
            f"Model: {signal.get('model', 'UNKNOWN')}\n"
            f"Direction: {signal.get('direction', 'UNKNOWN')}\n"
            f"Score: {signal.get('score', 'UNKNOWN')}\n"
            f"Qty attempted: {result.get('qty', 'UNKNOWN')}\n"
            f"Entry ref: ${float(signal.get('price', 0)):.2f}\n"
            f"SL: ${float(signal.get('sl', 0)):.2f}\n"
            f"TP: ${float(signal.get('tp', 0)):.2f}\n\n"
            f"Reason: {result['reason']}"
        )
        return False

    STATE[symbol]["TRADED_TODAY"] = True
    STATE[symbol]["ORDER_ID"] = result["id"]
    STATE[symbol]["ORDER_STATUS_NOTIFIED"] = "submitted"
    BOT_STATE["TRADES_TODAY"] += 1

    icon = E_ROCKET if signal["side"] == "buy" else E_DOWN
    mode_text = "PAPER ORDER" if result["dry_run"] else "ORDER SENT"

    send_once(
        f"{BOT_STATE['DATE']}:{symbol}:SIGNAL:{signal['model']}",
        f"{icon} {symbol} {mode_text}\n\n"
        f"Model: {signal['model']}\n"
        f"Direction: {signal['direction']}\n"
        f"Bias: {signal['bias']}\n"
        f"Score: {signal['score']}/100\n"
        f"Qty: {result['qty']}\n"
        f"Entry ref: ${signal['price']:.2f}\n"
        f"SL: ${signal['sl']:.2f}\n"
        f"TP: ${signal['tp']:.2f}\n"
        f"Reason: {signal['reason']}\n"
        f"Edge: {signal.get('edge_note', 'no_profile')}\n"
        f"Paper: {ALPACA_PAPER}\n"
        f"Execute: {EXECUTE_ORDERS}"
    )
    return True


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
            send_once(f"{BOT_STATE['DATE']}:{symbol}:FILLED", f"{E_CHECK} {symbol} ORDER FILLED\n\nStatus: {status}\nOrder ID: {order_id}")
        elif status in ["canceled", "expired", "rejected", "suspended"]:
            send_once(f"{BOT_STATE['DATE']}:{symbol}:{status}", f"{E_CROSS} {symbol} ORDER {status.upper()}\n\nOrder ID: {order_id}")

        s["ORDER_STATUS_NOTIFIED"] = status



def is_exit_order_for_position(order, symbol: str, qty: float) -> bool:
    if order.get("symbol") != symbol:
        return False

    status = order.get("status")
    if status not in ["new", "accepted", "pending_new", "held", "partially_filled"]:
        return False

    side = order.get("side")

    # Long positions need sell exits.
    if qty > 0 and side != "sell":
        return False

    # Short positions need buy exits.
    if qty < 0 and side != "buy":
        return False

    return True


def protect_uncovered_positions():
    if not PROTECT_UNCOVERED_POSITIONS:
        return

    positions = get_positions()
    if not positions:
        return

    open_orders = get_open_orders()

    for p in positions:
        symbol = p.get("symbol")
        if not symbol:
            continue

        try:
            qty = float(p.get("qty", 0))
            avg_entry = float(p.get("avg_entry_price", 0))
            market_value = float(p.get("market_value", 0))
            unrealized_pl = float(p.get("unrealized_pl", 0))
        except Exception:
            qty = 0
            avg_entry = 0
            market_value = 0
            unrealized_pl = 0

        if qty == 0:
            continue

        exits = [o for o in open_orders if is_exit_order_for_position(o, symbol, qty)]

        if exits:
            if symbol in STATE:
                STATE[symbol]["LAST_REASON"] = "POSITION_PROTECTED"
            continue

        direction = "LONG" if qty > 0 else "SHORT"

        send_once(
            f"{BOT_STATE['DATE']}:{symbol}:UNPROTECTED_POSITION",
            f"{E_WARN} UNPROTECTED POSITION FOUND\n\n"
            f"Symbol: {symbol}\n"
            f"Direction: {direction}\n"
            f"Qty: {qty}\n"
            f"Avg entry: ${avg_entry:.2f}\n"
            f"Market value: ${market_value:.2f}\n"
            f"Unrealized P/L: ${unrealized_pl:.2f}\n\n"
            f"No open TP/SL exit orders were found."
        )

        should_auto_close = (
            (ALPACA_PAPER and AUTO_CLOSE_UNPROTECTED_PAPER)
            or ((not ALPACA_PAPER) and AUTO_CLOSE_UNPROTECTED_LIVE)
        )

        if not should_auto_close:
            continue

        result = close_position_market(symbol)

        if isinstance(result, dict) and result.get("_ok") is False:
            send_once(
                f"{BOT_STATE['DATE']}:{symbol}:AUTO_CLOSE_FAILED",
                f"{E_WARN} AUTO-CLOSE FAILED\n\n"
                f"Symbol: {symbol}\n"
                f"Reason: {result.get('_error', 'unknown')}"
            )
        else:
            send_once(
                f"{BOT_STATE['DATE']}:{symbol}:AUTO_CLOSED_UNPROTECTED",
                f"{E_CROSS} AUTO-CLOSED UNPROTECTED POSITION\n\n"
                f"Symbol: {symbol}\n"
                f"Mode: {'PAPER' if ALPACA_PAPER else 'LIVE'}\n"
                f"Reason: no open TP/SL exit orders found."
            )


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
        f"{E_FIRE} STOCK MERGED AUTO-TRADER LIVE {E_FIRE}\n\n"
        f"Mode: {'PAPER' if ALPACA_PAPER else 'LIVE'}\n"
        f"Execute orders: {EXECUTE_ORDERS}\n"
        f"Data feed: {ALPACA_DATA_FEED}\n"
        f"Equity: ${float(account.get('equity', 0)):.2f}\n"
        f"Buying Power: ${float(account.get('buying_power', 0)):.2f}\n"
        f"Watchlist: {', '.join(WATCHLIST)}\n\n"
        f"Models:\n"
        f"- OR breakout continuation\n"
        f"- OR retest rejection\n"
        f"- trend pullback\n"
        f"- liquidity sweep reversal\n"
        f"- range rejection\n"
        f"- compression breakout\n"
        f"- EMA reclaim\n\n"
        f"Using soft edge weighting if research profile exists.\n"
        f"Min score to trade: {MIN_SCORE_TO_TRADE}\n"
        f"Min ATR pct: {MIN_ATR_PCT}\n"
        f"Volume mult: {MIN_VOLUME_MULT}\n"
        f"RR target: {RR_TARGET}\n"
        f"Max trades/day: {MAX_TRADES_PER_DAY}\n"
        f"One position at a time: {ONE_POSITION_AT_A_TIME}\n"
        f"Max entry drift pct: {MAX_ENTRY_DRIFT_PCT:.2%}\n"
        f"Protect uncovered positions: {PROTECT_UNCOVERED_POSITIONS}\n"
        f"Auto-close unprotected paper: {AUTO_CLOSE_UNPROTECTED_PAPER}"
    )
    return True


# ============================================================
# MAIN
# ============================================================

def run():
    time.sleep(3)
    if not startup_check():
        return

    while True:
        try:
            reset_daily_state()
            protect_uncovered_positions()
            t = now_ny()

            if not in_window(t, MARKET_OPEN, MARKET_CLOSE):
                for symbol in WATCHLIST:
                    STATE[symbol]["LAST_REASON"] = "MARKET_CLOSED"
                check_order_updates()
                end_of_day_summary()
                time.sleep(CHECK_INTERVAL)
                continue

            _BAR_CACHE.clear()
            _HTF_CACHE["stamp"] = None
            _HTF_CACHE["bias"] = {}

            all_candidates = []

            for symbol in WATCHLIST:
                candidates = get_candidates(symbol)
                if candidates:
                    all_candidates.extend(candidates)
                time.sleep(0.25)

            if all_candidates and BOT_STATE["TRADES_TODAY"] < MAX_TRADES_PER_DAY:
                if ONE_POSITION_AT_A_TIME and len(get_positions()) > 0:
                    print("ONE_POSITION_AT_A_TIME enabled: skipping new entries while a position is open.")
                    check_order_updates()
                    end_of_day_summary()
                    time.sleep(CHECK_INTERVAL)
                    continue

                ranked = sorted(all_candidates, key=lambda x: x["score"], reverse=True)
                ranked = [r for r in ranked if r["score"] >= MIN_SCORE_TO_TRADE]

                orders_sent = 0
                used_symbols = set()
                attempted_symbols = set()

                for signal in ranked:
                    if orders_sent >= MAX_NEW_ORDERS_PER_SCAN:
                        break
                    if BOT_STATE["TRADES_TODAY"] >= MAX_TRADES_PER_DAY:
                        break

                    symbol = signal["symbol"]

                    # Only attempt one model per symbol each scan.
                    # This prevents TREND_PULLBACK and EMA_RECLAIM firing duplicate rejected orders
                    # for the same stale/live-price issue.
                    if symbol in attempted_symbols:
                        continue
                    attempted_symbols.add(symbol)

                    if symbol in used_symbols:
                        continue
                    if STATE[symbol]["TRADED_TODAY"]:
                        continue
                    if has_position(symbol) or has_open_order(symbol):
                        continue

                    ok = handle_signal(signal)
                    if ok:
                        used_symbols.add(symbol)
                        orders_sent += 1

            check_order_updates()
            end_of_day_summary()
            time.sleep(CHECK_INTERVAL)

        except Exception as e:
            send(f"{E_WARN} STOCK BOT ERROR:\n{e}")
            time.sleep(15)


if __name__ == "__main__":
    run()
