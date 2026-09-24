from __future__ import annotations
import numpy as np
import pandas as pd


def candles_to_df(rows) -> pd.DataFrame:
    cols = [
        "open_time", "open", "high", "low", "close", "volume", "close_time",
        "quote_volume", "trades", "taker_base", "taker_quote", "ignore",
    ]
    df = pd.DataFrame(rows, columns=cols)
    for c in ["open", "high", "low", "close", "volume", "quote_volume"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    df["close_time"] = pd.to_datetime(df["close_time"], unit="ms", utc=True)
    return df


def ema(s: pd.Series, span: int) -> pd.Series:
    return s.ewm(span=span, adjust=False).mean()


def rsi(s: pd.Series, period: int = 14) -> pd.Series:
    delta = s.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False).mean()
    rs = gain / loss.replace(0, np.nan)
    return (100 - 100 / (1 + rs)).fillna(50)


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    prev = df["close"].shift(1)
    tr = pd.concat(
        [
            (df["high"] - df["low"]).abs(),
            (df["high"] - prev).abs(),
            (df["low"] - prev).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False).mean()


def bars_per_24h(df: pd.DataFrame) -> int:
    """Infer bars per 24 hours from candle timestamps.

    This keeps rolling 24h liquidity calculations correct on 15m, 30m, 1h,
    4h and any other regularly spaced timeframe instead of assuming 96 bars.
    """
    if len(df) < 2 or "open_time" not in df.columns:
        return 96
    diffs = df["open_time"].sort_values().diff().dropna().dt.total_seconds()
    if diffs.empty:
        return 96
    seconds = float(diffs.median())
    if not np.isfinite(seconds) or seconds <= 0:
        return 96
    return max(1, int(round(86_400 / seconds)))


def enrich(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out["ema20"] = ema(out.close, 20)
    out["ema50"] = ema(out.close, 50)
    out["ema200"] = ema(out.close, 200)
    out["rsi14"] = rsi(out.close, 14)
    out["atr14"] = atr(out, 14)
    out["roc12"] = out.close.pct_change(12) * 100
    out["vol_ma20"] = out.volume.rolling(20).mean()
    out["volume_ratio"] = out.volume / out.vol_ma20.replace(0, np.nan)
    out["high20_prev"] = out.high.shift(1).rolling(20).max()
    out["low20_prev"] = out.low.shift(1).rolling(20).min()
    out["ema50_slope"] = out.ema50.pct_change(5) * 100
    out["return_1"] = out.close.pct_change() * 100

    bars = bars_per_24h(out)
    min_periods = max(2, min(bars, int(round(bars * 0.25))))
    out["quote_volume_24h"] = out.quote_volume.rolling(bars, min_periods=min_periods).sum()
    return out


def clamp(x, lo=0.0, hi=100.0):
    return float(max(lo, min(hi, float(x))))


def linear(x, bad, good):
    if pd.isna(x):
        return 50.0
    if good == bad:
        return 50.0
    return clamp((float(x) - bad) / (good - bad) * 100)


def inverse(x, good, bad):
    return 100.0 - linear(x, good, bad)
