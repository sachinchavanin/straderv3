"""Tests for MarketPhaseChecker — NSE market phases and time guards."""

from datetime import date, time, datetime

import pytest

from strader3.notifier.market_phase import MarketPhase, MarketPhaseChecker, MarketStatus


class TestMarketPhaseCheckerInit:
    def test_default_holidays(self):
        holidays = ["2026-01-26", "2026-10-02", "2026-12-25"]
        mpc = MarketPhaseChecker(holidays=holidays)
        assert mpc.is_holiday(date(2026, 1, 26))
        assert mpc.is_holiday(date(2026, 10, 2))
        assert not mpc.is_holiday(date(2026, 3, 15))

    def test_custom_time_guards(self):
        mpc = MarketPhaseChecker(
            entry_start="09:30",
            entry_end="14:45",
            force_exit_time="15:20",
        )
        assert mpc._time_guards.entry_start == time(9, 30)
        assert mpc._time_guards.entry_end == time(14, 45)
        assert mpc._time_guards.force_exit_time == time(15, 20)

    def test_invalid_holiday_skipped(self):
        mpc = MarketPhaseChecker(holidays=["2026-01-26", "not-a-date"])
        assert mpc.is_holiday(date(2026, 1, 26))
        assert len(mpc._holidays) == 1


class TestHolidayManagement:
    def test_add_holiday(self):
        mpc = MarketPhaseChecker()
        mpc.add_holiday("2026-06-15")
        assert mpc.is_holiday(date(2026, 6, 15))

    def test_remove_holiday(self):
        mpc = MarketPhaseChecker(holidays=["2026-06-15"])
        mpc.remove_holiday("2026-06-15")
        assert not mpc.is_holiday(date(2026, 6, 15))

    def test_remove_nonexistent_holiday(self):
        mpc = MarketPhaseChecker()
        mpc.remove_holiday("2026-06-15")  # Should not raise


class TestWeekendDetection:
    def test_saturday_is_weekend(self):
        mpc = MarketPhaseChecker()
        assert mpc.is_weekend(date(2026, 6, 20))  # Saturday

    def test_sunday_is_weekend(self):
        mpc = MarketPhaseChecker()
        assert mpc.is_weekend(date(2026, 6, 21))  # Sunday

    def test_monday_is_not_weekend(self):
        mpc = MarketPhaseChecker()
        assert not mpc.is_weekend(date(2026, 6, 22))  # Monday


class TestTradingDay:
    def test_weekend_not_trading_day(self):
        mpc = MarketPhaseChecker()
        assert not mpc.is_trading_day(date(2026, 6, 20))  # Saturday

    def test_holiday_not_trading_day(self):
        mpc = MarketPhaseChecker(holidays=["2026-06-22"])
        assert not mpc.is_trading_day(date(2026, 6, 22))  # Monday but holiday

    def test_normal_day_is_trading_day(self):
        mpc = MarketPhaseChecker()
        assert mpc.is_trading_day(date(2026, 6, 23))  # Tuesday


class TestMarketPhase:
    def test_pre_open_phase(self):
        mpc = MarketPhaseChecker()
        assert mpc.get_phase(time(9, 5)) == MarketPhase.PRE_OPEN

    def test_open_auction_phase(self):
        mpc = MarketPhaseChecker()
        assert mpc.get_phase(time(9, 10)) == MarketPhase.OPEN_AUCTION

    def test_normal_phase(self):
        mpc = MarketPhaseChecker()
        assert mpc.get_phase(time(10, 0)) == MarketPhase.NORMAL
        assert mpc.get_phase(time(12, 0)) == MarketPhase.NORMAL
        assert mpc.get_phase(time(15, 0)) == MarketPhase.NORMAL

    def test_post_close_phase(self):
        mpc = MarketPhaseChecker()
        assert mpc.get_phase(time(15, 35)) == MarketPhase.POST_CLOSE

    def test_closed_phase(self):
        mpc = MarketPhaseChecker()
        assert mpc.get_phase(time(8, 0)) == MarketPhase.CLOSED
        assert mpc.get_phase(time(18, 0)) == MarketPhase.CLOSED

    def test_holiday_phase(self):
        mpc = MarketPhaseChecker(holidays=["2026-01-26"])
        from datetime import date; assert mpc.get_phase(time(10, 0), date(2026, 1, 26)) == MarketPhase.HOLIDAY

    def test_weekend_phase(self):
        mpc = MarketPhaseChecker()
        saturday = date(2026, 6, 20)
        assert mpc.get_phase(time(10, 0)) != MarketPhase.HOLIDAY  # Today might not be weekend
        # Test with explicit weekend date
        assert mpc.is_weekend(saturday)


class TestCanEnterPosition:
    def test_can_enter_during_normal_hours(self):
        mpc = MarketPhaseChecker()
        can_enter, reason = mpc.can_enter_position(time(10, 0))
        assert can_enter is True

    def test_cannot_enter_before_entry_start(self):
        mpc = MarketPhaseChecker(entry_start="09:20")
        can_enter, reason = mpc.can_enter_position(time(9, 15))
        assert can_enter is False
        assert "Too early" in reason

    def test_cannot_enter_after_entry_end(self):
        mpc = MarketPhaseChecker(entry_end="15:00")
        can_enter, reason = mpc.can_enter_position(time(15, 5))
        assert can_enter is False
        assert "Too late" in reason

    def test_cannot_enter_on_holiday(self):
        mpc = MarketPhaseChecker(holidays=["2026-01-26"])
        from datetime import date; can_enter, reason = mpc.can_enter_position(time(10, 0), date(2026, 1, 26))
        assert can_enter is False
        assert "Not a trading day" in reason

    def test_cannot_enter_in_pre_open(self):
        mpc = MarketPhaseChecker()
        can_enter, reason = mpc.can_enter_position(time(9, 5))
        assert can_enter is False
        assert "PRE_OPEN" in reason


class TestForceExit:
    def test_force_exit_not_active(self):
        mpc = MarketPhaseChecker(force_exit_time="15:15")
        assert not mpc.is_force_exit_time(time(14, 0))

    def test_force_exit_active(self):
        mpc = MarketPhaseChecker(force_exit_time="15:15")
        assert mpc.is_force_exit_time(time(15, 20))

    def test_force_exit_at_exact_time(self):
        mpc = MarketPhaseChecker(force_exit_time="15:15")
        assert mpc.is_force_exit_time(time(15, 15))


class TestGetStatus:
    def test_status_normal(self):
        mpc = MarketPhaseChecker()
        status = mpc.get_status(time(10, 0))
        assert isinstance(status, MarketStatus)
        assert status.phase == MarketPhase.NORMAL
        assert status.can_enter is True
        assert status.force_exit_active is False

    def test_status_force_exit(self):
        mpc = MarketPhaseChecker(force_exit_time="15:15")
        status = mpc.get_status(time(15, 20))
        assert status.force_exit_active is True
        assert "FORCE EXIT" in status.message


class TestNextTradingDay:
    def test_next_trading_day_from_weekday(self):
        mpc = MarketPhaseChecker()
        # Monday -> Tuesday
        result = mpc.next_trading_day(date(2026, 6, 22))
        assert result == date(2026, 6, 23)

    def test_next_trading_day_skips_weekend(self):
        mpc = MarketPhaseChecker()
        # Friday -> Monday
        result = mpc.next_trading_day(date(2026, 6, 26))
        assert result == date(2026, 6, 29)

    def test_next_trading_day_skips_holiday(self):
        mpc = MarketPhaseChecker(holidays=["2026-06-23", "2026-06-24"])
        # Monday -> skips Tue+Wed -> Thursday
        result = mpc.next_trading_day(date(2026, 6, 22))
        assert result == date(2026, 6, 25)


class TestGetConfig:
    def test_config_output(self):
        mpc = MarketPhaseChecker(
            holidays=["2026-01-26"],
            entry_start="09:20",
            force_exit_time="15:15",
        )
        cfg = mpc.get_config()
        assert "2026-01-26" in cfg["holidays"]
        assert cfg["entry_start"] == "09:20:00"
        assert cfg["force_exit_time"] == "15:15:00"
