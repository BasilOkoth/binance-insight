from django.core.management.base import BaseCommand
from trading.services.scanner import scan_market
class Command(BaseCommand):
    def handle(self,*args,**opts):
        rows=scan_market(True); self.stdout.write(self.style.SUCCESS(f"Scanned {len(rows)} symbols"))
