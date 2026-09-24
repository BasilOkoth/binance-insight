from __future__ import annotations

from datetime import timedelta

from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from trading.constants import (
    CANDIDATE_PAPER_VERSION,
    CANDIDATE_PAPER_ACCOUNT_NAME,
    CANDIDATE_PAPER_THRESHOLD,
    CANDIDATE_PAPER_SYMBOLS,
    CANDIDATE_PAPER_INTERVAL,
    CANDIDATE_PAPER_MAX_HOLD_HOURS,
)
from trading.models import AppConfig, PaperAccount, Trade, AuditEvent
from .binance_client import BinanceClient
from .indicators import candles_to_df, enrich
from .scoring import score_latest, is_actionable_setup, regime_score_from_values, confirmed_breakout
from .risk import position_size, daily_loss_limit_breached
from .execution_guards import projected_exposure_allowed
from .paper import _intrabar_exit


def candidate_account() -> PaperAccount:
    cfg = AppConfig.current()
    obj, _ = PaperAccount.objects.get_or_create(
        name=CANDIDATE_PAPER_ACCOUNT_NAME,
        defaults={
            "starting_cash": cfg.paper_starting_cash,
            "cash": cfg.paper_starting_cash,
            "equity": cfg.paper_starting_cash,
            "peak_equity": cfg.paper_starting_cash,
        },
    )
    return obj


def candidate_trades():
    return Trade.objects.filter(
        mode="paper",
        metadata__strategy_version=CANDIDATE_PAPER_VERSION,
    )


def _today_realized():
    today = timezone.localdate()
    return (
        candidate_trades()
        .filter(status="closed", closed_at__date=today)
        .aggregate(v=Sum("pnl"))["v"]
        or 0.0
    )


def mark_candidate_to_market(account: PaperAccount | None = None, client=None):
    account = account or candidate_account()
    client = client or BinanceClient(mode="paper")
    value = 0.0
    for trade in candidate_trades().filter(status="open"):
        try:
            value += float(trade.quantity) * client.price(trade.symbol)
        except Exception:
            value += float(trade.quantity) * float(trade.entry_price)

    account.equity = float(account.cash) + value
    account.peak_equity = max(float(account.peak_equity), float(account.equity))
    if account.peak_equity > 0:
        dd = (float(account.peak_equity) - float(account.equity)) / float(account.peak_equity) * 100
        account.max_drawdown_pct = max(float(account.max_drawdown_pct), dd)
    account.save(update_fields=["equity", "peak_equity", "max_drawdown_pct", "updated_at"])
    return account


def _candidate_scan(client: BinanceClient):
    cfg = AppConfig.current()
    btc_df = enrich(candles_to_df(client.klines("BTCUSDT", CANDIDATE_PAPER_INTERVAL, 250)))
    if len(btc_df) < 210:
        raise RuntimeError("Not enough BTC candles for candidate regime")
    btc_row = btc_df.iloc[-2]
    regime = regime_score_from_values(
        btc_row.close,
        btc_row.ema20,
        btc_row.ema50,
        btc_row.ema200,
        btc_row.rsi14,
    )

    tickers = {row.get("symbol"): row for row in client.ticker_24h()}
    results = []
    for symbol in CANDIDATE_PAPER_SYMBOLS:
        try:
            book = client.book_ticker(symbol)
            bid = float(book["bidPrice"])
            ask = float(book["askPrice"])
            mid = (bid + ask) / 2
            spread_bps = ((ask - bid) / mid * 10000) if mid else 999.0

            ticker = tickers.get(symbol) or {}
            qv = float(ticker.get("quoteVolume") or 0.0)
            df = enrich(candles_to_df(client.klines(symbol, CANDIDATE_PAPER_INTERVAL, 250)))
            if len(df) < 210:
                continue
            row = df.iloc[-2]
            scored = score_latest(
                row,
                qv,
                spread_bps,
                regime,
                cfg.stop_atr_multiple,
                cfg.target_r_multiple,
            )
            actionable = is_actionable_setup(
                row,
                scored,
                regime,
                spread_bps,
                CANDIDATE_PAPER_THRESHOLD,
            )
            results.append(
                {
                    "symbol": symbol,
                    "price": float(row.close),
                    "score": float(scored.score),
                    "stop": float(scored.stop_price),
                    "target": float(scored.target_price),
                    "factors": {k: float(v) for k, v in scored.factors.items()},
                    "regime": float(regime),
                    "rsi": float(row.rsi14),
                    "atr": float(row.atr14),
                    "volume_ratio": float(row.volume_ratio or 0),
                    "spread_bps": float(spread_bps),
                    "quote_volume_24h": qv,
                    "confirmed_breakout": bool(confirmed_breakout(row)),
                    "candle_open_time": row.open_time.isoformat(),
                    "candle_close_time": row.close_time.isoformat(),
                    "rationale": list(scored.rationale),
                    "warnings": list(scored.warnings),
                    "actionable": bool(actionable),
                }
            )
        except Exception as exc:
            AuditEvent.objects.create(
                level="WARN",
                category="candidate-paper",
                message=f"Candidate scan {symbol}: {exc}",
            )
    results.sort(key=lambda row: row["score"], reverse=True)
    return results


def _signal_already_used(signal: dict) -> bool:
    return candidate_trades().filter(
        symbol=signal["symbol"],
        metadata__signal_candle_close_time=signal["candle_close_time"],
    ).exists()


@transaction.atomic
def _maybe_open(signal: dict, client: BinanceClient):
    if not signal.get("actionable") or float(signal.get("score", 0)) < CANDIDATE_PAPER_THRESHOLD:
        return None
    if _signal_already_used(signal):
        return None

    cfg = AppConfig.current()
    account = candidate_account()
    mark_candidate_to_market(account, client)
    open_qs = candidate_trades().filter(status="open")
    if open_qs.filter(symbol=signal["symbol"]).exists():
        return None
    if open_qs.count() >= min(int(cfg.max_open_positions), len(CANDIDATE_PAPER_SYMBOLS)):
        return None
    if daily_loss_limit_breached(_today_realized(), account.starting_cash, cfg.max_daily_loss_pct):
        AuditEvent.objects.create(
            level="WARN",
            category="candidate-paper-risk",
            message="Candidate paper daily loss limit reached; no new positions",
        )
        return None

    try:
        current_price = client.price(signal["symbol"])
    except Exception:
        current_price = float(signal["price"])
    max_drift = max(float(signal["atr"]) * 0.75, float(signal["price"]) * 0.005)
    if abs(current_price - float(signal["price"])) > max_drift:
        return None
    if current_price <= float(signal["stop"]):
        return None

    slip = float(cfg.slippage_bps) / 10000.0
    fee_rate = float(cfg.fee_bps) / 10000.0
    entry = current_price * (1 + slip)
    stop = float(signal["stop"])
    if stop >= entry:
        return None

    sizing = position_size(
        account.equity,
        account.cash,
        entry,
        stop,
        cfg.risk_per_trade_pct,
        cfg.max_asset_exposure_pct,
    )
    if not sizing.allowed:
        return None

    current_exposure = max(float(account.equity) - float(account.cash), 0.0)
    if not projected_exposure_allowed(
        current_exposure,
        sizing.notional,
        account.equity,
        cfg.max_total_exposure_pct,
    ):
        return None

    risk_per_unit = entry - stop
    target = entry + float(cfg.target_r_multiple) * risk_per_unit
    notional = float(sizing.quantity) * entry
    entry_fee = notional * fee_rate
    if notional + entry_fee > float(account.cash):
        return None

    expected_stop_fill = stop * (1 - slip)
    expected_stop_exit_fee = float(sizing.quantity) * expected_stop_fill * fee_rate
    initial_risk = (
        (entry - expected_stop_fill) * float(sizing.quantity)
        + entry_fee
        + expected_stop_exit_fee
    )

    account.cash -= notional + entry_fee
    account.save(update_fields=["cash", "updated_at"])
    now_ms = int(timezone.now().timestamp() * 1000)

    trade = Trade.objects.create(
        mode="paper",
        symbol=signal["symbol"],
        entry_price=entry,
        quantity=sizing.quantity,
        stop_price=stop,
        target_price=target,
        entry_fee=entry_fee,
        signal_score=float(signal["score"]),
        risk_amount=float(sizing.risk_amount),
        metadata={
            "strategy_version": CANDIDATE_PAPER_VERSION,
            "experiment": "candidate-paper",
            "research_origin": "candidate-validation-v2.3",
            "timeframe": CANDIDATE_PAPER_INTERVAL,
            "threshold": float(CANDIDATE_PAPER_THRESHOLD),
            "signal_candle_open_time": signal["candle_open_time"],
            "signal_candle_close_time": signal["candle_close_time"],
            "btc_regime_score": float(signal["regime"]),
            "initial_risk_dollars": float(initial_risk),
            "risk_budget_dollars": float(sizing.risk_amount),
            "entry_fee_rate": fee_rate,
            "slippage_rate": slip,
            "expected_stop_fill": expected_stop_fill,
            "expected_stop_exit_fee": expected_stop_exit_fee,
            "trend_score": signal["factors"].get("trend", 0.0),
            "momentum_score": signal["factors"].get("momentum", 0.0),
            "volume_score": signal["factors"].get("volume", 0.0),
            "breakout_score": signal["factors"].get("breakout", 0.0),
            "volatility_score": signal["factors"].get("volatility", 0.0),
            "liquidity_score": signal["factors"].get("liquidity", 0.0),
            "rsi": float(signal["rsi"]),
            "volume_ratio": float(signal["volume_ratio"]),
            "spread_bps": float(signal["spread_bps"]),
            "signal_rationale": "; ".join(signal["rationale"]),
            "signal_warnings": "; ".join(signal["warnings"]),
            "last_exit_check_ms": now_ms,
            "max_hold_hours": CANDIDATE_PAPER_MAX_HOLD_HOURS,
        },
    )
    AuditEvent.objects.create(
        category="candidate-paper",
        message=(
            f"Candidate {CANDIDATE_PAPER_VERSION} opened {trade.symbol} "
            f"{CANDIDATE_PAPER_INTERVAL} score {trade.signal_score:.0f}"
        ),
        data={"trade_id": trade.id, "entry": entry, "qty": float(sizing.quantity)},
    )
    return trade


@transaction.atomic
def update_candidate_positions():
    cfg = AppConfig.current()
    account = candidate_account()
    client = BinanceClient(mode="paper")
    closed = []

    for trade in candidate_trades().select_for_update().filter(status="open"):
        now = timezone.now()
        reason = None
        trigger = None
        try:
            reason, trigger, now_ms = _intrabar_exit(client, trade)
        except Exception as exc:
            now_ms = int(now.timestamp() * 1000)
            AuditEvent.objects.create(
                level="WARN",
                category="candidate-paper",
                message=f"Candidate intrabar exit check failed {trade.symbol}: {exc}",
            )

        if not reason and now - trade.opened_at >= timedelta(hours=CANDIDATE_PAPER_MAX_HOLD_HOURS):
            try:
                trigger = client.price(trade.symbol)
                reason = "time"
            except Exception:
                trigger = None

        if not reason or trigger is None:
            md = dict(trade.metadata or {})
            md["last_exit_check_ms"] = now_ms
            trade.metadata = md
            trade.save(update_fields=["metadata"])
            continue

        slip = float(cfg.slippage_bps) / 10000.0
        fee_rate = float(cfg.fee_bps) / 10000.0
        exit_px = float(trigger) * (1 - slip)
        proceeds = float(trade.quantity) * exit_px
        exit_fee = proceeds * fee_rate
        account.cash += proceeds - exit_fee

        pnl = (exit_px - float(trade.entry_price)) * float(trade.quantity) - float(trade.entry_fee) - exit_fee
        basis = float(trade.entry_price) * float(trade.quantity) + float(trade.entry_fee)
        trade.exit_price = exit_px
        trade.exit_fee = exit_fee
        trade.pnl = pnl
        trade.pnl_pct = pnl / basis * 100 if basis else 0.0
        trade.exit_reason = reason
        trade.status = "closed"
        trade.closed_at = now
        md = dict(trade.metadata or {})
        md["last_exit_check_ms"] = now_ms
        md["exit_trigger_level"] = float(trigger)
        trade.metadata = md
        trade.save()
        closed.append(trade)

        AuditEvent.objects.create(
            category="candidate-paper",
            message=(
                f"Closed candidate {trade.symbol} at {reason} · "
                f"P/L ${pnl:.2f} ({trade.pnl_pct:.2f}%)"
            ),
            data={"trade_id": trade.id, "reason": reason, "pnl": pnl},
        )

    account.save(update_fields=["cash", "updated_at"])
    mark_candidate_to_market(account, client)
    return closed


def candidate_cycle():
    closed = update_candidate_positions()
    client = BinanceClient(mode="paper")
    signals = _candidate_scan(client)
    opened = []
    for signal in signals:
        if signal.get("actionable"):
            trade = _maybe_open(signal, client)
            if trade:
                opened.append(trade)

    mark_candidate_to_market(candidate_account(), client)
    AuditEvent.objects.create(
        category="candidate-paper",
        message=(
            f"Candidate cycle complete: {len(signals)} symbols, "
            f"{len(opened)} opened, {len(closed)} closed"
        ),
        data={
            "strategy_version": CANDIDATE_PAPER_VERSION,
            "threshold": CANDIDATE_PAPER_THRESHOLD,
            "interval": CANDIDATE_PAPER_INTERVAL,
            "symbols": list(CANDIDATE_PAPER_SYMBOLS),
        },
    )
    return {"signals": signals, "opened": opened, "closed": closed}
