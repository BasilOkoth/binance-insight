from django.core.management.base import BaseCommand
from trading.services.live_gate import evaluate
class Command(BaseCommand):
    def handle(self,*args,**opts):
        g=evaluate(); self.stdout.write((self.style.SUCCESS if g.eligible else self.style.WARNING)(f"eligible={g.eligible}; reasons={g.reasons}"))
