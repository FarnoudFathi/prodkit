"""
Digest assembler.

Builds the post that lands in the producer's channel at cutoff. Pure text
assembly — no Discord, no Jira — so the layout can be checked at a terminal.

THE ORDERING RULE
-----------------
Sections run by urgency, not by person. Alphabetical-by-person is what every
standup bot does and it's wrong: it forces the producer to read all six entries
to find the two that matter. Here the top of the post is the only part that
needs action today, and the rest is scannable.

    idle          nobody has anything to work on
    handoff fail  board says done, person never received it
    blocked       reported blockers
    at risk       stale, overdue, over WIP — derived, nobody was asked
    moving        one compressed line per person
    notes         team chatter, kept away from the operational sections
    gaps          non-responders, skipped questions

WHAT ISN'T HERE YET
-------------------
`board_flags` is accepted as a separate input and rendered as its own section.
It's empty today. The cycle-time tool will fill it — overruns, projected
slippage — and the digest gains a section without a rewrite. Building the seam
before it's needed costs nothing now and avoids tearing this open later.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from config import Config
from models import Issue, Person
from questions import Flag
from store import Session, Store


@dataclass
class Section:
    """One block of the digest. `lines` are already formatted for display."""
    heading: str
    lines: list[str]
    severity: int = 0        # 0 informational, 1 attention, 2 urgent


@dataclass
class DigestResult:
    title: str
    subtitle: str
    sections: list[Section]
    footer: str

    @property
    def severity(self) -> int:
        return max((s.severity for s in self.sections), default=0)

    def as_text(self) -> str:
        """Plain-text fallback, for terminals and any surface without embeds."""
        out = [f"## {self.title}", f"_{self.subtitle}_"]
        for section in self.sections:
            out.append("")
            out.append(f"**{section.heading}**")
            out.extend(section.lines)
        out.append("")
        out.append(f"_{self.footer}_")
        return "\n".join(out)


@dataclass
class DigestInput:
    """Everything the digest needs, gathered by the caller."""
    date_label: str
    config: Config
    session: Session
    store: Store
    date_key: str
    people: list[Person]
    issues_by_key: dict[str, Issue]
    flags: list[Flag] = field(default_factory=list)
    board_flags: list[str] = field(default_factory=list)


# Actions that mean the person cannot work on that ticket right now.
STUCK_ACTIONS = {"blocked", "not_received"}

# Actions that mean work is underway or finished.
PROGRESS_ACTIONS = {"done", "ongoing", "starting", "got_starting"}

ACTION_WORDS = {
    "done": "ready for review",
    "ongoing": "still on it",
    "blocked": "blocked",
    "starting": "starting today",
    "got_starting": "unblocked, starting",
    "got_later": "unblocked, not today",
    "can_start": "can start, not today",
    "not_received": "not received",
}


def _summary(inp: DigestInput, key: str) -> str:
    issue = inp.issues_by_key.get(key)
    return issue.summary if issue else key


def _is_idle(inp: DigestInput, person: Person) -> bool:
    """
    Does this person have nothing they can work on?

    Derived from answers already given rather than asked. Nobody volunteers
    "I have nothing to do", so asking would under-report it — and this costs
    no taps. It's the most valuable line in the digest when it fires.

    Only counted when they actually answered; silence is a different problem,
    reported under gaps.
    """
    answers = inp.session.answered_by(person.discord_user_id)
    if not answers:
        return False
    return all(a.action in STUCK_ACTIONS or a.action == "done"
               for a in answers.values())


def build_digest(inp: DigestInput) -> DigestResult:
    sections: list[Section] = []
    responded = sum(1 for p in inp.people
                    if inp.session.has_responded(p.discord_user_id))

    # --- idle ---------------------------------------------------------
    idle = [p for p in inp.people if _is_idle(inp, p)]
    if idle:
        sections.append(Section(
            "⛔ Nothing to work on",
            [f"**{p.name}** — everything blocked or finished" for p in idle],
            severity=2,
        ))

    # --- handoff failures --------------------------------------------
    handoff_lines = []
    for person in inp.people:
        for key, answer in inp.session.answered_by(person.discord_user_id).items():
            if answer.action != "not_received":
                continue
            days = inp.store.handoff_failure_days(
                person.discord_user_id, key, inp.date_key
            )
            issue = inp.issues_by_key.get(key)
            blocker = issue.done_blockers[0] if issue and issue.done_blockers else None
            who = blocker.assignee_name if blocker and blocker.assignee_name else "someone"
            blocker_key = blocker.key if blocker else "its blocker"

            # Escalation is the point. Day one is a note; day three is roughly
            # 27 working hours of someone idle on work the board calls finished.
            age = "" if days <= 1 else f"  ·  **day {days}**"
            handoff_lines.append(
                f"`{key}` **{person.name}** waiting on {who}{age}\n"
                f"　　`{blocker_key}` marked done, never handed off"
            )
    if handoff_lines:
        sections.append(Section("📭 Handoff not completed", handoff_lines, severity=2))

    # --- reported blockers -------------------------------------------
    blocker_lines = []
    for person in inp.people:
        for key, answer in inp.session.answered_by(person.discord_user_id).items():
            if answer.action != "blocked":
                continue
            reason = f"\n　　_{answer.text}_" if answer.text else ""
            blocker_lines.append(
                f"`{key}` **{person.name}** {_summary(inp, key)}{reason}"
            )
    if blocker_lines:
        sections.append(Section("🚧 Blocked", blocker_lines, severity=2))

    # --- awaiting review ----------------------------------------------
    # Its own section rather than buried under risk, because it's the
    # producer's queue. A review column that silts up is the main failure mode
    # of adding a review step at all, so it needs to be visible daily.
    review_lines = [
        f"`{f.issue.key}` **{f.person.name}** — {f.detail}"
        for f in inp.flags if f.kind == "awaiting_review"
    ]
    if review_lines:
        sections.append(Section("🔍 Awaiting review", review_lines, severity=1))

    # --- derived risk -------------------------------------------------
    # Nobody was asked about any of this. It's read off the board, which is
    # exactly why it belongs to the producer rather than to a prompt.
    risk: list[str] = []
    for flag in inp.flags:
        if flag.kind in ("stale", "overdue", "due_soon"):
            icon = {"stale": "🕸", "overdue": "🔴", "due_soon": "🟡"}[flag.kind]
            risk.append(
                f"{icon} `{flag.issue.key}` **{flag.person.name}** — {flag.detail}"
            )

    for person in inp.people:
        active = [i for i in inp.issues_by_key.values()
                  if i.assignee_id == person.jira_account_id
                  and i.state in inp.config.active_states]
        if len(active) > inp.config.max_active_wip:
            risk.append(
                f"⚖️ **{person.name}** — {len(active)} tickets active, "
                f"cap is {inp.config.max_active_wip}"
            )

    risk.extend(inp.board_flags)

    if risk:
        sections.append(Section(
            "⚠️ At risk  ·  read from the board, nobody was asked",
            risk, severity=1,
        ))

    # --- movement -----------------------------------------------------
    moving = []
    for person in inp.people:
        answers = inp.session.answered_by(person.discord_user_id)
        parts = [
            f"{key} {ACTION_WORDS.get(a.action, a.action)}"
            for key, a in answers.items()
            if a.action in PROGRESS_ACTIONS or a.action == "can_start"
        ]
        if parts:
            moving.append(f"**{person.name}**  ·  {'  ·  '.join(parts)}")
    if moving:
        sections.append(Section("⏱ Moving", moving, severity=0))

    # --- notes --------------------------------------------------------
    # Kept in their own section so team chatter never sits next to blocker
    # reporting. The field exists because a bot that only extracts information
    # feels extractive, and people disengage from it.
    notes = []
    for person in inp.people:
        for key, answer in inp.session.raw_answers(person.discord_user_id).items():
            if answer.action == "note" and answer.text:
                notes.append(f"**{person.name}**  {answer.text}")
    if notes:
        sections.append(Section("💬 Notes", notes, severity=0))

    # --- gaps ---------------------------------------------------------
    gaps = []
    silent = inp.session.non_responders([p.discord_user_id for p in inp.people])
    if silent:
        names = ", ".join(
            p.name for p in inp.people if p.discord_user_id in silent
        )
        gaps.append(f"🔇 No response — {names}")

    for person in inp.people:
        skipped = inp.session.finished_without_answering(person.discord_user_id)
        if skipped:
            gaps.append(
                f"⏭ **{person.name}** closed standup leaving "
                f"{len(skipped)} unanswered — {', '.join(skipped)}"
            )

    if gaps:
        sections.append(Section("🔇 Gaps", gaps, severity=1))

    # --- write-back tally ---------------------------------------------
    # The line that shows this isn't a chat form.
    writes = sum(
        1
        for person in inp.people
        for a in inp.session.answered_by(person.discord_user_id).values()
        if a.action in ("done", "starting", "got_starting", "blocked")
    )
    plural = "ticket" if writes == 1 else "tickets"
    return DigestResult(
        title=f"{inp.config.project_key} standup — {inp.date_label}",
        subtitle=f"{responded} of {len(inp.people)} responded",
        sections=sections,
        footer=f"{writes} {plural} updated in Jira from this standup",
    )
