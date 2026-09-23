from django.core.management.base import BaseCommand, CommandError
from trading.services.live_trading import execute_best_once
class Command(BaseCommand):
    help="Execute at most one gated testnet/live trade. Requires explicit environment unlock."
    def handle(self,*args,**opts):
        try:
            t=execute_best_once(); self.stdout.write(self.style.SUCCESS(f"Executed: {t}" if t else "No actionable signal"))
        except Exception as e: raise CommandError(str(e))
