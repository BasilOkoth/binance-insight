import time
from django.core.management.base import BaseCommand, CommandError
from trading.services.live_trading import assert_live_enabled, assert_exchange_access, reconcile_open_trades, execute_best_once
from trading.services.scanner import scan_market
from trading.models import AuditEvent

class Command(BaseCommand):
    help="Intentional testnet/live worker. Reconciles OCO exits; optional --auto-entry enables new entries."
    def add_arguments(self,p):
        p.add_argument("--seconds",type=int,default=60)
        p.add_argument("--auto-entry",action="store_true")
    def handle(self,*args,**opts):
        seconds=max(30,opts["seconds"]); auto=opts["auto_entry"]
        try:
            assert_live_enabled() if auto else assert_exchange_access()
        except Exception as e: raise CommandError(str(e))
        self.stdout.write(self.style.WARNING(f"Live worker active. auto_entry={auto}; cycle={seconds}s"))
        while True:
            try:
                reconcile_open_trades()
                if auto:
                    scan_market(True)
                    execute_best_once()
            except KeyboardInterrupt:
                break
            except Exception as e:
                AuditEvent.objects.create(level="ERROR",category="live-worker",message=str(e)); self.stderr.write(str(e))
            time.sleep(seconds)
