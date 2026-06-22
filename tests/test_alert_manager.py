"""Tests for AlertManager — Discord webhook alerts with embeds and rate limiting."""

import asyncio
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest

from strader3.notifier.alert_manager import AlertManager, AlertMessage, AlertType


# =====================================================================
# AlertType
# =====================================================================

class TestAlertType:
    def test_all_types_exist(self):
        assert AlertType.ENTRY == "ENTRY"
        assert AlertType.EXIT == "EXIT"
        assert AlertType.SL_HIT == "SL_HIT"
        assert AlertType.TARGET_HIT == "TARGET_HIT"
        assert AlertType.DAILY_SUMMARY == "DAILY_SUMMARY"
        assert AlertType.KILL_SWITCH == "KILL_SWITCH"
        assert AlertType.RISK_MODE_CHANGE == "RISK_MODE_CHANGE"
        assert AlertType.PNL_THRESHOLD == "PNL_THRESHOLD"


# =====================================================================
# AlertManager init
# =====================================================================

class TestAlertManagerInit:
    def test_not_configured_without_webhook(self):
        am = AlertManager(webhook_url=None)
        assert not am.is_configured

    def test_not_configured_when_disabled(self):
        am = AlertManager(webhook_url="https://discord.com/webhook", enabled=False)
        assert not am.is_configured

    def test_configured_with_webhook(self):
        am = AlertManager(webhook_url="https://discord.com/webhook", enabled=True)
        assert am.is_configured

    def test_default_rate_limit(self):
        am = AlertManager(webhook_url="https://discord.com/webhook")
        assert am._rate_limit_seconds == 60

    def test_custom_rate_limit(self):
        am = AlertManager(webhook_url="https://discord.com/webhook", rate_limit_seconds=30)
        assert am._rate_limit_seconds == 30


# =====================================================================
# Rate limiting
# =====================================================================

class TestRateLimiting:
    def test_not_rate_limited_initially(self):
        am = AlertManager(webhook_url="https://discord.com/webhook")
        assert not am._is_rate_limited("TCS")

    def test_rate_limited_after_record(self):
        am = AlertManager(webhook_url="https://discord.com/webhook")
        am._record_alert("TCS")
        assert am._is_rate_limited("TCS")

    def test_rate_limit_per_symbol(self):
        am = AlertManager(webhook_url="https://discord.com/webhook")
        am._record_alert("TCS")
        assert am._is_rate_limited("TCS")
        assert not am._is_rate_limited("INFY")

    @pytest.mark.asyncio
    async def test_send_alert_skips_rate_limited(self):
        am = AlertManager(webhook_url="https://discord.com/webhook", rate_limit_seconds=60)
        am._record_alert("TCS")
        result = await am.send_entry_alert(
            symbol="TCS", price=100.0, stop_loss=90.0, target=110.0, quantity=10
        )
        assert result is True  # Rate-limited = skipped, but not an error


# =====================================================================
# Embed building
# =====================================================================

class TestEmbedBuilding:
    def test_entry_embed_has_required_fields(self):
        am = AlertManager(webhook_url="https://discord.com/webhook")
        alert = AlertMessage(
            alert_type=AlertType.ENTRY,
            symbol="NSE:TCS-EQ",
            price=3500.0,
            stop_loss=3450.0,
            target=3600.0,
            quantity=100,
            confidence=0.85,
            reason="RSI oversold + supertrend up",
        )
        embed = am._build_embed(alert)

        assert embed["color"] == 0x00FF00
        assert "ENTRY" in embed["title"]
        assert "TCS-EQ" in embed["title"]

        field_names = [f["name"] for f in embed["fields"]]
        assert "Price" in field_names
        assert "Stop Loss" in field_names
        assert "Target" in field_names
        assert "Quantity" in field_names
        assert "Confidence" in field_names
        assert "Reason" in field_names

    def test_sl_hit_embed_is_red(self):
        am = AlertManager(webhook_url="https://discord.com/webhook")
        alert = AlertMessage(
            alert_type=AlertType.SL_HIT,
            symbol="TCS",
            price=3400.0,
            pnl=-5000.0,
        )
        embed = am._build_embed(alert)
        assert embed["color"] == 0xFF0000
        assert "P&L" in [f["name"] for f in embed["fields"]]

    def test_kill_switch_embed(self):
        am = AlertManager(webhook_url="https://discord.com/webhook")
        alert = AlertMessage(
            alert_type=AlertType.KILL_SWITCH,
            symbol="SYSTEM",
            reason="3 consecutive losses",
        )
        embed = am._build_embed(alert)
        assert embed["color"] == 0xFF0000
        assert "KILL_SWITCH" in embed["title"]

    def test_custom_extra_fields(self):
        am = AlertManager(webhook_url="https://discord.com/webhook")
        alert = AlertMessage(
            alert_type=AlertType.ENTRY,
            symbol="TCS",
            price=100.0,
            extra_fields={"RSI": "32.5", "Supertrend": "UP"},
        )
        embed = am._build_embed(alert)
        field_names = [f["name"] for f in embed["fields"]]
        assert "RSI" in field_names
        assert "Supertrend" in field_names


# =====================================================================
# Send without webhook (graceful degradation)
# =====================================================================

class TestSendWithoutWebhook:
    @pytest.mark.asyncio
    async def test_send_without_webhook_returns_true(self):
        am = AlertManager(webhook_url=None, enabled=True)
        result = await am.send_entry_alert(
            symbol="TCS", price=100.0, stop_loss=90.0, target=110.0, quantity=10
        )
        assert result is True

    @pytest.mark.asyncio
    async def test_send_when_disabled_returns_true(self):
        am = AlertManager(webhook_url="https://discord.com/webhook", enabled=False)
        result = await am.send_entry_alert(
            symbol="TCS", price=100.0, stop_loss=90.0, target=110.0, quantity=10
        )
        assert result is True


# =====================================================================
# Metrics
# =====================================================================

class TestMetrics:
    def test_initial_metrics(self):
        am = AlertManager(webhook_url="https://discord.com/webhook")
        metrics = am.get_metrics()
        assert metrics["sent"] == 0
        assert metrics["errors"] == 0
        assert metrics["configured"] is True

    def test_metrics_not_configured(self):
        am = AlertManager(webhook_url=None)
        metrics = am.get_metrics()
        assert metrics["configured"] is False


# =====================================================================
# Convenience methods (no network = should not crash)
# =====================================================================

class TestConvenienceMethods:
    @pytest.mark.asyncio
    async def test_all_convenience_methods_without_webhook(self):
        am = AlertManager(webhook_url=None)

        assert await am.send_entry_alert("TCS", 100.0, 90.0, 110.0, 10)
        assert await am.send_exit_alert("TCS", 105.0, 10, pnl=50.0)
        assert await am.send_sl_hit_alert("TCS", 89.0, pnl=-110.0)
        assert await am.send_daily_summary(
            "2026-01-15", total_pnl=500.0, trades_count=3,
            win_rate=0.67, avg_pnl=166.67, max_drawdown=50.0
        )
        assert await am.send_kill_switch_alert("3 consecutive losses")
