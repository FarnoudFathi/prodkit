"""
Write-back: turning a button tap into a change on the board.

This is the difference between a standup bot and a standup form. Sang taps
"Done" in Discord and the ticket moves in Jira. She never opens the board, and
the board is accurate for the first time — which also means the cycle-time tool
has real transition timestamps to work from.

WHAT WRITES AND WHAT DOESN'T
----------------------------
Only actions that unambiguously state intent touch Jira:

    done          -> Done            a finished ticket is finished
    starting      -> In Progress     "starting today" means starting
    got_starting  -> In Progress     same, after a confirmed handoff
    blocked       -> label + comment  status unchanged, on purpose

    ongoing       -> nothing          already correct
    can_start     -> nothing          able to start is not started
    got_later     -> nothing          received it is not started
    not_received  -> nothing          a handoff failure is not a board state

The three no-ops matter as much as the writes. Transitioning on "can start"
would fill In Progress with work nobody has begun, and the cycle-time engine
would count hours against tickets sitting untouched.

Blocked stays in its column deliberately. A label plus issue links keeps the
ticket in the status that reflects reality, so measurement is unaffected — a
Blocked column would break time-in-status for every ticket that passed through.
"""

from __future__ import annotations

from dataclasses import dataclass

from jira_client import JiraClient, JiraError
from models import State

# Actions that move a ticket, and where to. "done" is resolved at call time
# from the review setting — with review on it means "ready for review", not
# "finished", and sending it straight to Done would bypass the step entirely.
TRANSITIONS: dict[str, State] = {
    "starting": State.IN_PROGRESS,
    "got_starting": State.IN_PROGRESS,
    "approve": State.DONE,
    "send_back": State.IN_PROGRESS,
}

# Actions that record something without changing status.
ANNOTATIONS = {"blocked"}


@dataclass
class WriteResult:
    """What happened, in words fit to show the person who tapped."""
    attempted: bool
    ok: bool
    summary: str
    previous_status: str | None = None   # for undo


def _target_for(action: str, completion_state: State) -> State | None:
    if action == "done":
        return completion_state
    return TRANSITIONS.get(action)


def apply_action(
    jira: JiraClient,
    issue_key: str,
    action: str,
    text: str = "",
    blocked_label: str = "blocked",
    completion_state: State = State.DONE,
    reviewer_id: str | None = None,
) -> WriteResult:
    """
    Perform whatever this action means on the board.

    Failures are returned rather than raised. A Jira outage should not swallow
    someone's standup answer — the response still gets recorded, and the person
    is told plainly that the board was not updated so they aren't left assuming
    it was.
    """
    target = _target_for(action, completion_state)
    if target is not None:
        try:
            previous = jira.transition_to(issue_key, target)
            summary = f"{issue_key} moved to {target.value.replace('_', ' ')}"

            # Handing the ticket to a reviewer is what makes the review step
            # actually happen — otherwise it sits in the column with its
            # original assignee and nobody is prompted about it.
            if action == "done" and target is State.IN_REVIEW and reviewer_id:
                jira.assign(issue_key, reviewer_id)
                summary += ", handed to reviewer"

            # A failed review goes back to whoever did the work. Jira's
            # changelog already records who that was, so there's no state to
            # keep and it stays right even if the reassignment happened by hand.
            if action == "send_back":
                original = jira.previous_assignee(issue_key)
                if original:
                    jira.assign(issue_key, original)
                    summary += ", returned to the author"
                if text:
                    jira.add_comment(issue_key, f"Review feedback: {text}")

            return WriteResult(True, True, summary, previous_status=previous)
        except JiraError as e:
            return WriteResult(
                attempted=True, ok=False,
                summary=f"could not update {issue_key} — {e}",
            )

    if action in ANNOTATIONS:
        try:
            jira.add_label(issue_key, blocked_label)
            if text:
                jira.add_comment(issue_key, f"Blocked (via standup): {text}")
            return WriteResult(
                attempted=True, ok=True,
                summary=f"{issue_key} labelled {blocked_label}",
            )
        except JiraError as e:
            return WriteResult(
                attempted=True, ok=False,
                summary=f"could not flag {issue_key} — {e}",
            )

    return WriteResult(attempted=False, ok=True, summary="")


def undo_action(
    jira: JiraClient,
    issue_key: str,
    action: str,
    previous_status: str | None,
    blocked_label: str = "blocked",
) -> WriteResult:
    """
    Reverse a write-back when someone changes their answer.

    Reverting a transition needs the exact status the ticket came from, which
    is only known if this process performed the original write. After a restart
    that memory is gone, so the person is told to fix it manually rather than
    the bot guessing — a wrong guess would corrupt the changelog the
    cycle-time tool depends on.
    """
    if action in TRANSITIONS or action == "done":
        if not previous_status:
            return WriteResult(
                attempted=True, ok=False,
                summary=f"answer cleared, but {issue_key} was already moved "
                        f"in Jira — change it there if needed",
            )
        try:
            jira.transition_to_status_name(issue_key, previous_status)
            return WriteResult(
                attempted=True, ok=True,
                summary=f"{issue_key} moved back to {previous_status}",
            )
        except JiraError as e:
            return WriteResult(
                attempted=True, ok=False,
                summary=f"could not revert {issue_key} — {e}",
            )

    if action in ANNOTATIONS:
        try:
            jira.remove_label(issue_key, blocked_label)
            return WriteResult(
                attempted=True, ok=True,
                summary=f"{blocked_label} label removed from {issue_key}",
            )
        except JiraError as e:
            return WriteResult(
                attempted=True, ok=False,
                summary=f"could not remove label — {e}",
            )

    return WriteResult(attempted=False, ok=True, summary="")
