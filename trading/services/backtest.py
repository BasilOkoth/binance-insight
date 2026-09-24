from __future__ import annotations
import math
import os
import time
from pathlib import Path

import numpy as np
import pandas as pd

from trading.constants import STRATEGY_VERSION, BACKTEST_ENGINE_VERSION
from trading.models import AppConfig, BacktestRun
from .binance_client import BinanceClient
from .indicators import candles_to_df, enrich, bars_per_24h
from .scoring import score_latest, is_actionable_setup, regime_score_from_values

SUPPORTED_MATRIX_INTERVALS = ("15m", "30m", "1h", "4h")
INTERVAL_MINUTES = {"15m": 15, "30m": 30, "1h": 60, "4h": 240}
RESAMPLE_RULES = {"15m": "15min", "30m": "30min", "1h": "1h", "4h": "4h"}
CACHE_TTL_SECONDS = 30 * 60
CACHE_DIR = Path(os.getenv("BINANCE_BACKTEST_CACHE_DIR", "/tmp/binance-insight-backtest-cache"))


def _max_drawdown(equity_curve):
    peak = -1e99
    maxdd = 0.0
    for x in equity_curve:
        peak = max(peak, x)
        if peak > 0:
            maxdd = max(maxdd, (peak - x) / peak * 100)
    return maxdd


def _attach_btc_regime(asset_df: pd.DataFrame, btc_df: pd.DataFrame) -> pd.DataFrame:
    btc = btc_df[["open_time", "close", "ema20", "ema50", "ema200", "rsi14"]].copy()
    btc = btc.rename(
        columns={
            "close": "btc_close",
            "ema20": "btc_ema20",
            "ema50": "btc_ema50",
            "ema200": "btc_ema200",
            "rsi14": "btc_rsi14",
        }
    )
    return pd.merge_asof(
        asset_df.sort_values("open_time"),
        btc.sort_values("open_time"),
        on="open_time",
        direction="backward",
    )


def _cache_path(symbol: str, days: int) -> Path:
    safe_symbol = "".join(ch for ch in symbol.upper() if ch.isalnum())
    return CACHE_DIR / f"{safe_symbol}_15m_{int(days)}d.pkl"


def _cached_raw_15m(client: BinanceClient, symbol: str, days: int) -> pd.DataFrame:
    """Cache 15m historical candles briefly on the Render instance.

    A full matrix repeatedly needs the same 180-day source data. The cache
    avoids downloading the same history again for each timeframe and, where
    the web process is reused, for repeat runs during the next 30 minutes.
    """
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = _cache_path(symbol, days)
    try:
        if path.exists() and (time.time() - path.stat().st_mtime) < CACHE_TTL_SECONDS:
            cached = pd.read_pickle(path)
            if isinstance(cached, pd.DataFrame) and len(cached) >= 220:
                return cached
    except Exception:
        pass

    rows = client.historical_klines(symbol, "15m", days=days, max_candles=20_000)
    raw = candles_to_df(rows)
    try:
        raw.to_pickle(path)
    except Exception:
        pass
    return raw


def _resample_candles(raw: pd.DataFrame, interval: str) -> pd.DataFrame:
    if interval not in RESAMPLE_RULES:
        raise ValueError(f"Unsupported matrix timeframe: {interval}")
    if interval == "15m":
        return raw.copy()

    src = raw.sort_values("open_time").set_index("open_time")
    rule = RESAMPLE_RULES[interval]
    out = src.resample(rule, origin="epoch", label="left", closed="left").agg(
        {
            "open": "first",
            "high": "max",
            "low": "min",
            "close": "last",
            "volume": "sum",
            "quote_volume": "sum",
            "close_time": "max",
        }
    )
    out = out.dropna(subset=["open", "high", "low", "close"]).reset_index()
    return out


def _run_backtest_frames(
    symbol: str,
    interval: str,
    asset_df: pd.DataFrame,
    btc_df: pd.DataFrame,
    *,
    days: int,
    initial: float,
    cfg: AppConfig,
) -> BacktestRun:
    df = _attach_btc_regime(asset_df, btc_df)
    if len(df) < 220:
        raise RuntimeError("Not enough historical candles for a meaningful backtest")

    balance = float(initial)
    equity_curve = [balance]
    trades = []
    pos = None
    fee_rate = cfg.fee_bps / 10000
    slip = cfg.slippage_bps / 10000
    interval_minutes = INTERVAL_MINUTES.get(interval, 15)
    max_hold_bars = max(1, int(round((24 * 60) / interval_minutes)))
    inferred_bars_per_day = bars_per_24h(asset_df)

    for i in range(210, len(df) - 1):
        r = df.iloc[i]
        nxt = df.iloc[i + 1]
        if any(pd.isna(r[c]) for c in ["btc_close", "btc_ema20", "btc_ema50", "btc_ema200", "btc_rsi14"]):
            continue

        regime = regime_score_from_values(
            r.btc_close,
            r.btc_ema20,
            r.btc_ema50,
            r.btc_ema200,
            r.btc_rsi14,
        )
        qv = (
            float(r.quote_volume_24h)
            if not pd.isna(r.quote_volume_24h)
            else max(float(r.quote_volume) * inferred_bars_per_day, 1_000_000)
        )
        spread_bps = 5.0
        scored = score_latest(
            r,
            qv,
            spread_bps,
            regime,
            cfg.stop_atr_multiple,
            cfg.target_r_multiple,
        )

        if pos is None and is_actionable_setup(r, scored, regime, spread_bps, cfg.signal_threshold):
            entry = float(nxt.open) * (1 + slip)
            if scored.stop_price < entry:
                risk_per_unit = max(entry - scored.stop_price, entry * 0.002)
                risk_amount = balance * cfg.risk_per_trade_pct / 100
                qty_by_risk = risk_amount / risk_per_unit
                qty_by_asset = (balance * cfg.max_asset_exposure_pct / 100) / entry
                qty = max(0.0, min(qty_by_risk, qty_by_asset))
                notional = qty * entry
                if notional >= 5:
                    entry_fee = notional * fee_rate
                    balance -= entry_fee
                    actual_stop = float(scored.stop_price)
                    actual_target = entry + cfg.target_r_multiple * (entry - actual_stop)
                    pos = {
                        "entry": entry,
                        "qty": qty,
                        "stop": actual_stop,
                        "target": actual_target,
                        "entry_fee": entry_fee,
                        "open_i": i,
                    }

        if pos is not None:
            exit_px = None
            reason = None
            # Conservative if stop and target are both touched in one candle.
            if float(nxt.low) <= pos["stop"]:
                exit_px = pos["stop"] * (1 - slip)
                reason = "stop"
            elif float(nxt.high) >= pos["target"]:
                exit_px = pos["target"] * (1 - slip)
                reason = "target"
            elif i - pos["open_i"] >= max_hold_bars:
                exit_px = float(nxt.close) * (1 - slip)
                reason = "time"

            if exit_px is not None:
                exit_notional = pos["qty"] * exit_px
                exit_fee = exit_notional * fee_rate
                gross = (exit_px - pos["entry"]) * pos["qty"]
                # Entry fee was already removed from balance when position opened.
                balance += gross - exit_fee
                pnl = gross - pos["entry_fee"] - exit_fee
                planned_stop_fill = pos["stop"] * (1 - slip)
                planned_stop_fee = planned_stop_fill * pos["qty"] * fee_rate
                initial_risk_dollars = (
                    (pos["entry"] - planned_stop_fill) * pos["qty"]
                    + pos["entry_fee"]
                    + planned_stop_fee
                )
                trades.append(
                    {
                        "pnl": pnl,
                        "ret": pnl / (pos["entry"] * pos["qty"]) * 100,
                        "r": pnl / initial_risk_dollars if initial_risk_dollars > 0 else 0.0,
                        "reason": reason,
                    }
                )
                pos = None

        if pos is None:
            marked_equity = balance
        else:
            gross_unrealized = (float(nxt.close) - pos["entry"]) * pos["qty"]
            estimated_exit_fee = pos["qty"] * float(nxt.close) * fee_rate
            marked_equity = balance + gross_unrealized - estimated_exit_fee
        equity_curve.append(marked_equity)

    if pos is not None:
        final_close = float(df.iloc[-1].close) * (1 - slip)
        exit_fee = pos["qty"] * final_close * fee_rate
        gross = (final_close - pos["entry"]) * pos["qty"]
        balance += gross - exit_fee
        pnl = gross - pos["entry_fee"] - exit_fee
        planned_stop_fill = pos["stop"] * (1 - slip)
        planned_stop_fee = planned_stop_fill * pos["qty"] * fee_rate
        initial_risk_dollars = (
            (pos["entry"] - planned_stop_fill) * pos["qty"]
            + pos["entry_fee"]
            + planned_stop_fee
        )
        trades.append(
            {
                "pnl": pnl,
                "ret": pnl / (pos["entry"] * pos["qty"]) * 100,
                "r": pnl / initial_risk_dollars if initial_risk_dollars > 0 else 0.0,
                "reason": "end",
            }
        )
        equity_curve.append(balance)

    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] < 0]
    gross_profit = sum(t["pnl"] for t in wins)
    gross_loss = abs(sum(t["pnl"] for t in losses))
    profit_factor = gross_profit / gross_loss if gross_loss else (999 if gross_profit else 0)
    rets = [t["ret"] for t in trades]
    expectancy = sum(rets) / len(rets) if rets else 0
    r_values = [t["r"] for t in trades]
    expectancy_r = sum(r_values) / len(r_values) if r_values else 0
    sharpe = (
        np.mean(rets) / np.std(rets) * math.sqrt(len(rets))
        if len(rets) > 1 and np.std(rets) > 0
        else 0
    )

    run = BacktestRun.objects.create(
        symbol=symbol,
        timeframe=interval,
        start_date=df.open_time.iloc[0].to_pydatetime(),
        end_date=df.open_time.iloc[-1].to_pydatetime(),
        trades=len(trades),
        win_rate_pct=(len(wins) / len(trades) * 100 if trades else 0),
        net_return_pct=(balance / initial - 1) * 100,
        profit_factor=profit_factor,
        max_drawdown_pct=_max_drawdown(equity_curve),
        expectancy_pct=expectancy,
        sharpe=sharpe,
        results={
            "strategy_version": STRATEGY_VERSION,
            "backtest_engine_version": BACKTEST_ENGINE_VERSION,
            "trades": trades[-100:],
            "initial": initial,
            "final": balance,
            "btc_regime_aligned": True,
            "fees_bps": cfg.fee_bps,
            "slippage_bps": cfg.slippage_bps,
            "lookback_days": days,
            "max_hold_hours": 24,
            "max_hold_bars": max_hold_bars,
            "expectancy_r": expectancy_r,
            "source_interval": "15m",
            "resampled": interval != "15m",
        },
    )
    return run


def run_backtest(symbol="BTCUSDT", interval=None, days=180, initial=10000.0):
    """Run one Strategy v2 backtest using a consistent 15m source dataset."""
    cfg = AppConfig.current()
    interval = interval or cfg.scan_interval
    if interval not in SUPPORTED_MATRIX_INTERVALS:
        raise ValueError(f"Unsupported backtest timeframe: {interval}")

    client = BinanceClient(mode="paper")
    asset_raw = _cached_raw_15m(client, symbol, days)
    btc_raw = asset_raw.copy() if symbol == "BTCUSDT" else _cached_raw_15m(client, "BTCUSDT", days)

    asset_df = enrich(_resample_candles(asset_raw, interval))
    btc_df = enrich(_resample_candles(btc_raw, interval))
    return _run_backtest_frames(
        symbol,
        interval,
        asset_df,
        btc_df,
        days=days,
        initial=initial,
        cfg=cfg,
    )


def run_symbol_matrix(symbol: str, intervals=SUPPORTED_MATRIX_INTERVALS, days=180, initial=10000.0):
    """Run all requested timeframes for one symbol from one downloaded dataset.

    The browser matrix runner calls this once per core symbol. That turns a
    120-combination matrix into 30 sequential HTTP requests rather than 120.
    """
    cfg = AppConfig.current()
    client = BinanceClient(mode="paper")
    asset_raw = _cached_raw_15m(client, symbol, days)
    btc_raw = asset_raw.copy() if symbol == "BTCUSDT" else _cached_raw_15m(client, "BTCUSDT", days)

    runs = []
    errors = []
    for interval in intervals:
        if interval not in SUPPORTED_MATRIX_INTERVALS:
            errors.append({"interval": interval, "error": "unsupported timeframe"})
            continue
        try:
            asset_df = enrich(_resample_candles(asset_raw, interval))
            btc_df = enrich(_resample_candles(btc_raw, interval))
            run = _run_backtest_frames(
                symbol,
                interval,
                asset_df,
                btc_df,
                days=days,
                initial=initial,
                cfg=cfg,
            )
            runs.append(run)
        except Exception as exc:
            errors.append({"interval": interval, "error": str(exc)})
    return runs, errors
