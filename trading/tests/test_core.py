from django.test import SimpleTestCase
from trading.services.risk import position_size, daily_loss_limit_breached
from trading.services.binance_client import BinanceClient

class RiskTests(SimpleTestCase):
    def test_position_size_respects_risk_and_exposure(self):
        p=position_size(10000,10000,100,98,risk_pct=0.5,max_asset_pct=15)
        self.assertTrue(p.allowed); self.assertLessEqual(p.notional,1500.01); self.assertLessEqual(p.quantity*2,50.01)
    def test_daily_loss(self):
        self.assertTrue(daily_loss_limit_breached(-200,10000,2)); self.assertFalse(daily_loss_limit_breached(-199,10000,2))
    def test_rounding(self):
        self.assertEqual(BinanceClient.floor_to_step(1.23456,"0.001"),"1.234")
