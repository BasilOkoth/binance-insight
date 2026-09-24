from statistics import median

from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.http import JsonResponse
from django.shortcuts import render, redirect
from django.db.models import Sum
from django.utils import timezone
from django.urls import reverse

from trading.constants import (
    STRATEGY_VERSION,
    BACKTEST_ENGINE_VERSION,
    RESEARCH_ENGINE_VERSION,
    RESEARCH_DEFAULT_DAYS,
    RESEARCH_SPLIT_DATE,
    RESEARCH_INTERVALS,
    CORE_BASES,
)
from .models import AppConfig, MarketSignal, PaperAccount, Trade, BacktestRun, LiveGate, AuditEvent
from .forms import AppConfigForm
from .services.live_gate import evaluate
from .services.backtest import (
    run_backtest,
    run_symbol_matrix,
    run_research_backtest,
    run_research_symbol,
)
from .services.scanner import scan_market
from .services.paper import paper_cycle, mark_to_market


BACKTEST_SYMBOLS = [f"{base}USDT" for base in sorted(CORE_BASES)]
BACKTEST_INTERVALS = [
    ("15m", "15 minutes"),
    ("30m", "30 minutes"),
    ("1h", "1 hour"),
    ("4h", "4 hours"),
]
BACKTEST_INTERVAL_VALUES = [value for value, _label in BACKTEST_INTERVALS]
BACKTEST_INTERVAL_VALUE_SET = set(BACKTEST_INTERVAL_VALUES)
BACKTEST_MATRIX_TOTAL = len(BACKTEST_SYMBOLS) * len(BACKTEST_INTERVALS)

RESEARCH_INTERVAL_CHOICES = [("1h", "1 hour"), ("4h", "4 hours")]
RESEARCH_INTERVAL_VALUE_SET = set(RESEARCH_INTERVALS)
RESEARCH_LOOKBACK_CHOICES = [(730, "2 years"), (1095, "3 years")]
RESEARCH_LOOKBACK_VALUE_SET = {value for value, _label in RESEARCH_LOOKBACK_CHOICES}
RESEARCH_MATRIX_TOTAL = len(BACKTEST_SYMBOLS) * len(RESEARCH_INTERVAL_CHOICES)


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
    """Return the position's planned stop loss in dollars (1R)."""
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
    inferred_fee_rate = (
        (entry_fee / notional)
        if notional > 0 and entry_fee > 0
        else (float(cfg.fee_bps) / 10000.0)
    )
    fee_rate = float(metadata.get("entry_fee_rate", inferred_fee_rate))
    slippage = float(metadata.get("slippage_rate", float(cfg.slippage_bps) / 10000.0))
    stop_fill = float(metadata.get("expected_stop_fill", stop * (1 - slippage)))
    stop_exit_fee = float(metadata.get("expected_stop_exit_fee", stop_fill * qty * fee_rate))

    return max((entry - stop_fill) * qty + entry_fee + stop_exit_fee, 0.0)


def _journal_rows(queryset):
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


def _sample_quality(trades: int) -> str:
    if trades < 20:
        return "Very low"
    if trades < 50:
        return "Low"
    if trades < 100:
        return "Moderate"
    return "Stronger"


def _latest_backtest_matrix_runs():
    qs = BacktestRun.objects.filter(
        results__strategy_version=STRATEGY_VERSION,
        results__backtest_engine_version=BACKTEST_ENGINE_VERSION,
    ).order_by("-started_at")

    seen = set()
    latest = []
    for run in qs[:1000]:
        if (run.results or {}).get("research_mode"):
            continue
        key = (run.symbol, run.timeframe)
        if key in seen:
            continue
        seen.add(key)
        run.sample_quality = _sample_quality(run.trades)
        run.sample_slug = run.sample_quality.lower().replace(" ", "-")
        run.expectancy_r = float((run.results or {}).get("expectancy_r", 0.0) or 0.0)
        latest.append(run)
        if len(latest) >= BACKTEST_MATRIX_TOTAL:
            break

    interval_order = {value: idx for idx, value in enumerate(BACKTEST_INTERVAL_VALUES)}
    latest.sort(key=lambda r: (interval_order.get(r.timeframe, 99), r.symbol))
    return latest


def _matrix_summary(runs):
    positive = [r for r in runs if r.net_return_pct > 0 and r.expectancy_pct > 0 and r.profit_factor > 1]
    robust = [
        r for r in runs
        if r.trades >= 50
        and r.net_return_pct > 0
        and r.expectancy_pct > 0
        and r.profit_factor >= 1.25
    ]

    by_tf = []
    for interval, label in BACKTEST_INTERVALS:
        rows = [r for r in runs if r.timeframe == interval]
        pfs = [float(r.profit_factor) for r in rows if float(r.profit_factor) < 900]
        returns = [float(r.net_return_pct) for r in rows]
        expectancies = [float(r.expectancy_pct) for r in rows]
        by_tf.append(
            {
                "interval": interval,
                "label": label,
                "coverage": len(rows),
                "median_pf": median(pfs) if pfs else None,
                "median_return": median(returns) if returns else None,
                "median_expectancy": median(expectancies) if expectancies else None,
                "positive": sum(1 for r in rows if r.net_return_pct > 0 and r.expectancy_pct > 0 and r.profit_factor > 1),
                "robust": sum(
                    1 for r in rows
                    if r.trades >= 50
                    and r.net_return_pct > 0
                    and r.expectancy_pct > 0
                    and r.profit_factor >= 1.25
                ),
            }
        )

    return {
        "coverage": len(runs),
        "total": BACKTEST_MATRIX_TOTAL,
        "positive": len(positive),
        "robust": len(robust),
        "timeframes": by_tf,
    }


def _decorate_research_run(run):
    data = dict(run.results or {})
    run.research_full = data.get("full_metrics") or {}
    run.research_train = data.get("train_metrics") or {}
    run.research_oos = data.get("oos_metrics") or {}
    run.research_bands = data.get("score_bands") or []
    run.research_walk = data.get("walk_forward") or {}
    run.research_candidate_data = data.get("candidate") or {"passes": False, "reasons": ["No candidate assessment stored."]}
    run.research_candidate = bool(run.research_candidate_data.get("passes"))
    run.research_days = int(data.get("lookback_days") or RESEARCH_DEFAULT_DAYS)
    run.research_split_date = data.get("split_date") or RESEARCH_SPLIT_DATE
    run.research_oos_trades = int(run.research_oos.get("trades") or 0)
    run.research_oos_pf = float(run.research_oos.get("profit_factor") or 0.0)
    run.research_oos_return = float(run.research_oos.get("net_return_pct") or 0.0)
    run.research_oos_expectancy_r = float(run.research_oos.get("expectancy_r") or 0.0)
    run.research_positive_window_pct = float(run.research_walk.get("positive_window_pct") or 0.0)
    return run


def _latest_research_runs(days=RESEARCH_DEFAULT_DAYS):
    qs = BacktestRun.objects.filter(
        results__strategy_version=STRATEGY_VERSION,
        results__research_engine_version=RESEARCH_ENGINE_VERSION,
        results__research_mode=True,
        results__lookback_days=int(days),
        results__split_date=RESEARCH_SPLIT_DATE,
    ).order_by("-started_at")

    seen = set()
    latest = []
    for run in qs[:500]:
        key = (run.symbol, run.timeframe)
        if key in seen:
            continue
        seen.add(key)
        latest.append(_decorate_research_run(run))
        if len(latest) >= RESEARCH_MATRIX_TOTAL:
            break

    interval_order = {value: idx for idx, value in enumerate(RESEARCH_INTERVALS)}
    latest.sort(key=lambda r: (interval_order.get(r.timeframe, 99), r.symbol))
    return latest


def _research_summary(runs):
    candidates = [r for r in runs if r.research_candidate]
    positive_oos = [
        r for r in runs
        if r.research_oos_return > 0
        and r.research_oos_expectancy_r > 0
        and r.research_oos_pf > 1
    ]

    by_tf = []
    for interval, label in RESEARCH_INTERVAL_CHOICES:
        rows = [r for r in runs if r.timeframe == interval]
        pfs = [r.research_oos_pf for r in rows if r.research_oos_pf < 900]
        returns = [r.research_oos_return for r in rows]
        exp_rs = [r.research_oos_expectancy_r for r in rows]
        oos_trades = [r.research_oos_trades for r in rows]
        by_tf.append(
            {
                "interval": interval,
                "label": label,
                "coverage": len(rows),
                "median_oos_pf": median(pfs) if pfs else None,
                "median_oos_return": median(returns) if returns else None,
                "median_oos_expectancy_r": median(exp_rs) if exp_rs else None,
                "median_oos_trades": median(oos_trades) if oos_trades else None,
                "positive_oos": sum(
                    1 for r in rows
                    if r.research_oos_return > 0
                    and r.research_oos_expectancy_r > 0
                    and r.research_oos_pf > 1
                ),
                "candidates": sum(1 for r in rows if r.research_candidate),
            }
        )

    return {
        "coverage": len(runs),
        "total": RESEARCH_MATRIX_TOTAL,
        "positive_oos": len(positive_oos),
        "candidates": len(candidates),
        "timeframes": by_tf,
    }


def _latest_research_detail(symbol, interval, days):
    run = BacktestRun.objects.filter(
        symbol=symbol,
        timeframe=interval,
        results__strategy_version=STRATEGY_VERSION,
        results__research_engine_version=RESEARCH_ENGINE_VERSION,
        results__research_mode=True,
        results__lookback_days=int(days),
        results__split_date=RESEARCH_SPLIT_DATE,
    ).order_by("-started_at").first()
    return _decorate_research_run(run) if run else None


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
    selected_symbol = (request.GET.get("symbol") or "BTCUSDT").upper().strip()
    selected_interval = (request.GET.get("interval") or "15m").strip()
    research_symbol = (request.GET.get("research_symbol") or "INJUSDT").upper().strip()
    research_interval = (request.GET.get("research_interval") or "1h").strip()
    try:
        research_days = int(request.GET.get("research_days") or RESEARCH_DEFAULT_DAYS)
    except (TypeError, ValueError):
        research_days = RESEARCH_DEFAULT_DAYS

    if selected_symbol not in BACKTEST_SYMBOLS:
        selected_symbol = "BTCUSDT"
    if selected_interval not in BACKTEST_INTERVAL_VALUE_SET:
        selected_interval = "15m"
    if research_symbol not in BACKTEST_SYMBOLS:
        research_symbol = "INJUSDT"
    if research_interval not in RESEARCH_INTERVAL_VALUE_SET:
        research_interval = "1h"
    if research_days not in RESEARCH_LOOKBACK_VALUE_SET:
        research_days = RESEARCH_DEFAULT_DAYS

    if request.method == "POST":
        symbol = (request.POST.get("symbol") or "BTCUSDT").upper().strip()
        interval = (request.POST.get("interval") or "15m").strip()
        matrix_symbol = request.POST.get("matrix_symbol") == "1"
        research_one = request.POST.get("research_one") == "1"
        research_matrix_symbol = request.POST.get("research_matrix_symbol") == "1"
        wants_json = (
            matrix_symbol
            or research_matrix_symbol
            or request.headers.get("X-Requested-With") == "XMLHttpRequest"
        )

        if symbol not in BACKTEST_SYMBOLS:
            if wants_json:
                return JsonResponse({"ok": False, "error": "Pair is outside the Strategy v2 core universe."}, status=400)
            messages.error(request, "Choose a pair from the approved Strategy v2 core universe.")
            return redirect("backtest")

        if research_matrix_symbol:
            try:
                runs, errors = run_research_symbol(
                    symbol,
                    intervals=RESEARCH_INTERVALS,
                    days=RESEARCH_DEFAULT_DAYS,
                    split_date=RESEARCH_SPLIT_DATE,
                )
                payload = []
                for r in runs:
                    data = r.results or {}
                    oos = data.get("oos_metrics") or {}
                    candidate = data.get("candidate") or {}
                    payload.append(
                        {
                            "symbol": r.symbol,
                            "interval": r.timeframe,
                            "trades": r.trades,
                            "oos_trades": oos.get("trades", 0),
                            "oos_return_pct": oos.get("net_return_pct", 0),
                            "oos_profit_factor": oos.get("profit_factor", 0),
                            "oos_expectancy_r": oos.get("expectancy_r", 0),
                            "candidate": bool(candidate.get("passes")),
                        }
                    )
                return JsonResponse(
                    {
                        "ok": bool(runs),
                        "symbol": symbol,
                        "completed": len(runs),
                        "results": payload,
                        "errors": errors,
                    },
                    status=200 if runs else 500,
                )
            except Exception as exc:
                return JsonResponse({"ok": False, "symbol": symbol, "error": str(exc)}, status=500)

        if research_one:
            research_interval_post = (request.POST.get("research_interval") or "1h").strip()
            try:
                research_days_post = int(request.POST.get("research_days") or RESEARCH_DEFAULT_DAYS)
            except (TypeError, ValueError):
                research_days_post = RESEARCH_DEFAULT_DAYS

            if research_interval_post not in RESEARCH_INTERVAL_VALUE_SET:
                messages.error(request, "Research Engine v2.2 supports only 1h and 4h.")
                return redirect("backtest")
            if research_days_post not in RESEARCH_LOOKBACK_VALUE_SET:
                messages.error(request, "Choose a 2-year or 3-year research lookback.")
                return redirect("backtest")

            try:
                run_research_backtest(
                    symbol,
                    interval=research_interval_post,
                    days=research_days_post,
                    split_date=RESEARCH_SPLIT_DATE,
                )
                messages.success(
                    request,
                    f"Research Engine v{RESEARCH_ENGINE_VERSION} completed {symbol} {research_interval_post} over {research_days_post} days.",
                )
            except Exception as exc:
                messages.error(request, f"Research backtest failed: {exc}")

            return redirect(
                f"{reverse('backtest')}?research_symbol={symbol}&research_interval={research_interval_post}&research_days={research_days_post}"
            )

        if matrix_symbol:
            try:
                runs, errors = run_symbol_matrix(symbol, intervals=BACKTEST_INTERVAL_VALUES)
                payload = [
                    {
                        "symbol": r.symbol,
                        "interval": r.timeframe,
                        "trades": r.trades,
                        "net_return_pct": r.net_return_pct,
                        "profit_factor": r.profit_factor,
                        "expectancy_pct": r.expectancy_pct,
                    }
                    for r in runs
                ]
                return JsonResponse(
                    {
                        "ok": bool(runs),
                        "symbol": symbol,
                        "completed": len(runs),
                        "results": payload,
                        "errors": errors,
                    },
                    status=200 if runs else 500,
                )
            except Exception as exc:
                return JsonResponse({"ok": False, "symbol": symbol, "error": str(exc)}, status=500)

        if interval not in BACKTEST_INTERVAL_VALUE_SET:
            if wants_json:
                return JsonResponse({"ok": False, "error": "Unsupported backtest timeframe."}, status=400)
            messages.error(request, "Choose a supported backtest timeframe.")
            return redirect("backtest")

        try:
            run = run_backtest(symbol, interval=interval)
            if wants_json:
                return JsonResponse(
                    {
                        "ok": True,
                        "symbol": run.symbol,
                        "interval": run.timeframe,
                        "trades": run.trades,
                        "net_return_pct": run.net_return_pct,
                        "profit_factor": run.profit_factor,
                    }
                )
            messages.success(
                request,
                f"Strategy v{STRATEGY_VERSION} backtest complete for {symbol} on {interval}.",
            )
        except Exception as e:
            if wants_json:
                return JsonResponse({"ok": False, "error": str(e)}, status=500)
            messages.error(request, f"Backtest failed: {e}")

        return redirect(f"{reverse('backtest')}?symbol={symbol}&interval={interval}")

    runs = _latest_backtest_matrix_runs()
    summary = _matrix_summary(runs)
    research_runs = _latest_research_runs(RESEARCH_DEFAULT_DAYS)
    research_summary = _research_summary(research_runs)
    research_detail = _latest_research_detail(research_symbol, research_interval, research_days)

    return render(
        request,
        "backtest.html",
        {
            "runs": runs,
            "matrix_summary": summary,
            "strategy_version": STRATEGY_VERSION,
            "backtest_engine_version": BACKTEST_ENGINE_VERSION,
            "backtest_symbols": BACKTEST_SYMBOLS,
            "backtest_intervals": BACKTEST_INTERVALS,
            "backtest_interval_values": BACKTEST_INTERVAL_VALUES,
            "matrix_total": BACKTEST_MATRIX_TOTAL,
            "selected_symbol": selected_symbol,
            "selected_interval": selected_interval,
            "research_engine_version": RESEARCH_ENGINE_VERSION,
            "research_split_date": RESEARCH_SPLIT_DATE,
            "research_default_days": RESEARCH_DEFAULT_DAYS,
            "research_intervals": RESEARCH_INTERVAL_CHOICES,
            "research_lookback_choices": RESEARCH_LOOKBACK_CHOICES,
            "research_matrix_total": RESEARCH_MATRIX_TOTAL,
            "research_runs": research_runs,
            "research_summary": research_summary,
            "research_detail": research_detail,
            "research_selected_symbol": research_symbol,
            "research_selected_interval": research_interval,
            "research_selected_days": research_days,
        },
    )


@login_required
def live_view(request):
    gate = evaluate() if request.method == "POST" else (
        LiveGate.objects.filter(requirements__strategy_version=STRATEGY_VERSION).first() or evaluate()
    )
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
            "live_gate": bool(
                gate
                and gate.eligible
                and gate.requirements.get("strategy_version") == STRATEGY_VERSION
            ),
            "last_signal_at": sig.observed_at.isoformat() if sig else None,
        }
    )


def health(request):
    return JsonResponse({"ok": True, "strategy_version": STRATEGY_VERSION})
