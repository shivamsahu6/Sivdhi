"""Unit tests for Sivdhi date/time booking rules (no Flask required)."""
import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from utils.booking_schedule import minimum_booking_date, validate_customer_booking_slot

IST = ZoneInfo("Asia/Kolkata")


class BookingScheduleTests(unittest.TestCase):
    def test_full_day_rules_remain_unchanged(self):
        now = datetime(2026, 10, 12, 6, 30, tzinfo=IST)
        self.assertEqual(
            validate_customer_booking_slot("full-day", "2026-10-12", "", now),
            ("2026-10-12", "09:00", None),
        )
        self.assertEqual(minimum_booking_date("full-day", now), "2026-10-12")
        after_seven = datetime(2026, 10, 12, 7, 0, 1, tzinfo=IST)
        self.assertEqual(minimum_booking_date("full-day", after_seven), "2026-10-13")
        self.assertIn(
            "tomorrow",
            validate_customer_booking_slot("full-day", "2026-10-12", "", after_seven)[2].lower(),
        )

    def test_blank_same_day_time_defaults_to_two_hours_ahead(self):
        now = datetime(2026, 10, 12, 10, 0, tzinfo=IST)
        self.assertEqual(
            validate_customer_booking_slot("normal", "2026-10-12", "", now),
            ("2026-10-12", "12:00", None),
        )

    def test_blank_same_day_time_rounds_up_to_next_quarter_hour(self):
        now = datetime(2026, 10, 12, 10, 7, tzinfo=IST)
        self.assertEqual(
            validate_customer_booking_slot("normal", "2026-10-12", "", now),
            ("2026-10-12", "12:15", None),
        )
        now_with_seconds = datetime(2026, 10, 12, 10, 0, 30, tzinfo=IST)
        self.assertEqual(
            validate_customer_booking_slot("normal", "2026-10-12", "", now_with_seconds),
            ("2026-10-12", "12:15", None),
        )

    def test_blank_same_day_time_at_four_pm_defaults_to_six_pm(self):
        now = datetime(2026, 10, 12, 16, 0, tzinfo=IST)
        self.assertEqual(
            validate_customer_booking_slot("normal", "2026-10-12", "", now),
            ("2026-10-12", "18:00", None),
        )
        self.assertEqual(minimum_booking_date("normal", now), "2026-10-12")

    def test_blank_same_day_time_after_four_pm_rolls_to_tomorrow_nine_am(self):
        now = datetime(2026, 10, 12, 16, 1, tzinfo=IST)
        self.assertEqual(
            validate_customer_booking_slot("normal", "2026-10-12", "", now),
            ("2026-10-13", "09:00", None),
        )
        self.assertEqual(minimum_booking_date("normal", now), "2026-10-13")

    def test_blank_future_date_defaults_to_nine_am(self):
        now = datetime(2026, 10, 12, 15, 0, tzinfo=IST)
        self.assertEqual(
            validate_customer_booking_slot("normal", "2026-10-15", "", now),
            ("2026-10-15", "09:00", None),
        )

    def test_normal_selected_times_are_limited_to_nine_to_six(self):
        now = datetime(2026, 10, 12, 8, 0, tzinfo=IST)
        self.assertIsNone(validate_customer_booking_slot("normal", "2026-10-13", "09:00", now)[2])
        self.assertIsNone(validate_customer_booking_slot("normal", "2026-10-13", "18:00", now)[2])
        self.assertIn("9:00 AM", validate_customer_booking_slot("normal", "2026-10-13", "08:45", now)[2])
        self.assertIn("9:00 AM", validate_customer_booking_slot("normal", "2026-10-13", "18:15", now)[2])

    def test_same_day_manual_time_still_needs_two_hours_notice(self):
        now = datetime(2026, 10, 12, 15, 0, tzinfo=IST)
        self.assertIsNone(validate_customer_booking_slot("normal", "2026-10-12", "17:00", now)[2])
        self.assertIn("2 hours", validate_customer_booking_slot("normal", "2026-10-12", "16:45", now)[2])

    def test_manual_same_day_time_after_four_pm_is_rejected(self):
        now = datetime(2026, 10, 12, 16, 1, tzinfo=IST)
        self.assertIn(
            "tomorrow",
            validate_customer_booking_slot("normal", "2026-10-12", "18:00", now)[2].lower(),
        )

    def test_quarter_hour_intervals_and_past_dates_are_enforced(self):
        now = datetime(2026, 10, 12, 10, 0, tzinfo=IST)
        self.assertIn(
            "15-minute",
            validate_customer_booking_slot("normal", "2026-10-13", "09:10", now)[2],
        )
        self.assertIn(
            "Past dates",
            validate_customer_booking_slot("normal", "2026-10-11", "12:00", now)[2],
        )


if __name__ == "__main__":
    unittest.main()
