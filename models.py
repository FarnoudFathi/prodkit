"""
Data shapes used across ProdKit.

These are plain containers with no behaviour beyond a little arithmetic. They
exist so the rest of the code passes around its own objects rather than raw
Jira or Discord JSON — which means a change in either API's response shape is
absorbed in one place instead of rippling through every file.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time
from enum import Enum
from zoneinfo import ZoneInfo


class State(str, Enum):
    """
    The four canonical states, independent of what a board calls its columns.

    config.yaml maps real column names onto these. Everything downstream works
    in these terms, so a team using "Doing" instead of "In Progress" needs no
    code change.
    """
    TODO = "todo"
    IN_PROGRESS = "in_progress"
    IN_REVIEW = "in_review"
    DONE = "done"


@dataclass(frozen=True)
class WorkCalendar:
    """
    One person's working hours in their own timezone.

    This exists so durations mean something. A ticket picked up Friday at 16:30
    and finished Monday at 10:15 shows 65 hours elapsed but represents under 3
    hours of work. Without a calendar, every ticket that touches a weekend looks
    like a crisis, the warnings become noise, and people stop reading them.
    """
    timezone: str = "America/Vancouver"
    start_hour: int = 9
    end_hour: int = 18
    workdays: tuple[int, ...] = (0, 1, 2, 3, 4)   # Mon=0 .. Sun=6

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    def is_workday(self, local_date) -> bool:
        return local_date.weekday() in self.workdays

    def day_window(self, local_date) -> tuple[datetime, datetime]:
        """Start and end of the working window on a given local date."""
        return (
            datetime.combine(local_date, time(self.start_hour), tzinfo=self.tz),
            datetime.combine(local_date, time(self.end_hour), tzinfo=self.tz),
        )

    def local_time_today(self, clock: str, reference: datetime) -> datetime:
        """
        Turn a config string like "11:00" into a real instant on the reference
        date, in this person's timezone. This is how one schedule entry becomes
        six different UTC moments on a distributed team.
        """
        hour, minute = (int(part) for part in clock.split(":"))
        local_date = reference.astimezone(self.tz).date()
        return datetime.combine(
            local_date, time(hour, minute), tzinfo=self.tz
        )


@dataclass(frozen=True)
class Person:
    name: str
    discipline: str
    jira_account_id: str
    discord_user_id: int
    calendar: WorkCalendar = field(default_factory=WorkCalendar)

    @property
    def mention(self) -> str:
        """Discord's syntax for pinging someone by ID."""
        return f"<@{self.discord_user_id}>"


@dataclass(frozen=True)
class BlockerRef:
    """
    A ticket that another ticket is waiting on.

    Carries the blocker's own state, because that's what decides which question
    gets asked. If the blocker is still open there is nothing to ask — the
    person simply can't start, and a question would be noise. If the blocker is
    Done, the interesting question appears: the board says finished, but did the
    work actually reach the person waiting on it?
    """
    key: str
    summary: str
    status_name: str
    state: State
    assignee_name: str | None

    @property
    def is_done(self) -> bool:
        return self.state is State.DONE


@dataclass
class Issue:
    key: str
    summary: str
    issue_type: str
    status_name: str          # what Jira calls it, e.g. "IN REVIEW"
    state: State              # what we call it, e.g. State.IN_REVIEW
    assignee_id: str | None
    assignee_name: str | None
    due_date: datetime | None
    estimate_hours: float | None
    components: tuple[str, ...] = ()
    labels: tuple[str, ...] = ()
    blockers: tuple[BlockerRef, ...] = ()

    @property
    def is_active(self) -> bool:
        return self.state in (State.IN_PROGRESS, State.IN_REVIEW)

    @property
    def open_blockers(self) -> tuple[BlockerRef, ...]:
        """Blockers still unfinished — the person genuinely cannot start."""
        return tuple(b for b in self.blockers if not b.is_done)

    @property
    def done_blockers(self) -> tuple[BlockerRef, ...]:
        """
        Blockers marked Done. These are the handoff-verification cases: the
        board looks healthy, but the deliverable may never have changed hands.
        """
        return tuple(b for b in self.blockers if b.is_done)
