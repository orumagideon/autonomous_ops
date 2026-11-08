import unittest
from datetime import datetime, timezone

from src.scheduler_utils import get_previous_trading_day, get_target_day


class TradingDayTests(unittest.TestCase):
    def test_every_day_of_week(self):
        expected = (
            ("FRIDAY", "MONDAY"),
            ("MONDAY", "TUESDAY"),
            ("TUESDAY", "WEDNESDAY"),
            ("WEDNESDAY", "THURSDAY"),
            ("THURSDAY", "FRIDAY"),
            ("FRIDAY", "MONDAY"),
            ("FRIDAY", "MONDAY"),
        )
        for day, (previous_day, target_day) in enumerate(expected, start=2):
            with self.subTest(day=day):
                now = datetime(2026, 11, day)
                self.assertEqual(get_previous_trading_day(now), previous_day)
                self.assertEqual(get_target_day(now), target_day)

    def test_utc_instant_is_converted_to_configured_timezone(self):
        # Friday 21:00 UTC is Saturday midnight in Nairobi.
        now = datetime(2026, 11, 6, 21, tzinfo=timezone.utc)
        self.assertEqual(get_previous_trading_day(now), "FRIDAY")
        self.assertEqual(get_target_day(now), "MONDAY")
        self.assertEqual(get_previous_trading_day(now, "UTC"), "THURSDAY")
        self.assertEqual(get_target_day(now, "UTC"), "FRIDAY")

    def test_weekend_rollover_across_year_boundary(self):
        now = datetime(2022, 12, 31)
        self.assertEqual(get_previous_trading_day(now), "FRIDAY")
        self.assertEqual(get_target_day(now), "MONDAY")


if __name__ == "__main__":
    unittest.main()
