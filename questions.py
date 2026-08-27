"""
Question builder.

Takes one person's tickets and produces the exact set of messages they'll
receive. Pure logic — no Discord, no network. That separation means the hard
part is testable at a terminal, and swapping Discord for Slack later touches
nothing in this file.

THE GOVERNING RULE
------------------
The bot asks only what cannot be read off the board. Everything derivable —
staleness, due dates, open blockers — is reported to the producer in the digest
instead. Nobody gets pinged about something the producer could already see.

    ask   : is this actually done? did the handoff happen? starting today?
    flag  : stale, overdue, due soon, blocked, idle

Run it directly to print one person's prompt set:

    python questions.py
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum

from business_time import business_hours_between, business_hours_until, humanise
from config import Config, ConfigError, load_config, load_secrets
from models import BlockerRef, Issue, Person, State

# Working hours in a status before the producer sees a stale flag.
# 16 ≈ two working days. Low enough to catch real drift, high enough that
# normal overnight gaps don't fire and train people to ignore it.
STALE_THRESHOLD_HOURS = 16.0

# Working hours before a due date counts as "due soon" in the digest.
DUE_SOON_HOURS = 16.0


class Kind(str, Enum):
    ACTIVE = "active"        # how's this going?
    HANDOFF = "handoff"      # blocker is Done — did you actually receive it?
    START = "start"          # plain To Do — starting today?
    REVIEW = "review"        # someone else's work, waiting on your sign-off
    INFO = "info"            # notice only, no buttons, no tap
    WRAP = "wrap"            # notes + finish


@dataclass
class Button:
    label: str
    action: str              # encoded into the Discord custom_id later
    writes_back: bool = False


@dataclass
class Question:
    kind: Kind
    issue: Issue | None
    headline: str
    detail_lines: list[str] = field(default_factory=list)
    buttons: list[Button] = field(default_factory=list)

    @property
    def needs_tap(self) -> bool:
        return bool(self.buttons)


@dataclass
class Flag:
    """Something for the producer's digest. Never shown to the person."""
    kind: str                # stale | overdue | due_soon | blocked | skipped
    issue: Issue
    person: Person
    detail: str


@dataclass
class PersonPrompt:
    person: Person
    questions: list[Question]
    flags: list[Flag]

    @property
    def tap_count(self) -> int:
        return sum(1 for q in self.questions if q.needs_tap)


# ------------------------------------------------------------- buttons

def active_buttons(config: Config) -> list[Button]:
    # The completion label follows the review setting. With review on it reads
    # "Ready for review", because the person doing the work signals completion
    # and someone else confirms it — if assignees can self-close, the review
    # column is decoration.
    return [
        Button(config.review.completion_label, "done", writes_back=True),
        Button("⏳ Still on it", "ongoing"),
        Button("🚧 Blocked", "blocked", writes_back=True),
    ]


def review_buttons() -> list[Button]:
    # Shown to whoever holds a ticket sitting in review. Without these the
    # review step creates work for the producer that the tool was meant to
    # remove — someone has to open Jira to approve things.
    return [
        Button("✅ Approve", "approve", writes_back=True),
        Button("↩︎ Send back", "send_back", writes_back=True),
        Button("⏳ Not yet", "not_yet"),
    ]


def handoff_buttons() -> list[Button]:
    # Only the first writes back. "Got it" alone means unblocked, not started —
    # transitioning on it would put tickets into In Progress that nobody has
    # begun, and then the cycle-time tool starts counting hours against work
    # that hasn't started. The button that moves the ticket says so plainly.
    return [
        Button("▶️ Got it, starting today", "got_starting", writes_back=True),
        Button("👍 Got it, not today", "got_later"),
        Button("📭 Still haven't received it", "not_received"),
    ]


def start_buttons() -> list[Button]:
    return [
        Button("▶️ Starting today", "starting", writes_back=True),
        Button("👍 Can start, not today", "can_start"),
        Button("🚧 Blocked", "blocked", writes_back=True),
    ]


# ------------------------------------------------------------ ranking

def _due_or_far(issue: Issue) -> datetime:
    return issue.due_date or datetime.max.replace(tzinfo=timezone.utc)


def rank_active(
    issues: list[Issue], last_moved: dict[str, datetime], person: Person, now: datetime
) -> list[Issue]:
    """
    Order active tickets for slot selection, longest-untouched first.

    This is a hidden selector. Every active ticket gets the same neutral
    question regardless of rank, so nobody can tell why theirs was picked. The
    point is to spend the limited slots on the tickets the producer knows least
    about — asking about something moved this morning confirms what's already
    known.
    """
    def staleness(issue: Issue) -> float:
        moved = last_moved.get(issue.key)
        if not moved:
            return 0.0
        return business_hours_between(moved, now, person.calendar)

    return sorted(issues, key=lambda i: (-staleness(i), _due_or_far(i), i.key))


def rank_todo(issues: list[Issue], settled_handoffs: set[str]) -> list[Issue]:
    """
    Order To Do tickets for slot selection.

    Newly-unblocked tickets come first and it isn't close. A blocker that just
    went Done is the most time-sensitive thing on a board: work that just became
    startable, where the deliverable may never have changed hands. If a cap ever
    swallows that question, the handoff feature effectively doesn't exist.
    """
    def priority(issue: Issue) -> tuple:
        unsettled_handoff = (
            bool(issue.done_blockers)
            and not issue.open_blockers
            and issue.key not in settled_handoffs
        )
        return (0 if unsettled_handoff else 1, _due_or_far(issue), issue.key)

    return sorted(issues, key=priority)


def rank_dropdown(issues: list[Issue], now: datetime, calendar) -> list[Issue]:
    """
    Order the "starting anything else?" options.

    Shortest estimate first, once deadlines are accounted for. Not only for the
    satisfaction of finishing something — a 4h ticket produces a completed
    signal today, where a 14h ticket produces nothing until Thursday. Small
    tickets clear faster and keep the board's status more current.

    Anything due within two working days jumps the queue regardless of size, so
    smoothing never pushes a deadline off the list.
    """
    def priority(issue: Issue) -> tuple:
        urgent = 0
        if issue.due_date:
            remaining = business_hours_until(now, issue.due_date, calendar)
            if remaining <= DUE_SOON_HOURS:
                urgent = -1
        # Unestimated tickets sort last: an unknown size is the worst candidate
        # for "can you squeeze this in today".
        size = issue.estimate_hours if issue.estimate_hours else 999
        return (urgent, size, issue.key)

    return sorted(issues, key=priority)


def dropdown_options(
    person: Person, issues: list[Issue], config: Config, now: datetime,
    already_asked: set[str],
) -> list[Issue]:
    """
    Extra tickets to offer, or nothing.

    Returns empty when the person is already at the WIP cap. Someone spread
    across three tickets does not need a fourth — they need the producer to
    notice, which is what the digest flag does.
    """
    theirs = [i for i in issues if i.assignee_id == person.jira_account_id]
    active = [i for i in theirs if i.state in config.active_states]
    if len(active) >= config.max_active_wip:
        return []

    candidates = [
        i for i in theirs
        if i.state is State.TODO
        and i.key not in already_asked
        and not i.open_blockers
    ]
    ranked = rank_dropdown(candidates, now, person.calendar)
    room = config.max_active_wip - len(active)
    return ranked[: min(config.dropdown_options, room)]


# ------------------------------------------------------------ building

def _blocker_summary(blockers: tuple[BlockerRef, ...]) -> str:
    parts = []
    for b in blockers:
        who = f", {b.assignee_name}" if b.assignee_name else ""
        parts.append(f"{b.key} ({b.status_name}{who})")
    return " · ".join(parts)


def build_prompt(
    person: Person,
    issues: list[Issue],
    config: Config,
    now: datetime,
    last_moved: dict[str, datetime] | None = None,
    settled_handoffs: set[str] | None = None,
) -> PersonPrompt:
    """
    Build one person's full message set plus the flags their board state raises.

    `settled_handoffs` holds ticket keys where this person already confirmed
    receipt on an earlier day. Handoff verification asks once and stops; only
    the failure case repeats, escalating in the digest each day. That asymmetry
    keeps the unresolved thing loud without nagging about resolved ones.
    """
    last_moved = last_moved or {}
    settled_handoffs = settled_handoffs or set()

    theirs = [i for i in issues if i.assignee_id == person.jira_account_id]

    # A ticket sitting in review is somebody else's work awaiting this person's
    # sign-off, not their own work in progress. Asking "how's this going?" about
    # it would be wrong, so it gets its own question type.
    reviewing = (
        [i for i in theirs if i.state is State.IN_REVIEW]
        if config.review.enabled else []
    )
    review_keys = {i.key for i in reviewing}

    active_all = [i for i in theirs
                  if i.state in config.active_states and i.key not in review_keys]
    todo_all = [i for i in theirs if i.state is State.TODO]

    questions: list[Question] = []
    flags: list[Flag] = []

    # --- flags: everything derivable from the board, uncapped ----------
    for issue in active_all:
        moved = last_moved.get(issue.key)
        if moved:
            idle_hours = business_hours_between(moved, now, person.calendar)
            if idle_hours >= STALE_THRESHOLD_HOURS:
                flags.append(Flag(
                    "stale", issue, person,
                    f"untouched {humanise(idle_hours)} in {issue.status_name}",
                ))

    for issue in reviewing:
        moved = last_moved.get(issue.key)
        if moved:
            waiting = business_hours_between(moved, now, person.calendar)
            if waiting >= STALE_THRESHOLD_HOURS / 2:
                flags.append(Flag(
                    "awaiting_review", issue, person,
                    f"waiting {humanise(waiting)} for review",
                ))

    for issue in active_all + todo_all:
        if not issue.due_date:
            continue
        remaining = business_hours_until(now, issue.due_date, person.calendar)
        if remaining < 0:
            flags.append(Flag(
                "overdue", issue, person, f"overdue by {humanise(abs(remaining))}"
            ))
        elif remaining <= DUE_SOON_HOURS:
            flags.append(Flag(
                "due_soon", issue, person, f"due in {humanise(remaining)}"
            ))

    for issue in todo_all:
        if issue.open_blockers:
            flags.append(Flag(
                "blocked", issue, person,
                f"waiting on {_blocker_summary(issue.open_blockers)}",
            ))

    # --- questions: only what the board can't answer, capped -----------
    questions.append(Question(
        kind=Kind.INFO,
        issue=None,
        headline=f"Morning {person.name} — standup for {now.strftime('%a %d %b')}.",
    ))

    # Longest-waiting first. Previously these were ordered by due date, so a
    # review ticket with no due date sorted last and the OLDEST item in the
    # queue was the one dropped by the cap — the exact opposite of what the
    # feature is for. What falls off the end now is the newest, which comes
    # back tomorrow while still fresh.
    def waited(issue: Issue) -> float:
        moved = last_moved.get(issue.key)
        if not moved:
            return 0.0
        return business_hours_between(moved, now, person.calendar)

    reviewing = sorted(reviewing, key=lambda i: -waited(i))

    # Review sits above the person's own work: it unblocks someone else, and
    # a review queue that silts up is the main failure mode of adding the step.
    for issue in reviewing[: config.max_review]:
        waiting = ""
        moved = last_moved.get(issue.key)
        if moved:
            waiting = f"waiting {humanise(business_hours_between(moved, now, person.calendar))}"
        questions.append(Question(
            kind=Kind.REVIEW,
            issue=issue,
            headline=f"{issue.key} · {issue.summary}",
            detail_lines=[f"Ready for your review{' · ' + waiting if waiting else ''}"],
            buttons=review_buttons(),
        ))

    # The cap limits taps, not information. Anything beyond it is named here
    # and listed in full in the digest, so nothing is ever silently hidden.
    overflow = len(reviewing) - config.max_review
    if overflow > 0:
        questions.append(Question(
            kind=Kind.INFO,
            issue=None,
            headline=f"{overflow} more waiting your review — full list in the digest.",
        ))

    for issue in rank_active(active_all, last_moved, person, now)[: config.max_in_progress]:
        detail = []
        if issue.estimate_hours:
            detail.append(f"estimated {issue.estimate_hours:.0f}h")
        if issue.due_date:
            detail.append(f"due {issue.due_date.strftime('%a %d %b')}")

        questions.append(Question(
            kind=Kind.ACTIVE,
            issue=issue,
            headline=f"{issue.key} · {issue.summary}",
            detail_lines=[" · ".join(detail)] if detail else [],
            buttons=active_buttons(config),
        ))

    todo_slots = config.max_todo
    for issue in rank_todo(todo_all, settled_handoffs):
        # Open blocker: the person genuinely cannot act, so a question would be
        # noise. Shown as a notice with no buttons, and it costs no slot —
        # otherwise a notice could displace a real question.
        if issue.open_blockers:
            questions.append(Question(
                kind=Kind.INFO,
                issue=issue,
                headline=f"{issue.key} · {issue.summary}",
                detail_lines=[f"Waiting on {_blocker_summary(issue.open_blockers)}"],
            ))
            continue

        if todo_slots <= 0:
            continue

        if issue.done_blockers and issue.key not in settled_handoffs:
            blocker = issue.done_blockers[0]
            who = blocker.assignee_name or "someone"
            questions.append(Question(
                kind=Kind.HANDOFF,
                issue=issue,
                headline=f"{issue.key} · {issue.summary}",
                detail_lines=[
                    f"{blocker.key} was marked done by {who}.",
                    "Do you have what you need to start?",
                ],
                buttons=handoff_buttons(),
            ))
        else:
            due = f"due {issue.due_date.strftime('%a %d %b')}" if issue.due_date else ""
            questions.append(Question(
                kind=Kind.START,
                issue=issue,
                headline=f"{issue.key} · {issue.summary}",
                detail_lines=[due] if due else [],
                buttons=start_buttons(),
            ))

        todo_slots -= 1

    questions.append(Question(
        kind=Kind.WRAP,
        issue=None,
        headline=f"Anything else? Cutoff is {config.cutoff_time}.",
        buttons=[
            Button("💬 Add a note", "note"),
            Button("✔️ Done with standup", "finish"),
        ],
    ))

    return PersonPrompt(person=person, questions=questions, flags=flags)


def render(prompt: PersonPrompt) -> str:
    """Plain-text preview of what a person will see. Terminal testing only."""
    out = [f"=== {prompt.person.name} — {prompt.tap_count} taps ==="]
    for q in prompt.questions:
        out.append(f"\n  {q.headline}")
        for line in q.detail_lines:
            if line:
                out.append(f"    {line}")
        if q.buttons:
            out.append("    [ " + " ]  [ ".join(b.label for b in q.buttons) + " ]")
    if prompt.flags:
        out.append(f"\n  --- flags for the digest (never shown to {prompt.person.name}) ---")
        for f in prompt.flags:
            out.append(f"    {f.kind:<9} {f.issue.key}  {f.detail}")
    return "\n".join(out)


if __name__ == "__main__":
    from jira_client import JiraClient, JiraError

    try:
        config = load_config()
        secrets = load_secrets()
    except ConfigError as e:
        print(f"Config problem: {e}")
        sys.exit(1)

    client = JiraClient(secrets, config)
    now = datetime.now(timezone.utc)

    try:
        issues = client.fetch_open_issues()

        # Changelog is one call per ticket, so only fetch it for active ones —
        # they're the only tickets staleness applies to.
        last_moved: dict[str, datetime] = {}
        for issue in issues:
            if issue.state in config.active_states:
                moved = client.last_status_change(issue.key)
                if moved:
                    last_moved[issue.key] = moved
    except JiraError as e:
        print(f"Jira problem: {e}")
        sys.exit(1)

    for person in config.team:
        prompt = build_prompt(person, issues, config, now, last_moved)
        print(render(prompt))
        print()
