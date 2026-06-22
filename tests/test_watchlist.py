"""Tests for WatchlistManager — config-driven watchlist with hot-reload."""

import os
import tempfile
import time

import pytest
import yaml

from strader3.notifier.watchlist import WatchlistManager, WatchlistEntry, WatchlistConfig


class TestWatchlistEntry:
    def test_full_symbol_with_exchange(self):
        entry = WatchlistEntry(symbol="TCS-EQ", exchange="NSE")
        assert entry.full_symbol == "NSE:TCS-EQ"

    def test_full_symbol_already_qualified(self):
        entry = WatchlistEntry(symbol="NSE:TCS-EQ", exchange="NSE")
        assert entry.full_symbol == "NSE:TCS-EQ"


class TestWatchlistConfig:
    def test_symbols_property(self):
        cfg = WatchlistConfig(
            entries=[
                WatchlistEntry("TCS-EQ", "NSE"),
                WatchlistEntry("INFY-EQ", "NSE"),
            ]
        )
        assert cfg.symbols == ["NSE:TCS-EQ", "NSE:INFY-EQ"]

    def test_get_entry(self):
        cfg = WatchlistConfig(
            entries=[WatchlistEntry("TCS-EQ", "NSE", lot_size=1)]
        )
        entry = cfg.get_entry("TCS-EQ")
        assert entry is not None
        assert entry.lot_size == 1

    def test_get_entry_not_found(self):
        cfg = WatchlistConfig()
        assert cfg.get_entry("UNKNOWN") is None


class TestWatchlistManagerFromDict:
    def test_load_from_config_dict(self):
        config = {
            "market_data": {
                "watchlist": ["NSE:TCS-EQ", "NSE:INFY-EQ", "NSE:HDFCBANK-EQ"],
                "bar_interval": "5m",
                "gap_fill": {"enabled": True, "lookback_minutes": 20},
            }
        }
        wm = WatchlistManager(config=config)
        assert len(wm.symbols) == 3
        assert "NSE:TCS-EQ" in wm.symbols
        assert wm.watchlist.bar_interval == "5m"
        assert wm.watchlist.gap_fill_lookback == 20

    def test_load_from_config_dict_with_dict_entries(self):
        config = {
            "market_data": {
                "watchlist": [
                    {"symbol": "TCS-EQ", "exchange": "NSE", "lot_size": 1},
                    {"symbol": "INFY-EQ", "exchange": "NSE", "lot_size": 2},
                ],
            }
        }
        wm = WatchlistManager(config=config)
        assert len(wm.symbols) == 2
        entry = wm.watchlist.get_entry("TCS-EQ")
        assert entry is not None
        assert entry.lot_size == 1

    def test_empty_watchlist(self):
        config = {"market_data": {"watchlist": []}}
        wm = WatchlistManager(config=config)
        assert wm.symbols == []

    def test_default_values(self):
        config = {}
        wm = WatchlistManager(config=config)
        assert wm.watchlist.bar_interval == "1m"
        assert wm.watchlist.gap_fill_enabled is True


class TestWatchlistManagerFromFile:
    def test_load_from_yaml_file(self):
        config_data = {
            "market_data": {
                "watchlist": ["NSE:TCS-EQ", "NSE:INFY-EQ"],
                "bar_interval": "1m",
            }
        }
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            yaml.dump(config_data, f)
            path = f.name

        try:
            wm = WatchlistManager(config_path=path)
            assert len(wm.symbols) == 2
            assert wm.reload_count == 0
        finally:
            os.unlink(path)

    def test_reload_if_changed(self):
        config_data = {
            "market_data": {
                "watchlist": ["NSE:TCS-EQ"],
            }
        }
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            yaml.dump(config_data, f)
            path = f.name

        try:
            wm = WatchlistManager(config_path=path)
            assert len(wm.symbols) == 1

            # Modify the file
            time.sleep(0.1)  # Ensure mtime changes
            config_data["market_data"]["watchlist"].append("NSE:INFY-EQ")
            with open(path, "w") as f:
                yaml.dump(config_data, f)

            reloaded = wm.reload_if_changed()
            assert reloaded is True
            assert len(wm.symbols) == 2
            assert wm.reload_count == 1
        finally:
            os.unlink(path)

    def test_no_reload_when_unchanged(self):
        config_data = {
            "market_data": {
                "watchlist": ["NSE:TCS-EQ"],
            }
        }
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            yaml.dump(config_data, f)
            path = f.name

        try:
            wm = WatchlistManager(config_path=path)
            reloaded = wm.reload_if_changed()
            assert reloaded is False
            assert wm.reload_count == 0
        finally:
            os.unlink(path)

    def test_reload_missing_file(self):
        wm = WatchlistManager(config_path="/nonexistent/config.yaml")
        reloaded = wm.reload_if_changed()
        assert reloaded is False

    def test_update_from_config(self):
        config1 = {"market_data": {"watchlist": ["NSE:TCS-EQ"]}}
        config2 = {"market_data": {"watchlist": ["NSE:TCS-EQ", "NSE:INFY-EQ"]}}

        wm = WatchlistManager(config=config1)
        assert len(wm.symbols) == 1

        wm.update_from_config(config2)
        assert len(wm.symbols) == 2
        assert wm.reload_count == 1
