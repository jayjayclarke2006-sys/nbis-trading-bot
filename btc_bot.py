import os
import time
import requests
import pandas as pd

# =========================
# CONFIG
# =========================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

SYMBOL = "BTC-USD"
CHECK_INTERVAL = 60  # seconds

IN_TRADE = False
ENTRY_PRICE = 0
STOP_LOSS = 0
TAKE_PROFIT = 0

# =========================
# TELEGRAM
# =========================
def send_telegram(msg):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    requests.post(url, json={"chat_id": CHAT_ID, "text": msg})

# =========================
# DATA
# =========================
def get_price():
    url = "https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT"
    return float(requests.get(url).json()["price"])

def get_klines():
    url = "https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=1m&limit=100"
    data = requests.get(url).json()
    df = pd.DataFrame(data, columns=[
        "time","open","high","low","close","volume",
        "ct","qav","nt","tbv","tqv","ignore"
    ])
    df["close"] = df["close"].astype(float)
    return df

# =========================
# INDICATORS
# =========================
def calculate_rsi(df, period=14):
    delta = df["close"].diff()
    gain = (delta.where(delta > 0, 0)).rolling(period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(period).mean()
    rs = gain / loss
    return 100 - (100 / (1 + rs))

def calculate_ema(df, span):
    return df["close"].ewm(span=span).mean()

# =========================
# SIGNAL ENGINE (ELITE)
# =========================
def get_signal():
    df = get_klines()

    df["RSI"] = calculate_rsi(df)
    df["EMA9"] = calculate_ema(df, 9)
    df["EMA21"] = calculate_ema(df, 21)

    latest = df.iloc[-1]
    prev = df.iloc[-2]

    price = latest["close"]
    rsi = latest["RSI"]
    ema9 = latest["EMA9"]
    ema21 = latest["EMA21"]

    trend = "BULLISH" if ema9 > ema21 else "BEARISH"

    long_score = 0
    short_score = 0

    # =========================
    # CORE SCORING
    # =========================
    if trend == "BULLISH":
        long_score += 30
    else:
        short_score += 30

    if rsi > 55:
        long_score += 20
    if rsi < 45:
        short_score += 20

    # =========================
    # SNIPER ENTRY LOGIC 🔥
    # =========================
    sniper_long = False
    sniper_short = False

    # EMA CROSS (strong)
    if prev["EMA9"] < prev["EMA21"] and ema9 > ema21:
        long_score += 30
        sniper_long = True

    if prev["EMA9"] > prev["EMA21"] and ema9 < ema21:
        short_score += 30
        sniper_short = True

    # RSI bounce sniper
    if prev["RSI"] < 40 and rsi > 45:
        long_score += 20
        sniper_long = True

    if prev["RSI"] > 60 and rsi < 55:
        short_score += 20
        sniper_short = True

    return price, rsi, trend, long_score, short_score, sniper_long, sniper_short

# =========================
# TRADE MANAGER
# =========================
def run_bot():
    global IN_TRADE, ENTRY_PRICE, STOP_LOSS, TAKE_PROFIT

    send_telegram("🔥 BTC ELITE SNIPER BOT LIVE 🔥")

    while True:
        try:
            price, rsi, trend, long_score, short_score, sniper_long, sniper_short = get_signal()

            # =========================
            # HEARTBEAT
            # =========================
            send_telegram(f"""
💓 BTC HEARTBEAT

Price: {price}
RSI: {round(rsi,2)}
Trend: {trend}

Long score: {long_score}
Short score: {short_score}

Sniper long: {sniper_long}
Sniper short: {sniper_short}

In trade: {IN_TRADE}
""")

            # =========================
            # ENTRY LOGIC (FIXED 🔥)
            # =========================
            if not IN_TRADE:

                if long_score >= 60 or sniper_long:
                    IN_TRADE = True
                    ENTRY_PRICE = price
                    STOP_LOSS = price * 0.995
                    TAKE_PROFIT = price * 1.01

                    send_telegram(f"""
🚀 BTC LONG ENTRY

Price: {price}
Score: {long_score}

SL: {round(STOP_LOSS,2)}
TP: {round(TAKE_PROFIT,2)}

🔥 SNIPER: {sniper_long}
""")

                elif short_score >= 60 or sniper_short:
                    IN_TRADE = True
                    ENTRY_PRICE = price
                    STOP_LOSS = price * 1.005
                    TAKE_PROFIT = price * 0.99

                    send_telegram(f"""
📉 BTC SHORT ENTRY

Price: {price}
Score: {short_score}

SL: {round(STOP_LOSS,2)}
TP: {round(TAKE_PROFIT,2)}

🔥 SNIPER: {sniper_short}
""")

            # =========================
            # TRADE MANAGEMENT
            # =========================
            else:
                # BREAK EVEN
                if price > ENTRY_PRICE * 1.003:
                    STOP_LOSS = ENTRY_PRICE
                    send_telegram("⚡ BREAK EVEN ACTIVATED")

                # TRAILING STOP
                if price > ENTRY_PRICE * 1.006:
                    STOP_LOSS = price * 0.997
                    send_telegram(f"📈 TRAILING STOP: {round(STOP_LOSS,2)}")

                # EXIT CONDITIONS
                if price <= STOP_LOSS:
                    send_telegram(f"❌ STOP LOSS HIT: {price}")
                    IN_TRADE = False

                elif price >= TAKE_PROFIT:
                    send_telegram(f"🎯 TAKE PROFIT HIT: {price}")
                    IN_TRADE = False

            time.sleep(CHECK_INTERVAL)

        except Exception as e:
            send_telegram(f"ERROR: {e}")
            time.sleep(10)

# =========================
# RUN
# =========================
if __name__ == "__main__":
    run_bot()
