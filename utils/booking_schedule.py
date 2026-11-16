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
    # After 4 PM there is no same-day Normal slot with two hours' notice
    # inside the 9 AM–6 PM service window.
    if now.time() > time(16, 0):
        return (now.date() + timedelta(days=1)).isoformat()
    return now.date().isoformat()


def _default_normal_time_for_today(now):
    """Return the next quarter-hour at least two hours ahead, up to 6 PM."""
    target = now + timedelta(hours=2)
    total_minutes = target.hour * 60 + target.minute
    if target.second or target.microsecond:
        total_minutes += 1
    rounded_minutes = ((total_minutes + 14) // 15) * 15
    if rounded_minutes > 18 * 60:
        return None
    return time(rounded_minutes // 60, rounded_minutes % 60)


def validate_customer_booking_slot(mode, date_value, time_value="", now=None):
    """Validate customer booking date/time.

    Normal: 9 AM–6 PM in 15-minute slots; same-day bookings need >=2h notice.
    If time is blank, use +2h (rounded up to 15 minutes) for today, or 9 AM
    for a future date. After 4 PM, a blank same-day time rolls to tomorrow 9 AM.
    Full-day keeps its existing date rules and fixed 9 AM start.
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

    raw_time = str(time_value or "").strip()
    if not raw_time:
        if selected_date == now.date():
            if now.time() > time(16, 0):
                selected_date = now.date() + timedelta(days=1)
                selected_time = time(9, 0)
            else:
                selected_time = _default_normal_time_for_today(now)
                if selected_time is None:
                    selected_date = now.date() + timedelta(days=1)
                    selected_time = time(9, 0)
        else:
            selected_time = time(9, 0)
    else:
        if selected_date == now.date() and now.time() > time(16, 0):
            return None, None, "After 4:00 PM, Normal bookings start tomorrow at 9:00 AM."
        try:
            selected_time = time.fromisoformat(raw_time)
        except (TypeError, ValueError):
            return None, None, "Please choose a valid service time or leave it blank for automatic scheduling."

    if selected_time.second or selected_time.microsecond or selected_time.minute % 15:
        return None, None, "Choose a time in 15-minute intervals."
    if selected_time < time(9, 0) or selected_time > time(18, 0):
        return None, None, "Normal service slots are available from 9:00 AM to 6:00 PM."

    slot = datetime.combine(selected_date, selected_time).replace(tzinfo=now.tzinfo)
    if selected_date == now.date() and slot < now + timedelta(hours=2):
        return None, None, "Choose a slot at least 2 hours from now."
    return selected_date.isoformat(), selected_time.strftime("%H:%M"), None
