"""
Working-hours arithmetic.

A ticket moved to In Progress on Friday at 16:30 and untouched until Monday at
10:15 shows 65.75 hours elapsed. Real working time: 2.75 hours.

If staleness were measured in wall-clock hours, every ticket that touched a
weekend would look abandoned, the digest would be full of false alarms, and you
would stop reading it within a week. So every duration in ProdKit is measured
against the assignee's own working calendar.

On a distributed team the same interval is a different number of working hours
per person — Friday 16:30 Pacific is already past end-of-day Eastern. That's why
the calendar belongs to the person, not to the project.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from models import WorkCalendar


def business_hours_between(
    start: datetime, end: datetime, calendar: WorkCalendar
) -> float:
    """
    Working hours between two instants.

    Walks the range one local day at a time, adding up how much of each day's
    working window the interval covers. Day-by-day rather than clever modular
    arithmetic, because it handles daylight saving for free: each day's 09:00
    and 18:00 are built by the timezone library rather than assumed to be 24
    hours apart. Hand-rolled math gets DST weeks wrong by an hour.
    """
    if start.tzinfo is None or end.tzinfo is None:
        raise ValueError("business_hours_between needs timezone-aware datetimes")
    if end <= start:
        return 0.0

    tz = calendar.tz
    local_start = start.astimezone(tz)
    local_end = end.astimezone(tz)

    total = timedelta()
    current = local_start.date()
    last = local_end.date()

    while current <= last:
        if calendar.is_workday(current):
            window_start, window_end = calendar.day_window(current)
            overlap_start = max(window_start, local_start)
            overlap_end = min(window_end, local_end)
            if overlap_end > overlap_start:
                total += overlap_end - overlap_start
        current += timedelta(days=1)

    return total.total_seconds() / 3600.0


def business_hours_until(
    now: datetime, deadline: datetime, calendar: WorkCalendar
) -> float:
    """
    Working hours remaining before a deadline. Negative once past.

    Used for due-soon flags. "Due in 24 hours" has to mean 24 *working* hours,
    or a Friday-afternoon warning about a Monday deadline arrives after everyone
    has logged off — precisely when it is least useful.
    """
    if deadline <= now:
        return -business_hours_between(deadline, now, calendar)
    return business_hours_between(now, deadline, calendar)


def humanise(hours: float) -> str:
    """Short readable duration for prompts and digests."""
    if hours < 1:
        return "under an hour"
    if hours < 9:
        return f"{hours:.0f}h"
    days = hours / 9.0          # roughly one working day
    if days < 2:
        return "about a day"
    return f"{days:.0f} days"
