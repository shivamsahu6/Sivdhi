"""Server-side Sivdhi booking slot rules, separated for isolated unit tests."""

from datetime import datetime, date, time, timedelta
from zoneinfo import ZoneInfo


def india_now():
    try:
        return datetime.now(ZoneInfo("Asia/Kolkata"))
    except Exception:
        return datetime.now()


def normalise_booking_mode(value):
    value = str(value or "normal").strip().lower().replace("_", "-")
    return "full-day" if value in {"full-day", "fullday", "full day"} else "normal"


def minimum_booking_date(mode="normal", now=None):
    now = now or india_now()
    if normalise_booking_mode(mode) == "full-day":
        if now.time() > time(7, 0):
            return (now.date() + timedelta(days=1)).isoformat()
        return now.date().isoformat()
    if now.time() >= time(18, 0):
        return (now.date() + timedelta(days=1)).isoformat()
    return now.date().isoformat()


def _round_up_to_quarter_hour(value):
    """Round a datetime's clock time up to the next 15-minute boundary."""
    minutes = value.hour * 60 + value.minute
    if value.second or value.microsecond:
        minutes += 1
    rounded_minutes = ((minutes + 14) // 15) * 15
    hour, minute = divmod(rounded_minutes, 60)
    return time(hour, minute)


def validate_customer_booking_slot(mode, date_value, time_value="", now=None, enforce_lead_time=True):
    """
    Validate a customer booking.

    Normal:
      - Explicit service time: 9 AM–6 PM, in 15-minute intervals.
      - Same-day explicit slots must be at least 2 hours ahead.
      - Empty time for a future date: use 9 AM on that exact date.
      - Empty time for today: use booking time + 2 hours, rounded up
        to a 15-minute slot, with 9 AM as the earliest time.
      - If no same-day slot remains within the 9 AM–6 PM window,
        ask the customer to choose a later date. Never shift the date.
      - Normal same-day booking date closes at 6 PM.

    Full-Day: fixed 9 AM–5 PM, same-day only before 7 AM.
    """
    mode = normalise_booking_mode(mode)
    now = now or india_now()

    try:
        selected_date = date.fromisoformat(str(date_value or "").strip())
    except (TypeError, ValueError):
        return None, None, "Please choose a valid service date."

    if mode == "full-day":
        earliest = now.date() if now.time() <= time(7, 0) else now.date() + timedelta(days=1)
        if selected_date < earliest:
            if earliest == now.date():
                message = "Full-Day service can be booked for today only up to 7:00 AM."
            else:
                message = "After 7:00 AM, Full-Day bookings start tomorrow."
            return None, None, message
        return selected_date.isoformat(), "09:00", None

    if selected_date < now.date():
        return None, None, "Past dates cannot be booked."

    if selected_date == now.date() and now.time() >= time(18, 0):
        return None, None, (
            "Normal bookings are closed for today after 6:00 PM. "
            "Please choose tomorrow or a later date."
        )

    # Empty time means Sivdhi selects the service slot automatically.
    if not str(time_value or "").strip():
        # A future date always uses 9 AM on the exact date selected.
        if selected_date > now.date():
            return selected_date.isoformat(), "09:00", None

        # For today, enforce the two-hour lead time within normal hours.
        candidate = now + timedelta(hours=2)
        selected_time = _round_up_to_quarter_hour(candidate)

        if selected_time < time(9, 0):
            selected_time = time(9, 0)

        if selected_time > time(18, 0):
            return None, None, (
                "No automatic slot is available today. "
                "Please choose a later service date."
            )

        return selected_date.isoformat(), selected_time.strftime("%H:%M"), None

    # Customer-selected time: preserve it, subject to the slot rules.
    try:
        selected_time = time.fromisoformat(str(time_value).strip())
    except (TypeError, ValueError):
        return None, None, "Please choose a valid service time."

    if selected_time.second or selected_time.microsecond or selected_time.minute % 15:
        return None, None, "Choose a time in 15-minute intervals."

    if selected_time < time(9, 0) or selected_time > time(18, 0):
        return None, None, "Normal service slots are available from 9:00 AM to 6:00 PM."

    slot = datetime.combine(selected_date, selected_time).replace(tzinfo=now.tzinfo)
    if selected_date == now.date():
        if enforce_lead_time and slot < now + timedelta(hours=2):
            return None, None, "Choose a slot at least 2 hours from now."
        if not enforce_lead_time and slot <= now:
            return None, None, (
                "The selected service slot has already passed. "
                "Please choose another date or time."
            )

    return selected_date.isoformat(), selected_time.strftime("%H:%M"), None
