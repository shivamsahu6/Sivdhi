"""Unit tests for Sivdhi date/time booking rules (no Flask required)."""
import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from utils.booking_schedule import (
    minimum_booking_date,
    validate_customer_booking_slot,
)

IST = ZoneInfo("Asia/Kolkata")


class BookingScheduleTests(unittest.TestCase):
    def test_full_day_can_be_booked_for_today_before_seven(self):
        now = datetime(2026, 10, 12, 6, 30, tzinfo=IST)
        selected_date, selected_time, error = validate_customer_booking_slot(
            "full-day", "2026-10-12", "", now=now
        )
        self.assertIsNone(error)
        self.assertEqual(selected_date, "2026-10-12")
        self.assertEqual(selected_time, "09:00")
        self.assertEqual(minimum_booking_date("full-day", now), "2026-10-12")

    def test_full_day_after_seven_starts_tomorrow(self):
        now = datetime(2026, 10, 12, 7, 0, 1, tzinfo=IST)
        selected_date, selected_time, error = validate_customer_booking_slot(
            "full-day", "2026-10-12", "", now=now
        )
        self.assertIsNone(selected_date)
        self.assertIsNone(selected_time)
        self.assertIn("tomorrow", error.lower())
        self.assertEqual(minimum_booking_date("full-day", now), "2026-10-13")
        selected_date, selected_time, error = validate_customer_booking_slot(
            "full-day", "2026-10-13", "", now=now
        )
        self.assertIsNone(error)
        self.assertEqual((selected_date, selected_time), ("2026-10-13", "09:00"))

    def test_full_day_at_exactly_seven_is_still_same_day_eligible(self):
        now = datetime(2026, 10, 12, 7, 0, tzinfo=IST)
        selected_date, selected_time, error = validate_customer_booking_slot(
            "full-day", "2026-10-12", "", now=now
        )
        self.assertIsNone(error)
        self.assertEqual((selected_date, selected_time), ("2026-10-12", "09:00"))

    def test_normal_requires_two_hours_and_quarter_hour_slots(self):
        now = datetime(2026, 10, 12, 17, 0, tzinfo=IST)
        self.assertEqual(
            validate_customer_booking_slot("normal", "2026-10-12", "19:00", now)[2],
            None,
        )
        _, _, error = validate_customer_booking_slot(
            "normal", "2026-10-12", "18:45", now
        )
        self.assertIn("2 hours", error)
        _, _, error = validate_customer_booking_slot(
            "normal", "2026-10-12", "19:10", now
        )
        self.assertIn("15-minute", error)

    def test_normal_hours_and_past_dates(self):
        now = datetime(2026, 10, 12, 10, 0, tzinfo=IST)
        _, _, error = validate_customer_booking_slot("normal", "2026-10-12", "07:45", now)
        self.assertIn("8:00 AM", error)
        _, _, error = validate_customer_booking_slot("normal", "2026-10-12", "20:15", now)
        self.assertIn("8:00 AM", error)
        _, _, error = validate_customer_booking_slot("normal", "2026-10-11", "12:00", now)
        self.assertIn("Past dates", error)
        self.assertEqual(
            validate_customer_booking_slot("normal", "2026-10-13", "08:00", now)[2],
            None,
        )

    def test_normal_minimum_date_moves_to_tomorrow_at_six_pm(self):
        from utils.booking_schedule import minimum_booking_date
        now = datetime(2026, 10, 12, 18, 0, tzinfo=IST)
        self.assertEqual(minimum_booking_date("normal", now), "2026-10-13")
        _, _, error = validate_customer_booking_slot("normal", "2026-10-12", "20:00", now)
        self.assertIn("closed for today", error.lower())

    def test_normal_minimum_date_is_today_before_six_pm(self):
        from utils.booking_schedule import minimum_booking_date
        now = datetime(2026, 10, 12, 17, 59, tzinfo=IST)
        self.assertEqual(minimum_booking_date("normal", now), "2026-10-12")


if __name__ == "__main__":
    unittest.main()
