import os
import time
import math
import requests
import pandas as pd
import yfinance as yf
from datetime import datetime
from zoneinfo import ZoneInfo

# ============================================================
# NBIS BTC + GOLD LIVE TELEGRAM BOT
# PRO LIQUIDITY SWEEP VERSION
# FIXED: AUTO BACKFILLS ASIA + LONDON LEVELS FROM PREVIOUS CANDLES
# ============================================================

# ============================================================
# SAFE EMOJIS
# ============================================================
E_CHECK = "\u2705"
E_FIRE = "\U0001F525"
E_HEART = "\U0001F493"
E_WARN = "\u26A0\uFE0F"
E_ROCKET = "\U0001F680"
E_DOWN = "\U0001F4C9"
E_TARGET = "\U0001F3AF"
E_CROSS = "\u274C"
E_ZAP = "\u26A1"
E_MONEY = "\U0001F4B0"
E_UP = "\U0001F4C8"
E_CHART = "\U0001F4CA"
E_PIN = "\U0001F4CD"

# ============================================================
# ENV
# ============================================================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID") or os.getenv("TELEGRAM_CHAT_ID")
TWELVEDATA_API_KEY = os.getenv("TWELVEDATA_API_KEY")

# ============================================================
# CONFIG
# ============================================================
CHECK_INTERVAL = 60
HEARTBEAT_SECONDS = 3100
DATA_FAIL_ALERT_COOLDOWN = 1800

TIMEZONE = "Europe/London"

ASIA_START = "00:00"
ASIA_END = "06:00"

LONDON_MARK_TIME = "09:30"
TRADE_START = "09:30"
TRADE_END = "16:00"

# Extra institutional windows
NY_OPEN_TIME = "13:30"
NY_CONTINUATION_TIME = "14:30"

ONE_TRADE_PER_DAY = True

RR_TARGET = 2.0
SL_BUFFER_ATR = 0.15
BE_TRIGGER_R = 1.0
TRAIL_START_R = 1.5
TRAIL_ATR_MULT = 1.8

MIN_ATR_PCT = {
    "BTC": 0.00035,
    "GOLD": 0.00008,
}

MAX_DISPLACEMENT_ATR = {
    "BTC": 1.8,
    "GOLD": 1.5,
}

USE_EMA_FILTER = True
EMA_FILTER_LEN = 200
ATR_LEN = 14
RSI_LEN = 14

ASSETS = {
    "BTC": {
        "name": "BTC",
        "binance_symbol": "BTCUSDT",
        "coinbase_symbol": "BTC-USD",
        "yfinance_ticker": "BTC-USD",
        "td_symbol": "BTC/USD",
    },
    "GOLD": {
        "name": "GOLD",
        "binance_symbol": None,
        "coinbase_symbol": None,
        "yfinance_ticker": "GC=F",
        "td_symbol": "XAU/USD",
    },
}

STATE = {
    asset: {
        "IN_TRADE": False,
        "SIDE": None,
        "ENTRY": 0.0,
        "SL": 0.0,
        "TP": 0.0,
        "RISK": 0.0,
        "BE_ACTIVE": False,
        "TRAIL_ACTIVE": False,
        "HIGH": 0.0,
        "LOW": 0.0,

        "DATE": None,
        "ASIA_HIGH": None,
        "ASIA_LOW": None,
        "LONDON_HIGH": None,
        "LONDON_LOW": None,

        "SWEPT_HIGH": False,
        "SWEPT_LOW": False,
        "SWEEP_HIGH_EXTREME": None,
        "SWEEP_LOW_EXTREME": None,

        "TRADED_TODAY": False,
        "LAST_HEARTBEAT": 0.0,
        "LAST_DATA_FAIL": 0.0,
        "DATA_SOURCE": "UNKNOWN",
        "LAST_PRICE": None,

        "BACKFILLED_TODAY": False,
        "BACKFILL_ALERT_SENT": False,
        "LONDON_MARK_ALERT_SENT": False,
    }
    for asset in ASSETS
}

# ============================================================
# TELEGRAM
# ============================================================
def send(msg: str):
    print(msg)

    if not TELEGRAM_TOKEN or not CHAT_ID:
        print("TELEGRAM NOT SET")
        return

    for _ in range(3):
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                json={"chat_id": CHAT_ID, "text": msg},
                timeout=10,
            )
            if r.status_code == 200:
                return
            print("TELEGRAM FAIL:", r.status_code, r.text)
        except Exception as e:
            print("TELEGRAM ERROR:", e)
        time.sleep(2)

# ============================================================
# TIME HELPERS
# ============================================================
def now_london():
    return datetime.now(ZoneInfo(TIMEZONE))

def to_minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)

def minute_of_day(dt: datetime) -> int:
    return dt.hour * 60 + dt.minute

def in_window(dt: datetime, start: str, end: str) -> bool:
    t = minute_of_day(dt)
    return to_minutes(start) <= t <= to_minutes(end)

def is_mark_time(dt: datetime) -> bool:
    return minute_of_day(dt) == to_minutes(LONDON_MARK_TIME)

# ============================================================
# DATA
# ============================================================
def normalize_ohlcv(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()

    df = df.copy()

    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)

    df.columns = [str(c).lower() for c in df.columns]

    if "volume" not in df.columns:
        df["volume"] = 1.0

    needed = ["open", "high", "low", "close", "volume"]
    for c in needed:
        if c not in df.columns:
            return pd.DataFrame()

    df = df[needed].copy()

    for c in needed:
        df[c] = pd.to_numeric(df[c], errors="coerce")

    df.dropna(inplace=True)
    return df.reset_index(drop=True)

def td_interval(interval: str) -> str:
    return {"1m": "1min", "5m": "5min", "15m": "15min"}[interval]

def get_binance_klines(symbol: str, interval: str, limit: int = 500) -> pd.DataFrame:
    try:
        r = requests.get(
            "https://api.binance.com/api/v3/klines",
            params={"symbol": symbol, "interval": interval, "limit": limit},
            timeout=10,
            headers={"User-Agent": "Mozilla/5.0"},
        )

        if r.status_code != 200:
            print("BINANCE STATUS:", r.status_code, r.text[:250])
            return pd.DataFrame()

        data = r.json()

        if not isinstance(data, list) or len(data) < 30:
            return pd.DataFrame()

        df = pd.DataFrame(
            data,
            columns=[
                "time", "open", "high", "low", "close", "volume",
                "ct", "qav", "trades", "tbv", "tqv", "ignore"
            ],
        )

        return normalize_ohlcv(df)

    except Exception as e:
        print("BINANCE ERROR:", e)
        return pd.DataFrame()

def get_coinbase_klines(symbol: str, interval: str) -> pd.DataFrame:
    try:
        granularity = {"1m": 60, "5m": 300, "15m": 900}[interval]

        r = requests.get(
            f"https://api.exchange.coinbase.com/products/{symbol}/candles",
            params={"granularity": granularity},
            timeout=10,
            headers={"User-Agent": "Mozilla/5.0"},
        )

        if r.status_code != 200:
            print("COINBASE STATUS:", r.status_code, r.text[:250])
            return pd.DataFrame()

        data = r.json()

        if not isinstance(data, list) or len(data) < 30:
            return pd.DataFrame()

        df = pd.DataFrame(data, columns=["time", "low", "high", "open", "close", "volume"])
        df = df.sort_values("time").reset_index(drop=True)

        return normalize_ohlcv(df)

    except Exception as e:
        print("COINBASE ERROR:", e)
        return pd.DataFrame()

def get_twelvedata_klines(td_symbol: str, interval: str, outputsize: int = 500) -> pd.DataFrame:
    try:
        if not TWELVEDATA_API_KEY or not td_symbol:
            return pd.DataFrame()

        r = requests.get(
            "https://api.twelvedata.com/time_series",
            params={
                "symbol": td_symbol,
                "interval": td_interval(interval),
                "outputsize": outputsize,
                "apikey": TWELVEDATA_API_KEY,
                "format": "JSON",
            },
            timeout=15,
            headers={"User-Agent": "Mozilla/5.0"},
        )

        if r.status_code != 200:
            print("TWELVEDATA STATUS:", r.status_code, r.text[:250])
            return pd.DataFrame()

        data = r.json()

        if not isinstance(data, dict) or "values" not in data:
            print("TWELVEDATA BAD DATA:", data)
            return pd.DataFrame()

        values = data["values"]

        if not isinstance(values, list) or len(values) < 30:
            return pd.DataFrame()

        df = pd.DataFrame(values)
        df = df.iloc[::-1].reset_index(drop=True)

        return normalize_ohlcv(df)

    except Exception as e:
        print("TWELVEDATA ERROR:", e)
        return pd.DataFrame()

def get_yfinance_klines(ticker: str, interval: str) -> pd.DataFrame:
    try:
        period_map = {"1m": "7d", "5m": "30d", "15m": "60d"}
        df = yf.download(
            ticker,
            period=period_map.get(interval, "30d"),
            interval=interval,
            progress=False,
            auto_adjust=False,
            threads=False,
        )
        return normalize_ohlcv(df)
    except Exception as e:
        print("YFINANCE ERROR:", e)
        return pd.DataFrame()

def get_klines(asset_key: str, interval: str):
    asset = ASSETS[asset_key]

    if asset_key == "BTC":
        df = get_binance_klines(asset["binance_symbol"], interval)
        if not df.empty:
            return df, "BINANCE"

        df = get_coinbase_klines(asset["coinbase_symbol"], interval)
        if not df.empty:
            return df, "COINBASE"

        df = get_yfinance_klines(asset["yfinance_ticker"], interval)
        if not df.empty:
            return df, "YFINANCE"

        df = get_twelvedata_klines(asset["td_symbol"], interval)
        if not df.empty:
            return df, "TWELVEDATA"

        return pd.DataFrame(), "NONE"

    df = get_twelvedata_klines(asset["td_symbol"], interval)
    if not df.empty:
        return df, "TWELVEDATA"

    df = get_yfinance_klines(asset["yfinance_ticker"], interval)
    if not df.empty:
        return df, "YFINANCE"

    return pd.DataFrame(), "NONE"

# ============================================================
# INDICATORS
# ============================================================
def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty or len(df) < 60:
        return pd.DataFrame()

    out = df.copy()

    out["ema200"] = out["close"].ewm(span=EMA_FILTER_LEN, adjust=False).mean()

    delta = out["close"].diff()
    gain = delta.clip(lower=0).rolling(RSI_LEN).mean()
    loss = delta.clip(upper=0).abs().rolling(RSI_LEN).mean()
    rs = gain / loss.replace(0, pd.NA)
    out["rsi"] = 100 - (100 / (1 + rs))

    tr = pd.concat(
        [
            out["high"] - out["low"],
            (out["high"] - out["close"].shift()).abs(),
            (out["low"] - out["close"].shift()).abs(),
        ],
        axis=1,
    ).max(axis=1)

    out["atr"] = tr.rolling(ATR_LEN).mean()
    out["atr_pct"] = out["atr"] / out["close"]
    out["body"] = (out["close"] - out["open"]).abs()

    out.dropna(inplace=True)
    return out.reset_index(drop=True)

# ============================================================
# SESSION STATE
# ============================================================
def reset_day(asset_key: str, dt: datetime):
    s = STATE[asset_key]
    if s["DATE"] == dt.date():
        return

    s["DATE"] = dt.date()
    s["ASIA_HIGH"] = None
    s["ASIA_LOW"] = None
    s["LONDON_HIGH"] = None
    s["LONDON_LOW"] = None
    s["SWEPT_HIGH"] = False
    s["SWEPT_LOW"] = False
    s["SWEEP_HIGH_EXTREME"] = None
    s["SWEEP_LOW_EXTREME"] = None
    s["TRADED_TODAY"] = False
    s["BACKFILLED_TODAY"] = False
    s["BACKFILL_ALERT_SENT"] = False
    s["LONDON_MARK_ALERT_SENT"] = False

def backfill_sessions(asset_key: str, df1: pd.DataFrame, dt: datetime):
    """
    FIXED: Rebuilds today's Asia range and London 09:30 mark from previous 1m candles.
    This means the bot works even if you start it after Asia / after London / mid NY.
    """
    s = STATE[asset_key]

    if s["BACKFILLED_TODAY"]:
        return

    if df1.empty or len(df1) < 120:
        return

    df = df1.copy()

    # Assumes the latest row is now and candles are 1-minute. This matches the live feeds used here.
    df["dt"] = pd.date_range(end=dt, periods=len(df), freq="min")

    today = dt.date()

    asia_high = None
    asia_low = None
    london_high = None
    london_low = None

    # Also detect already-swept levels from the historical part of today.
    swept_high = False
    swept_low = False
    sweep_high_extreme = None
    sweep_low_extreme = None

    for _, row in df.iterrows():
        candle_dt = row["dt"].to_pydatetime() if hasattr(row["dt"], "to_pydatetime") else row["dt"]

        if candle_dt.date() != today:
            continue

        m = minute_of_day(candle_dt)
        h = float(row["high"])
        l = float(row["low"])
        c = float(row["close"])

        # Build Asia range from 00:00-06:00.
        if to_minutes(ASIA_START) <= m <= to_minutes(ASIA_END):
            asia_high = h if asia_high is None else max(asia_high, h)
            asia_low = l if asia_low is None else min(asia_low, l)

        # Mark London 09:30 candle.
        if m == to_minutes(LONDON_MARK_TIME):
            london_high = h
            london_low = l

        # Once Asia levels are known, scan if they were already swept after Asia ended.
        if asia_high is not None and asia_low is not None and m > to_minutes(ASIA_END):
            if h > asia_high and c < asia_high:
                swept_high = True
                sweep_high_extreme = h if sweep_high_extreme is None else max(sweep_high_extreme, h)

            if l < asia_low and c > asia_low:
                swept_low = True
                sweep_low_extreme = l if sweep_low_extreme is None else min(sweep_low_extreme, l)

    changed = False

    if s["ASIA_HIGH"] is None and asia_high is not None:
        s["ASIA_HIGH"] = asia_high
        s["ASIA_LOW"] = asia_low
        changed = True

    if s["LONDON_HIGH"] is None and london_high is not None:
        s["LONDON_HIGH"] = london_high
        s["LONDON_LOW"] = london_low
        changed = True

    if swept_high and not s["SWEPT_HIGH"]:
        s["SWEPT_HIGH"] = True
        s["SWEEP_HIGH_EXTREME"] = sweep_high_extreme
        changed = True

    if swept_low and not s["SWEPT_LOW"]:
        s["SWEPT_LOW"] = True
        s["SWEEP_LOW_EXTREME"] = sweep_low_extreme
        changed = True

    s["BACKFILLED_TODAY"] = True

    if changed and not s["BACKFILL_ALERT_SENT"]:
        send(
            f"{E_CHART} {asset_key} MARKET BACKFILLED\n\n"
            f"Asia High: {fmt_level(s['ASIA_HIGH'])}\n"
            f"Asia Low: {fmt_level(s['ASIA_LOW'])}\n"
            f"London High: {fmt_level(s['LONDON_HIGH'])}\n"
            f"London Low: {fmt_level(s['LONDON_LOW'])}\n"
            f"Swept High: {'YES' if s['SWEPT_HIGH'] else 'NO'}\n"
            f"Swept Low: {'YES' if s['SWEPT_LOW'] else 'NO'}"
        )
        s["BACKFILL_ALERT_SENT"] = True

def build_asia_range(asset_key: str, df1: pd.DataFrame, dt: datetime):
    if not in_window(dt, ASIA_START, ASIA_END):
        return

    s = STATE[asset_key]
    r = df1.iloc[-1]

    h = float(r["high"])
    l = float(r["low"])

    s["ASIA_HIGH"] = h if s["ASIA_HIGH"] is None else max(s["ASIA_HIGH"], h)
    s["ASIA_LOW"] = l if s["ASIA_LOW"] is None else min(s["ASIA_LOW"], l)

def mark_london_reference(asset_key: str, df1: pd.DataFrame, dt: datetime):
    if not is_mark_time(dt):
        return

    s = STATE[asset_key]
    r = df1.iloc[-1]

    s["LONDON_HIGH"] = float(r["high"])
    s["LONDON_LOW"] = float(r["low"])

    if not s["LONDON_MARK_ALERT_SENT"]:
        send(
            f"{E_CHECK} {asset_key} LONDON MARK SET\n\n"
            f"High: ${s['LONDON_HIGH']:.2f}\n"
            f"Low: ${s['LONDON_LOW']:.2f}\n"
            f"Asia High: {fmt_level(s['ASIA_HIGH'])}\n"
            f"Asia Low: {fmt_level(s['ASIA_LOW'])}"
        )
        s["LONDON_MARK_ALERT_SENT"] = True

def detect_sweep(asset_key: str, df1: pd.DataFrame):
    s = STATE[asset_key]
    r = df1.iloc[-1]

    if s["ASIA_HIGH"] is None or s["ASIA_LOW"] is None:
        return

    high = float(r["high"])
    low = float(r["low"])
    close = float(r["close"])

    if high > s["ASIA_HIGH"] and close < s["ASIA_HIGH"] and not s["SWEPT_HIGH"]:
        s["SWEPT_HIGH"] = True
        s["SWEEP_HIGH_EXTREME"] = high
        send(
            f"{E_WARN} {asset_key} ASIA HIGH SWEPT\n\n"
            f"Sweep high: ${high:.2f}\n"
            f"Asia high: ${s['ASIA_HIGH']:.2f}\n"
            f"Waiting for short confirmation."
        )

    if low < s["ASIA_LOW"] and close > s["ASIA_LOW"] and not s["SWEPT_LOW"]:
        s["SWEPT_LOW"] = True
        s["SWEEP_LOW_EXTREME"] = low
        send(
            f"{E_WARN} {asset_key} ASIA LOW SWEPT\n\n"
            f"Sweep low: ${low:.2f}\n"
            f"Asia low: ${s['ASIA_LOW']:.2f}\n"
            f"Waiting for long confirmation."
        )

# ============================================================
# SIGNAL
# ============================================================
def data_ready(asset_key: str) -> bool:
    s = STATE[asset_key]
    return (
        s["ASIA_HIGH"] is not None
        and s["ASIA_LOW"] is not None
        and s["LONDON_HIGH"] is not None
        and s["LONDON_LOW"] is not None
    )

def long_filter(asset_key: str, r) -> bool:
    if float(r["atr_pct"]) < MIN_ATR_PCT[asset_key]:
        return False
    if float(r["body"]) > float(r["atr"]) * MAX_DISPLACEMENT_ATR[asset_key]:
        return False
    if USE_EMA_FILTER and float(r["close"]) < float(r["ema200"]):
        return False
    return True

def short_filter(asset_key: str, r) -> bool:
    if float(r["atr_pct"]) < MIN_ATR_PCT[asset_key]:
        return False
    if float(r["body"]) > float(r["atr"]) * MAX_DISPLACEMENT_ATR[asset_key]:
        return False
    if USE_EMA_FILTER and float(r["close"]) > float(r["ema200"]):
        return False
    return True

def get_live_signal(asset_key: str):
    df1_raw, src = get_klines(asset_key, "1m")
    df1 = add_indicators(df1_raw)

    s = STATE[asset_key]
    s["DATA_SOURCE"] = src

    if df1.empty:
        return None

    dt = now_london()
    reset_day(asset_key, dt)

    # KEY FIX: scan previous market first, so bot works even if started late.
    backfill_sessions(asset_key, df1, dt)

    # Then keep building live levels normally.
    build_asia_range(asset_key, df1, dt)
    mark_london_reference(asset_key, df1, dt)

    r = df1.iloc[-1]
    price = float(r["close"])
    s["LAST_PRICE"] = price

    detect_sweep(asset_key, df1)

    signal = {
        "asset": asset_key,
        "price": price,
        "rsi": float(r["rsi"]),
        "atr": float(r["atr"]),
        "atr_pct": float(r["atr_pct"]),
        "df1": df1,
        "feed": src,
        "side": None,
        "reason": "WAITING",
    }

    if not in_window(dt, TRADE_START, TRADE_END):
        signal["reason"] = "OUTSIDE_TRADE_WINDOW"
        return signal

    if not data_ready(asset_key):
        signal["reason"] = "SESSION_LEVELS_NOT_READY"
        return signal

    if ONE_TRADE_PER_DAY and s["TRADED_TODAY"]:
        signal["reason"] = "TRADED_TODAY"
        return signal

    if s["IN_TRADE"]:
        signal["reason"] = "IN_TRADE"
        return signal

    close = float(r["close"])
    open_ = float(r["open"])
    current_min = minute_of_day(dt)

    # Original long: Asia low swept, then bullish close above London mark high.
    if (
        s["SWEPT_LOW"]
        and close > s["LONDON_HIGH"]
        and close > open_
        and long_filter(asset_key, r)
    ):
        signal["side"] = "LONG"
        signal["reason"] = "ASIA_LOW_SWEEP_RECLAIM_BREAK_LONDON_HIGH"
        return signal

    # Original short: Asia high swept, then bearish close below London mark low.
    if (
        s["SWEPT_HIGH"]
        and close < s["LONDON_LOW"]
        and close < open_
        and short_filter(asset_key, r)
    ):
        signal["side"] = "SHORT"
        signal["reason"] = "ASIA_HIGH_SWEEP_REJECT_BREAK_LONDON_LOW"
        return signal

    # NY open model: after 13:30, if sweep has happened, allow confirmation back through Asia level.
    if current_min >= to_minutes(NY_OPEN_TIME):
        if (
            s["SWEPT_HIGH"]
            and close < s["ASIA_HIGH"]
            and close < open_
            and short_filter(asset_key, r)
        ):
            signal["side"] = "SHORT"
            signal["reason"] = "NY_OPEN_ASIA_HIGH_SWEEP_REJECTION"
            return signal

        if (
            s["SWEPT_LOW"]
            and close > s["ASIA_LOW"]
            and close > open_
            and long_filter(asset_key, r)
        ):
            signal["side"] = "LONG"
            signal["reason"] = "NY_OPEN_ASIA_LOW_SWEEP_RECLAIM"
            return signal

    # 14:30 continuation model: use London mark breakout/breakdown after NY has opened.
    if current_min >= to_minutes(NY_CONTINUATION_TIME):
        if (
            close > s["LONDON_HIGH"]
            and close > open_
            and long_filter(asset_key, r)
        ):
            signal["side"] = "LONG"
            signal["reason"] = "NY_1430_LONDON_HIGH_CONTINUATION"
            return signal

        if (
            close < s["LONDON_LOW"]
            and close < open_
            and short_filter(asset_key, r)
        ):
            signal["side"] = "SHORT"
            signal["reason"] = "NY_1430_LONDON_LOW_CONTINUATION"
            return signal

    signal["reason"] = "NO_CONFIRMATION"
    return signal

# ============================================================
# HEARTBEAT
# ============================================================
def heartbeat(asset_key: str, sig):
    s = STATE[asset_key]
    now = time.time()

    if now - s["LAST_HEARTBEAT"] < HEARTBEAT_SECONDS:
        return

    if sig is None:
        send(
            f"{E_HEART} {asset_key} HEARTBEAT\n\n"
            f"Status: NO DATA\n"
            f"In trade: {'YES' if s['IN_TRADE'] else 'NO'}\n"
            f"Feed: {s['DATA_SOURCE']}"
        )
    else:
        send(
            f"{E_HEART} {asset_key} HEARTBEAT\n\n"
            f"Price: ${sig['price']:.2f}\n"
            f"RSI: {sig['rsi']:.1f}\n"
            f"Asia High: {fmt_level(s['ASIA_HIGH'])}\n"
            f"Asia Low: {fmt_level(s['ASIA_LOW'])}\n"
            f"London High: {fmt_level(s['LONDON_HIGH'])}\n"
            f"London Low: {fmt_level(s['LONDON_LOW'])}\n"
            f"Swept High: {'YES' if s['SWEPT_HIGH'] else 'NO'}\n"
            f"Swept Low: {'YES' if s['SWEPT_LOW'] else 'NO'}\n"
            f"Reason: {sig['reason']}\n"
            f"In trade: {'YES' if s['IN_TRADE'] else 'NO'}\n"
            f"Feed: {sig['feed']}"
        )

    s["LAST_HEARTBEAT"] = now

def fmt_level(x):
    if x is None:
        return "WAITING"
    return f"${float(x):.2f}"

# ============================================================
# TRADE MANAGEMENT
# ============================================================
def start_trade(asset_key: str, sig):
    s = STATE[asset_key]

    side = sig["side"]
    price = sig["price"]
    atr = sig["atr"]

    if side == "LONG":
        sl_base = s["SWEEP_LOW_EXTREME"] if s["SWEEP_LOW_EXTREME"] is not None else sig["df1"].iloc[-1]["low"]
        sl = float(sl_base) - atr * SL_BUFFER_ATR
        risk = price - sl
        tp = price + risk * RR_TARGET
        icon = E_ROCKET
    else:
        sl_base = s["SWEEP_HIGH_EXTREME"] if s["SWEEP_HIGH_EXTREME"] is not None else sig["df1"].iloc[-1]["high"]
        sl = float(sl_base) + atr * SL_BUFFER_ATR
        risk = sl - price
        tp = price - risk * RR_TARGET
        icon = E_DOWN

    if risk <= 0:
        return

    s["IN_TRADE"] = True
    s["SIDE"] = side
    s["ENTRY"] = price
    s["SL"] = sl
    s["TP"] = tp
    s["RISK"] = risk
    s["BE_ACTIVE"] = False
    s["TRAIL_ACTIVE"] = False
    s["HIGH"] = price
    s["LOW"] = price
    s["TRADED_TODAY"] = True

    send(
        f"{icon} {asset_key} {side} ENTRY\n\n"
        f"Model: ASIA / LONDON / NY LIQUIDITY SWEEP\n"
        f"Reason: {sig['reason']}\n"
        f"Entry: ${price:.2f}\n"
        f"SL: ${sl:.2f}\n"
        f"TP: ${tp:.2f}\n"
        f"RR: 1:{RR_TARGET:.1f}\n"
        f"Asia High: {fmt_level(s['ASIA_HIGH'])}\n"
        f"Asia Low: {fmt_level(s['ASIA_LOW'])}\n"
        f"London High: {fmt_level(s['LONDON_HIGH'])}\n"
        f"London Low: {fmt_level(s['LONDON_LOW'])}\n"
        f"Feed: {sig['feed']}"
    )

def reset_trade(asset_key: str):
    s = STATE[asset_key]
    s["IN_TRADE"] = False
    s["SIDE"] = None
    s["ENTRY"] = 0.0
    s["SL"] = 0.0
    s["TP"] = 0.0
    s["RISK"] = 0.0
    s["BE_ACTIVE"] = False
    s["TRAIL_ACTIVE"] = False
    s["HIGH"] = 0.0
    s["LOW"] = 0.0

def manage_trade(asset_key: str, sig):
    if sig is None:
        return

    s = STATE[asset_key]
    if not s["IN_TRADE"]:
        return

    price = sig["price"]
    atr = sig["atr"]

    if s["SIDE"] == "LONG":
        s["HIGH"] = max(s["HIGH"], price)

        if not s["BE_ACTIVE"] and price >= s["ENTRY"] + s["RISK"] * BE_TRIGGER_R:
            s["SL"] = max(s["SL"], s["ENTRY"])
            s["BE_ACTIVE"] = True
            send(f"{E_ZAP} {asset_key} LONG BREAK-EVEN\nNew SL: ${s['SL']:.2f}")

        if price >= s["ENTRY"] + s["RISK"] * TRAIL_START_R:
            new_sl = s["HIGH"] - atr * TRAIL_ATR_MULT
            if new_sl > s["SL"]:
                s["SL"] = new_sl
                s["TRAIL_ACTIVE"] = True
                send(f"{E_UP} {asset_key} LONG TRAILING STOP\nNew SL: ${s['SL']:.2f}")

        if price <= s["SL"]:
            send(f"{E_CROSS} {asset_key} LONG STOP HIT\nExit: ${price:.2f}")
            reset_trade(asset_key)
            return

        if price >= s["TP"]:
            send(f"{E_TARGET} {asset_key} LONG TARGET HIT\nExit: ${price:.2f}")
            reset_trade(asset_key)
            return

    elif s["SIDE"] == "SHORT":
        s["LOW"] = min(s["LOW"], price)

        if not s["BE_ACTIVE"] and price <= s["ENTRY"] - s["RISK"] * BE_TRIGGER_R:
            s["SL"] = min(s["SL"], s["ENTRY"])
            s["BE_ACTIVE"] = True
            send(f"{E_ZAP} {asset_key} SHORT BREAK-EVEN\nNew SL: ${s['SL']:.2f}")

        if price <= s["ENTRY"] - s["RISK"] * TRAIL_START_R:
            new_sl = s["LOW"] + atr * TRAIL_ATR_MULT
            if new_sl < s["SL"]:
                s["SL"] = new_sl
                s["TRAIL_ACTIVE"] = True
                send(f"{E_DOWN} {asset_key} SHORT TRAILING STOP\nNew SL: ${s['SL']:.2f}")

        if price >= s["SL"]:
            send(f"{E_CROSS} {asset_key} SHORT STOP HIT\nExit: ${price:.2f}")
            reset_trade(asset_key)
            return

        if price <= s["TP"]:
            send(f"{E_TARGET} {asset_key} SHORT TARGET HIT\nExit: ${price:.2f}")
            reset_trade(asset_key)
            return

# ============================================================
# MAIN
# ============================================================
def run():
    time.sleep(5)
    send(
        f"{E_FIRE} BTC + GOLD LIQUIDITY SWEEP BOT LIVE {E_FIRE}\n"
        f"Time: {now_london().strftime('%H:%M:%S')}\n"
        f"Model: Asia sweep + London 09:30 + NY 13:30/14:30\n"
        f"Backfill: ON"
    )

    while True:
        try:
            for asset_key in ASSETS:
                sig = get_live_signal(asset_key)
                heartbeat(asset_key, sig)

                if sig is not None:
                    print(
                        asset_key,
                        "PRICE:", sig["price"],
                        "REASON:", sig["reason"],
                        "SIDE:", sig["side"],
                        "FEED:", sig["feed"],
                    )

                if sig is None:
                    continue

                if STATE[asset_key]["IN_TRADE"]:
                    manage_trade(asset_key, sig)
                    continue

                if sig["side"] in ["LONG", "SHORT"]:
                    start_trade(asset_key, sig)

            time.sleep(CHECK_INTERVAL)

        except Exception as e:
            send(f"{E_WARN} BOT ERROR:\n{e}")
            time.sleep(15)

if __name__ == "__main__":
    run()
