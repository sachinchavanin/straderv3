"""Core package."""

from strader3.core.alpha_engine import BaseStrategy, STRsiTrendRider
from strader3.core.feed_handler import BarAggregator, FeedHandler

__all__ = ["BarAggregator", "FeedHandler", "BaseStrategy", "STRsiTrendRider"]
