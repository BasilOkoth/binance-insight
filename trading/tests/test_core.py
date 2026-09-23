from types import SimpleNamespace
from django.test import SimpleTestCase
from trading.services.risk import position_size, daily_loss_limit_breached
from trading.services.binance_client import BinanceClient
from trading.services.scoring import (
    smooth_volume_score,
    score_latest,
    is_actionable_setup,
    confirmed_breakout,
)
from trading.services.execution_guards import projected_exposure_allowed


class RiskTests(SimpleTestCase):
    def test_position_size_respects_risk_and_exposure(self):
        p = position_size(10000, 10000, 100, 98, risk_pct=0.5, max_asset_pct=15)
        self.assertTrue(p.allowed)
        self.assertLessEqual(p.notional, 1500.01)
        self.assertLessEqual(p.quantity * 2, 50.01)

    def test_daily_loss(self):
        self.assertTrue(daily_loss_limit_breached(-200, 10000, 2))
        self.assertFalse(daily_loss_limit_breached(-199, 10000, 2))

    def test_rounding(self):
        self.assertEqual(BinanceClient.floor_to_step(1.23456, "0.001"), "1.234")

    def test_projected_exposure_blocks_new_trade(self):
        self.assertFalse(projected_exposure_allowed(5500, 1500, 10000, 60))
        self.assertTrue(projected_exposure_allowed(4000, 1500, 10000, 60))


class ScoringTests(SimpleTestCase):
    def _row(self, close=101.0, high20_prev=100.0, volume_ratio=1.5):
        return SimpleNamespace(
            close=close,
            ema20=99.0,
            ema50=97.0,
            ema200=90.0,
            ema50_slope=0.5,
            rsi14=60.0,
            roc12=3.0,
            volume_ratio=volume_ratio,
            high20_prev=high20_prev,
            atr14=1.2,
        )

    def test_volume_score_is_smooth(self):
        self.assertAlmostEqual(smooth_volume_score(1.0), 50.0)
        self.assertAlmostEqual(smooth_volume_score(1.5), 75.0)
        self.assertGreater(smooth_volume_score(1.25), smooth_volume_score(1.0))

    def test_approaching_resistance_is_not_confirmed_breakout(self):
        row = self._row(close=99.8, high20_prev=100.0)
        self.assertFalse(confirmed_breakout(row))
        scored = score_latest(row, 100_000_000, 5, 75)
        self.assertFalse(is_actionable_setup(row, scored, 75, 5, 60))

    def test_confirmed_breakout_can_be_actionable(self):
        row = self._row(close=101.0, high20_prev=100.0, volume_ratio=1.5)
        scored = score_latest(row, 500_000_000, 5, 75)
        self.assertTrue(confirmed_breakout(row))
        self.assertTrue(is_actionable_setup(row, scored, 75, 5, 60))
