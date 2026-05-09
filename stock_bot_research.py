import os
import math
import requests
import numpy as np
import pandas as pd
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

# ============================================================
# NBIS ALPACA STOCK BOT - RESEARCH / WALK-FORWARD / MONTE CARLO
# ============================================================

ALPACA_API_KEY = os.getenv("ALPACA_API_KEY") or os.getenv("APCA_API_KEY_ID") or os.getenv("ALPACA_KEY_ID")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY") or os.getenv("APCA_API_SECRET_KEY") or os.getenv("ALPACA_SECRET")
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
WATCHLIST = ["AAPL", "TSLA", "NVDA", "AMD", "META", "MSFT", "AMZN", "SPY", "QQQ", "NBIS", "WULF", "IREN"]

RR_TARGET = 1.8
ATR_LEN = 14
EMA_FAST = 20
EMA_MID = 50
EMA_SLOW = 200
FEE_BPS = 2
INITIAL_BALANCE = 10000
RISK_PER_TRADE = 0.005

TRAIN_BARS = 260
TEST_BARS = 80
MONTE_CARLO_RUNS = 1000


def send(msg: str):
    print(msg)
    if not TELEGRAM_TOKEN or not CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": msg},
            timeout=10,
        )
    except Exception:
        pass


def now_ny() -> datetime:
    return datetime.now(NY_TZ)


def iso_utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


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


def bars_to_df(symbol: str, timeframe: str = "5Min", limit: int = 2000) -> pd.DataFrame:
    end = now_ny()
    start = end - timedelta(days=45)
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


def candle_quality(row, min_body_atr, max_body_atr) -> bool:
    atr = float(row["atr"])
    if atr <= 0:
        return False
    body_atr = float(row["body"]) / atr
    return min_body_atr <= body_atr <= max_body_atr


def volume_ok(row, min_volume_mult) -> bool:
    if float(row["vol_ma"]) <= 0:
        return True
    return float(row["volume"]) >= float(row["vol_ma"]) * min_volume_mult


def htf_bias_from_row(row, prev) -> str:
    if row["close"] > row["ema50"] > row["ema200"] and row["ema50"] >= prev["ema50"]:
        return "BULL"
    if row["close"] < row["ema50"] < row["ema200"] and row["ema50"] <= prev["ema50"]:
        return "BEAR"
    if row["close"] > row["ema200"]:
        return "BULL_WEAK"
    if row["close"] < row["ema200"]:
        return "BEAR_WEAK"
    return "CHOP"


def bias_allows(side: str, bias: str, allow_shorts: bool) -> bool:
    if side == "LONG":
        return bias in ["BULL", "BULL_WEAK"]
    if side == "SHORT":
        return allow_shorts and bias in ["BEAR", "BEAR_WEAK"]
    return False


def build_intraday_or(df: pd.DataFrame, idx: int):
    current_day = df.iloc[idx]["time"].date()
    day_df = df[df["time"].dt.date == current_day].copy()
    if day_df.empty:
        return None, None, False
    opening = day_df[
        day_df["time"].apply(lambda x: x.hour == 9 and x.minute >= 30 or x.hour == 9 and x.minute < 60)
    ]
    opening = opening[(opening["time"].dt.hour == 9) & (opening["time"].dt.minute >= 30) | ((opening["time"].dt.hour == 10) & (opening["time"].dt.minute < 0))]
    opening = day_df[(day_df["time"].dt.hour == 9) & (day_df["time"].dt.minute >= 30) | ((day_df["time"].dt.hour == 9) & (day_df["time"].dt.minute < 60))]
    opening = day_df[(day_df["time"].dt.hour == 9) & (day_df["time"].dt.minute >= 30)]
    opening = opening[opening["time"].dt.minute < 60]
    opening = opening[opening["time"].dt.hour == 9]
    if len(opening) < 6:
        return None, None, False
    return float(opening["high"].max()), float(opening["low"].min()), True


def backtest_symbol(df5: pd.DataFrame, df15: pd.DataFrame, params: dict):
    balance = INITIAL_BALANCE
    trades = []

    if df5.empty or df15.empty:
        return pd.DataFrame(), pd.Series(dtype=float)

    df5 = add_indicators(df5)
    df15 = add_indicators(df15)
    if df5.empty or df15.empty:
        return pd.DataFrame(), pd.Series(dtype=float)

    df15_indexed = df15.set_index("time")
    equity_curve = []

    last_trade_day = None

    for i in range(1, len(df5) - 12):
        row = df5.iloc[i]
        prev5 = df5.iloc[i - 1]
        ts = row["time"]
        if ts.hour < 9 or ts.hour > 15:
            equity_curve.append(balance)
            continue
        if ts.hour == 15 and ts.minute > 40:
            equity_curve.append(balance)
            continue

        trade_day = ts.date()
        if last_trade_day == trade_day:
            equity_curve.append(balance)
            continue

        # nearest 15m bar
        htf_slice = df15_indexed[df15_indexed.index <= ts]
        if len(htf_slice) < 2:
            equity_curve.append(balance)
            continue
        htf_row = htf_slice.iloc[-1]
        htf_prev = htf_slice.iloc[-2]
        bias = htf_bias_from_row(htf_row, htf_prev)

        if bias == "CHOP":
            equity_curve.append(balance)
            continue

        if float(row["atr_pct"]) < params["min_atr_pct"]:
            equity_curve.append(balance)
            continue
        if not volume_ok(row, params["min_volume_mult"]):
            equity_curve.append(balance)
            continue
        if not candle_quality(row, params["min_body_atr"], params["max_body_atr"]):
            equity_curve.append(balance)
            continue

        or_high, or_low, or_ready = build_intraday_or(df5, i)
        if not or_ready or or_high is None or or_low is None:
            equity_curve.append(balance)
            continue

        signal = None
        close = float(row["close"])
        low = float(row["low"])
        high = float(row["high"])
        open_ = float(row["open"])
        atr = float(row["atr"])

        # OR continuation
        if close > or_high and bias_allows("LONG", bias, params["allow_shorts"]) and close > open_:
            sl = min(low, or_low)
            risk = close - sl
            if risk > 0:
                signal = {"side": "buy", "entry": close, "sl": sl, "tp": close + risk * params["rr_target"], "model": "OR_LONG"}
        elif close < or_low and bias_allows("SHORT", bias, params["allow_shorts"]) and close < open_:
            sl = max(high, or_high)
            risk = sl - close
            if risk > 0:
                signal = {"side": "sell", "entry": close, "sl": sl, "tp": close - risk * params["rr_target"], "model": "OR_SHORT"}

        # Pullback
        if signal is None and bias_allows("LONG", bias, params["allow_shorts"]):
            touched = low <= float(row["ema20"]) + atr * params["pullback_buffer_atr"]
            reclaimed = close > open_ and close > float(row["ema20"])
            if touched and reclaimed and strong_rejection(row, "LONG"):
                sl = min(low, float(row["ema50"]) - atr * 0.10)
                risk = close - sl
                if risk > 0:
                    signal = {"side": "buy", "entry": close, "sl": sl, "tp": close + risk * params["rr_target"], "model": "PB_LONG"}

        if signal is None and bias_allows("SHORT", bias, params["allow_shorts"]):
            touched = high >= float(row["ema20"]) - atr * params["pullback_buffer_atr"]
            rejected = close < open_ and close < float(row["ema20"])
            if touched and rejected and strong_rejection(row, "SHORT"):
                sl = max(high, float(row["ema50"]) + atr * 0.10)
                risk = sl - close
                if risk > 0:
                    signal = {"side": "sell", "entry": close, "sl": sl, "tp": close - risk * params["rr_target"], "model": "PB_SHORT"}

        if signal is None:
            equity_curve.append(balance)
            continue

        risk_per_share = abs(signal["entry"] - signal["sl"])
        if risk_per_share <= 0:
            equity_curve.append(balance)
            continue

        risk_cash = balance * RISK_PER_TRADE
        qty = max(math.floor(risk_cash / risk_per_share), 1)

        exit_price = None
        exit_type = "TIME"
        future = df5.iloc[i + 1:i + 13]

        for _, bar in future.iterrows():
            if signal["side"] == "buy":
                if float(bar["low"]) <= signal["sl"]:
                    exit_price = signal["sl"]
                    exit_type = "STOP"
                    break
                if float(bar["high"]) >= signal["tp"]:
                    exit_price = signal["tp"]
                    exit_type = "TARGET"
                    break
            else:
                if float(bar["high"]) >= signal["sl"]:
                    exit_price = signal["sl"]
                    exit_type = "STOP"
                    break
                if float(bar["low"]) <= signal["tp"]:
                    exit_price = signal["tp"]
                    exit_type = "TARGET"
                    break

        if exit_price is None:
            exit_price = float(future.iloc[-1]["close"]) if len(future) else signal["entry"]

        gross = (exit_price - signal["entry"]) * qty if signal["side"] == "buy" else (signal["entry"] - exit_price) * qty
        fees = qty * signal["entry"] * (FEE_BPS / 10000)
        pnl = gross - fees
        balance += pnl
        last_trade_day = trade_day

        trades.append({
            "time": ts,
            "side": signal["side"],
            "model": signal["model"],
            "entry": signal["entry"],
            "exit": exit_price,
            "pnl": pnl,
            "balance": balance,
            "exit_type": exit_type,
        })
        equity_curve.append(balance)

    return pd.DataFrame(trades), pd.Series(equity_curve)


def performance(trades: pd.DataFrame, equity: pd.Series):
    if trades.empty or equity.empty:
        return None

    wins = trades[trades["pnl"] > 0]
    losses = trades[trades["pnl"] <= 0]

    win_rate = len(wins) / len(trades)
    gross_profit = float(wins["pnl"].sum()) if not wins.empty else 0.0
    gross_loss = abs(float(losses["pnl"].sum())) if not losses.empty else 0.0
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else np.inf
    expectancy = float(trades["pnl"].mean())

    drawdown = equity / equity.cummax() - 1
    max_drawdown = float(drawdown.min()) if len(drawdown) else 0.0
    total_return = float(equity.iloc[-1] / INITIAL_BALANCE - 1)

    return {
        "trades": len(trades),
        "win_rate": win_rate,
        "profit_factor": profit_factor,
        "expectancy": expectancy,
        "max_drawdown": max_drawdown,
        "total_return": total_return,
        "final_balance": float(equity.iloc[-1]),
    }


def parameter_grid():
    for min_atr_pct in [0.0018, 0.0022, 0.0028]:
        for min_volume_mult in [0.70, 0.85, 1.00]:
            for min_body_atr in [0.10, 0.15, 0.20]:
                for max_body_atr in [2.0, 2.8]:
                    for pullback_buffer_atr in [0.30, 0.45]:
                        for rr_target in [1.5, 1.8, 2.0]:
                            yield {
                                "min_atr_pct": min_atr_pct,
                                "min_volume_mult": min_volume_mult,
                                "min_body_atr": min_body_atr,
                                "max_body_atr": max_body_atr,
                                "pullback_buffer_atr": pullback_buffer_atr,
                                "rr_target": rr_target,
                                "allow_shorts": False,
                            }


def optimize(df5: pd.DataFrame, df15: pd.DataFrame):
    rows = []
    for params in parameter_grid():
        trades, equity = backtest_symbol(df5, df15, params)
        stats = performance(trades, equity)
        if not stats or stats["trades"] < 5:
            continue
        score = stats["profit_factor"] * 0.4 + stats["win_rate"] * 0.3 + stats["total_return"] * 0.3
        rows.append({**params, **stats, "score": score})
    if not rows:
        return None
    ranked = pd.DataFrame(rows).sort_values("score", ascending=False)
    return ranked.iloc[0].to_dict()


def walk_forward(df5: pd.DataFrame, df15: pd.DataFrame):
    reports = []
    trades_all = []

    if len(df5) < TRAIN_BARS + TEST_BARS + 20:
        return pd.DataFrame(), pd.DataFrame()

    start = 0
    window = 1

    while start + TRAIN_BARS + TEST_BARS < len(df5):
        train5 = df5.iloc[start:start + TRAIN_BARS].reset_index(drop=True)
        test5 = df5.iloc[start + TRAIN_BARS:start + TRAIN_BARS + TEST_BARS].reset_index(drop=True)

        train_start = train5.iloc[0]["time"]
        train_end = train5.iloc[-1]["time"]
        test_start = test5.iloc[0]["time"]
        test_end = test5.iloc[-1]["time"]

        train15 = df15[(df15["time"] >= train_start) & (df15["time"] <= train_end)].reset_index(drop=True)
        test15 = df15[(df15["time"] >= test_start) & (df15["time"] <= test_end)].reset_index(drop=True)

        best = optimize(train5, train15)
        if best is None:
            start += TEST_BARS
            window += 1
            continue

        params = {
            "min_atr_pct": best["min_atr_pct"],
            "min_volume_mult": best["min_volume_mult"],
            "min_body_atr": best["min_body_atr"],
            "max_body_atr": best["max_body_atr"],
            "pullback_buffer_atr": best["pullback_buffer_atr"],
            "rr_target": best["rr_target"],
            "allow_shorts": bool(best["allow_shorts"]),
        }

        test_trades, test_equity = backtest_symbol(test5, test15, params)
        stats = performance(test_trades, test_equity)

        if stats:
            reports.append({"window": window, **params, **stats})
            test_trades["window"] = window
            trades_all.append(test_trades)

        start += TEST_BARS
        window += 1

    return pd.DataFrame(reports), (pd.concat(trades_all, ignore_index=True) if trades_all else pd.DataFrame())


def monte_carlo(trades: pd.DataFrame):
    if trades.empty:
        return None, None

    pnl = trades["pnl"].values
    results = []

    for _ in range(MONTE_CARLO_RUNS):
        sampled = np.random.choice(pnl, size=len(pnl), replace=True)
        equity = INITIAL_BALANCE + np.cumsum(sampled)
        peak = np.maximum.accumulate(equity)
        dd = equity / peak - 1
        results.append({
            "final_balance": float(equity[-1]),
            "total_return": float(equity[-1] / INITIAL_BALANCE - 1),
            "max_drawdown": float(dd.min()),
        })

    mc = pd.DataFrame(results)
    summary = {
        "median_final_balance": float(mc["final_balance"].median()),
        "worst_5pct_balance": float(mc["final_balance"].quantile(0.05)),
        "median_return": float(mc["total_return"].median()),
        "worst_5pct_return": float(mc["total_return"].quantile(0.05)),
        "median_drawdown": float(mc["max_drawdown"].median()),
        "worst_5pct_drawdown": float(mc["max_drawdown"].quantile(0.05)),
        "risk_of_loss": float((mc["final_balance"] < INITIAL_BALANCE).mean()),
    }
    return mc, summary


def run_symbol(symbol: str):
    print(f"Researching {symbol} ...")
    df5 = bars_to_df(symbol, "5Min", 2000)
    df15 = bars_to_df(symbol, "15Min", 800)
    if df5.empty or df15.empty:
        return {"symbol": symbol, "status": "NO_DATA"}

    report, trades = walk_forward(df5, df15)
    if report.empty or trades.empty:
        return {"symbol": symbol, "status": "NO_ROBUST_RESULT"}

    mc, mc_summary = monte_carlo(trades)
    avg_pf = float(report["profit_factor"].replace(np.inf, np.nan).mean())
    avg_wr = float(report["win_rate"].mean())
    avg_ret = float(report["total_return"].mean())
    total_trades = int(report["trades"].sum())

    return {
        "symbol": symbol,
        "status": "OK",
        "windows": len(report),
        "avg_profit_factor": avg_pf,
        "avg_win_rate": avg_wr,
        "avg_return_per_window": avg_ret,
        "total_trades": total_trades,
        "risk_of_loss": mc_summary["risk_of_loss"] if mc_summary else None,
        "worst_5pct_return": mc_summary["worst_5pct_return"] if mc_summary else None,
    }


def run():
    rows = []
    for sym in WATCHLIST:
        try:
            rows.append(run_symbol(sym))
        except Exception as e:
            rows.append({"symbol": sym, "status": f"ERROR: {e}"})

    out = pd.DataFrame(rows)
    out.to_csv("stock_research_summary.csv", index=False)

    ok = out[out["status"] == "OK"].copy()
    if not ok.empty:
        ok = ok.sort_values(["avg_profit_factor", "avg_win_rate"], ascending=False)
        lines = []
        for _, r in ok.head(8).iterrows():
            lines.append(
                f"{r['symbol']}: PF {r['avg_profit_factor']:.2f}, WR {r['avg_win_rate']:.1%}, "
                f"Trades {int(r['total_trades'])}, RiskLoss {r['risk_of_loss']:.1%}"
            )
        send("STOCK BOT RESEARCH SUMMARY\n\n" + "\n".join(lines))
    else:
        send("STOCK BOT RESEARCH SUMMARY\n\nNo robust results.")

    print(out.to_string(index=False))


if __name__ == "__main__":
    run()
