from __future__ import annotations
from django.conf import settings
from django.db import transaction
from django.utils import timezone
from datetime import timedelta
from django.db.models import Sum
from trading.constants import STRATEGY_VERSION, MAX_SIGNAL_AGE_MINUTES
from trading.models import AppConfig, MarketSignal, Trade, AuditEvent
from .binance_client import BinanceClient
from .live_gate import evaluate
from .risk import position_size, daily_loss_limit_breached
from .execution_guards import (
    projected_exposure_allowed,
    signal_can_open,
    signal_key,
    strategy_trade_queryset,
)

ACK_TEXT = "I_ACCEPT_LIVE_TRADING_RISK"


def assert_exchange_access():
    if settings.BINANCE_MODE not in {"testnet", "live"}:
        raise RuntimeError("BINANCE_MODE must be testnet or live")
    if not settings.BINANCE_API_KEY:
        raise RuntimeError("Binance API key is missing")
    if settings.BINANCE_KEY_TYPE == "ed25519" and not settings.BINANCE_PRIVATE_KEY_PATH:
        raise RuntimeError("Ed25519 private key path is missing")
    if settings.BINANCE_KEY_TYPE != "ed25519" and not settings.BINANCE_API_SECRET:
        raise RuntimeError("Binance API secret is missing")
    return True


def assert_live_enabled():
    gate = evaluate()
    if not gate.eligible:
        raise RuntimeError("Live gate is locked: " + "; ".join(gate.reasons))
    if not settings.LIVE_TRADING_ENABLED:
        raise RuntimeError("LIVE_TRADING_ENABLED is false")
    if settings.LIVE_TRADING_ACK != ACK_TEXT:
        raise RuntimeError(f"LIVE_TRADING_ACK must equal {ACK_TEXT}")
    assert_exchange_access()
    return gate


def _live_equity_usdt(client: BinanceClient):
    account = client.account()
    balances = {
        b["asset"]: float(b["free"]) + float(b["locked"])
        for b in account.get("balances", [])
        if float(b["free"]) + float(b["locked"]) > 0
    }
    total = balances.get("USDT", 0.0)
    for asset, qty in balances.items():
        if asset == "USDT":
            continue
        try:
            total += qty * client.price(asset + "USDT")
        except Exception:
            pass
    return total, balances


def _realized_today(mode):
    return (
        strategy_trade_queryset(mode)
        .filter(status="closed", closed_at__date=timezone.localdate())
        .aggregate(v=Sum("pnl"))["v"]
        or 0.0
    )


@transaction.atomic
def execute_signal(signal: MarketSignal):
    assert_live_enabled()
    cfg = AppConfig.current()
    mode = settings.BINANCE_MODE
    client = BinanceClient(mode=mode)

    allowed, reason = signal_can_open(mode, signal)
    if not allowed:
        raise RuntimeError(reason)
    if strategy_trade_queryset(mode).filter(status="open", symbol=signal.symbol).exists():
        raise RuntimeError("Position already open for symbol")
    if strategy_trade_queryset(mode).filter(status="open").count() >= cfg.max_open_positions:
        raise RuntimeError("Max open positions reached")

    equity, balances = _live_equity_usdt(client)
    cash = balances.get("USDT", 0.0)
    deployed = max(0.0, equity - cash)
    if daily_loss_limit_breached(_realized_today(mode), equity, cfg.max_daily_loss_pct):
        raise RuntimeError("Daily live loss kill-switch is active")

    current_price = client.price(signal.symbol)
    max_drift = max(float(signal.atr) * 0.75, float(signal.price) * 0.005)
    if abs(current_price - float(signal.price)) > max_drift:
        raise RuntimeError("Market moved too far from the validated signal price")
    if current_price <= signal.stop_price:
        raise RuntimeError("Market is already at or below the signal stop")

    sizing = position_size(
        equity,
        cash,
        current_price,
        signal.stop_price,
        cfg.risk_per_trade_pct,
        cfg.max_asset_exposure_pct,
    )
    if not sizing.allowed:
        raise RuntimeError(sizing.reason)
    if not projected_exposure_allowed(deployed, sizing.notional, equity, cfg.max_total_exposure_pct):
        raise RuntimeError("Projected total exposure would exceed the configured limit")

    rules = client.symbol_rules(client.exchange_info(), signal.symbol)
    symbol_info = rules["symbol"]
    if symbol_info.get("status") != "TRADING":
        raise RuntimeError(f"{signal.symbol} is not currently TRADING")

    lot = rules["filters"].get("LOT_SIZE", {})
    price_filter = rules["filters"].get("PRICE_FILTER", {})
    step = lot.get("stepSize", "0.00000001")
    tick = price_filter.get("tickSize", "0.00000001")
    notional_filter = rules["filters"].get("NOTIONAL") or rules["filters"].get("MIN_NOTIONAL") or {}
    min_notional = float(notional_filter.get("minNotional", 0) or 0)
    max_notional = float(notional_filter.get("maxNotional", 0) or 0)
    if min_notional and sizing.notional < min_notional:
        raise RuntimeError(f"Order notional {sizing.notional:.2f} is below exchange minimum {min_notional:.2f}")
    if max_notional and sizing.notional > max_notional:
        raise RuntimeError(f"Order notional {sizing.notional:.2f} exceeds exchange maximum {max_notional:.2f}")

    buy = client.market_buy_quote(signal.symbol, sizing.notional)
    executed = float(buy.get("executedQty", 0))
    quote = float(buy.get("cummulativeQuoteQty", 0))
    entry = quote / executed if executed else signal.price
    base = rules["symbol"].get("baseAsset", signal.symbol[:-4])
    account = client.account()
    free_base = next((float(b["free"]) for b in account.get("balances", []) if b["asset"] == base), 0.0)
    protective_qty = client.floor_to_step(min(executed, free_base), step)
    raw_stop = min(float(signal.stop_price), entry * 0.998)
    risk_per_unit = entry - raw_stop
    raw_target = entry + cfg.target_r_multiple * risk_per_unit
    target = client.floor_price(raw_target, tick)
    stop = client.floor_price(raw_stop, tick)

    trade = Trade.objects.create(
        mode=mode,
        symbol=signal.symbol,
        entry_price=entry,
        quantity=float(protective_qty),
        stop_price=float(stop),
        target_price=float(target),
        entry_fee=quote * (cfg.fee_bps / 10000),
        signal_score=signal.score,
        risk_amount=sizing.risk_amount,
        order_id=str(buy.get("orderId", "")),
        metadata={
            "strategy_version": STRATEGY_VERSION,
            "signal_id": signal.id,
            "signal_key": signal_key(signal),
            "buy_response": buy,
        },
    )

    try:
        oco = client.place_sell_oco(signal.symbol, protective_qty, target, stop)
        trade.protection_order_id = str(oco.get("orderListId", ""))
        trade.metadata = {**trade.metadata, "oco_response": oco}
        trade.save(update_fields=["protection_order_id", "metadata"])
        AuditEvent.objects.create(
            category="live",
            message=f"Strategy v{STRATEGY_VERSION} {mode} entry protected with OCO: {signal.symbol}",
            data={"trade_id": trade.id},
        )
    except Exception as e:
        AuditEvent.objects.create(
            level="CRITICAL",
            category="live",
            message=f"Protection failed for {signal.symbol}; flattening position",
            data={"error": str(e), "trade_id": trade.id},
        )
        try:
            sell = client.market_sell_qty(signal.symbol, protective_qty)
            sold = float(sell.get("executedQty", 0))
            q = float(sell.get("cummulativeQuoteQty", 0))
            exit_px = q / sold if sold else entry
            trade.exit_price = exit_px
            trade.pnl = (exit_px - entry) * float(protective_qty)
            trade.pnl_pct = (exit_px / entry - 1) * 100 if entry else 0
            trade.exit_reason = "protection_failed_flatten"
            trade.status = "closed"
            trade.closed_at = timezone.now()
            trade.metadata = {**trade.metadata, "flatten_response": sell}
            trade.save()
        finally:
            raise RuntimeError(f"Entry protection failed; position was flattened if possible: {e}")
    return trade


def reconcile_open_trades():
    """Always reconcile all open exchange positions, including older versions."""
    assert_exchange_access()
    cfg = AppConfig.current()
    mode = settings.BINANCE_MODE
    client = BinanceClient(mode=mode)
    updates = []

    for trade in Trade.objects.filter(mode=mode, status="open").order_by("opened_at"):
        if not trade.protection_order_id:
            AuditEvent.objects.create(
                level="CRITICAL",
                category="live",
                message=f"Open {trade.symbol} has no protection order",
                data={"trade_id": trade.id},
            )
            continue
        try:
            order_list = client.query_order_list(trade.protection_order_id)
            legs = []
            for item in order_list.get("orders", []):
                oid = item.get("orderId")
                if oid is not None:
                    legs.append(client.query_order(trade.symbol, oid))

            filled = next(
                (o for o in legs if o.get("status") == "FILLED" and o.get("side") == "SELL"),
                None,
            )
            if filled:
                qty = float(filled.get("executedQty", 0))
                quote = float(filled.get("cummulativeQuoteQty", 0))
                exit_px = quote / qty if qty else trade.target_price
                exit_fee = quote * (cfg.fee_bps / 10000)
                trade.exit_price = exit_px
                trade.exit_fee = exit_fee
                trade.pnl = (exit_px - trade.entry_price) * trade.quantity - trade.entry_fee - exit_fee
                basis = trade.entry_price * trade.quantity + trade.entry_fee
                trade.pnl_pct = trade.pnl / basis * 100 if basis else 0
                trade.exit_reason = "exchange_oco"
                trade.status = "closed"
                trade.closed_at = timezone.now()
                trade.metadata = {**trade.metadata, "final_order_list": order_list, "final_exit_order": filled}
                trade.save()
                updates.append(trade)
                continue

            statuses = {o.get("status") for o in legs}
            terminal_without_fill = bool(statuses) and statuses.issubset({"CANCELED", "EXPIRED", "REJECTED"})
            if terminal_without_fill:
                rules = client.symbol_rules(client.exchange_info(), trade.symbol)
                step = rules["filters"].get("LOT_SIZE", {}).get("stepSize", "0.00000001")
                base = rules["symbol"].get("baseAsset")
                account = client.account()
                free_base = next((float(b["free"]) for b in account.get("balances", []) if b["asset"] == base), 0.0)
                qty = client.floor_to_step(min(trade.quantity, free_base), step)
                if float(qty) > 0:
                    sell = client.market_sell_qty(trade.symbol, qty)
                    sold = float(sell.get("executedQty", 0))
                    quote = float(sell.get("cummulativeQuoteQty", 0))
                    exit_px = quote / sold if sold else client.price(trade.symbol)
                    exit_fee = quote * (cfg.fee_bps / 10000)
                    trade.exit_price = exit_px
                    trade.exit_fee = exit_fee
                    trade.pnl = (exit_px - trade.entry_price) * float(qty) - trade.entry_fee - exit_fee
                    trade.pnl_pct = (
                        trade.pnl / (trade.entry_price * float(qty) + trade.entry_fee) * 100 if qty else 0
                    )
                    trade.exit_reason = "protection_lost_flatten"
                    trade.status = "closed"
                    trade.closed_at = timezone.now()
                    trade.metadata = {
                        **trade.metadata,
                        "lost_protection_order_list": order_list,
                        "flatten_response": sell,
                    }
                    trade.save()
                    updates.append(trade)
                    AuditEvent.objects.create(
                        level="CRITICAL",
                        category="live",
                        message=f"Protection disappeared for {trade.symbol}; position flattened",
                        data={"trade_id": trade.id},
                    )
        except Exception as e:
            AuditEvent.objects.create(
                level="ERROR",
                category="live-reconcile",
                message=f"{trade.symbol}: {e}",
                data={"trade_id": trade.id},
            )
    return updates


def execute_best_once():
    assert_live_enabled()
    cutoff = timezone.now() - timedelta(minutes=MAX_SIGNAL_AGE_MINUTES)
    mode = settings.BINANCE_MODE
    seen = set()
    open_symbols = set(strategy_trade_queryset(mode).filter(status="open").values_list("symbol", flat=True))
    candidates = MarketSignal.objects.filter(
        is_actionable=True,
        observed_at__gte=cutoff,
        data__strategy_version=STRATEGY_VERSION,
    ).order_by("-observed_at", "-score")[:100]

    for candidate in candidates:
        if candidate.symbol in seen or candidate.symbol in open_symbols:
            continue
        seen.add(candidate.symbol)
        allowed, _ = signal_can_open(mode, candidate)
        if not allowed:
            continue
        return execute_signal(candidate)
    return None
