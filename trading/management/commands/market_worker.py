import time

from django.core.management.base import BaseCommand

from trading.models import AuditEvent
from trading.services.scanner import scan_market
from trading.services.paper import paper_cycle
from trading.services.candidate_paper import candidate_cycle


class Command(BaseCommand):
    help = "Continuous scanner + Strategy v2 control paper + candidate paper worker"

    def add_arguments(self, parser):
        parser.add_argument("--seconds", type=int, default=300)

    def handle(self, *args, **opts):
        seconds = max(60, opts["seconds"])
        self.stdout.write(
            f"Worker started; cycle every {seconds}s; control + candidate paper enabled"
        )
        while True:
            try:
                scan_market(True)
                paper_cycle()
                candidate_cycle()
            except KeyboardInterrupt:
                break
            except Exception as exc:
                AuditEvent.objects.create(
                    level="ERROR",
                    category="worker",
                    message=str(exc),
                )
                self.stderr.write(str(exc))
            time.sleep(seconds)
