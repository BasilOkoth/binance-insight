from django.urls import path
from . import views
urlpatterns=[
 path("health/",views.health,name="health"),
 path("",views.dashboard,name="dashboard"),
 path("scanner/",views.scanner_view,name="scanner"),
 path("paper/",views.paper_view,name="paper"),
 path("backtest/",views.backtest_view,name="backtest"),
 path("live/",views.live_view,name="live"),
 path("settings/",views.settings_view,name="settings"),
 path("api/status/",views.api_status,name="api_status"),
]
