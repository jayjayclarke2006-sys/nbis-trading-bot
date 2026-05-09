import ccxt
import yfinance as yf
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
import requests
import time
import json
import os

from ta.trend import EMAIndicator, ADXIndicator
from ta.momentum import RSIIndicator
from ta.volatility import AverageTrueRange

BOT_TOKEN = "YOUR_BOT_TOKEN"
CHAT_ID = "YOUR_CHAT_ID"

TIMEFRAME = "1h"
HIGHER_TIMEFRAME = "4h"

BTC_LIMIT = 1500
BTC_HTF_LIMIT = 500

GOLD_PERIOD = "730d"
GOLD_HTF_PERIOD = "730d"

FEE = 0.0006
INITIAL_BALANCE = 10_000
RISK_PER_TRADE = 0.01

TRAIN_SIZE = 500
TEST_SIZE = 150
MONTE_CARLO_RUNS = 500

LIVE_SCAN_SECONDS = 300

SENT_SIGNAL_FILE = "sent_signals.json"

EXCHANGES = [
    ("coinbase", "BTC/USD"),
    ("kraken", "BTC/USD"),
    ("bybit", "BTC/USDT"),
    ("binanceus", "BTC/USDT"),
]


def send_telegram(message):
    try:
        if BOT_TOKEN == "YOUR_BOT_TOKEN":
            print("Telegram not configured")
            print(message)
            return

        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"

        requests.post(
            url,
            json={
                "chat_id": CHAT_ID,
                "text": message,
            },
            timeout=10
        )

    except Exception as e:
        print("Telegram error:", e)


def load_json(path, default):
    if not os.path.exists(path):
        return default

    try:
        with open(path, "r") as f:
            return json.load(f)
    except:
        return default


def save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2, default=str)


def get_exchange(name):
    return getattr(ccxt, name)({
        "enableRateLimit": True,
        "timeout": 10000,
    })


def validate_data(df):

    if df is None or len(df) < 300:
        return False, "Not enough candles"

    if df.isna().sum().sum() > 0:
        return False, "NaN values"

    return True, "OK"


def fetch_btc(timeframe=TIMEFRAME, limit=BTC_LIMIT):

    for name, symbol in EXCHANGES:

        try:
            exchange = get_exchange(name)

            exchange_timeframe = timeframe

            if name == "coinbase" and timeframe == "4h":
                exchange_timeframe = "1h"

            candles = exchange.fetch_ohlcv(
                symbol,
                timeframe=exchange_timeframe,
                limit=limit
            )

            df = pd.DataFrame(
                candles,
                columns=[
                    "timestamp",
                    "Open",
                    "High",
                    "Low",
                    "Close",
                    "Volume"
                ]
            )

            df["timestamp"] = pd.to_datetime(
                df["timestamp"],
                unit="ms",
                utc=True
            )

            valid, _ = validate_data(df)

            if valid:
                return df

        except Exception as e:
            print(f"{name} failed:", e)

    raise Exception("No BTC feed available")


def fetch_gold(interval=TIMEFRAME, period=GOLD_PERIOD):

    df = yf.download(
        "GC=F",
        period=period,
        interval=interval,
        auto_adjust=True,
        progress=False
    ).reset_index()

    if "Datetime" in df.columns:
        df = df.rename(columns={"Datetime": "timestamp"})

    elif "Date" in df.columns:
        df = df.rename(columns={"Date": "timestamp"})

    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)

    return df[
        [
            "timestamp",
            "Open",
            "High",
            "Low",
            "Close",
            "Volume"
        ]
    ].dropna()


def add_indicators(df):

    df = df.copy()

    df["ema_20"] = EMAIndicator(df["Close"], 20).ema_indicator()
    df["ema_50"] = EMAIndicator(df["Close"], 50).ema_indicator()
    df["ema_200"] = EMAIndicator(df["Close"], 200).ema_indicator()

    df["rsi"] = RSIIndicator(df["Close"], 14).rsi()

    df["adx"] = ADXIndicator(
        df["High"],
        df["Low"],
        df["Close"],
        14
    ).adx()

    df["atr"] = AverageTrueRange(
        df["High"],
        df["Low"],
        df["Close"],
        14
    ).average_true_range()

    df["avg_volume"] = df["Volume"].rolling(30).mean()

    return df.dropna().reset_index(drop=True)


def trend(row):

    if row["Close"] > row["ema_20"] > row["ema_50"] > row["ema_200"]:
        return "BULLISH"

    if row["Close"] < row["ema_20"] < row["ema_50"] < row["ema_200"]:
        return "BEARISH"

    return "NEUTRAL"


def bullish_pin(row):

    body = abs(row["Close"] - row["Open"])
    rng = row["High"] - row["Low"]

    if rng == 0:
        return False

    lower = min(row["Open"], row["Close"]) - row["Low"]
    upper = row["High"] - max(row["Open"], row["Close"])

    return (
        lower > body * 2.5
        and upper < body * 1.2
        and row["Close"] > row["Open"]
    )


def bearish_pin(row):

    body = abs(row["Close"] - row["Open"])
    rng = row["High"] - row["Low"]

    if rng == 0:
        return False

    upper = row["High"] - max(row["Open"], row["Close"])
    lower = min(row["Open"], row["Close"]) - row["Low"]

    return (
        upper > body * 2.5
        and lower < body * 1.2
        and row["Close"] < row["Open"]
    )


def bullish_engulf(prev, curr):

    return bool(
        prev["Close"] < prev["Open"]
        and curr["Close"] > curr["Open"]
        and curr["Close"] > prev["Open"]
        and curr["Open"] < prev["Close"]
    )


def bearish_engulf(prev, curr):

    return bool(
        prev["Close"] > prev["Open"]
        and curr["Close"] < curr["Open"]
        and curr["Open"] > prev["Close"]
        and curr["Close"] < prev["Open"]
    )


def liquidity_sweep_low(df, i, lookback):

    swing_low = df["Low"].iloc[i - lookback:i].min()

    return bool(
        df.iloc[i]["Low"] < swing_low
        and df.iloc[i]["Close"] > swing_low
    )


def liquidity_sweep_high(df, i, lookback):

    swing_high = df["High"].iloc[i - lookback:i].max()

    return bool(
        df.iloc[i]["High"] > swing_high
        and df.iloc[i]["Close"] < swing_high
    )


def displacement(row, atr_mult):

    body = abs(row["Close"] - row["Open"])
    rng = row["High"] - row["Low"]

    if rng == 0:
        return False

    return bool(
        body / rng > 0.55
        and rng > row["atr"] * atr_mult
    )


def performance(trades, equity):

    if trades.empty or len(equity) < 2:
        return None

    wins = trades[trades["pnl"] > 0]
    losses = trades[trades["pnl"] <= 0]

    win_rate = len(wins) / len(trades)

    profit_factor = (
        wins["pnl"].sum() / abs(losses["pnl"].sum())
        if len(losses)
        else np.inf
    )

    expectancy = trades["pnl"].mean()

    drawdown = equity / equity.cummax() - 1
    max_drawdown = drawdown.min()

    total_return = equity.iloc[-1] / INITIAL_BALANCE - 1

    return {
        "trades": len(trades),
        "win_rate": win_rate,
        "profit_factor": profit_factor,
        "expectancy": expectancy,
        "max_drawdown": max_drawdown,
        "total_return": total_return,
        "final_balance": equity.iloc[-1]
    }


def backtest(df, params):

    balance = INITIAL_BALANCE
    equity_curve = []
    trades = []

    lookback = int(params["sweep_lookback"])

    for i in range(max(220, lookback + 2), len(df) - 2):

        prev = df.iloc[i - 1]
        curr = df.iloc[i]

        market_trend = trend(curr)

        bull_pattern = bool(
            bullish_pin(curr)
            or bullish_engulf(prev, curr)
        )

        bear_pattern = bool(
            bearish_pin(curr)
            or bearish_engulf(prev, curr)
        )

        bull_signal = (
            market_trend == "BULLISH"
            and liquidity_sweep_low(df, i, lookback)
            and curr["rsi"] >= params["rsi_bull"]
            and curr["adx"] >= params["min_adx"]
            and bull_pattern
        )

        bear_signal = (
            market_trend == "BEARISH"
            and liquidity_sweep_high(df, i, lookback)
            and curr["rsi"] <= params["rsi_bear"]
            and curr["adx"] >= params["min_adx"]
            and bear_pattern
        )

        if not bull_signal and not bear_signal:
            equity_curve.append(balance)
            continue

        entry = df.iloc[i + 1]["Open"]
        atr = curr["atr"]

        risk_amount = balance * RISK_PER_TRADE

        if bull_signal:
            stop = entry - atr * params["atr_stop"]
            target = entry + ((entry - stop) * params["rr"])
            risk_per_unit = entry - stop
            direction = "LONG"

        else:
            stop = entry + atr * params["atr_stop"]
            target = entry - ((stop - entry) * params["rr"])
            risk_per_unit = stop - entry
            direction = "SHORT"

        if risk_per_unit <= 0:
            continue

        position_size = risk_amount / risk_per_unit

        exit_price = None

        for j in range(i + 1, min(i + int(params["max_hold"]), len(df))):

            candle = df.iloc[j]

            if direction == "LONG":

                if candle["Low"] <= stop:
                    exit_price = stop
                    break

                if candle["High"] >= target:
                    exit_price = target
                    break

            else:

                if candle["High"] >= stop:
                    exit_price = stop
                    break

                if candle["Low"] <= target:
                    exit_price = target
                    break

        if exit_price is None:
            exit_price = df.iloc[
                min(i + int(params["max_hold"]), len(df) - 1)
            ]["Close"]

        if direction == "LONG":
            pnl = (exit_price - entry) * position_size
        else:
            pnl = (entry - exit_price) * position_size

        pnl -= abs(entry * position_size) * FEE

        balance += pnl

        trades.append({
            "time": curr["timestamp"],
            "direction": direction,
            "pnl": pnl,
            "balance": balance
        })

        equity_curve.append(balance)

    return pd.DataFrame(trades), pd.Series(equity_curve)


def parameter_grid():

    for sweep_lookback in [5, 10, 20]:

        for min_adx in [10, 15, 18]:

            for rsi_bull in [48, 50, 52]:

                for rsi_bear in [52, 50, 48]:

                    for volume_mult in [0.8, 1.0]:

                        for atr_stop in [1.0, 1.2]:

                            for rr in [1.2, 1.5, 2.0]:

                                for displacement_atr in [0.5, 0.8]:

                                    yield {
                                        "sweep_lookback": sweep_lookback,
                                        "min_adx": min_adx,
                                        "rsi_bull": rsi_bull,
                                        "rsi_bear": rsi_bear,
                                        "volume_mult": volume_mult,
                                        "atr_stop": atr_stop,
                                        "rr": rr,
                                        "displacement_atr": displacement_atr,
                                        "max_hold": 48
                                    }


def optimize(train_df):

    results = []

    for params in parameter_grid():

        trades, equity = backtest(train_df, params)

        stats = performance(trades, equity)

        if not stats:
            continue

        if stats["trades"] < 5:
            continue

        score = (
            stats["profit_factor"] * 0.35
            + stats["win_rate"] * 0.25
            + stats["total_return"] * 0.25
        )

        results.append({
            **params,
            **stats,
            "score": score
        })

    if not results:
        return None

    ranked = pd.DataFrame(results).sort_values(
        "score",
        ascending=False
    )

    return ranked.iloc[0].to_dict()


def walk_forward(df):

    reports = []
    all_trades = []

    start = 0

    while start + TRAIN_SIZE + TEST_SIZE < len(df):

        train_df = df.iloc[
            start:start + TRAIN_SIZE
        ].reset_index(drop=True)

        test_df = df.iloc[
            start + TRAIN_SIZE:
            start + TRAIN_SIZE + TEST_SIZE
        ].reset_index(drop=True)

        best = optimize(train_df)

        if best is None:
            start += TEST_SIZE
            continue

        params = {
            "sweep_lookback": best["sweep_lookback"],
            "min_adx": best["min_adx"],
            "rsi_bull": best["rsi_bull"],
            "rsi_bear": best["rsi_bear"],
            "volume_mult": best["volume_mult"],
            "atr_stop": best["atr_stop"],
            "rr": best["rr"],
            "displacement_atr": best["displacement_atr"],
            "max_hold": best["max_hold"]
        }

        test_trades, test_equity = backtest(test_df, params)

        stats = performance(test_trades, test_equity)

        if stats:
            reports.append({
                **params,
                **stats
            })

            all_trades.append(test_trades)

        start += TEST_SIZE

    report_df = pd.DataFrame(reports)

    trades_df = (
        pd.concat(all_trades, ignore_index=True)
        if all_trades
        else pd.DataFrame()
    )

    return report_df, trades_df


def build_live_signal(name, df, htf_df, params):

    try:

        df = add_indicators(df)
        htf_df = add_indicators(htf_df)

        lookback = int(params["sweep_lookback"])

        i = len(df) - 2

        prev = df.iloc[i - 1]
        curr = df.iloc[i]

        htf_curr = htf_df.iloc[-2]

        market_trend = trend(curr)
        htf_trend = trend(htf_curr)

        bull_pattern = bool(
            bullish_pin(curr)
            or bullish_engulf(prev, curr)
            or liquidity_sweep_low(df, i, lookback)
        )

        bear_pattern = bool(
            bearish_pin(curr)
            or bearish_engulf(prev, curr)
            or liquidity_sweep_high(df, i, lookback)
        )

        bullish_bias = (
            market_trend == "BULLISH"
            and htf_trend in ["BULLISH", "NEUTRAL"]
            and curr["rsi"] >= params["rsi_bull"]
            and curr["adx"] >= params["min_adx"]
            and bull_pattern
        )

        bearish_bias = (
            market_trend == "BEARISH"
            and htf_trend in ["BEARISH", "NEUTRAL"]
            and curr["rsi"] <= params["rsi_bear"]
            and curr["adx"] >= params["min_adx"]
            and bear_pattern
        )

        print(
            f"{name} | "
            f"LTF={market_trend} | "
            f"HTF={htf_trend} | "
            f"RSI={curr['rsi']:.2f} | "
            f"ADX={curr['adx']:.2f}"
        )

        if not bullish_bias and not bearish_bias:
            return None

        direction = "LONG" if bullish_bias else "SHORT"

        entry = float(df.iloc[i + 1]["Open"])

        atr = float(curr["atr"])

        if direction == "LONG":

            stop = entry - atr * params["atr_stop"]

            target = entry + ((entry - stop) * params["rr"])

        else:

            stop = entry + atr * params["atr_stop"]

            target = entry - ((stop - entry) * params["rr"])

        return {
            "market": name,
            "direction": direction,
            "entry": round(entry, 2),
            "stop": round(float(stop), 2),
            "target": round(float(target), 2),
            "time": str(curr["timestamp"]),
        }

    except Exception as e:
        print(f"{name} live signal error:", e)
        return None


def format_signal(signal):

    return f"""
ð¨ LIVE TRADE SIGNAL ð¨

Market: {signal['market']}

Direction: {signal['direction']}

Entry: {signal['entry']}

Stop Loss: {signal['stop']}

Take Profit: {signal['target']}

Time: {signal['time']}
"""


def maybe_send_signal(signal):

    sent = load_json(SENT_SIGNAL_FILE, {})

    key = (
        f"{signal['market']}_"
        f"{signal['direction']}_"
        f"{signal['time']}"
    )

    if sent.get(key):
        print("Duplicate signal blocked")
        return

    send_telegram(format_signal(signal))

    sent[key] = True

    save_json(SENT_SIGNAL_FILE, sent)


def run_market(name, df, htf_df):

    print(f"\nRUNNING {name}")

    df_ind = add_indicators(df)

    report, trades = walk_forward(df_ind)

    if report.empty:
        print("No valid walk-forward results")
        return

    best_params = report.iloc[-1].to_dict()

    signal = build_live_signal(
        name,
        df,
        htf_df,
        best_params
    )

    if signal:
        maybe_send_signal(signal)
        print(format_signal(signal))
    else:
        print("No live signal")

    if not trades.empty:

        pnl = trades["pnl"].values
        mc_results = []

        for _ in range(MONTE_CARLO_RUNS):

            sampled = np.random.choice(
                pnl,
                size=len(pnl),
                replace=True
            )

            mc_results.append(
                INITIAL_BALANCE + np.cumsum(sampled)
            )

        plt.figure()

        for line in mc_results[:50]:
            plt.plot(line)

        plt.title(f"{name} Monte Carlo")
        plt.savefig(f"{name}_mc.png")
        plt.close()


if __name__ == "__main__":

    while True:

        try:

            btc = fetch_btc(
                TIMEFRAME,
                BTC_LIMIT
            )

            btc_htf = fetch_btc(
                HIGHER_TIMEFRAME,
                BTC_HTF_LIMIT
            )

            gold = fetch_gold(
                TIMEFRAME,
                GOLD_PERIOD
            )

            gold_htf = fetch_gold(
                HIGHER_TIMEFRAME,
                GOLD_HTF_PERIOD
            )

            run_market(
                "BTC",
                btc,
                btc_htf
            )

            run_market(
                "GOLD",
                gold,
                gold_htf
            )

        except Exception as e:

            print("Bot error:", e)

            send_telegram(
                f"Bot error: {e}"
            )

        time.sleep(LIVE_SCAN_SECONDS)
