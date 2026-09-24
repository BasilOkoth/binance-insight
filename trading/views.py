from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.http import JsonResponse
from django.shortcuts import render, redirect
from django.db.models import Sum
from django.utils import timezone
from trading.constants import STRATEGY_VERSION
from .models import AppConfig, MarketSignal, PaperAccount, Trade, BacktestRun, LiveGate, AuditEvent
from .forms import AppConfigForm
from .services.live_gate import evaluate
from .services.backtest import run_backtest
from .services.scanner import scan_market
from .services.paper import paper_cycle, mark_to_market


def _paper_trades():
    return Trade.objects.filter(mode="paper", metadata__strategy_version=STRATEGY_VERSION)


def _format_hold_duration(start, end):
    if not start:
        return "—"
    end = end or timezone.now()
    seconds = max(int((end - start).total_seconds()), 0)
    if seconds < 60:
        return "<1m"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {minutes}m" if minutes else f"{hours}h"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h" if hours else f"{days}d"




def _actual_initial_risk_dollars(trade, cfg):
    """Return the position's planned stop loss in dollars (1R).

    For new trades this value is frozen in metadata at entry. For older
    Strategy v2 trades created before that field existed, reconstruct it from
    the immutable trade prices/quantity/entry fee plus the current configured
    stop slippage. The entry fee rate can be inferred from the trade itself,
    so the fallback remains stable even if fee settings later change.
    """
    metadata = dict(trade.metadata or {})
    frozen = metadata.get("initial_risk_dollars")
    if frozen is not None:
        try:
            frozen = float(frozen)
            if frozen > 0:
                return frozen
        except (TypeError, ValueError):
            pass

    entry = float(trade.entry_price or 0.0)
    stop = float(trade.stop_price or 0.0)
    qty = float(trade.quantity or 0.0)
    entry_fee = float(trade.entry_fee or 0.0)
    if entry <= 0 or stop <= 0 or qty <= 0 or stop >= entry:
        return 0.0

    notional = entry * qty
    inferred_fee_rate = (entry_fee / notional) if notional > 0 and entry_fee > 0 else (float(cfg.fee_bps) / 10000.0)
    fee_rate = float(metadata.get("entry_fee_rate", inferred_fee_rate))
    slippage = float(metadata.get("slippage_rate", float(cfg.slippage_bps) / 10000.0))
    stop_fill = float(metadata.get("expected_stop_fill", stop * (1 - slippage)))
    stop_exit_fee = float(metadata.get("expected_stop_exit_fee", stop_fill * qty * fee_rate))

    return max((entry - stop_fill) * qty + entry_fee + stop_exit_fee, 0.0)


def _journal_rows(queryset):
    """Attach display-only trade journal fields without changing the database schema.

    New Strategy v2 trades persist the BTC regime in Trade.metadata. For older v2
    trades created before this journal update, recover it from the original
    MarketSignal referenced by signal_id when that signal is still available.
    """
    trades = list(queryset)
    cfg = AppConfig.current()
    signal_ids = {
        int((trade.metadata or {}).get("signal_id"))
        for trade in trades
        if (trade.metadata or {}).get("signal_id") is not None
    }
    signals = MarketSignal.objects.in_bulk(signal_ids) if signal_ids else {}

    for trade in trades:
        metadata = dict(trade.metadata or {})
        signal_id = metadata.get("signal_id")
        signal = signals.get(int(signal_id)) if signal_id is not None else None

        btc_regime = metadata.get("btc_regime_score")
        if btc_regime is None and signal is not None:
            btc_regime = signal.regime_score
        trade.journal_btc_regime = float(btc_regime) if btc_regime is not None else None

        trade.journal_hold = _format_hold_duration(trade.opened_at, trade.closed_at)
        initial_risk = _actual_initial_risk_dollars(trade, cfg)
        trade.journal_initial_risk = initial_risk if initial_risk > 0 else None
        trade.journal_r_multiple = (
            float(trade.pnl) / initial_risk
            if trade.status == "closed" and trade.pnl is not None and initial_risk > 0
            else None
        )

    return trades


def latest_per_symbol(limit=12):
    seen = set()
    out = []
    qs = MarketSignal.objects.filter(data__strategy_version=STRATEGY_VERSION).order_by("-observed_at", "-score")[:300]
    for s in qs:
        if s.symbol in seen:
            continue
        seen.add(s.symbol)
        out.append(s)
        if len(out) >= limit:
            break
    return out


def paper_pnl_snapshot(account: PaperAccount) -> dict:
    realized = _paper_trades().filter(status="closed").aggregate(v=Sum("pnl"))["v"] or 0.0
    total = float(account.equity) - float(account.starting_cash)
    unrealized = total - float(realized)
    return {"realized": float(realized), "unrealized": unrealized, "total": total}


def current_live_gate(account: PaperAccount):
    gate = LiveGate.objects.filter(requirements__strategy_version=STRATEGY_VERSION).first()
    qs = _paper_trades()
    closed_count = qs.filter(status="closed").count()
    first_trade = qs.order_by("opened_at").first()
    current_days = (timezone.localdate() - first_trade.opened_at.date()).days + 1 if first_trade else 0

    if (
        gate is None
        or gate.closed_trades != closed_count
        or gate.paper_days != current_days
        or gate.requirements.get("strategy_version") != STRATEGY_VERSION
        or abs(float(gate.max_drawdown_pct) - float(account.max_drawdown_pct)) > 1e-9
    ):
        gate = evaluate()
    return gate


@login_required
def dashboard(request):
    acct = mark_to_market(PaperAccount.primary())
    pnl = paper_pnl_snapshot(acct)
    gate = current_live_gate(acct)
    open_trades = _paper_trades().filter(status="open")[:8]
    return render(
        request,
        "dashboard.html",
        {
            "account": acct,
            "gate": gate,
            "signals": latest_per_symbol(),
            "open_trades": open_trades,
            "realized_pnl": pnl["realized"],
            "unrealized_pnl": pnl["unrealized"],
            "total_pnl": pnl["total"],
            "events": AuditEvent.objects.all()[:8],
            "strategy_version": STRATEGY_VERSION,
        },
    )


@login_required
def scanner_view(request):
    if request.method == "POST":
        try:
            scan_market(save=True)
            messages.success(request, f"Strategy v{STRATEGY_VERSION} market scan completed.")
        except Exception as e:
            messages.error(request, f"Scan failed: {e}")
        return redirect("scanner")
    return render(request, "scanner.html", {"signals": latest_per_symbol(50), "strategy_version": STRATEGY_VERSION})


@login_required
def paper_view(request):
    acct = mark_to_market(PaperAccount.primary())
    if request.method == "POST":
        paper_cycle()
        messages.success(request, f"Strategy v{STRATEGY_VERSION} paper engine cycle completed.")
        return redirect("paper")
    return render(
        request,
        "paper.html",
        {
            "account": acct,
            "open_trades": _journal_rows(_paper_trades().filter(status="open")),
            "closed_trades": _journal_rows(_paper_trades().filter(status="closed")[:100]),
            "strategy_version": STRATEGY_VERSION,
        },
    )


@login_required
def backtest_view(request):
    if request.method == "POST":
        symbol = (request.POST.get("symbol") or "BTCUSDT").upper().strip()
        try:
            run_backtest(symbol)
            messages.success(request, f"Strategy v{STRATEGY_VERSION} backtest complete for {symbol}.")
        except Exception as e:
            messages.error(request, f"Backtest failed: {e}")
        return redirect("backtest")
    return render(request, "backtest.html", {"runs": BacktestRun.objects.all()[:30], "strategy_version": STRATEGY_VERSION})


@login_required
def live_view(request):
    gate = evaluate() if request.method == "POST" else (LiveGate.objects.filter(requirements__strategy_version=STRATEGY_VERSION).first() or evaluate())
    live_trades = Trade.objects.filter(
        mode__in=["testnet", "live"],
        metadata__strategy_version=STRATEGY_VERSION,
    )[:50]
    return render(
        request,
        "live.html",
        {"gate": gate, "live_trades": live_trades, "strategy_version": STRATEGY_VERSION},
    )


@login_required
def settings_view(request):
    cfg = AppConfig.current()
    form = AppConfigForm(request.POST or None, instance=cfg)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Settings saved.")
        return redirect("settings")
    return render(request, "settings.html", {"form": form, "strategy_version": STRATEGY_VERSION})


@login_required
def api_status(request):
    acct = PaperAccount.primary()
    gate = LiveGate.objects.filter(requirements__strategy_version=STRATEGY_VERSION).first()
    sig = MarketSignal.objects.filter(data__strategy_version=STRATEGY_VERSION).first()
    return JsonResponse(
        {
            "strategy_version": STRATEGY_VERSION,
            "paper_equity": round(acct.equity, 2),
            "paper_cash": round(acct.cash, 2),
            "live_gate": bool(gate and gate.eligible and gate.requirements.get("strategy_version") == STRATEGY_VERSION),
            "last_signal_at": sig.observed_at.isoformat() if sig else None,
        }
    )


def health(request):
    return JsonResponse({"ok": True, "strategy_version": STRATEGY_VERSION})
