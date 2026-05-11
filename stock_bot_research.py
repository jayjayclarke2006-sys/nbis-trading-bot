import os
import json
import requests
import numpy as np
import pandas as pd
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

# ============================================================
# STOCK EDGE RESEARCH
# - studies time-of-day + price-zone expectancy
# - produces stock_edge_profile.json
# ============================================================

ALPACA_API_KEY = os.getenv("ALPACA_API_KEY") or os.getenv("APCA_API_KEY_ID") or os.getenv("ALPACA_KEY_ID")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY") or os.getenv("APCA_API_SECRET_KEY") or os.getenv("ALPACA_SECRET")
ALPACA_DATA_BASE = "https://data.alpaca.markets"
ALPACA_DATA_FEED = os.getenv("ALPACA_DATA_FEED", "iex")
OUT_FILE = os.getenv("STOCK_EDGE_PROFILE_FILE", "stock_edge_profile.json")

BOT_TOKEN = os.getenv("TELEGRAM_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID") or os.getenv("TELEGRAM_CHAT_ID")

HEADERS = {
    "APCA-API-KEY-ID": ALPACA_API_KEY or "",
    "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY or "",
    "Content-Type": "application/json",
}

NY_TZ = ZoneInfo("America/New_York")
WATCHLIST = ["AAPL", "TSLA", "NVDA", "AMD", "META", "MSFT", "AMZN", "SPY", "QQQ", "NBIS", "WULF", "IREN"]

ALLOW_SHORTS = False
RR_TARGET = 1.8
ATR_LEN = 14
EMA_FAST = 20
EMA_MID = 50
EMA_SLOW = 200

MIN_ATR_PCT = 0.0022
MIN_VOLUME_MULT = 0.75
MIN_BODY_ATR = 0.12
MAX_BODY_ATR = 2.80
RETEST_BUFFER_ATR = 0.35
PULLBACK_BUFFER_ATR = 0.45

MARKET_OPEN = "09:30"
OPENING_RANGE_END = "10:00"
TRADE_START = "09:45"
LAST_ENTRY_TIME = "15:40"


def send(msg: str):
    print(msg)
    if not BOT_TOKEN or not CHAT_ID:
        return
    try:
        requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage", json={"chat_id": CHAT_ID, "text": msg}, timeout=10)
    except Exception:
        pass


def now_ny() -> datetime:
    return datetime.now(NY_TZ)


def iso_utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def to_minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def minute_of_day(dt: datetime) -> int:
    return dt.hour * 60 + dt.minute


def in_window(dt: datetime, start: str, end: str) -> bool:
    m = minute_of_day(dt)
    return to_minutes(start) <= m <= to_minutes(end)


def alpaca_get(path: str, params=None):
    try:
        r = requests.get(f"{ALPACA_DATA_BASE}{path}", headers=HEADERS, params=params or {}, timeout=20)
        if r.status_code >= 400:
            print("ALPACA GET ERROR:", r.status_code, r.text[:300])
            return None
        return r.json()
    except Exception as e:
        print("ALPACA GET EXCEPTION:", e)
        return None


def bars_to_df(symbol: str, timeframe: str, limit: int = 1200) -> pd.DataFrame:
    end = now_ny()
    start = end - timedelta(days=30)
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

    data = alpaca_get("/v2/stocks/bars", params=params)
    if not data or "bars" not in data:
        return pd.DataFrame()

    rows = data["bars"].get(symbol, [])
    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)
    df.rename(columns={"t": "time", "o": "open", "h": "high", "l": "low", "c": "close", "v": "volume"}, inplace=True)
    df["time"] = pd.to_datetime(df["time"], utc=True).dt.tz_convert(NY_TZ)
    for c in ["open", "high", "low", "close", "volume"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df.dropna(inplace=True)
    return df[["time", "open", "high", "low", "close", "volume"]].reset_index(drop=True)


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


def htf_bias_from_df(df15: pd.DataFrame, ts) -> str:
    sl = df15[df15["time"] <= ts]
    if len(sl) < 2:
        return "NONE"
    r = sl.iloc[-1]
    p = sl.iloc[-2]
    if r["close"] > r["ema50"] > r["ema200"] and r["ema50"] >= p["ema50"]:
        return "BULL"
    if r["close"] < r["ema50"] < r["ema200"] and r["ema50"] <= p["ema50"]:
        return "BEAR"
    if r["close"] > r["ema200"]:
        return "BULL_WEAK"
    if r["close"] < r["ema200"]:
        return "BEAR_WEAK"
    return "CHOP"


def bias_allows(side: str, bias: str) -> bool:
    if side == "LONG":
        return bias in ["BULL", "BULL_WEAK"]
    if side == "SHORT":
        return ALLOW_SHORTS and bias in ["BEAR", "BEAR_WEAK"]
    return False


def build_opening_range(day_df):
    opening = day_df[(day_df["time"].dt.hour == 9) & (day_df["time"].dt.minute >= 30)]
    opening = opening[opening["time"].dt.minute < 60]
    if len(opening) < 6:
        return None, None
    return float(opening["high"].max()), float(opening["low"].min())


def time_bucket(ts):
    ts = pd.Timestamp(ts)
    minute = (ts.minute // 15) * 15
    return f"{ts.hour:02d}:{minute:02d}"


def price_zone(row, or_high, or_low):
    close = float(row["close"])
    ema20 = float(row["ema20"])
    ema50 = float(row["ema50"])
    if or_high is not None and close > or_high:
        return "above_or"
    if or_low is not None and close < or_low:
        return "below_or"
    if close > ema20 > ema50:
        return "above_ema_stack"
    if close < ema20 < ema50:
        return "below_ema_stack"
    return "mixed"


def trade_outcome(df, i, side, entry, stop, target, max_hold=12):
    for j in range(i + 1, min(i + max_hold, len(df))):
        bar = df.iloc[j]
        if side == "LONG":
            if float(bar["low"]) <= stop:
                return -1.0
            if float(bar["high"]) >= target:
                return (target - entry) / max(entry - stop, 1e-9)
        else:
            if float(bar["high"]) >= stop:
                return -1.0
            if float(bar["low"]) <= target:
                return (entry - target) / max(stop - entry, 1e-9)

    last_close = float(df.iloc[min(i + max_hold, len(df) - 1)]["close"])
    if side == "LONG":
        return (last_close - entry) / max(entry - stop, 1e-9)
    return (entry - last_close) / max(stop - entry, 1e-9)


def collect_candidates(symbol: str, df5: pd.DataFrame, df15: pd.DataFrame, i: int, or_high, or_low):
    row = df5.iloc[i]
    if not in_window(row["time"], TRADE_START, LAST_ENTRY_TIME):
        return []

    bias = htf_bias_from_df(df15, row["time"])
    if bias in ["CHOP", "NONE"]:
        return []
    if float(row["atr_pct"]) < MIN_ATR_PCT:
        return []
    if not volume_ok(row) or not candle_quality(row):
        return []

    close = float(row["close"])
    low = float(row["low"])
    high = float(row["high"])
    open_ = float(row["open"])
    atr = float(row["atr"])
    ema20 = float(row["ema20"])
    ema50 = float(row["ema50"])

    out = []

    if or_high is not None and close > or_high and bias_allows("LONG", bias) and close > open_:
        sl = min(low, or_low)
        risk = close - sl
        if risk > 0:
            out.append(("OR_BREAKOUT_CONTINUATION", "LONG", close, sl, close + risk * RR_TARGET))

    if or_low is not None and close < or_low and bias_allows("SHORT", bias) and close < open_:
        sl = max(high, or_high)
        risk = sl - close
        if risk > 0:
            out.append(("OR_BREAKOUT_CONTINUATION", "SHORT", close, sl, close - risk * RR_TARGET))

    recent = df5.iloc[max(0, i - 10):i + 1]
    bull_broke = or_high is not None and (recent["close"] > or_high).any()
    bear_broke = or_low is not None and (recent["close"] < or_low).any()

    if bull_broke and low <= or_high + atr * RETEST_BUFFER_ATR and close > or_high and strong_rejection(row, "LONG") and bias_allows("LONG", bias):
        sl = min(low, or_low)
        risk = close - sl
        if risk > 0:
            out.append(("OR_RETEST_REJECTION", "LONG", close, sl, close + risk * RR_TARGET))

    if bear_broke and high >= or_low - atr * RETEST_BUFFER_ATR and close < or_low and strong_rejection(row, "SHORT") and bias_allows("SHORT", bias):
        sl = max(high, or_high)
        risk = sl - close
        if risk > 0:
            out.append(("OR_RETEST_REJECTION", "SHORT", close, sl, close - risk * RR_TARGET))

    touched_value = low <= ema20 + atr * PULLBACK_BUFFER_ATR or low <= ema50 + atr * 0.20
    reclaimed = close > open_ and close > ema20
    if touched_value and reclaimed and strong_rejection(row, "LONG") and bias_allows("LONG", bias):
        sl = min(low, ema50 - atr * 0.10)
        risk = close - sl
        if risk > 0:
            out.append(("TREND_PULLBACK", "LONG", close, sl, close + risk * RR_TARGET))

    touched_value_s = high >= ema20 - atr * PULLBACK_BUFFER_ATR or high >= ema50 - atr * 0.20
    rejected = close < open_ and close < ema20
    if touched_value_s and rejected and strong_rejection(row, "SHORT") and bias_allows("SHORT", bias):
        sl = max(high, ema50 + atr * 0.10)
        risk = sl - close
        if risk > 0:
            out.append(("TREND_PULLBACK", "SHORT", close, sl, close - risk * RR_TARGET))

    recent8 = df5.iloc[max(0, i - 8):i]
    if len(recent8) >= 6:
        swing_low = float(recent8["low"].min())
        swing_high = float(recent8["high"].max())
        if low < swing_low and close > swing_low and bullish_pin(row):
            sl = low
            risk = close - sl
            if risk > 0:
                out.append(("LIQUIDITY_SWEEP_REVERSAL", "LONG", close, sl, close + risk * RR_TARGET))
        if ALLOW_SHORTS and high > swing_high and close < swing_high and bearish_pin(row):
            sl = high
            risk = sl - close
            if risk > 0:
                out.append(("LIQUIDITY_SWEEP_REVERSAL", "SHORT", close, sl, close - risk * RR_TARGET))

    recent20 = df5.iloc[max(0, i - 20):i]
    if len(recent20) >= 10:
        range_high = float(recent20["high"].max())
        range_low = float(recent20["low"].min())
        if low <= range_low and close > range_low and bullish_pin(row):
            sl = low
            risk = close - sl
            if risk > 0:
                out.append(("RANGE_REJECTION", "LONG", close, sl, close + risk * RR_TARGET))
        if ALLOW_SHORTS and high >= range_high and close < range_high and bearish_pin(row):
            sl = high
            risk = sl - close
            if risk > 0:
                out.append(("RANGE_REJECTION", "SHORT", close, sl, close - risk * RR_TARGET))

    recent12 = df5.iloc[max(0, i - 12):i]
    if len(recent12) >= 8:
        width = float(recent12["high"].max() - recent12["low"].min())
        if width <= atr * 2.4:
            hi = float(recent12["high"].max())
            lo = float(recent12["low"].min())
            if close > hi and bias_allows("LONG", bias):
                sl = low
                risk = close - sl
                if risk > 0:
                    out.append(("COMPRESSION_BREAKOUT", "LONG", close, sl, close + risk * RR_TARGET))
            if ALLOW_SHORTS and close < lo and bias_allows("SHORT", bias):
                sl = high
                risk = sl - close
                if risk > 0:
                    out.append(("COMPRESSION_BREAKOUT", "SHORT", close, sl, close - risk * RR_TARGET))

    if i >= 1:
        prev = df5.iloc[i - 1]
        if prev["close"] < prev["ema20"] and close > ema20 and close > open_ and bias_allows("LONG", bias):
            sl = min(low, ema20)
            risk = close - sl
            if risk > 0:
                out.append(("EMA_RECLAIM", "LONG", close, sl, close + risk * RR_TARGET))
        if ALLOW_SHORTS and prev["close"] > prev["ema20"] and close < ema20 and close < open_ and bias_allows("SHORT", bias):
            sl = max(high, ema20)
            risk = sl - close
            if risk > 0:
                out.append(("EMA_RECLAIM", "SHORT", close, sl, close - risk * RR_TARGET))

    return out


def run():
    bucket_stats = {}

    for symbol in WATCHLIST:
        print(f"Researching {symbol} ...")
        df5 = add_indicators(bars_to_df(symbol, "5Min", 1500))
        df15 = add_indicators(bars_to_df(symbol, "15Min", 600))

        if df5.empty or df15.empty:
            continue

        for day, day_df in df5.groupby(df5["time"].dt.date):
            if len(day_df) < 30:
                continue

            or_high, or_low = build_opening_range(day_df)
            if or_high is None:
                continue

            idxs = day_df.index.tolist()
            for i in idxs:
                row = df5.loc[i]
                if not in_window(row["time"], TRADE_START, LAST_ENTRY_TIME):
                    continue

                candidates = collect_candidates(symbol, df5, df15, i, or_high, or_low)
                if not candidates:
                    continue

                bucket = time_bucket(row["time"])
                zone = price_zone(row, or_high, or_low)

                for model, direction, entry, stop, target in candidates:
                    r_mult = trade_outcome(df5, i, direction, entry, stop, target, max_hold=12)
                    keys = [
                        f"{symbol}|{model}|{direction}|{bucket}|{zone}",
                        f"{symbol}|{model}|{direction}|{bucket}|ALL",
                        f"ALL|{model}|{direction}|{bucket}|ALL",
                        f"ALL|{model}|{direction}|ALL|ALL",
                    ]
                    for k in keys:
                        bucket_stats.setdefault(k, []).append(float(r_mult))

    profile = {"updated_at": pd.Timestamp.utcnow().isoformat(), "buckets": {}}
    for k, vals in bucket_stats.items():
        arr = np.array(vals, dtype=float)
        if len(arr) < 6:
            continue
        profile["buckets"][k] = {
            "trades": int(len(arr)),
            "expectancy_r": float(arr.mean()),
            "win_rate": float((arr > 0).mean()),
            "median_r": float(np.median(arr)),
        }

    with open(OUT_FILE, "w") as f:
        json.dump(profile, f, indent=2)

    lines = []
    model_groups = {}
    for k, info in profile["buckets"].items():
        sym, model, direction, bucket, zone = k.split("|")
        if bucket == "ALL":
            model_groups.setdefault((sym, model, direction), []).append(info)

    for key, infos in model_groups.items():
        exp = np.mean([x["expectancy_r"] for x in infos])
        wr = np.mean([x["win_rate"] for x in infos])
        n = int(sum([x["trades"] for x in infos]))
        sym, model, direction = key
        lines.append(f"{sym} {model} {direction}: expR {exp:.2f}, WR {wr:.1%}, n {n}")

    send("STOCK EDGE RESEARCH COMPLETE\n\n" + ("\n".join(lines[:15]) if lines else "No robust buckets found."))


if __name__ == "__main__":
    run()
