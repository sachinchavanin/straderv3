"""Core package."""

from strader3.core.db import init_db
from strader3.core.order_manager import OrderManager
from strader3.core.portfolio_manager import (
    CapitalAllocator,
    InvalidTransitionError,
    PortfolioManager,
)
from strader3.core.repository import (
    DailyPnlRepository,
    MigrationManager,
    OrderRepository,
    PositionRepository,
    SignalRepository,
    TradeRepository,
)
from strader3.core.risk_manager import RiskManager
from strader3.core.trading_engine import TradingEngine

__all__ = [
    "CapitalAllocator",
    "DailyPnlRepository",
    "InvalidTransitionError",
    "MigrationManager",
    "OrderManager",
    "OrderRepository",
    "PortfolioManager",
    "PositionRepository",
    "RiskManager",
    "SignalRepository",
    "TradeRepository",
    "TradingEngine",
    "init_db",
]

# Alpha engine requires pandas-ta (Python 3.12+); import on demand.
try:
    from strader3.core.alpha_engine import BaseStrategy, STRsiTrendRider  # noqa: F401

    __all__.extend(["BaseStrategy", "STRsiTrendRider"])
except ImportError:
    pass

try:
    from strader3.core.feed_handler import BarAggregator, FeedHandler  # noqa: F401

    __all__.extend(["BarAggregator", "FeedHandler"])
except ImportError:
    pass
