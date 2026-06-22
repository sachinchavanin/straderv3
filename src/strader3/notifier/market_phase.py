"""Market phase awareness — NSE market phases, time guards, and holiday calendar.

NSE trading day phases:
    - PRE_OPEN:      09:00 - 09:08 (order collection)
    - OPEN_AUCTION:  09:08 - 09:12 (price discovery)
    - NORMAL:        09:15 - 15:30 (continuous trading)
    - POST_CLOSE:    15:30 - 15:40 (closing price determination)
    - CLOSED:        Outside trading hours

Time guards:
    - No new entries before 09:20 (after opening volatility settles)
    - Force exit all positions by 15:15 (before market close)
    - No trading on NSE holidays
"""

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from enum import StrEnum
from typing import Any

import structlog

logger = structlog.get_logger(__name__)


class MarketPhase(StrEnum):
    """NSE market phases within a trading day."""

    PRE_OPEN = "PRE_OPEN"
    OPEN_AUCTION = "OPEN_AUCTION"
    NORMAL = "NORMAL"
    POST_CLOSE = "POST_CLOSE"
    CLOSED = "CLOSED"
    HOLIDAY = "HOLIDAY"


# NSE standard times
_NSE_MARKET_OPEN = time(9, 15)
_NSE_MARKET_CLOSE = time(15, 30)
_NSE_PRE_OPEN_START = time(9, 0)
_NSE_OPEN_AUCTION_START = time(9, 8)
_NSE_OPEN_AUCTION_START = time(9, 8)
_NSE_OPEN_AUCTION_END = time(9, 12)
_NSE_POST_CLOSE_END = time(15, 40)


@dataclass(frozen=True)
class TimeGuardConfig:
    """Time guard configuration for trading restrictions."""

    entry_start: time          # Earliest time to enter new positions
    entry_end: time            # Latest time to enter new positions
    force_exit_time: time      # Time to force-exit all positions
    market_open: time          # Market open time
    market_close: time         # Market close time


@dataclass(frozen=True)
class MarketStatus:
    """Current market status snapshot."""

    phase: MarketPhase
    is_trading_day: bool
    can_enter: bool
    can_exit: bool
    force_exit_active: bool
    current_time: time
    message: str


class MarketPhaseChecker:
    """Determines NSE market phase and enforces time guards.

    Features:
        - Phase detection (pre-open, normal, post-close, closed)
        - Time guards (no entries before entry_start, force exit by force_exit_time)
        - Holiday calendar integration
        - Weekend detection (Saturday/Sunday)
    """

    def __init__(
        self,
        holidays: list[str] | None = None,
        entry_start: str = "09:20",
        entry_end: str = "15:00",
        force_exit_time: str = "15:15",
        market_open: str = "09:15",
        market_close: str = "15:30",
    ) -> None:
        self._holidays: set[date] = set()
        if holidays:
            for h in holidays:
                try:
                    self._holidays.add(date.fromisoformat(h))
                except ValueError:
                    logger.warning("market_phase.invalid_holiday", holiday=h)

        self._time_guards = TimeGuardConfig(
            entry_start=self._parse_time(entry_start),
            entry_end=self._parse_time(entry_end),
            force_exit_time=self._parse_time(force_exit_time),
            market_open=self._parse_time(market_open),
            market_close=self._parse_time(market_close),
        )

    @staticmethod
    def _parse_time(time_str: str) -> time:
        """Parse HH:MM string to time object."""
        parts = time_str.split(":")
        return time(int(parts[0]), int(parts[1]))

    def add_holiday(self, holiday_date: str) -> None:
        """Add a holiday to the calendar."""
        try:
            self._holidays.add(date.fromisoformat(holiday_date))
        except ValueError:
            logger.warning("market_phase.invalid_holiday", holiday=holiday_date)

    def remove_holiday(self, holiday_date: str) -> None:
        """Remove a holiday from the calendar."""
        try:
            self._holidays.discard(date.fromisoformat(holiday_date))
        except ValueError:
            pass

    def is_holiday(self, check_date: date | None = None) -> bool:
        """Check if a date is an NSE holiday."""
        if check_date is None:
            check_date = date.today()
        return check_date in self._holidays

    def is_weekend(self, check_date: date | None = None) -> bool:
        """Check if a date is a weekend (Saturday=5, Sunday=6)."""
        if check_date is None:
            check_date = date.today()
        return check_date.weekday() >= 5

    def is_trading_day(self, check_date: date | None = None) -> bool:
        """Check if a date is a trading day (not weekend, not holiday)."""
        if check_date is None:
            check_date = date.today()
        return not self.is_weekend(check_date) and not self.is_holiday(check_date)

    def get_phase(self, check_time: time | None = None, check_date: date | None = None) -> MarketPhase:
        """Determine the current NSE market phase.

        Args:
            check_time: Time to check. Defaults to current time.
            check_date: Date to check. Defaults to today.

        Returns:
            MarketPhase enum value.
        """
        if not self.is_trading_day(check_date):
            return MarketPhase.HOLIDAY

        if check_time is None:
            check_time = datetime.now().time()

        tg = self._time_guards

        if check_time < _NSE_OPEN_AUCTION_START:
            if check_time >= _NSE_PRE_OPEN_START:
                return MarketPhase.PRE_OPEN
            return MarketPhase.CLOSED
        elif check_time < _NSE_OPEN_AUCTION_END:
            return MarketPhase.OPEN_AUCTION
        elif check_time < tg.market_open:
            return MarketPhase.OPEN_AUCTION
        elif check_time <= tg.market_close:
            return MarketPhase.NORMAL
        elif check_time <= _NSE_POST_CLOSE_END:
            return MarketPhase.POST_CLOSE
        else:
            return MarketPhase.CLOSED

    def can_enter_position(self, check_time: time | None = None, check_date: date | None = None) -> tuple[bool, str]:
        """Check if new positions can be entered.

        Args:
            check_time: Time to check. Defaults to current time.
            check_date: Date to check. Defaults to today.

        Returns:
            (allowed, reason) tuple.
        """
        if not self.is_trading_day(check_date):
            return False, "Not a trading day (weekend or holiday)"

        phase = self.get_phase(check_time)

        if phase in (MarketPhase.PRE_OPEN, MarketPhase.OPEN_AUCTION):
            return False, f"Market in {phase.value} phase — entries not allowed"
        if phase in (MarketPhase.POST_CLOSE, MarketPhase.CLOSED):
            return False, f"Market {phase.value} — entries not allowed"

        if check_time is None:
            check_time = datetime.now().time()

        tg = self._time_guards

        if check_time < tg.entry_start:
            return False, f"Too early — entry window starts at {tg.entry_start}"
        if check_time > tg.entry_end:
            return False, f"Too late — entry window ended at {tg.entry_end}"

        return True, "Entry allowed"

    def is_force_exit_time(self, check_time: time | None = None) -> bool:
        """Check if force-exit time has been reached."""
        if check_time is None:
            check_time = datetime.now().time()
        return check_time >= self._time_guards.force_exit_time

    def get_status(self, check_time: time | None = None, check_date: date | None = None) -> MarketStatus:
        """Get a full market status snapshot."""
        if check_time is None:
            check_time = datetime.now().time()

        phase = self.get_phase(check_time, check_date)
        trading_day = self.is_trading_day(check_date)
        can_enter, reason = self.can_enter_position(check_time, check_date)
        force_exit = self.is_force_exit_time(check_time)

        if not trading_day:
            msg = "Market closed (weekend/holiday)"
        elif force_exit:
            msg = f"FORCE EXIT — past {self._time_guards.force_exit_time}"
        elif can_enter:
            msg = f"Normal trading — {phase.value}"
        else:
            msg = reason

        return MarketStatus(
            phase=phase,
            is_trading_day=trading_day,
            can_enter=can_enter,
            can_exit=True,  # Exits always allowed during trading hours
            force_exit_active=force_exit,
            current_time=check_time,
            message=msg,
        )

    def next_trading_day(self, from_date: date | None = None) -> date:
        """Find the next trading day from a given date."""
        if from_date is None:
            from_date = date.today()

        candidate = from_date + timedelta(days=1)
        while not self.is_trading_day(candidate):
            candidate += timedelta(days=1)
        return candidate

    def get_time_to_market_open(self) -> timedelta:
        """Get time remaining until market opens today."""
        now = datetime.now()
        market_open = datetime.combine(now.date(), self._time_guards.market_open)
        if now >= market_open:
            return timedelta(0)
        return market_open - now

    def get_time_to_force_exit(self) -> timedelta:
        """Get time remaining until force-exit time today."""
        now = datetime.now()
        force_exit = datetime.combine(now.date(), self._time_guards.force_exit_time)
        if now >= force_exit:
            return timedelta(0)
        return force_exit - now

    def get_config(self) -> dict[str, Any]:
        """Return current configuration as dict."""
        return {
            "holidays": sorted(h.isoformat() for h in self._holidays),
            "entry_start": str(self._time_guards.entry_start),
            "entry_end": str(self._time_guards.entry_end),
            "force_exit_time": str(self._time_guards.force_exit_time),
            "market_open": str(self._time_guards.market_open),
            "market_close": str(self._time_guards.market_close),
        }
