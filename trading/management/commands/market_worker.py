import time
from django.core.management.base import BaseCommand
from trading.services.scanner import scan_market
from trading.services.paper import paper_cycle
from trading.models import AuditEvent
class Command(BaseCommand):
    help="Continuous scanner + paper-trading worker"
    def add_arguments(self,p): p.add_argument("--seconds",type=int,default=300)
    def handle(self,*args,**opts):
        seconds=max(60,opts["seconds"]); self.stdout.write(f"Worker started; cycle every {seconds}s")
        while True:
            try: scan_market(True); paper_cycle()
            except KeyboardInterrupt: break
            except Exception as e: AuditEvent.objects.create(level="ERROR",category="worker",message=str(e)); self.stderr.write(str(e))
            time.sleep(seconds)
