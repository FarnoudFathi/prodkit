"""
Session store.

Answers have to survive a restart. The bot runs from the 11:00 prompt through
the 13:00 cutoff and on to the 17:00 wrap — several hours of a process that
might be redeployed, crash, or have its host recycle it. Holding responses in
memory means a restart at 12:00 silently loses the morning, and the digest
reports everyone as a non-responder.

It also has to remember across DAYS, for two features:

  1. Handoff verification asks once. "Got it" on Monday must not re-ask on
     Tuesday — and that fact exists nowhere in Jira, because receiving a file
     is not a board state.

  2. Handoff failures escalate. "Haven't received it" three days running should
     read as three days in the digest, not as three separate day-one notices.

One JSON file per day under sessions/. A file rather than a database because a
six-person standup produces a few dozen rows a day, the data is naturally
partitioned by date, and a plain file can be opened and read by a human when
something looks wrong. SQLite here would be ceremony.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

# Overridable so a host can point it at a mounted volume. Container
# filesystems are wiped on every redeploy — without a volume, deploying a fix
# at noon would erase the morning's answers and the digest would report
# everyone as a non-responder.
SESSION_DIR = Path(os.environ.get("PRODKIT_SESSION_DIR",
                                  Path(__file__).parent / "sessions"))

# How many days back to look when deciding whether a handoff was already
# settled, or how long a failure has been running. Two working weeks is well
# past the point where an unresolved handoff should have been escalated by a
# human instead.
LOOKBACK_DAYS = 14


@dataclass
class Answer:
    action: str
    text: str = ""
    previous_status: str | None = None   # for undoing a write-back
    at: str = ""                         # ISO timestamp, for ordering
    late: bool = False                   # answered after the day's close time

    @property
    def is_handoff_confirmed(self) -> bool:
        return self.action in ("got_starting", "got_later")

    @property
    def is_handoff_failure(self) -> bool:
        return self.action == "not_received"


# Keys used for messages that aren't about a ticket — the wrap message. They
# must not count as answers, or someone who taps only "Done with standup"
# reads as a responder while having said nothing.
PSEUDO_KEYS = {"none"}


@dataclass
class Session:
    """One day's standup."""
    date_key: str
    # discord_user_id (as string, because JSON keys must be strings) -> issue key -> Answer
    answers: dict[str, dict[str, Answer]] = field(default_factory=dict)
    # Who was prompted, and about what. Needed to tell a non-responder apart
    # from someone who simply had nothing to be asked.
    asked: dict[str, list[str]] = field(default_factory=dict)
    # Thread ids per person, so the 17:00 wrap posts into the same thread
    # rather than opening a second one.
    threads: dict[str, int] = field(default_factory=dict)
    digest_posted: bool = False
    wrap_posted: bool = False
    # Scheduler events already fired today, e.g. "prompt:699984861632659527".
    # Kept in the session file rather than in memory so a restart mid-morning
    # resumes instead of re-prompting everyone.
    fired: list[str] = field(default_factory=list)

    # ------------------------------------------------------- recording

    def record(self, user_id: int, issue_key: str, action: str,
               text: str = "", previous_status: str | None = None,
               late: bool = False) -> None:
        person = self.answers.setdefault(str(user_id), {})
        person[issue_key] = Answer(
            action=action,
            text=text,
            previous_status=previous_status,
            at=datetime.now(timezone.utc).isoformat(),
            late=late,
        )

    def clear(self, user_id: int, issue_key: str) -> Answer | None:
        """Remove an answer so it can be given again. Returns what was there."""
        return self.answers.get(str(user_id), {}).pop(issue_key, None)

    def get(self, user_id: int, issue_key: str) -> Answer | None:
        return self.answers.get(str(user_id), {}).get(issue_key)

    def mark_asked(self, user_id: int, issue_keys: list[str]) -> None:
        self.asked[str(user_id)] = issue_keys

    def set_thread(self, user_id: int, thread_id: int) -> None:
        self.threads[str(user_id)] = thread_id

    def mark_fired(self, event: str) -> None:
        if event not in self.fired:
            self.fired.append(event)

    def has_fired(self, event: str) -> bool:
        return event in self.fired

    # -------------------------------------------------------- querying

    def answered_by(self, user_id: int) -> dict[str, Answer]:
        """Real ticket answers only — wrap-message taps excluded."""
        return {
            key: answer
            for key, answer in self.answers.get(str(user_id), {}).items()
            if key not in PSEUDO_KEYS
        }

    def raw_answers(self, user_id: int) -> dict[str, Answer]:
        """Everything recorded, including wrap-message taps."""
        return self.answers.get(str(user_id), {})

    def finished_without_answering(self, user_id: int) -> list[str]:
        """
        Tickets this person was asked about but never answered, when they
        nonetheless tapped "Done with standup".

        Closing the standup while leaving questions untouched is not the same
        as not responding, and it should not be silently equivalent to
        answering. The producer sees exactly which tickets were skipped.
        """
        wrap = self.raw_answers(user_id).get("none")
        if not wrap or wrap.action != "finish":
            return []
        answered = set(self.answered_by(user_id))
        return [k for k in self.asked.get(str(user_id), []) if k not in answered]

    def has_responded(self, user_id: int) -> bool:
        """
        Did this person actually answer anything today?

        Any single real answer counts — someone who answered two of three
        questions engaged, and listing them as a non-responder would be both
        wrong and annoying.

        Tapping only "Done with standup" does NOT count. Closing the standup
        without answering anything is its own case, reported separately, and
        letting it read as a response would hide exactly the behaviour worth
        seeing.
        """
        return bool(self.answered_by(user_id))

    def non_responders(self, user_ids: list[int]) -> list[int]:
        return [uid for uid in user_ids
                if str(uid) in self.asked and not self.has_responded(uid)]

    def active_starts(self, user_id: int) -> list[str]:
        """Tickets this person said they'd start today — the 17:00 wrap list."""
        return [
            key for key, answer in self.answered_by(user_id).items()
            if answer.action in ("starting", "got_starting", "ongoing")
        ]

    # ------------------------------------------------------------ i/o

    def to_json(self) -> dict:
        return {
            "date_key": self.date_key,
            "answers": {
                uid: {k: asdict(a) for k, a in issues.items()}
                for uid, issues in self.answers.items()
            },
            "asked": self.asked,
            "threads": self.threads,
            "digest_posted": self.digest_posted,
            "wrap_posted": self.wrap_posted,
            "fired": self.fired,
        }

    @classmethod
    def from_json(cls, raw: dict) -> "Session":
        session = cls(date_key=raw["date_key"])
        for uid, issues in raw.get("answers", {}).items():
            session.answers[uid] = {
                key: Answer(**data) for key, data in issues.items()
            }
        session.asked = raw.get("asked", {})
        session.threads = {k: int(v) for k, v in raw.get("threads", {}).items()}
        session.digest_posted = raw.get("digest_posted", False)
        session.wrap_posted = raw.get("wrap_posted", False)
        session.fired = raw.get("fired", [])
        return session


class Store:
    def __init__(self, directory: Path = SESSION_DIR):
        self.dir = directory
        self.dir.mkdir(parents=True, exist_ok=True)

    def path_for(self, date_key: str) -> Path:
        return self.dir / f"{date_key}.json"

    def load(self, date_key: str) -> Session:
        path = self.path_for(date_key)
        if not path.exists():
            return Session(date_key=date_key)
        try:
            return Session.from_json(json.loads(path.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            # A corrupt file must not take the bot down mid-standup. Move it
            # aside so it can be inspected, and carry on with an empty session —
            # losing today's answers is bad, refusing to run at all is worse.
            broken = path.with_suffix(".json.broken")
            path.rename(broken)
            print(f"Session file was unreadable ({e}); moved to {broken.name}")
            return Session(date_key=date_key)

    def save(self, session: Session) -> None:
        """
        Write atomically.

        Answers arrive one tap at a time over hours, so this is called
        constantly. Writing to a temp file and renaming means a crash mid-write
        leaves the previous good file intact rather than a half-written one —
        rename is atomic on every platform we care about.
        """
        path = self.path_for(session.date_key)
        fd, tmp = tempfile.mkstemp(dir=str(self.dir), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(session.to_json(), f, indent=2)
            os.replace(tmp, path)
        except Exception:
            Path(tmp).unlink(missing_ok=True)
            raise

    # ------------------------------------------- cross-day questions

    def recent_sessions(self, before: str, days: int = LOOKBACK_DAYS) -> list[Session]:
        """Sessions from the N days before `before`, newest first."""
        anchor = date.fromisoformat(before)
        out = []
        for offset in range(1, days + 1):
            key = (anchor - timedelta(days=offset)).isoformat()
            if self.path_for(key).exists():
                out.append(self.load(key))
        return out

    def settled_handoffs(self, user_id: int, today: str) -> set[str]:
        """
        Tickets where this person already confirmed receipt on an earlier day.

        Handoff verification asks once and stops. Only the failure repeats —
        that asymmetry keeps the unresolved case loud without nagging about
        resolved ones.

        A later "haven't received it" un-settles a ticket. That handles the case
        where someone confirms receipt, then the file turns out to be wrong or
        gets superseded.
        """
        settled: set[str] = set()
        # Oldest first, so a newer answer overrides an older one.
        for session in reversed(self.recent_sessions(today)):
            for key, answer in session.answered_by(user_id).items():
                if answer.is_handoff_confirmed:
                    settled.add(key)
                elif answer.is_handoff_failure:
                    settled.discard(key)
        return settled

    def handoff_failure_days(self, user_id: int, issue_key: str, today: str) -> int:
        """
        How many consecutive days this handoff has been reported as failed,
        counting today.

        Drives escalation in the digest: day one is a note, day three is
        roughly 27 working hours of someone idle on work the board says is
        finished, and it should read that way.

        Counts consecutive days only. A gap means the person wasn't asked or
        didn't answer, and treating that as continued failure would overstate it.
        """
        streak = 0
        today_session = self.load(today)
        answer = today_session.get(user_id, issue_key)
        if answer and answer.is_handoff_failure:
            streak = 1
        else:
            return 0

        for session in self.recent_sessions(today):
            previous = session.get(user_id, issue_key)
            if previous and previous.is_handoff_failure:
                streak += 1
            else:
                break
        return streak


if __name__ == "__main__":
    # Round-trip check against a temporary directory.
    import shutil

    tmpdir = Path(tempfile.mkdtemp())
    store = Store(tmpdir)
    USER = 699984861632659527

    # Monday: handoff fails.
    monday = store.load("2026-08-17")
    monday.mark_asked(USER, ["SR71-17", "SR71-18"])
    monday.record(USER, "SR71-17", "not_received")
    monday.record(USER, "SR71-18", "got_starting", previous_status="To Do")
    store.save(monday)

    # Tuesday: still failing.
    tuesday = store.load("2026-08-18")
    tuesday.mark_asked(USER, ["SR71-17"])
    tuesday.record(USER, "SR71-17", "not_received")
    store.save(tuesday)

    # Wednesday: third day.
    wednesday = store.load("2026-08-19")
    wednesday.mark_asked(USER, ["SR71-17"])
    wednesday.record(USER, "SR71-17", "not_received")
    store.save(wednesday)

    print("settled handoffs   :", store.settled_handoffs(USER, "2026-08-19"))
    print("SR71-17 fail streak:", store.handoff_failure_days(USER, "SR71-17", "2026-08-19"))

    # Reload from disk to prove persistence.
    reloaded = store.load("2026-08-19")
    print("reloaded answer    :", reloaded.get(USER, "SR71-17").action)
    print("responded          :", reloaded.has_responded(USER))
    print("non-responders     :", reloaded.non_responders([USER, 111111]))

    # Undo path.
    removed = reloaded.clear(USER, "SR71-17")
    print("cleared, was       :", removed.action)
    print("responded after    :", reloaded.has_responded(USER))

    shutil.rmtree(tmpdir)
