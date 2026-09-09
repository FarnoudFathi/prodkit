"""
Scheduler.

Fires the day's events without anyone running a command. Until now every step
was triggered by hand, which is fine for solo testing and impossible to ask
five people to rely on.

PER-PERSON LOCAL TIMES
----------------------
`prompt_time: "11:00"` means 11:00 where each person is, not 11:00 UTC. On a
single-timezone team those are the same instant; the machinery exists so adding
a remote teammate needs a config line rather than a rewrite.

The digest is the exception. It waits until the LAST person's cutoff has
passed — posting while someone in a later timezone is still inside their window
would report them as a non-responder while they still have time to answer.

MISSED WINDOWS
--------------
A restart at 12:00 must not skip the 11:00 prompt — nobody would be asked all
day. So each event has a window rather than an instant, and fires late if the
window is still open. The prompt window closes at cutoff, because prompting
someone at 16:00 for a standup that closed at 13:00 is worse than silence.

Fired events are recorded in the session file, so a restart inside a window
doesn't re-fire something that already went out.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone

from config import Config
from models import Person

# How often to check. Events resolve to the minute, so a shorter tick buys
# nothing but API noise, and a longer one risks drifting past a window edge.
TICK_SECONDS = 60


@dataclass
class Event:
    """Something that should happen once per person per day."""
    name: str
    person: Person | None      # None for team-wide events like the digest


def _local_now(person: Person, now: datetime) -> datetime:
    return now.astimezone(person.calendar.tz)


def _scheduled(person: Person, clock: str, now: datetime) -> datetime:
    """Turn a config time string into today's instant in this person's zone."""
    return person.calendar.local_time_today(clock, now)


def _in_window(now: datetime, opens: datetime, closes: datetime) -> bool:
    return opens <= now < closes


def due_events(config: Config, now: datetime, already_fired: set[str],
               has_responded) -> list[Event]:
    """
    Which events should fire right now.

    `has_responded` is a callable taking a discord user id, so the scheduler can
    decide whether to nudge someone without owning the session store.
    """
    events: list[Event] = []

    for person in config.team:
        local = _local_now(person, now)

        # Weekends and configured non-working days are skipped per person,
        # because "the working week" is not the same everywhere.
        if local.weekday() not in config.active_days:
            continue

        prompt_at = _scheduled(person, config.prompt_time, now)
        nudge_at = _scheduled(person, config.nudge_time, now)
        cutoff_at = _scheduled(person, config.cutoff_time, now)
        eod_at = _scheduled(person, config.eod_time, now)
        close_at = _scheduled(person, config.eod_close_time, now)

        key = f"prompt:{person.discord_user_id}"
        if key not in already_fired and _in_window(local, prompt_at, cutoff_at):
            events.append(Event("prompt", person))
            continue      # don't nudge in the same tick as the prompt

        key = f"nudge:{person.discord_user_id}"
        if (
            key not in already_fired
            and _in_window(local, nudge_at, cutoff_at)
            and not has_responded(person.discord_user_id)
        ):
            events.append(Event("nudge", person))

        key = f"eod:{person.discord_user_id}"
        if (
            config.eod_enabled
            and key not in already_fired
            and _in_window(local, eod_at, close_at)
        ):
            events.append(Event("eod", person))

    # Second digest at close of day. The cutoff digest is a snapshot at 13:00 —
    # anything answered later, plus every end-of-day check-in, is invisible to
    # it. Rather than editing the earlier post, a separate one is published:
    # different purpose, not a correction. This is the one a producer reads to
    # plan tomorrow.
    if ("digest_close" not in already_fired and config.team
            and config.eod_enabled):
        all_closed = all(
            _local_now(p, now) >= _scheduled(p, config.eod_close_time, now)
            for p in config.team
        )
        working_day = any(
            _local_now(p, now).weekday() in config.active_days
            for p in config.team
        )
        # Bounded so a bot started at 02:00 doesn't publish a close-of-day
        # digest for a day that ended hours ago.
        not_stale = any(
            _local_now(p, now).hour < 23 for p in config.team
        )
        if all_closed and working_day and not_stale:
            events.append(Event("digest_close", None))

    # Team-wide: only once every person's cutoff has passed.
    if "digest" not in already_fired and config.team:
        all_closed = all(
            _local_now(p, now) >= _scheduled(p, config.cutoff_time, now)
            for p in config.team
        )
        # Bounded on the other side so a bot started at 22:00 doesn't post a
        # digest for a day that's effectively over.
        not_too_late = any(
            _local_now(p, now) < _scheduled(p, config.eod_close_time, now)
            for p in config.team
        )
        working_day = any(
            _local_now(p, now).weekday() in config.active_days
            for p in config.team
        )
        if all_closed and not_too_late and working_day:
            events.append(Event("digest", None))

    return events


class Scheduler:
    """
    Drives the day.

    Deliberately owns no state of its own — fired events live in the session
    file so a restart mid-morning resumes correctly rather than re-prompting
    everyone.
    """

    def __init__(self, bot, config: Config, store):
        self.bot = bot
        self.store = store
        self._task: asyncio.Task | None = None

    @property
    def config(self) -> Config:
        """
        Always the bot's current config, never a captured copy.

        Holding a reference taken at construction meant a slash command could
        change the schedule, report success, and change nothing — because
        reload_config replaced the bot's config object while the scheduler kept
        pointing at the old one. Reading through the bot on every access is the
        only way a runtime change reaches the thing that fires events.
        """
        return self.bot.config

    def start(self):
        self._task = asyncio.create_task(self._loop())

    async def _loop(self):
        print(
            f"Scheduler running — prompt {self.config.prompt_time}, "
            f"nudge {self.config.nudge_time}, cutoff {self.config.cutoff_time}"
            + (f", end of day {self.config.eod_time}"
               if self.config.eod_enabled else "")
            + " (each person's local time)"
        )
        while True:
            try:
                await self._tick()
            except Exception as e:
                # A failure on one tick must not kill the loop — the bot would
                # go silent for the rest of the day with no obvious cause.
                print(f"Scheduler tick failed: {e}")
            await asyncio.sleep(TICK_SECONDS)

    async def _tick(self):
        now = datetime.now(timezone.utc)
        # Project-local, not UTC. In UTC the day rolls over at 17:00 Pacific,
        # which reset the session mid-afternoon and re-fired the digest against
        # an empty day.
        date_key = self.config.date_key(now)
        session = self.store.load(date_key)

        events = due_events(
            self.config,
            now,
            set(session.fired),
            lambda uid: session.has_responded(uid),
        )
        if not events:
            return

        for event in events:
            label = (
                f"{event.name}:{event.person.discord_user_id}"
                if event.person else event.name
            )
            try:
                await self.bot.run_event(event, date_key, now)
            except Exception as e:
                # Marking a failed event as fired would mean it never retries.
                # Leaving it unmarked means the next tick tries again, which is
                # the right behaviour for a transient network failure.
                print(f"Event {label} failed: {e}")
                continue

            session = self.store.load(date_key)
            session.mark_fired(label)
            self.store.save(session)
            print(f"Fired {label}")
