import os
import time
import requests
import pandas as pd
import yfinance as yf

# =========================
# ENV
# =========================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID") or os.getenv("CHAT_ID")

# =========================
# CONFIG
# =========================
CHECK_INTERVAL = 60
HEARTBEAT_SECONDS = 1800
COOLDOWN_SECONDS = 600

LONG_ALERT_SCORE = 60
SHORT_ALERT_SCORE = 60
A_SETUP_SCORE = 70
FULL_SIZE_SCORE = 75

ATR_SL_MULT = 1.5
ATR_TP_MULT = 3.0
ATR_TRAIL_MULT = 1.5
BREAK_EVEN_ATR_TRIGGER = 1.0
PARTIAL_ATR_TRIGGER = 1.5

MIN_VOLATILITY_PCT = 0.001
MAX_EMA9_DISTANCE_PCT = 0.01

MAX_SCALE_INS = 2
SCALE_IN_ATR_STEP = 0.5
SCALE_IN_COOLDOWN_SECONDS = 120

ASSETS = {
    "BTC": {
        "ticker": "BTC-USD",
        "name": "BTC",
    },
    "GOLD": {
        "ticker": "XAUUSD=X",
        "name": "GOLD",
    },
}

# =========================
# STATE
# =========================
STATE = {
    key: {
        "IN_TRADE": False,
        "TRADE_SIDE": None,
        "ENTRY_PRICE": 0.0,
        "AVG_ENTRY_PRICE": 0.0,
        "STOP_LOSS": 0.0,
        "TAKE_PROFIT": 0.0,
        "PARTIAL_SENT": False,
        "BREAK_EVEN_ACTIVE": False,
        "LAST_HEARTBEAT_TS": 0.0,
        "HIGHEST_PRICE": 0.0,
        "LOWEST_PRICE": 0.0,
        "LAST_TRADE_TIME": 0.0,
        "LAST_SCALE_TIME": 0.0,
        "SCALE_COUNT": 0,
        "ENTRY_TYPE": None,
        "CONFIDENCE_LABEL": None,
    }
    for key in ASSETS
}

# =========================
# TELEGRAM
# =========================
def send(msg: str):
    try:
        if not TELEGRAM_TOKEN or not CHAT_ID:
            print(msg)
            return
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": CHAT_ID, "text": msg}, timeout=10)
    except Exception as e:
        print("Telegram error:", e)

# =========================
# DATA
# =========================
def get_klines(asset_key: str, interval: str) -> pd.DataFrame:
    try:
        ticker = ASSETS[asset_key]["ticker"]
        period = "7d" if interval == "1m" else "60d"

        df = yf.download(
            ticker,
            period=period,
            interval=interval,
            progress=False,
            auto_adjust=False,
        )

        # GOLD fallback: if 1m fails, use 5m
        if (df is None or df.empty) and asset_key == "GOLD" and interval == "1m":
            df = yf.download(
                ticker,
                period="7d",
                interval="5m",
                progress=False,
                auto_adjust=False,
            )

        if df is None or df.empty:
            return pd.DataFrame()

        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        df = df.rename(columns={
            "Open": "open",
            "High": "high",
            "Low": "low",
            "Close": "close",
            "Volume": "volume",
        })

        needed = ["open", "high", "low", "close", "volume"]

        for col in needed:
            if col not in df.columns:
                return pd.DataFrame()
            df[col] = pd.to_numeric(df[col], errors="coerce")

        df = df[needed].copy()
        df.dropna(inplace=True)

        if len(df) < 30:
            return pd.DataFrame()

        return df

    except Exception:
        return pd.DataFrame()

# =========================
# INDICATORS
# =========================
def ema(df: pd.DataFrame, span: int) -> pd.Series:
    return df["close"].ewm(span=span, adjust=False).mean()

def rsi(df: pd.DataFrame, period: int = 14) -> pd.Series:
    delta = df["close"].diff()
    gain = delta.where(delta > 0, 0.0).rolling(period).mean()
    loss = (-delta.where(delta < 0, 0.0)).rolling(period).mean()
    rs = gain / loss.replace(0, pd.NA)
    return 100 - (100 / (1 + rs))

def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - df["close"].shift()).abs(),
        (df["low"] - df["close"].shift()).abs()
    ], axis=1).max(axis=1)
    return tr.rolling(period).mean()

def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty or len(df) < 30:
        return pd.DataFrame()

    out = df.copy()
    out["ema9"] = ema(out, 9)
    out["ema21"] = ema(out, 21)
    out["ema50"] = ema(out, 50)
    out["rsi"] = rsi(out, 14)
    out["atr"] = atr(out, 14)
    out["vol_ma"] = out["volume"].rolling(20).mean()
    out["hh10"] = out["high"].rolling(10).max().shift(1)
    out["ll10"] = out["low"].rolling(10).min().shift(1)
    out.dropna(inplace=True)

    if len(out) < 25:
        return pd.DataFrame()

    return out

# =========================
# MARKET FILTERS
# =========================
def market_trend(df1: pd.DataFrame, df5: pd.DataFrame) -> str:
    r1 = df1.iloc[-1]
    r5 = df5.iloc[-1]

    if r5["ema9"] > r5["ema21"] and r1["ema9"] > r1["ema21"]:
        return "BULLISH"
    if r5["ema9"] < r5["ema21"] and r1["ema9"] < r1["ema21"]:
        return "BEARISH"
    return "CHOPPY"

def has_enough_volatility(price: float, atr_now: float) -> bool:
    return (atr_now / max(price, 1.0)) >= MIN_VOLATILITY_PCT

def not_too_extended(price: float, ema9_value: float) -> bool:
    distance = abs(price - ema9_value) / max(price, 1.0)
    return distance <= MAX_EMA9_DISTANCE_PCT

# =========================
# SCORING
# =========================
def long_score(df1: pd.DataFrame, df5: pd.DataFrame):
    r1 = df1.iloc[-1]
    p1 = df1.iloc[-2]
    r5 = df5.iloc[-1]

    score = 0
    reasons = []

    if r5["ema9"] > r5["ema21"] > r5["ema50"]:
        score += 25
        reasons.append("5m strong uptrend")
    elif r5["ema9"] > r5["ema21"]:
        score += 15
        reasons.append("5m bullish bias")

    if r1["ema9"] > r1["ema21"] > r1["ema50"]:
        score += 20
        reasons.append("1m aligned uptrend")
    elif r1["ema9"] > r1["ema21"]:
        score += 10
        reasons.append("1m bullish bias")

    if 50 <= r1["rsi"] <= 72:
        score += 15
        reasons.append("healthy RSI")
    elif 45 <= r1["rsi"] <= 78:
        score += 8
        reasons.append("acceptable RSI")

    if r1["volume"] > r1["vol_ma"] * 1.5:
        score += 15
        reasons.append("strong volume")
    elif r1["volume"] > r1["vol_ma"] * 1.15:
        score += 8
        reasons.append("volume confirm")

    if r1["close"] > p1["close"]:
        score += 10
        reasons.append("bullish candle")

    if r1["close"] > r1["ema9"]:
        score += 10
        reasons.append("holding EMA9")

    if r1["close"] > r1["hh10"] * 1.001:
        score += 5
        reasons.append("micro breakout")

    return int(min(score, 100)), reasons

def short_score(df1: pd.DataFrame, df5: pd.DataFrame):
    r1 = df1.iloc[-1]
    p1 = df1.iloc[-2]
    r5 = df5.iloc[-1]

    score = 0
    reasons = []

    if r5["ema9"] < r5["ema21"] < r5["ema50"]:
        score += 25
        reasons.append("5m strong downtrend")
    elif r5["ema9"] < r5["ema21"]:
        score += 15
        reasons.append("5m bearish bias")

    if r1["ema9"] < r1["ema21"] < r1["ema50"]:
        score += 20
        reasons.append("1m aligned downtrend")
    elif r1["ema9"] < r1["ema21"]:
        score += 10
        reasons.append("1m bearish bias")

    if 28 <= r1["rsi"] <= 50:
        score += 15
        reasons.append("healthy short RSI")
    elif 22 <= r1["rsi"] <= 55:
        score += 8
        reasons.append("acceptable short RSI")

    if r1["volume"] > r1["vol_ma"] * 1.5:
        score += 15
        reasons.append("strong volume")
    elif r1["volume"] > r1["vol_ma"] * 1.15:
        score += 8
        reasons.append("volume confirm")

    if r1["close"] < p1["close"]:
        score += 10
        reasons.append("bearish candle")

    if r1["close"] < r1["ema9"]:
        score += 10
        reasons.append("below EMA9")

    if r1["close"] < r1["ll10"] * 0.999:
        score += 5
        reasons.append("micro breakdown")

    return int(min(score, 100)), reasons

# =========================
# CONFIDENCE GRADING
# =========================
def confidence_grade(score: int) -> str:
    if score >= 100:
        return "S"
    if score >= 90:
        return "A+"
    if score >= 80:
        return "A"
    if score >= 70:
        return "B+"
    if score >= 60:
        return "B"
    return "C"

# =========================
# ENTRY LOGIC
# =========================
def breakout_long(df1: pd.DataFrame) -> bool:
    r = df1.iloc[-1]
    p = df1.iloc[-2]

    clean_break = r["close"] > r["hh10"] * 1.001
    strong_close = r["close"] > p["close"]
    volume_ok = r["volume"] > r["vol_ma"]
    holding_ema = r["close"] > r["ema9"]
    not_stretched = (r["close"] - r["ema9"]) / max(r["ema9"], 1.0) < 0.01

    return bool(clean_break and strong_close and volume_ok and holding_ema and not_stretched)

def breakout_short(df1: pd.DataFrame) -> bool:
    r = df1.iloc[-1]
    p = df1.iloc[-2]

    clean_break = r["close"] < r["ll10"] * 0.999
    strong_close = r["close"] < p["close"]
    volume_ok = r["volume"] > r["vol_ma"]
    below_ema = r["close"] < r["ema9"]
    not_stretched = (r["ema9"] - r["close"]) / max(r["ema9"], 1.0) < 0.01

    return bool(clean_break and strong_close and volume_ok and below_ema and not_stretched)

def sniper_long(df1: pd.DataFrame) -> bool:
    r = df1.iloc[-1]
    p = df1.iloc[-2]
    p2 = df1.iloc[-3]

    ema_reclaim = p["close"] < p["ema9"] and r["close"] > r["ema9"]
    rsi_reclaim = p["rsi"] < 45 and r["rsi"] > 50
    higher_low = r["low"] > p2["low"]

    return bool(ema_reclaim and rsi_reclaim and higher_low)

def sniper_short(df1: pd.DataFrame) -> bool:
    r = df1.iloc[-1]
    p = df1.iloc[-2]
    p2 = df1.iloc[-3]

    ema_reject = p["close"] > p["ema9"] and r["close"] < r["ema9"]
    rsi_reject = p["rsi"] > 55 and r["rsi"] < 50
    lower_high = r["high"] < p2["high"]

    return bool(ema_reject and rsi_reject and lower_high)

# =========================
# CONFIRMATION LOGIC
# =========================
def confirm_long(df1: pd.DataFrame) -> bool:
    r = df1.iloc[-1]
    p = df1.iloc[-2]

    strong_close = r["close"] > p["high"]
    ema_hold = r["close"] > r["ema9"]

    return bool(strong_close and ema_hold)

def confirm_short(df1: pd.DataFrame) -> bool:
    r = df1.iloc[-1]
    p = df1.iloc[-2]

    strong_close = r["close"] < p["low"]
    ema_hold = r["close"] < r["ema9"]

    return bool(strong_close and ema_hold)

# =========================
# A SETUP LOGIC
# =========================
def a_setup_long(sig: dict) -> bool:
    return sig["long_score"] >= A_SETUP_SCORE and sig["trend"] == "BULLISH" and sig["confirm_long"]

def a_setup_short(sig: dict) -> bool:
    return sig["short_score"] >= A_SETUP_SCORE and sig["trend"] == "BEARISH" and sig["confirm_short"]

# =========================
# HEARTBEAT
# =========================
def maybe_send_heartbeat(asset_key: str, df1: pd.DataFrame, df5: pd.DataFrame):
    now = time.time()
    state = STATE[asset_key]

    if now - state["LAST_HEARTBEAT_TS"] < HEARTBEAT_SECONDS:
        return

    ls, _ = long_score(df1, df5)
    ss, _ = short_score(df1, df5)
    r1 = df1.iloc[-1]
    name = ASSETS[asset_key]["name"]

    send(
        f"💓 {name} HEARTBEAT\n\n"
        f"Price: ${float(r1['close']):.2f}\n"
        f"RSI: {float(r1['rsi']):.1f}\n"
        f"Trend: {market_trend(df1, df5)}\n"
        f"Long score: {ls}\n"
        f"Short score: {ss}\n"
        f"In trade: {'YES' if state['IN_TRADE'] else 'NO'}"
    )
    state["LAST_HEARTBEAT_TS"] = now

# =========================
# SIGNAL ENGINE
# =========================
def get_signal(asset_key: str):
    df1_raw = get_klines(asset_key, "1m")
    df5_raw = get_klines(asset_key, "5m")

    df1 = add_indicators(df1_raw)
    df5 = add_indicators(df5_raw)

    # GOLD FLEX FIX
    if asset_key == "GOLD":
        if df1.empty and df5.empty:
            return None
        if df1.empty:
            df1 = df5.copy()
        if df5.empty:
            df5 = df1.copy()
    else:
        if df1.empty or df5.empty:
            return None

    if len(df1) < 25 or len(df5) < 25:
        return None

    price = float(df1.iloc[-1]["close"])
    atr_now = float(df1.iloc[-1]["atr"])

    if not has_enough_volatility(price, atr_now):
        return None

    ls, lr = long_score(df1, df5)
    ss, sr = short_score(df1, df5)

    return {
        "price": price,
        "atr": atr_now,
        "df1": df1,
        "df5": df5,
        "trend": market_trend(df1, df5),
        "long_score": ls,
        "short_score": ss,
        "long_reasons": lr,
        "short_reasons": sr,
        "long_breakout": breakout_long(df1),
        "short_breakout": breakout_short(df1),
        "long_sniper": sniper_long(df1),
        "short_sniper": sniper_short(df1),
        "confirm_long": confirm_long(df1),
        "confirm_short": confirm_short(df1),
    }

# =========================
# TRADE HELPERS
# =========================
def entry_size_label(score: int) -> str:
    return "FULL" if score >= FULL_SIZE_SCORE else "SNIPER"

def reset_trade(asset_key: str):
    state = STATE[asset_key]
    state["IN_TRADE"] = False
    state["TRADE_SIDE"] = None
    state["ENTRY_PRICE"] = 0.0
    state["AVG_ENTRY_PRICE"] = 0.0
    state["STOP_LOSS"] = 0.0
    state["TAKE_PROFIT"] = 0.0
    state["PARTIAL_SENT"] = False
    state["BREAK_EVEN_ACTIVE"] = False
    state["HIGHEST_PRICE"] = 0.0
    state["LOWEST_PRICE"] = 0.0
    state["LAST_TRADE_TIME"] = time.time()
    state["LAST_SCALE_TIME"] = 0.0
    state["SCALE_COUNT"] = 0
    state["ENTRY_TYPE"] = None
    state["CONFIDENCE_LABEL"] = None

def start_trade(asset_key: str, side: str, trigger: str, score: int, reasons: list, price: float, atr_now: float):
    state = STATE[asset_key]
    name = ASSETS[asset_key]["name"]

    state["ENTRY_PRICE"] = price
    state["AVG_ENTRY_PRICE"] = price
    state["HIGHEST_PRICE"] = price
    state["LOWEST_PRICE"] = price
    state["TRADE_SIDE"] = side
    state["IN_TRADE"] = True
    state["SCALE_COUNT"] = 1
    state["LAST_SCALE_TIME"] = time.time()
    state["ENTRY_TYPE"] = trigger
    state["CONFIDENCE_LABEL"] = confidence_grade(score)

    if side == "LONG":
        state["STOP_LOSS"] = price - (atr_now * ATR_SL_MULT)
        state["TAKE_PROFIT"] = price + (atr_now * ATR_TP_MULT)
        emoji = "🚀"
    else:
        state["STOP_LOSS"] = price + (atr_now * ATR_SL_MULT)
        state["TAKE_PROFIT"] = price - (atr_now * ATR_TP_MULT)
        emoji = "📉"

    size = entry_size_label(score)

    send(
        f"{emoji} {name} {side} ENTRY\n\n"
        f"Trigger: {trigger}\n"
        f"Size: {size}\n"
        f"Confidence: {state['CONFIDENCE_LABEL']}\n"
        f"Scale: 1/{MAX_SCALE_INS}\n"
        f"Price: ${price:.2f}\n"
        f"Score: {score}\n"
        f"Reasons: {', '.join(reasons[:4])}\n\n"
        f"SL: ${state['STOP_LOSS']:.2f}\n"
        f"TP: ${state['TAKE_PROFIT']:.2f}"
    )

def can_scale_in(asset_key: str) -> bool:
    state = STATE[asset_key]
    if not state["IN_TRADE"]:
        return False
    if state["SCALE_COUNT"] >= MAX_SCALE_INS:
        return False
    if time.time() - state["LAST_SCALE_TIME"] < SCALE_IN_COOLDOWN_SECONDS:
        return False
    return True

def maybe_scale_in(asset_key: str, sig: dict):
    state = STATE[asset_key]
    name = ASSETS[asset_key]["name"]

    if not can_scale_in(asset_key):
        return

    price = sig["price"]
    atr_now = sig["atr"]

    if state["TRADE_SIDE"] == "LONG":
        favorable_move = price >= state["AVG_ENTRY_PRICE"] + (atr_now * SCALE_IN_ATR_STEP)
        confirmation_ok = sig["confirm_long"]
        score_ok = sig["long_score"] >= LONG_ALERT_SCORE

        if favorable_move and confirmation_ok and score_ok:
            old_avg = state["AVG_ENTRY_PRICE"]
            state["AVG_ENTRY_PRICE"] = (state["AVG_ENTRY_PRICE"] * state["SCALE_COUNT"] + price) / (state["SCALE_COUNT"] + 1)
            state["SCALE_COUNT"] += 1
            state["LAST_SCALE_TIME"] = time.time()
            state["HIGHEST_PRICE"] = max(state["HIGHEST_PRICE"], price)

            send(
                f"➕ {name} LONG SCALE-IN\n\n"
                f"Entry type: {state['ENTRY_TYPE']}\n"
                f"New add price: ${price:.2f}\n"
                f"Old avg: ${old_avg:.2f}\n"
                f"New avg: ${state['AVG_ENTRY_PRICE']:.2f}\n"
                f"Scale: {state['SCALE_COUNT']}/{MAX_SCALE_INS}\n"
                f"Confidence: {state['CONFIDENCE_LABEL']}"
            )

    elif state["TRADE_SIDE"] == "SHORT":
        favorable_move = price <= state["AVG_ENTRY_PRICE"] - (atr_now * SCALE_IN_ATR_STEP)
        confirmation_ok = sig["confirm_short"]
        score_ok = sig["short_score"] >= SHORT_ALERT_SCORE

        if favorable_move and confirmation_ok and score_ok:
            old_avg = state["AVG_ENTRY_PRICE"]
            state["AVG_ENTRY_PRICE"] = (state["AVG_ENTRY_PRICE"] * state["SCALE_COUNT"] + price) / (state["SCALE_COUNT"] + 1)
            state["SCALE_COUNT"] += 1
            state["LAST_SCALE_TIME"] = time.time()
            state["LOWEST_PRICE"] = min(state["LOWEST_PRICE"], price)

            send(
                f"➕ {name} SHORT SCALE-IN\n\n"
                f"Entry type: {state['ENTRY_TYPE']}\n"
                f"New add price: ${price:.2f}\n"
                f"Old avg: ${old_avg:.2f}\n"
                f"New avg: ${state['AVG_ENTRY_PRICE']:.2f}\n"
                f"Scale: {state['SCALE_COUNT']}/{MAX_SCALE_INS}\n"
                f"Confidence: {state['CONFIDENCE_LABEL']}"
            )

# =========================
# TRADE MANAGEMENT
# =========================
def manage_trade(asset_key: str, sig: dict):
    state = STATE[asset_key]
    name = ASSETS[asset_key]["name"]

    price = sig["price"]
    atr_now = sig["atr"]
    entry_ref = state["AVG_ENTRY_PRICE"] if state["AVG_ENTRY_PRICE"] > 0 else state["ENTRY_PRICE"]

    maybe_scale_in(asset_key, sig)

    if state["TRADE_SIDE"] == "LONG":
        state["HIGHEST_PRICE"] = max(state["HIGHEST_PRICE"], price)

        if (not state["BREAK_EVEN_ACTIVE"]) and price >= entry_ref + (atr_now * BREAK_EVEN_ATR_TRIGGER):
            state["STOP_LOSS"] = max(state["STOP_LOSS"], entry_ref)
            state["BREAK_EVEN_ACTIVE"] = True
            send(f"⚡ {name} LONG BREAK-EVEN\nNew SL: ${state['STOP_LOSS']:.2f}")

        if (not state["PARTIAL_SENT"]) and price >= entry_ref + (atr_now * PARTIAL_ATR_TRIGGER):
            state["PARTIAL_SENT"] = True
            send(f"💰 {name} LONG PARTIAL PROFIT ZONE\nPrice: ${price:.2f}")

        if state["BREAK_EVEN_ACTIVE"] and price > entry_ref + atr_now:
            new_sl = state["HIGHEST_PRICE"] - (atr_now * ATR_TRAIL_MULT)
            if new_sl > state["STOP_LOSS"]:
                state["STOP_LOSS"] = new_sl
                send(f"📈 {name} LONG TRAILING STOP\nNew SL: ${state['STOP_LOSS']:.2f}")

        if price <= state["STOP_LOSS"]:
            send(f"❌ {name} LONG STOP HIT\nExit: ${price:.2f}")
            reset_trade(asset_key)
            return

        if price >= state["TAKE_PROFIT"]:
            send(f"🎯 {name} LONG TARGET HIT\nExit: ${price:.2f}")
            reset_trade(asset_key)
            return

    elif state["TRADE_SIDE"] == "SHORT":
        state["LOWEST_PRICE"] = min(state["LOWEST_PRICE"], price)

        if (not state["BREAK_EVEN_ACTIVE"]) and price <= entry_ref - (atr_now * BREAK_EVEN_ATR_TRIGGER):
            state["STOP_LOSS"] = min(state["STOP_LOSS"], entry_ref)
            state["BREAK_EVEN_ACTIVE"] = True
            send(f"⚡ {name} SHORT BREAK-EVEN\nNew SL: ${state['STOP_LOSS']:.2f}")

        if (not state["PARTIAL_SENT"]) and price <= entry_ref - (atr_now * PARTIAL_ATR_TRIGGER):
            state["PARTIAL_SENT"] = True
            send(f"💰 {name} SHORT PARTIAL PROFIT ZONE\nPrice: ${price:.2f}")

        if state["BREAK_EVEN_ACTIVE"] and price < entry_ref - atr_now:
            new_sl = state["LOWEST_PRICE"] + (atr_now * ATR_TRAIL_MULT)
            if new_sl < state["STOP_LOSS"]:
                state["STOP_LOSS"] = new_sl
                send(f"📉 {name} SHORT TRAILING STOP\nNew SL: ${state['STOP_LOSS']:.2f}")

        if price >= state["STOP_LOSS"]:
            send(f"❌ {name} SHORT STOP HIT\nExit: ${price:.2f}")
            reset_trade(asset_key)
            return

        if price <= state["TAKE_PROFIT"]:
            send(f"🎯 {name} SHORT TARGET HIT\nExit: ${price:.2f}")
            reset_trade(asset_key)
            return

# =========================
# MAIN LOOP
# =========================
def run():
    send("🔥 BTC + GOLD ELITE SCALING BOT LIVE 🔥")

    while True:
        try:
            for asset_key in ASSETS:
                state = STATE[asset_key]

                sig = get_signal(asset_key)

                if sig is None:
                    continue

                maybe_send_heartbeat(asset_key, sig["df1"], sig["df5"])

                if not state["IN_TRADE"]:
                    if time.time() - state["LAST_TRADE_TIME"] < COOLDOWN_SECONDS:
                        continue

                    if sig["trend"] == "CHOPPY":
                        continue

                    price = sig["price"]
                    ema9_value = float(sig["df1"].iloc[-1]["ema9"])

                    if not not_too_extended(price, ema9_value):
                        continue

                    # A SETUP
                    if a_setup_long(sig):
                        start_trade(
                            asset_key=asset_key,
                            side="LONG",
                            trigger="A SETUP",
                            score=sig["long_score"],
                            reasons=sig["long_reasons"],
                            price=price,
                            atr_now=sig["atr"],
                        )

                    elif a_setup_short(sig):
                        start_trade(
                            asset_key=asset_key,
                            side="SHORT",
                            trigger="A SETUP",
                            score=sig["short_score"],
                            reasons=sig["short_reasons"],
                            price=price,
                            atr_now=sig["atr"],
                        )

                    # BREAKOUT
                    elif sig["long_score"] >= LONG_ALERT_SCORE and sig["long_breakout"] and sig["confirm_long"]:
                        start_trade(
                            asset_key=asset_key,
                            side="LONG",
                            trigger="BREAKOUT",
                            score=sig["long_score"],
                            reasons=sig["long_reasons"],
                            price=price,
                            atr_now=sig["atr"],
                        )

                    elif sig["short_score"] >= SHORT_ALERT_SCORE and sig["short_breakout"] and sig["confirm_short"]:
                        start_trade(
                            asset_key=asset_key,
                            side="SHORT",
                            trigger="BREAKOUT",
                            score=sig["short_score"],
                            reasons=sig["short_reasons"],
                            price=price,
                            atr_now=sig["atr"],
                        )

                    # SNIPER
                    elif sig["long_score"] >= LONG_ALERT_SCORE and sig["long_sniper"] and sig["confirm_long"]:
                        start_trade(
                            asset_key=asset_key,
                            side="LONG",
                            trigger="SNIPER",
                            score=sig["long_score"],
                            reasons=sig["long_reasons"],
                            price=price,
                            atr_now=sig["atr"],
                        )

                    elif sig["short_score"] >= SHORT_ALERT_SCORE and sig["short_sniper"] and sig["confirm_short"]:
                        start_trade(
                            asset_key=asset_key,
                            side="SHORT",
                            trigger="SNIPER",
                            score=sig["short_score"],
                            reasons=sig["short_reasons"],
                            price=price,
                            atr_now=sig["atr"],
                        )

                else:
                    manage_trade(asset_key, sig)

            time.sleep(CHECK_INTERVAL)

        except Exception as e:
            send(f"BOT ERROR: {e}")
            time.sleep(15)

if __name__ == "__main__":
    run()
