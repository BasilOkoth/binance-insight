from django.contrib import admin
from .models import AppConfig, MarketSignal, PaperAccount, Trade, BacktestRun, LiveGate, AuditEvent
for model in [AppConfig, MarketSignal, PaperAccount, Trade, BacktestRun, LiveGate, AuditEvent]:
    admin.site.register(model)
