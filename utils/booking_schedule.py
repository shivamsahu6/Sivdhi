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


def validate_customer_booking_slot(mode, date_value, time_value="", now=None):
    """Validate booking date/time. Normal: 8 AM–8 PM in 15-min slots; >=2h ahead same day. Full-day: 9 AM–5 PM, same day only before 7 AM."""
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
    # Normal same-day booking closes at 6 PM; later bookings start tomorrow.
    if selected_date == now.date() and now.time() >= time(18, 0):
        return None, None, "Normal bookings are closed for today after 6:00 PM. Please choose tomorrow or a later date."
    try:
        selected_time = time.fromisoformat(str(time_value or "").strip())
    except (TypeError, ValueError):
        return None, None, "Please choose a service time."
    if selected_time.second or selected_time.microsecond or selected_time.minute % 15:
        return None, None, "Choose a time in 15-minute intervals."
    if selected_time < time(8, 0) or selected_time > time(20, 0):
        return None, None, "Normal service slots are available from 8:00 AM to 8:00 PM."
    slot = datetime.combine(selected_date, selected_time).replace(tzinfo=now.tzinfo)
    if selected_date == now.date() and slot < now + timedelta(hours=2):
        return None, None, "Choose a slot at least 2 hours from now."
    return selected_date.isoformat(), selected_time.strftime("%H:%M"), None
