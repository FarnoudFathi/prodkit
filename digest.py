"""
Digest assembler.

Produces two things: a short attention block for the producer's channel, and a
fuller per-person roster that goes into a thread on it.

WHY TWO PARTS
-------------
A per-person roster answers "where is everyone", which is what a producer checks
when planning. But six people times seven line types is forty lines, and a
critical handoff failure for the fifth person ends up below the first person's
routine "in progress" line. Everything urgent has to be readable in ten seconds.

So the attention block carries anything needing action today, and the roster —
which repeats some of it with context — sits in a thread. Threads render
collapsed in Discord, so the channel stays scannable and the detail is one click
away.

Repetition between the two is deliberate: the top is for scanning, the roster is
for context.

Pure text assembly. No Discord, no Jira, so the layout is checkable at a
terminal.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from business_time import business_hours_until, humanise
from config import Config
from models import Issue, Person, State
from store import Session, Store

# Working hours before a due date counts as approaching. Roughly two working
# days. Working hours rather than wall clock on purpose: 48 wall-clock hours
# from Friday morning lands on Sunday, so a Monday deadline would not appear.
DUE_WINDOW_HOURS = 16.0

# Answers meaning the person is working on that ticket today.
WORKING_TODAY = {"starting", "got_starting", "ongoing"}

# Answers meaning they cannot act on it.
STUCK = {"blocked", "not_received"}

STATE_WORDS = {
    State.TODO: "back in To Do",
    State.IN_PROGRESS: "in progress",
    State.IN_REVIEW: "in review",
    State.DONE: "done",
}


@dataclass
class Section:
    heading: str
    lines: list[str]
    severity: int = 0          # 0 informational, 1 attention, 2 urgent


@dataclass
class DigestResult:
    title: str
    subtitle: str
    top: list[Section]         # the attention message
    roster: list[Section]      # one section per person, for the thread
    footer: str

    @property
    def severity(self) -> int:
        return max((s.severity for s in self.top), default=0)

    def as_text(self) -> str:
        """Plain-text rendering, for terminals and anything without embeds."""
        out = [f"## {self.title}", f"_{self.subtitle}_"]
        for s in self.top:
            out += ["", f"**{s.heading}**"] + s.lines
        out += ["", f"_{self.footer}_", "", "— — — roster (thread) — — —"]
        for s in self.roster:
            out += ["", f"**{s.heading}**"] + s.lines
        return "\n".join(out)


@dataclass
class DigestInput:
    date_label: str
    config: Config
    session: Session
    previous: Session | None
    store: Store
    date_key: str
    people: list[Person]
    issues_by_key: dict[str, Issue]
    now: datetime
    stale: dict[str, str] = field(default_factory=dict)   # key -> detail
    board_flags: list[str] = field(default_factory=list)


# ------------------------------------------------------------ per person

def _theirs(inp: DigestInput, person: Person) -> list[Issue]:
    return [i for i in inp.issues_by_key.values()
            if i.assignee_id == person.jira_account_id]


def _due_soon(inp: DigestInput, person: Person) -> list[tuple[Issue, float]]:
    out = []
    for issue in _theirs(inp, person):
        if not issue.due_date or issue.state is State.DONE:
            continue
        remaining = business_hours_until(inp.now, issue.due_date, person.calendar)
        if remaining <= DUE_WINDOW_HOURS:
            out.append((issue, remaining))
    return sorted(out, key=lambda p: p[1])


def _working_today(inp: DigestInput, person: Person) -> list[str]:
    return [k for k, a in inp.session.answered_by(person.discord_user_id).items()
            if a.action in WORKING_TODAY]


def _yesterday_outcomes(inp: DigestInput, person: Person) -> list[str]:
    """
    What they said they'd work on last time, and where those tickets are now.

    The continuity view. A ticket that was "starting today" on Wednesday and is
    still in To Do on Friday is the thing a producer most wants to see, and no
    single day's answers reveal it.
    """
    if inp.previous is None:
        return []

    lines = []
    for key, answer in inp.previous.answered_by(person.discord_user_id).items():
        if answer.action not in WORKING_TODAY:
            continue

        issue = inp.issues_by_key.get(key)
        if issue is None:
            # Not on the open board any more, so it completed.
            lines.append(f"`{key}` → **done**")
            continue

        word = STATE_WORDS.get(issue.state, issue.status_name)
        due = ""
        if issue.due_date:
            remaining = business_hours_until(inp.now, issue.due_date, person.calendar)
            due = (f" · overdue {humanise(abs(remaining))}" if remaining < 0
                   else f" · due in {humanise(remaining)}")

        # A blocker picked up mid-day is the highest-value detail here: someone
        # started fine and hit a wall, and the reason only exists in their answer.
        today = inp.session.answered_by(person.discord_user_id).get(key)
        if today and today.action == "blocked" and today.text:
            lines.append(f"`{key}` → **blocked** — {today.text}")
        else:
            lines.append(f"`{key}` → {word}{due}")
    return lines


def _handoff_lines(inp: DigestInput, person: Person) -> list[str]:
    lines = []
    for key, answer in inp.session.answered_by(person.discord_user_id).items():
        if answer.action != "not_received":
            continue
        days = inp.store.handoff_failure_days(
            person.discord_user_id, key, inp.date_key
        )
        issue = inp.issues_by_key.get(key)
        blocker = issue.done_blockers[0] if issue and issue.done_blockers else None
        who = blocker.assignee_name if blocker and blocker.assignee_name else "someone"
        bkey = blocker.key if blocker else "its blocker"
        age = "" if days <= 1 else f" · **day {days}**"
        lines.append(f"`{key}` waiting on {who} — `{bkey}` marked done, "
                     f"never handed off{age}")
    return lines


def _blocked_lines(inp: DigestInput, person: Person) -> list[str]:
    lines = []
    for key, answer in inp.session.answered_by(person.discord_user_id).items():
        if answer.action != "blocked":
            continue
        reason = f" — {answer.text}" if answer.text else ""
        lines.append(f"`{key}` {_summary(inp, key)}{reason}")
    return lines


def _waiting_lines(inp: DigestInput, person: Person) -> list[str]:
    """Tickets they cannot start because a blocker is still open."""
    out = []
    for issue in _theirs(inp, person):
        if issue.state is not State.TODO or not issue.open_blockers:
            continue
        b = issue.open_blockers[0]
        who = f", {b.assignee_name}" if b.assignee_name else ""
        out.append(f"`{issue.key}` waiting on `{b.key}` ({b.status_name}{who})")
    return out


def _overdue_lines(inp: DigestInput, person: Person) -> list[str]:
    out = []
    for issue, remaining in _due_soon(inp, person):
        if remaining < 0:
            out.append(f"`{issue.key}` {_summary(inp, issue.key)} — "
                       f"overdue {humanise(abs(remaining))}")
    return out


def _review_lines(inp: DigestInput, person: Person) -> list[str]:
    out = []
    for issue in _theirs(inp, person):
        if issue.state is not State.IN_REVIEW:
            continue
        detail = inp.stale.get(issue.key, "")
        out.append(f"`{issue.key}` {_summary(inp, issue.key)}"
                   + (f" · {detail}" if detail else ""))
    return out


def _is_idle(inp: DigestInput, person: Person) -> bool:
    """
    Nothing they can work on. Derived from answers, never asked.

    Nobody volunteers "I have nothing to do", so asking would under-report it —
    and this costs no taps. Only counted when they actually answered; silence is
    a different problem, reported separately.
    """
    answers = inp.session.answered_by(person.discord_user_id)
    if not answers:
        return False
    return all(a.action in STUCK or a.action == "done" for a in answers.values())


def _summary(inp: DigestInput, key: str) -> str:
    issue = inp.issues_by_key.get(key)
    return issue.summary if issue else key


def _wip(inp: DigestInput, person: Person) -> list[Issue]:
    return [i for i in _theirs(inp, person) if i.state in inp.config.wip_states]


# ------------------------------------------------------------- assembly

def build_digest(inp: DigestInput) -> DigestResult:
    responded = [p for p in inp.people
                 if inp.session.has_responded(p.discord_user_id)]
    silent = [p for p in inp.people
              if p.discord_user_id in inp.session.non_responders(
                  [x.discord_user_id for x in inp.people])]

    top: list[Section] = []
    attention: list[str] = []
    flagged: set[str] = set()      # people who appear in the attention block

    # --- idle first. Someone with nothing to do is the most expensive thing
    # --- on the board and the least visible without being told.
    for person in inp.people:
        if _is_idle(inp, person):
            attention.append(f"⛔ **{person.name}** has nothing to work on — "
                             f"everything blocked or complete")
            flagged.add(person.name)

    # --- someone answered but named no work for today
    for person in responded:
        if not _working_today(inp, person) and not _is_idle(inp, person):
            attention.append(f"⛔ **{person.name}** has no ticket in progress today")
            flagged.add(person.name)

    for person in inp.people:
        for line in _handoff_lines(inp, person):
            attention.append(f"📭 **{person.name}** {line}")
            flagged.add(person.name)

    for person in inp.people:
        for line in _blocked_lines(inp, person):
            attention.append(f"🚧 **{person.name}** {line}")
            flagged.add(person.name)

    for person in inp.people:
        for line in _overdue_lines(inp, person):
            attention.append(f"🔴 **{person.name}** {line}")
            flagged.add(person.name)

    for person in inp.people:
        active = _wip(inp, person)
        if len(active) > inp.config.max_active_wip:
            attention.append(f"⚖️ **{person.name}** — {len(active)} tickets in "
                             f"progress, cap is {inp.config.max_active_wip}")
            flagged.add(person.name)

    for line in inp.board_flags:
        attention.append(line)

    if attention:
        top.append(Section("Needs you today", attention, severity=2))

    # --- everyone else, one line each -----------------------------------
    on_track = []
    for person in responded:
        if person.name in flagged:
            continue
        working = _working_today(inp, person)
        detail = ", ".join(f"`{k}`" for k in working) if working else "—"
        on_track.append(f"**{person.name}** {detail}")
    if on_track:
        top.append(Section("On track", on_track, severity=0))

    # --- gaps -------------------------------------------------------------
    gaps = []
    if silent:
        gaps.append("🔇 No response — " + ", ".join(p.name for p in silent))
    for person in inp.people:
        skipped = inp.session.finished_without_answering(person.discord_user_id)
        if skipped:
            gaps.append(f"⏭ **{person.name}** closed standup leaving "
                        f"{len(skipped)} unanswered — {', '.join(skipped)}")
    if gaps:
        top.append(Section("Gaps", gaps, severity=1))

    # --- totals -----------------------------------------------------------
    all_issues = list(inp.issues_by_key.values())
    handoffs_open = sum(
        1 for p in inp.people
        for a in inp.session.answered_by(p.discord_user_id).values()
        if a.action == "not_received"
    )
    in_review = sum(1 for i in all_issues if i.state is State.IN_REVIEW)
    overdue = sum(len(_overdue_lines(inp, p)) for p in inp.people)

    due_soon = []
    for person in inp.people:
        for issue, remaining in _due_soon(inp, person):
            if remaining >= 0:
                due_soon.append(f"`{issue.key}` {person.name} "
                                f"({humanise(remaining)})")

    totals = [
        f"Handoffs open **{handoffs_open}** · In review **{in_review}** · "
        f"Overdue **{overdue}** · Untouched 2+ days **{len(inp.stale)}**"
    ]
    if due_soon:
        totals.append("Due within 2 working days — " + " · ".join(due_soon))
    top.append(Section("Totals", totals, severity=0))

    # --- roster ------------------------------------------------------------
    roster: list[Section] = []
    for person in inp.people:
        lines: list[str] = []

        for line in _handoff_lines(inp, person):
            lines.append(f"📭 {line}")
        for line in _waiting_lines(inp, person):
            lines.append(f"⏸ {line}")
        for line in _overdue_lines(inp, person):
            lines.append(f"🔴 {line}")

        previous = _yesterday_outcomes(inp, person)
        if previous:
            lines.append("**Last standup** " + " · ".join(previous))

        working = _working_today(inp, person)
        if working:
            lines.append("**Today** " + ", ".join(f"`{k}`" for k in working))
        elif inp.session.has_responded(person.discord_user_id):
            lines.append("**Today** ⛔ nothing in progress")
        else:
            lines.append("**Today** no response")

        upcoming = [f"`{i.key}` ({humanise(r)})"
                    for i, r in _due_soon(inp, person) if r >= 0]
        if upcoming:
            lines.append("**Due within 2 working days** " + " · ".join(upcoming))

        reviews = _review_lines(inp, person)
        if reviews:
            lines.append("**To review** " + " · ".join(reviews))

        roster.append(Section(
            f"{person.name} · {person.discipline}",
            lines or ["Nothing on the board."],
            severity=0,
        ))

    writes = sum(
        1 for p in inp.people
        for a in inp.session.answered_by(p.discord_user_id).values()
        if a.action in ("done", "starting", "got_starting", "blocked",
                        "approve", "send_back")
    )
    plural = "ticket" if writes == 1 else "tickets"

    return DigestResult(
        title=f"{inp.config.project_key} standup — {inp.date_label}",
        subtitle=f"{len(responded)} of {len(inp.people)} responded",
        top=top,
        roster=roster,
        footer=f"{writes} {plural} updated in Jira from this standup",
    )
