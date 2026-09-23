from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.http import JsonResponse
from django.shortcuts import render, redirect
from django.db.models import Sum
from .models import AppConfig, MarketSignal, PaperAccount, Trade, BacktestRun, LiveGate, AuditEvent
from .forms import AppConfigForm
from .services.live_gate import evaluate
from .services.backtest import run_backtest
from .services.scanner import scan_market
from .services.paper import paper_cycle, mark_to_market

def latest_per_symbol(limit=12):
    seen=set(); out=[]
    for s in MarketSignal.objects.order_by("-observed_at","-score")[:300]:
        if s.symbol in seen: continue
        seen.add(s.symbol); out.append(s)
        if len(out)>=limit: break
    return out

@login_required
def dashboard(request):
    acct=mark_to_market(PaperAccount.primary())
    gate=LiveGate.objects.first() or evaluate()
    open_trades=Trade.objects.filter(mode="paper",status="open")[:8]
    closed=Trade.objects.filter(mode="paper",status="closed")
    pnl=closed.aggregate(v=Sum("pnl"))["v"] or 0
    return render(request,"dashboard.html",{"account":acct,"gate":gate,"signals":latest_per_symbol(),"open_trades":open_trades,"paper_pnl":pnl,"events":AuditEvent.objects.all()[:8]})

@login_required
def scanner_view(request):
    if request.method=="POST":
        try: scan_market(save=True); messages.success(request,"Market scan completed.")
        except Exception as e: messages.error(request,f"Scan failed: {e}")
        return redirect("scanner")
    return render(request,"scanner.html",{"signals":latest_per_symbol(50)})

@login_required
def paper_view(request):
    acct=mark_to_market(PaperAccount.primary())
    if request.method=="POST":
        paper_cycle(); messages.success(request,"Paper engine cycle completed."); return redirect("paper")
    return render(request,"paper.html",{"account":acct,"open_trades":Trade.objects.filter(mode="paper",status="open"),"closed_trades":Trade.objects.filter(mode="paper",status="closed")[:100]})

@login_required
def backtest_view(request):
    if request.method=="POST":
        symbol=(request.POST.get("symbol") or "BTCUSDT").upper().strip()
        try: run_backtest(symbol); messages.success(request,f"Backtest complete for {symbol}.")
        except Exception as e: messages.error(request,f"Backtest failed: {e}")
        return redirect("backtest")
    return render(request,"backtest.html",{"runs":BacktestRun.objects.all()[:30]})

@login_required
def live_view(request):
    gate=evaluate() if request.method=="POST" else (LiveGate.objects.first() or evaluate())
    return render(request,"live.html",{"gate":gate,"live_trades":Trade.objects.filter(mode__in=["testnet","live"])[:50]})

@login_required
def settings_view(request):
    cfg=AppConfig.current(); form=AppConfigForm(request.POST or None,instance=cfg)
    if request.method=="POST" and form.is_valid(): form.save(); messages.success(request,"Settings saved."); return redirect("settings")
    return render(request,"settings.html",{"form":form})

@login_required
def api_status(request):
    acct=PaperAccount.primary(); gate=LiveGate.objects.first()
    sig=MarketSignal.objects.first()
    return JsonResponse({"paper_equity":round(acct.equity,2),"paper_cash":round(acct.cash,2),"live_gate":bool(gate and gate.eligible),"last_signal_at":sig.observed_at.isoformat() if sig else None})


def health(request):
    return JsonResponse({"ok": True})
