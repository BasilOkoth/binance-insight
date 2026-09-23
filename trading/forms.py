from django import forms
from .models import AppConfig
class AppConfigForm(forms.ModelForm):
    class Meta:
        model=AppConfig
        exclude=("name",)
        widgets={f:forms.NumberInput(attrs={"step":"0.1"}) for f in ["signal_threshold","paper_starting_cash","risk_per_trade_pct","max_daily_loss_pct","max_total_exposure_pct","max_asset_exposure_pct","fee_bps","slippage_bps","stop_atr_multiple","target_r_multiple"]}
