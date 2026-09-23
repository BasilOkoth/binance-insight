from __future__ import annotations
import math
import numpy as np
import pandas as pd
from trading.constants import STRATEGY_VERSION
from trading.models import AppConfig, BacktestRun
from .binance_client import BinanceClient
from .indicators import candles_to_df, enrich
from .scoring import score_latest, is_actionable_setup, regime_score_from_values


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


def run_backtest(symbol="BTCUSDT", interval=None, days=180, initial=10000.0):
    cfg = AppConfig.current()
    interval = interval or cfg.scan_interval
    client = BinanceClient(mode="paper")

    asset_df = enrich(candles_to_df(client.historical_klines(symbol, interval, days=days)))
    btc_df = enrich(candles_to_df(client.historical_klines("BTCUSDT", interval, days=days)))
    df = _attach_btc_regime(asset_df, btc_df)
    if len(df) < 220:
        raise RuntimeError("Not enough historical candles for a meaningful backtest")

    balance = float(initial)
    equity_curve = [balance]
    trades = []
    pos = None
    fee_rate = cfg.fee_bps / 10000
    slip = cfg.slippage_bps / 10000

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
        qv = float(r.quote_volume_24h) if not pd.isna(r.quote_volume_24h) else max(float(r.quote_volume) * 96, 1_000_000)
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
            # Conservative if both stop and target occur in the same candle: stop first.
            if float(nxt.low) <= pos["stop"]:
                exit_px = pos["stop"] * (1 - slip)
                reason = "stop"
            elif float(nxt.high) >= pos["target"]:
                exit_px = pos["target"] * (1 - slip)
                reason = "target"
            elif i - pos["open_i"] >= 96:
                exit_px = float(nxt.close) * (1 - slip)
                reason = "time"

            if exit_px is not None:
                exit_notional = pos["qty"] * exit_px
                exit_fee = exit_notional * fee_rate
                gross = (exit_px - pos["entry"]) * pos["qty"]
                # Entry fee was already removed from balance when position opened.
                balance += gross - exit_fee
                pnl = gross - pos["entry_fee"] - exit_fee
                trades.append(
                    {
                        "pnl": pnl,
                        "ret": pnl / (pos["entry"] * pos["qty"]) * 100,
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
        trades.append(
            {
                "pnl": pnl,
                "ret": pnl / (pos["entry"] * pos["qty"]) * 100,
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
            "trades": trades[-100:],
            "initial": initial,
            "final": balance,
            "btc_regime_aligned": True,
            "fees_bps": cfg.fee_bps,
            "slippage_bps": cfg.slippage_bps,
        },
    )
    return run
