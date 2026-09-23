from django.core.management.base import BaseCommand
from trading.services.backtest import run_backtest
class Command(BaseCommand):
    def add_arguments(self,p): p.add_argument("symbol",nargs="?",default="BTCUSDT")
    def handle(self,*args,**opts):
        r=run_backtest(opts["symbol"].upper()); self.stdout.write(self.style.SUCCESS(f"{r.symbol}: trades={r.trades}, return={r.net_return_pct:.2f}%, PF={r.profit_factor:.2f}, DD={r.max_drawdown_pct:.2f}%"))
