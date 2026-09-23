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
            "open_trades": _paper_trades().filter(status="open"),
            "closed_trades": _paper_trades().filter(status="closed")[:100],
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
