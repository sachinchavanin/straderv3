"""Notifier package — Discord alerts and paper-trade validation."""

from strader3.notifier.alert_manager import AlertManager, AlertMessage, AlertType
from strader3.notifier.market_phase import MarketPhase, MarketPhaseChecker
from strader3.notifier.paper_trade_validator import PaperTradeValidator
from strader3.notifier.watchlist import WatchlistConfig, WatchlistEntry, WatchlistManager

__all__ = [
    "AlertManager",
    "AlertMessage",
    "AlertType",
    "MarketPhase",
    "MarketPhaseChecker",
    "PaperTradeValidator",
    "WatchlistConfig",
    "WatchlistEntry",
    "WatchlistManager",
]
