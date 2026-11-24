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
        self.assertEqual(
            validate_customer_booking_slot("full-day", "2026-10-12", "", now),
            ("2026-10-12", "09:00", None),
        )
        self.assertEqual(minimum_booking_date("full-day", now), "2026-10-12")

    def test_full_day_after_seven_starts_tomorrow(self):
        now = datetime(2026, 10, 12, 7, 0, 1, tzinfo=IST)
        _, _, error = validate_customer_booking_slot("full-day", "2026-10-12", "", now)
        self.assertIn("tomorrow", error.lower())
        self.assertEqual(minimum_booking_date("full-day", now), "2026-10-13")
        self.assertEqual(
            validate_customer_booking_slot("full-day", "2026-10-13", "", now),
            ("2026-10-13", "09:00", None),
        )

    def test_full_day_at_exactly_seven_is_still_same_day_eligible(self):
        now = datetime(2026, 10, 12, 7, 0, tzinfo=IST)
        self.assertEqual(
            validate_customer_booking_slot("full-day", "2026-10-12", "", now),
            ("2026-10-12", "09:00", None),
        )

    def test_empty_time_defaults_two_hours_ahead(self):
        now = datetime(2026, 10, 12, 10, 0, tzinfo=IST)
        self.assertEqual(
            validate_customer_booking_slot("normal", "2026-10-12", "", now),
            ("2026-10-12", "12:00", None),
        )

    def test_future_empty_time_defaults_to_nine_on_selected_date(self):
        for now in (
            datetime(2026, 10, 12, 10, 0, tzinfo=IST),
            datetime(2026, 10, 12, 17, 30, tzinfo=IST),
        ):
            with self.subTest(now=now):
                self.assertEqual(
                    validate_customer_booking_slot(
                        "normal", "2026-10-20", "", now
                    ),
                    ("2026-10-20", "09:00", None),
                )

    def test_empty_time_rounds_up_to_next_quarter_hour(self):
        now = datetime(2026, 10, 12, 10, 7, 30, tzinfo=IST)
        self.assertEqual(
            validate_customer_booking_slot("normal", "2026-10-12", "", now),
            ("2026-10-12", "12:15", None),
        )

    def test_empty_time_uses_nine_am_as_earliest_slot(self):
        now = datetime(2026, 10, 12, 6, 30, tzinfo=IST)
        self.assertEqual(
            validate_customer_booking_slot("normal", "2026-10-12", "", now),
            ("2026-10-12", "09:00", None),
        )

    def test_exactly_four_pm_defaults_to_six_pm(self):
        now = datetime(2026, 10, 12, 16, 0, tzinfo=IST)
        self.assertEqual(
            validate_customer_booking_slot("normal", "2026-10-12", "", now),
            ("2026-10-12", "18:00", None),
        )

    def test_after_four_pm_future_date_stays_selected_and_defaults_to_nine(self):
        now = datetime(2026, 10, 12, 16, 0, 1, tzinfo=IST)
        self.assertEqual(
            validate_customer_booking_slot("normal", "2026-10-20", "", now),
            ("2026-10-20", "09:00", None),
        )

    def test_after_four_pm_same_day_blank_time_requires_later_date(self):
        now = datetime(2026, 10, 12, 17, 30, tzinfo=IST)
        selected_date, selected_time, error = validate_customer_booking_slot(
            "normal", "2026-10-12", "", now
        )
        self.assertIsNone(selected_date)
        self.assertIsNone(selected_time)
        self.assertIn("later service date", error.lower())

    def test_explicit_future_time_is_preserved_after_four_pm(self):
        now = datetime(2026, 10, 12, 17, 30, tzinfo=IST)
        self.assertEqual(
            validate_customer_booking_slot("normal", "2026-10-20", "10:30", now),
            ("2026-10-20", "10:30", None),
        )

    def test_explicit_times_accept_nine_am_and_six_pm(self):
        now = datetime(2026, 10, 12, 10, 0, tzinfo=IST)
        for selected_time in ("09:00", "18:00"):
            with self.subTest(selected_time=selected_time):
                self.assertIsNone(
                    validate_customer_booking_slot(
                        "normal", "2026-10-13", selected_time, now
                    )[2]
                )

    def test_explicit_times_reject_outside_nine_to_six(self):
        now = datetime(2026, 10, 12, 10, 0, tzinfo=IST)
        for selected_time in ("08:45", "18:15"):
            with self.subTest(selected_time=selected_time):
                _, _, error = validate_customer_booking_slot(
                    "normal", "2026-10-13", selected_time, now
                )
                self.assertIn("9:00 AM to 6:00 PM", error)

    def test_explicit_times_require_fifteen_minute_intervals(self):
        now = datetime(2026, 10, 12, 10, 0, tzinfo=IST)
        _, _, error = validate_customer_booking_slot(
            "normal", "2026-10-13", "10:10", now
        )
        self.assertIn("15-minute", error)

    def test_explicit_same_day_time_must_be_two_hours_ahead(self):
        now = datetime(2026, 10, 12, 16, 0, tzinfo=IST)
        self.assertIsNone(
            validate_customer_booking_slot("normal", "2026-10-12", "18:00", now)[2]
        )
        _, _, error = validate_customer_booking_slot(
            "normal", "2026-10-12", "17:45", now
        )
        self.assertIn("2 hours", error)

    def test_past_dates_are_rejected(self):
        now = datetime(2026, 10, 12, 10, 0, tzinfo=IST)
        _, _, error = validate_customer_booking_slot(
            "normal", "2026-10-11", "12:00", now
        )
        self.assertIn("Past dates", error)

    def test_normal_same_day_closes_at_six_pm(self):
        now = datetime(2026, 10, 12, 18, 0, tzinfo=IST)
        self.assertEqual(minimum_booking_date("normal", now), "2026-10-13")
        _, _, error = validate_customer_booking_slot(
            "normal", "2026-10-12", "", now
        )
        self.assertIn("closed for today", error.lower())

    def test_normal_minimum_date_is_today_before_six_pm(self):
        now = datetime(2026, 10, 12, 17, 59, tzinfo=IST)
        self.assertEqual(minimum_booking_date("normal", now), "2026-10-12")


    def test_checkout_revalidation_does_not_restart_two_hour_lead_time(self):
        now = datetime(2026, 10, 12, 15, 10, tzinfo=IST)
        self.assertEqual(
            validate_customer_booking_slot(
                "normal", "2026-10-12", "17:00",
                now=now, enforce_lead_time=False,
            ),
            ("2026-10-12", "17:00", None),
        )

    def test_checkout_revalidation_rejects_a_same_day_slot_that_has_passed(self):
        now = datetime(2026, 10, 12, 15, 10, tzinfo=IST)
        _, _, error = validate_customer_booking_slot(
            "normal", "2026-10-12", "15:00",
            now=now, enforce_lead_time=False,
        )
        self.assertIn("already passed", error.lower())


if __name__ == "__main__":
    unittest.main()
