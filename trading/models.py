from django.db import models
from django.utils import timezone

class AppConfig(models.Model):
    name = models.CharField(max_length=80, unique=True, default="default")
    quote_asset = models.CharField(max_length=12, default="USDT")
    scan_interval = models.CharField(max_length=8, default="15m")
    scan_universe_size = models.PositiveIntegerField(default=30)
    signal_threshold = models.FloatField(default=72.0)
    paper_starting_cash = models.FloatField(default=10000.0)
    risk_per_trade_pct = models.FloatField(default=0.5)
    max_daily_loss_pct = models.FloatField(default=2.0)
    max_total_exposure_pct = models.FloatField(default=60.0)
    max_asset_exposure_pct = models.FloatField(default=15.0)
    max_open_positions = models.PositiveIntegerField(default=5)
    fee_bps = models.FloatField(default=10.0)
    slippage_bps = models.FloatField(default=5.0)
    stop_atr_multiple = models.FloatField(default=1.5)
    target_r_multiple = models.FloatField(default=2.2)
    updated_at = models.DateTimeField(auto_now=True)

    @classmethod
    def current(cls):
        obj, _ = cls.objects.get_or_create(name="default")
        return obj

class MarketSignal(models.Model):
    symbol = models.CharField(max_length=24, db_index=True)
    timeframe = models.CharField(max_length=8, default="15m")
    observed_at = models.DateTimeField(default=timezone.now, db_index=True)
    price = models.FloatField()
    score = models.FloatField(db_index=True)
    trend_score = models.FloatField(default=0)
    momentum_score = models.FloatField(default=0)
    volume_score = models.FloatField(default=0)
    breakout_score = models.FloatField(default=0)
    volatility_score = models.FloatField(default=0)
    liquidity_score = models.FloatField(default=0)
    regime_score = models.FloatField(default=0)
    atr = models.FloatField(default=0)
    rsi = models.FloatField(default=0)
    volume_ratio = models.FloatField(default=0)
    spread_bps = models.FloatField(default=0)
    stop_price = models.FloatField(default=0)
    target_price = models.FloatField(default=0)
    rationale = models.TextField(blank=True)
    warnings = models.TextField(blank=True)
    is_actionable = models.BooleanField(default=False)
    data = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-observed_at", "-score"]
        indexes = [models.Index(fields=["symbol", "-observed_at"])]

class PaperAccount(models.Model):
    name = models.CharField(max_length=80, unique=True, default="primary")
    starting_cash = models.FloatField(default=10000)
    cash = models.FloatField(default=10000)
    equity = models.FloatField(default=10000)
    peak_equity = models.FloatField(default=10000)
    max_drawdown_pct = models.FloatField(default=0)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    @classmethod
    def primary(cls):
        cfg = AppConfig.current()
        obj, _ = cls.objects.get_or_create(name="primary", defaults={"starting_cash": cfg.paper_starting_cash, "cash": cfg.paper_starting_cash, "equity": cfg.paper_starting_cash, "peak_equity": cfg.paper_starting_cash})
        return obj

class Trade(models.Model):
    MODE_CHOICES = [("paper","Paper"),("testnet","Testnet"),("live","Live")]
    STATUS_CHOICES = [("open","Open"),("closed","Closed"),("rejected","Rejected"),("error","Error")]
    mode = models.CharField(max_length=10, choices=MODE_CHOICES, default="paper", db_index=True)
    symbol = models.CharField(max_length=24, db_index=True)
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default="open", db_index=True)
    opened_at = models.DateTimeField(default=timezone.now, db_index=True)
    closed_at = models.DateTimeField(null=True, blank=True)
    entry_price = models.FloatField()
    quantity = models.FloatField()
    stop_price = models.FloatField()
    target_price = models.FloatField()
    exit_price = models.FloatField(null=True, blank=True)
    entry_fee = models.FloatField(default=0)
    exit_fee = models.FloatField(default=0)
    pnl = models.FloatField(default=0)
    pnl_pct = models.FloatField(default=0)
    exit_reason = models.CharField(max_length=64, blank=True)
    signal_score = models.FloatField(default=0)
    risk_amount = models.FloatField(default=0)
    order_id = models.CharField(max_length=128, blank=True)
    protection_order_id = models.CharField(max_length=128, blank=True)
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-opened_at"]

class BacktestRun(models.Model):
    symbol = models.CharField(max_length=24)
    timeframe = models.CharField(max_length=8)
    started_at = models.DateTimeField(default=timezone.now)
    start_date = models.DateTimeField(null=True, blank=True)
    end_date = models.DateTimeField(null=True, blank=True)
    trades = models.PositiveIntegerField(default=0)
    win_rate_pct = models.FloatField(default=0)
    net_return_pct = models.FloatField(default=0)
    profit_factor = models.FloatField(default=0)
    max_drawdown_pct = models.FloatField(default=0)
    expectancy_pct = models.FloatField(default=0)
    sharpe = models.FloatField(default=0)
    results = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-started_at"]

class LiveGate(models.Model):
    checked_at = models.DateTimeField(default=timezone.now)
    eligible = models.BooleanField(default=False)
    paper_days = models.PositiveIntegerField(default=0)
    closed_trades = models.PositiveIntegerField(default=0)
    net_profit = models.FloatField(default=0)
    profit_factor = models.FloatField(default=0)
    expectancy = models.FloatField(default=0)
    max_drawdown_pct = models.FloatField(default=0)
    reasons = models.JSONField(default=list)
    requirements = models.JSONField(default=dict)

    class Meta:
        ordering = ["-checked_at"]

class AuditEvent(models.Model):
    created_at = models.DateTimeField(default=timezone.now, db_index=True)
    level = models.CharField(max_length=12, default="INFO")
    category = models.CharField(max_length=40, db_index=True)
    message = models.TextField()
    data = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-created_at"]
