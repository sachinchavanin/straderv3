"""AlertManager — Discord webhook alerting with rich embeds and rate limiting.

Sends formatted trading alerts to a Discord channel via webhook.
Supports multiple alert types (entry, exit, SL hit, daily summary, kill switch)
with embed formatting and per-symbol rate limiting.
"""

import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

import aiohttp
import structlog

logger = structlog.get_logger(__name__)


class AlertType(StrEnum):
    ENTRY = "ENTRY"
    EXIT = "EXIT"
    SL_HIT = "SL_HIT"
    TARGET_HIT = "TARGET_HIT"
    DAILY_SUMMARY = "DAILY_SUMMARY"
    KILL_SWITCH = "KILL_SWITCH"
    RISK_MODE_CHANGE = "RISK_MODE_CHANGE"
    PNL_THRESHOLD = "PNL_THRESHOLD"


# Discord embed colour mapping per alert type
_ALERT_COLOURS: dict[AlertType, int] = {
    AlertType.ENTRY: 0x00FF00,
    AlertType.EXIT: 0x3498DB,
    AlertType.SL_HIT: 0xFF0000,
    AlertType.TARGET_HIT: 0x00FF00,
    AlertType.DAILY_SUMMARY: 0x9B59B6,
    AlertType.KILL_SWITCH: 0xFF0000,
    AlertType.RISK_MODE_CHANGE: 0xFFA500,
    AlertType.PNL_THRESHOLD: 0xFFFF00,
}


@dataclass(slots=True)
class AlertMessage:
    """Single alert payload to be sent to Discord."""

    alert_type: AlertType
    symbol: str
    price: float = 0.0
    stop_loss: float = 0.0
    target: float = 0.0
    quantity: int = 0
    confidence: float = 0.0
    reason: str = ""
    pnl: float = 0.0
    extra_fields: dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=datetime.now)


class AlertManager:
    """Sends trading alerts to Discord via webhook with rate limiting.

    Features:
        - Rich Discord embeds with colour-coded alert types
        - Per-symbol rate limiting (configurable cooldown)
        - Async HTTP via aiohttp
        - Graceful degradation on network failure
    """

    def __init__(
        self,
        webhook_url: str | None = None,
        enabled: bool = True,
        rate_limit_seconds: int = 60,
    ) -> None:
        self._webhook_url = webhook_url
        self._enabled = enabled and webhook_url is not None
        self._rate_limit_seconds = rate_limit_seconds
        self._last_alert_time: dict[str, float] = {}
        self._session: aiohttp.ClientSession | None = None
        self._send_count: int = 0
        self._error_count: int = 0

    @property
    def is_configured(self) -> bool:
        """Return True if webhook URL is set and enabled."""
        return self._enabled and self._webhook_url is not None

    async def _get_session(self) -> aiohttp.ClientSession:
        """Lazy-init aiohttp session."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=10),
            )
        return self._session

    def _is_rate_limited(self, symbol: str) -> bool:
        """Check if an alert for this symbol is within the rate-limit window."""
        now = time.monotonic()
        last = self._last_alert_time.get(symbol)
        if last is not None and (now - last) < self._rate_limit_seconds:
            return True
        return False

    def _record_alert(self, symbol: str) -> None:
        """Record that we sent an alert for this symbol."""
        self._last_alert_time[symbol] = time.monotonic()

    def _build_embed(self, alert: AlertMessage) -> dict:
        """Build a Discord embed dict from an AlertMessage."""
        colour = _ALERT_COLOURS.get(alert.alert_type, 0x808080)

        if alert.alert_type in (AlertType.ENTRY, AlertType.TARGET_HIT):
            emoji = "[GREEN]"
        elif alert.alert_type in (AlertType.SL_HIT, AlertType.KILL_SWITCH):
            emoji = "[RED]"
        else:
            emoji = "[BLUE]"

        title = f"{emoji} {alert.alert_type.value} -- {alert.symbol}"

        fields: list[dict[str, Any]] = []

        if alert.price > 0:
            fields.append({"name": "Price", "value": f"Rs.{alert.price:,.2f}", "inline": True})

        if alert.quantity > 0:
            fields.append({"name": "Quantity", "value": str(alert.quantity), "inline": True})

        if alert.stop_loss > 0:
            fields.append({"name": "Stop Loss", "value": f"Rs.{alert.stop_loss:,.2f}", "inline": True})

        if alert.target > 0:
            fields.append({"name": "Target", "value": f"Rs.{alert.target:,.2f}", "inline": True})

        if alert.confidence > 0:
            fields.append({"name": "Confidence", "value": f"{alert.confidence:.1%}", "inline": True})

        if alert.pnl != 0:
            sign = "+" if alert.pnl > 0 else ""
            fields.append({"name": "P&L", "value": f"Rs.{sign}{alert.pnl:,.2f}", "inline": True})

        if alert.reason:
            fields.append({"name": "Reason", "value": alert.reason, "inline": False})

        for key, value in alert.extra_fields.items():
            fields.append({"name": key, "value": str(value), "inline": True})

        embed: dict[str, Any] = {
            "title": title,
            "color": colour,
            "fields": fields,
            "timestamp": alert.timestamp.isoformat(),
            "footer": {"text": "straderv3"},
        }

        return embed

    async def send_alert(self, alert: AlertMessage) -> bool:
        """Send an alert to Discord.

        Returns True if the alert was sent (or skipped due to rate limit).
        Returns False on error.
        """
        if not self.is_configured:
            logger.debug("alert_manager.not_configured", alert_type=alert.alert_type.value)
            return True

        if self._is_rate_limited(alert.symbol):
            logger.debug(
                "alert_manager.rate_limited",
                symbol=alert.symbol,
                alert_type=alert.alert_type.value,
            )
            return True

        self._record_alert(alert.symbol)
        return await self._send_to_discord(alert)

    async def send_entry_alert(
        self,
        symbol: str,
        price: float,
        stop_loss: float,
        target: float,
        quantity: int,
        confidence: float = 0.0,
        reason: str = "",
    ) -> bool:
        """Convenience: send ENTRY alert."""
        return await self.send_alert(AlertMessage(
            alert_type=AlertType.ENTRY,
            symbol=symbol,
            price=price,
            stop_loss=stop_loss,
            target=target,
            quantity=quantity,
            confidence=confidence,
            reason=reason,
        ))

    async def send_exit_alert(
        self,
        symbol: str,
        price: float,
        quantity: int,
        pnl: float = 0.0,
        reason: str = "",
    ) -> bool:
        """Convenience: send EXIT alert."""
        return await self.send_alert(AlertMessage(
            alert_type=AlertType.EXIT,
            symbol=symbol,
            price=price,
            quantity=quantity,
            pnl=pnl,
            reason=reason,
        ))

    async def send_sl_hit_alert(
        self,
        symbol: str,
        price: float,
        pnl: float = 0.0,
    ) -> bool:
        """Convenience: send SL_HIT alert."""
        return await self.send_alert(AlertMessage(
            alert_type=AlertType.SL_HIT,
            symbol=symbol,
            price=price,
            pnl=pnl,
            reason="Stop-loss triggered",
        ))

    async def send_daily_summary(
        self,
        date: str,
        total_pnl: float,
        trades_count: int,
        win_rate: float,
        avg_pnl: float,
        max_drawdown: float,
        extra: dict[str, Any] | None = None,
    ) -> bool:
        """Convenience: send DAILY_SUMMARY alert."""
        extra_fields = {
            "Avg P&L": f"Rs.{avg_pnl:,.2f}",
            "Max Drawdown": f"Rs.{max_drawdown:,.2f}",
        }
        if extra:
            extra_fields.update(extra)

        return await self.send_alert(AlertMessage(
            alert_type=AlertType.DAILY_SUMMARY,
            symbol="PORTFOLIO",
            pnl=total_pnl,
            extra_fields=extra_fields,
            reason=f"Daily Summary -- {date} | Trades: {trades_count} | Win Rate: {win_rate:.1%}",
        ))

    async def send_kill_switch_alert(
        self,
        reason: str,
        daily_pnl: float = 0.0,
    ) -> bool:
        """Convenience: send KILL_SWITCH alert."""
        return await self.send_alert(AlertMessage(
            alert_type=AlertType.KILL_SWITCH,
            symbol="SYSTEM",
            pnl=daily_pnl,
            reason=f"KILL SWITCH TRIGGERED: {reason}",
        ))

    async def _send_to_discord(self, alert: AlertMessage) -> bool:
        """Send the embed payload to the Discord webhook."""
        embed = self._build_embed(alert)
        payload = {"embeds": [embed]}

        try:
            session = await self._get_session()
            async with session.post(self._webhook_url, json=payload) as resp:
                if resp.status in (200, 204):
                    self._send_count += 1
                    logger.info(
                        "alert_manager.sent",
                        alert_type=alert.alert_type.value,
                        symbol=alert.symbol,
                    )
                    return True
                else:
                    body = await resp.text()
                    self._error_count += 1
                    logger.error(
                        "alert_manager.discord_error",
                        status=resp.status,
                        body=body,
                    )
                    return False
        except aiohttp.ClientError as exc:
            self._error_count += 1
            logger.error("alert_manager.http_error", error=str(exc))
            return False
        except Exception:
            self._error_count += 1
            logger.exception("alert_manager.unexpected_error")
            return False

    async def close(self) -> None:
        """Close the aiohttp session."""
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    def get_metrics(self) -> dict[str, Any]:
        """Return send/error counters."""
        return {
            "sent": self._send_count,
            "errors": self._error_count,
            "configured": self.is_configured,
        }
