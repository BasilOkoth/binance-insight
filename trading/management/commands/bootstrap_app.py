from django.core.management.base import BaseCommand
from trading.models import AppConfig, PaperAccount
class Command(BaseCommand):
    help="Create default configuration and paper account"
    def handle(self,*args,**opts):
        cfg=AppConfig.current(); acct=PaperAccount.primary(); self.stdout.write(self.style.SUCCESS(f"Ready: config={cfg.id}, paper_account={acct.id}"))
