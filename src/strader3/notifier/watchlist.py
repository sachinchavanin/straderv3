"""Config-driven watchlist with hot-reload support.

Watchlist is loaded from YAML config and can be reloaded at runtime
without restarting the bot. Uses file modification time to detect changes.
"""

import os
from dataclasses import dataclass, field
from pathlib import Path

import structlog
import yaml

logger = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class WatchlistEntry:
    """Single watchlist entry."""

    symbol: str
    exchange: str
    lot_size: int = 1

    @property
    def full_symbol(self) -> str:
        """Return fully qualified symbol (e.g. NSE:TCS-EQ)."""
        if ":" in self.symbol:
            return self.symbol
        return f"{self.exchange}:{self.symbol}"


@dataclass
class WatchlistConfig:
    """Parsed watchlist configuration."""

    entries: list[WatchlistEntry] = field(default_factory=list)
    bar_interval: str = "1m"
    gap_fill_enabled: bool = True
    gap_fill_lookback: int = 15

    @property
    def symbols(self) -> list[str]:
        """Return list of fully qualified symbols."""
        return [e.full_symbol for e in self.entries]

    @property
    def exchanges(self) -> list[str]:
        """Return unique list of exchanges."""
        return list(dict.fromkeys(e.exchange for e in self.entries))

    def get_entry(self, symbol: str) -> WatchlistEntry | None:
        """Get watchlist entry by symbol."""
        for entry in self.entries:
            if entry.symbol == symbol or entry.full_symbol == symbol:
                return entry
        return None


class WatchlistManager:
    """Manages watchlist configuration with hot-reload.

    Loads watchlist from YAML config file and detects changes
    by monitoring file modification time.
    """

    def __init__(
        self,
        config: dict | None = None,
        config_path: str = "config.yaml",
    ) -> None:
        self._config_path = config_path
        self._config = config
        self._watchlist: WatchlistConfig = WatchlistConfig()
        self._last_mtime: float = 0.0
        self._reload_count: int = 0

        if config:
            self._load_from_dict(config)
        elif os.path.exists(config_path):
            self._load_from_file()

    def _load_from_dict(self, config: dict) -> None:
        """Load watchlist from config dict."""
        market_data = config.get("market_data", {})

        raw_watchlist = market_data.get("watchlist", [])
        entries = []
        for item in raw_watchlist:
            if isinstance(item, str):
                # Parse "NSE:TCS-EQ" format
                if ":" in item:
                    exchange, symbol = item.split(":", 1)
                else:
                    exchange = "NSE"
                    symbol = item
                entries.append(WatchlistEntry(symbol=symbol, exchange=exchange))
            elif isinstance(item, dict):
                entries.append(WatchlistEntry(
                    symbol=item.get("symbol", ""),
                    exchange=item.get("exchange", "NSE"),
                    lot_size=item.get("lot_size", 1),
                ))

        self._watchlist = WatchlistConfig(
            entries=entries,
            bar_interval=market_data.get("bar_interval", "1m"),
            gap_fill_enabled=market_data.get("gap_fill", {}).get("enabled", True),
            gap_fill_lookback=market_data.get("gap_fill", {}).get("lookback_minutes", 15),
        )

    def _load_from_file(self) -> None:
        """Load watchlist from YAML file."""
        path = Path(self._config_path)
        if not path.exists():
            logger.warning("watchlist.config_not_found", path=self._config_path)
            return

        self._last_mtime = path.stat().st_mtime

        with open(path) as f:
            config = yaml.safe_load(f)

        if config:
            self._config = config
            self._load_from_dict(config)
            logger.info(
                "watchlist.loaded",
                symbols=len(self._watchlist.entries),
                path=self._config_path,
            )

    def reload_if_changed(self) -> bool:
        """Check if config file changed and reload if so.

        Returns True if watchlist was reloaded.
        """
        if not os.path.exists(self._config_path):
            return False

        current_mtime = os.path.getmtime(self._config_path)
        if current_mtime <= self._last_mtime:
            return False

        logger.info("watchlist.reloading", path=self._config_path)
        old_symbols = set(self._watchlist.symbols)

        self._load_from_file()
        self._reload_count += 1

        new_symbols = set(self._watchlist.symbols)
        added = new_symbols - old_symbols
        removed = old_symbols - new_symbols

        if added:
            logger.info("watchlist.symbols_added", symbols=sorted(added))
        if removed:
            logger.info("watchlist.symbols_removed", symbols=sorted(removed))

        return True

    @property
    def watchlist(self) -> WatchlistConfig:
        """Return current watchlist configuration."""
        return self._watchlist

    @property
    def symbols(self) -> list[str]:
        """Return current symbol list."""
        return self._watchlist.symbols

    @property
    def reload_count(self) -> int:
        """Return number of times watchlist has been reloaded."""
        return self._reload_count

    def update_from_config(self, config: dict) -> None:
        """Update watchlist from a new config dict (programmatic reload)."""
        old_symbols = set(self._watchlist.symbols)
        self._config = config
        self._load_from_dict(config)
        self._reload_count += 1

        new_symbols = set(self._watchlist.symbols)
        added = new_symbols - old_symbols
        removed = old_symbols - new_symbols
        if added or removed:
            logger.info(
                "watchlist.updated",
                added=sorted(added) if added else [],
                removed=sorted(removed) if removed else [],
            )
