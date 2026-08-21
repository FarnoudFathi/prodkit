"""
Jira read layer.

Everything that knows how Jira's API is shaped lives in this file. The rest of
the project works with Issue and BlockerRef objects and never sees a raw JSON
response — so a change on Atlassian's side is absorbed here rather than rippling
through the bot, the digest, and the cycle-time engine.

Run it directly to dump the board grouped by person:

    python jira_client.py
"""

from __future__ import annotations

import sys
from datetime import datetime, time, timedelta, timezone

import requests

from config import Config, ConfigError, Secrets, load_config, load_secrets
from models import BlockerRef, Issue, Person, State

# Fields we ask Jira for. Requesting explicitly rather than taking everything
# keeps responses small and makes it obvious what the tool actually depends on.
FIELDS = [
    "summary",
    "status",
    "assignee",
    "duedate",
    "timetracking",
    "components",
    "labels",
    "issuetype",
    "issuelinks",
]

# Jira's link types are named per-site, but the direction is what matters.
# On an "is blocked by" link, the INWARD side is the ticket that's waiting.
BLOCKED_BY_INWARD = "is blocked by"

# A move away from a status and straight back inside this window is treated as
# a correction rather than movement, so it doesn't reset the staleness clock.
# Thirty minutes is comfortably longer than a mis-tap and an undo, and far
# shorter than any real spell of work.
BOUNCE_WINDOW = timedelta(minutes=30)


class JiraError(Exception):
    pass


class JiraClient:
    def __init__(self, secrets: Secrets, config: Config):
        self.base = f"https://{secrets.jira_site}"
        self.config = config

        # A Session reuses the underlying TCP connection across requests.
        # Fetching a board plus per-issue transitions is dozens of calls, and
        # reconnecting each time is wasted latency.
        self.session = requests.Session()
        self.session.auth = (secrets.jira_email, secrets.jira_api_token)
        self.session.headers.update({"Accept": "application/json"})

    # ------------------------------------------------------------ HTTP

    def _get(self, path: str, params: dict | None = None) -> dict:
        try:
            response = self.session.get(
                f"{self.base}{path}", params=params or {}, timeout=30
            )
        except requests.RequestException as e:
            raise JiraError(f"Could not reach Jira: {e}") from e

        if response.status_code == 401:
            raise JiraError("Jira rejected the credentials (401). Check .env.")
        if response.status_code == 403:
            raise JiraError("Jira denied access (403). Check project permissions.")
        if not response.ok:
            raise JiraError(f"Jira {response.status_code} on {path}: {response.text[:200]}")

        return response.json()

    # ------------------------------------------------------- parsing

    @staticmethod
    def _parse_due(value: str | None) -> datetime | None:
        """
        Jira stores due date as a bare date with no time — "2026-08-24".

        We anchor it to end of day rather than midnight. A ticket due Monday is
        due by the end of Monday; treating it as 00:00 would report it overdue
        for the whole day it's actually due. Timezone is applied later against
        the assignee's own calendar.
        """
        if not value:
            return None
        parsed = datetime.strptime(value, "%Y-%m-%d")
        return datetime.combine(parsed.date(), time(23, 59), tzinfo=timezone.utc)

    @staticmethod
    def _parse_estimate(timetracking: dict | None) -> float | None:
        """
        Read the original estimate in hours.

        Jira returns an empty object rather than a null field when no estimate
        is set, so .get() on a missing key is the normal case, not an error.
        Reading originalEstimateSeconds rather than the display string avoids
        parsing "1d 4h", which would need Jira's working-day length to decode.
        """
        if not timetracking:
            return None
        seconds = timetracking.get("originalEstimateSeconds")
        return seconds / 3600.0 if seconds else None

    def _parse_blockers(self, issuelinks: list[dict]) -> tuple[BlockerRef, ...]:
        """
        Extract the tickets this issue is waiting on.

        Jira represents a link once, from both sides. When our issue is the one
        waiting, the payload carries an `inwardIssue` and the inward description
        reads "is blocked by". When our issue is the blocker, it carries an
        `outwardIssue` instead — and we ignore that, because being a blocker is
        not something to ask its owner about during their own standup.
        """
        blockers: list[BlockerRef] = []

        for link in issuelinks or []:
            link_type = link.get("type", {})
            inward_issue = link.get("inwardIssue")

            if not inward_issue:
                continue
            if link_type.get("inward", "").lower() != BLOCKED_BY_INWARD:
                continue

            fields = inward_issue.get("fields", {})
            status_name = fields.get("status", {}).get("name", "")

            # A blocker in an unmapped column shouldn't crash the whole standup.
            # Treat it as unfinished, which is the safe assumption: at worst the
            # person is told they're waiting on something already delivered.
            try:
                state = self.config.state_for(status_name)
            except ConfigError:
                state = State.TODO

            assignee = fields.get("assignee") or {}

            blockers.append(
                BlockerRef(
                    key=inward_issue["key"],
                    summary=fields.get("summary", ""),
                    status_name=status_name,
                    state=state,
                    assignee_name=assignee.get("displayName"),
                )
            )

        return tuple(blockers)

    def _to_issue(self, raw: dict) -> Issue:
        fields = raw["fields"]
        status_name = fields["status"]["name"]
        assignee = fields.get("assignee") or {}

        return Issue(
            key=raw["key"],
            summary=fields.get("summary", ""),
            issue_type=fields.get("issuetype", {}).get("name", "Task"),
            status_name=status_name,
            state=self.config.state_for(status_name),
            assignee_id=assignee.get("accountId"),
            assignee_name=assignee.get("displayName"),
            due_date=self._parse_due(fields.get("duedate")),
            estimate_hours=self._parse_estimate(fields.get("timetracking")),
            components=tuple(c["name"] for c in fields.get("components", [])),
            labels=tuple(fields.get("labels", [])),
            blockers=self._parse_blockers(fields.get("issuelinks", [])),
        )

    # -------------------------------------------------------- fetching

    # -------------------------------------------------------- writing

    def _post(self, path: str, body: dict) -> None:
        try:
            response = self.session.post(
                f"{self.base}{path}", json=body, timeout=30
            )
        except requests.RequestException as e:
            raise JiraError(f"Could not reach Jira: {e}") from e

        if response.status_code == 403:
            raise JiraError(f"Jira denied the write (403) on {path}")
        if not response.ok:
            raise JiraError(
                f"Jira {response.status_code} on {path}: {response.text[:200]}"
            )

    def _put(self, path: str, body: dict) -> None:
        try:
            response = self.session.put(
                f"{self.base}{path}", json=body, timeout=30
            )
        except requests.RequestException as e:
            raise JiraError(f"Could not reach Jira: {e}") from e

        if not response.ok:
            raise JiraError(
                f"Jira {response.status_code} on {path}: {response.text[:200]}"
            )

    def current_status(self, key: str) -> str:
        """The ticket's status name right now, straight from Jira."""
        payload = self._get(f"/rest/api/3/issue/{key}", {"fields": "status"})
        return payload["fields"]["status"]["name"]

    def transition_to(self, key: str, target: State) -> str:
        """
        Move a ticket into a canonical state. Returns the status it left.

        Jira does not let you set a status directly. You have to ask which
        transitions are legal from where the ticket currently sits, then POST
        the matching transition id — and those ids differ per workflow and per
        starting status. That's why this is a lookup at call time rather than a
        constant in config: hardcoding ids would work on this board and break on
        anyone else's.
        """
        payload = self._get(f"/rest/api/3/issue/{key}/transitions")
        available = payload.get("transitions", [])

        chosen = None
        for transition in available:
            status_name = transition["to"]["name"]
            try:
                if self.config.state_for(status_name) is target:
                    chosen = transition
                    break
            except ConfigError:
                # A column not in config can't be a valid destination for us.
                continue

        if chosen is None:
            legal = ", ".join(t["to"]["name"] for t in available) or "none"
            raise JiraError(
                f"No transition from {key}'s current status reaches "
                f"{target.value}. Legal moves: {legal}"
            )

        previous = self.current_status(key)
        self._post(
            f"/rest/api/3/issue/{key}/transitions",
            {"transition": {"id": chosen["id"]}},
        )
        return previous

    def transition_to_status_name(self, key: str, status_name: str) -> None:
        """
        Move a ticket to a specific Jira status by name.

        Used to undo a write-back when someone changes their answer — we need
        the exact status it came from, not a canonical state, because To Do and
        In Review could both map through the same enum on some boards.
        """
        payload = self._get(f"/rest/api/3/issue/{key}/transitions")
        for transition in payload.get("transitions", []):
            if transition["to"]["name"] == status_name:
                self._post(
                    f"/rest/api/3/issue/{key}/transitions",
                    {"transition": {"id": transition["id"]}},
                )
                return
        raise JiraError(f"Cannot move {key} back to {status_name}")

    def component_lead(self, component_name: str) -> str | None:
        """
        The account id of a component's Lead, or None.

        Read from Jira rather than duplicated in config so there's one source
        of truth. A team that reorganises updates Jira and the bot follows.
        """
        components = self._get(f"/rest/api/3/project/{self.config.project_key}/components")
        items = components if isinstance(components, list) else components.get("values", [])
        for component in items:
            if component.get("name") == component_name:
                lead = component.get("lead") or component.get("componentLead") or {}
                return lead.get("accountId")
        return None

    def assign(self, key: str, account_id: str | None) -> None:
        """Set the assignee. None unassigns."""
        self._put(f"/rest/api/3/issue/{key}", {"fields": {"assignee":
                  {"accountId": account_id} if account_id else None}})

    def previous_assignee(self, key: str) -> str | None:
        """
        Who held this ticket before the most recent reassignment.

        Needed to send a failed review back to the person who did the work.
        Jira logs assignee changes in the same changelog as status changes, so
        the answer is already recorded — no state to maintain, and it stays
        correct even if the reassignment happened outside the bot.
        """
        payload = self._get(
            f"/rest/api/3/issue/{key}", {"expand": "changelog", "fields": "assignee"}
        )
        changes = []
        for entry in payload.get("changelog", {}).get("histories", []):
            for item in entry.get("items", []):
                if item.get("field") == "assignee":
                    changes.append((entry["created"], item.get("from")))
        if not changes:
            return None
        changes.sort(key=lambda c: c[0])
        return changes[-1][1]

    def add_label(self, key: str, label: str) -> None:
        """Add a label without disturbing existing ones."""
        self._put(
            f"/rest/api/3/issue/{key}",
            {"update": {"labels": [{"add": label}]}},
        )

    def remove_label(self, key: str, label: str) -> None:
        self._put(
            f"/rest/api/3/issue/{key}",
            {"update": {"labels": [{"remove": label}]}},
        )

    def add_comment(self, key: str, text: str) -> None:
        """
        Post a comment.

        API v3 requires Atlassian Document Format rather than plain text — a
        nested JSON structure describing the content. Sending a bare string
        returns a 400 with an unhelpful message, so the wrapping happens here
        rather than being rediscovered at every call site.
        """
        self._post(
            f"/rest/api/3/issue/{key}/comment",
            {
                "body": {
                    "type": "doc",
                    "version": 1,
                    "content": [
                        {
                            "type": "paragraph",
                            "content": [{"type": "text", "text": text}],
                        }
                    ],
                }
            },
        )

    # ------------------------------------------------------- fetching

    def search(self, jql: str) -> list[Issue]:
        """
        Run a JQL query and return every matching issue.

        Pages through results using nextPageToken. Jira caps page size, so a
        board larger than one page would silently truncate without this — and a
        truncated board means people quietly stop being asked about tickets.
        """
        issues: list[Issue] = []
        next_token: str | None = None

        while True:
            params = {
                "jql": jql,
                "maxResults": 100,
                "fields": ",".join(FIELDS),
            }
            if next_token:
                params["nextPageToken"] = next_token

            payload = self._get("/rest/api/3/search/jql", params)

            for raw in payload.get("issues", []):
                issues.append(self._to_issue(raw))

            next_token = payload.get("nextPageToken")
            if payload.get("isLast", True) or not next_token:
                break

        return issues

    def status_history(self, key: str) -> tuple[list[tuple[datetime, str, str]], datetime]:
        """
        Every status change on this ticket, oldest first, plus its creation time.

        Each entry is (when, from_status, to_status) using Jira's own status
        names. Raw history rather than a single timestamp, because staleness
        needs to distinguish real movement from a correction — see
        time_in_current_status.
        """
        payload = self._get(
            f"/rest/api/3/issue/{key}",
            {"expand": "changelog", "fields": "created"},
        )

        created = datetime.fromisoformat(
            payload["fields"]["created"].replace("Z", "+00:00")
        )

        history: list[tuple[datetime, str, str]] = []
        for entry in payload.get("changelog", {}).get("histories", []):
            when = datetime.fromisoformat(entry["created"].replace("Z", "+00:00"))
            for item in entry.get("items", []):
                # Only status changes count as movement. An assignee or
                # description edit is not a sign of progress.
                if item.get("field") != "status":
                    continue
                history.append((
                    when,
                    item.get("fromString") or "",
                    item.get("toString") or "",
                ))

        history.sort(key=lambda h: h[0])
        return history, created

    def time_in_current_status(self, key: str) -> tuple[datetime, int]:
        """
        When the ticket effectively entered the status it's in now, and how many
        corrections were collapsed to work that out.

        THE PROBLEM
        -----------
        A ticket moved to Done and immediately moved back writes two entries to
        the changelog. Measured naively, its staleness clock resets — so a
        ticket untouched for a week can be made to look fresh by tapping Done
        and then Change answer. Nobody has to intend to game it for this to
        corrupt the data; an honest mis-tap does the same damage.

        THE FIX
        -------
        A move away and straight back within BOUNCE_WINDOW is a correction, not
        work. Those pairs are collapsed and the clock keeps running from before
        them. A genuine reopen — Done for two days, then back to In Progress —
        falls outside the window and correctly resets.

        The count is returned rather than discarded, because repeated
        corrections on one ticket are themselves worth showing a producer.
        """
        history, created = self.status_history(key)
        if not history:
            return created, 0

        current = history[-1][2]
        index = len(history) - 1
        entered = history[index][0]
        bounces = 0

        while index >= 2:
            back_in = history[index]        # something -> current
            went_out = history[index - 1]   # current -> something

            is_round_trip = (
                back_in[2] == current
                and went_out[1] == current
                and history[index - 2][2] == current
            )
            if not is_round_trip:
                break

            if back_in[0] - went_out[0] > BOUNCE_WINDOW:
                break

            bounces += 1
            index -= 2
            entered = history[index][0]

        return entered, bounces

    def last_status_change(self, key: str) -> datetime | None:
        """Kept for callers that only need the corrected entry time."""
        entered, _ = self.time_in_current_status(key)
        return entered

    def _enrich_blockers(self, issues: list[Issue]) -> None:
        """
        Fill in blocker assignees.

        Jira's issuelinks payload carries only a stub of the linked ticket —
        key, summary, status, type. No assignee. That's a problem, because the
        useful half of a handoff flag is *who* to chase: "waiting on Mia" is
        actionable, "waiting on someone" is not.

        Rather than one lookup per blocker, this collects every blocker key on
        the board and resolves them in a single JQL query. On a board with a
        dozen dependencies that's one request instead of twelve.

        Blockers are frequently Done, so they don't appear in fetch_open_issues
        and can't be resolved from data already in hand.
        """
        keys = {b.key for issue in issues for b in issue.blockers}
        if not keys:
            return

        key_list = ", ".join(sorted(keys))
        payload = self._get(
            "/rest/api/3/search/jql",
            {
                "jql": f"key in ({key_list})",
                "maxResults": 100,
                "fields": "summary,status,assignee",
            },
        )

        resolved: dict[str, dict] = {}
        for raw in payload.get("issues", []):
            fields = raw["fields"]
            assignee = fields.get("assignee") or {}
            resolved[raw["key"]] = {
                "status_name": fields["status"]["name"],
                "assignee_name": assignee.get("displayName"),
            }

        for issue in issues:
            rebuilt = []
            for blocker in issue.blockers:
                info = resolved.get(blocker.key)
                if not info:
                    rebuilt.append(blocker)
                    continue

                status_name = info["status_name"]
                try:
                    state = self.config.state_for(status_name)
                except ConfigError:
                    state = blocker.state

                rebuilt.append(
                    BlockerRef(
                        key=blocker.key,
                        summary=blocker.summary,
                        status_name=status_name,
                        state=state,
                        assignee_name=info["assignee_name"],
                    )
                )
            issue.blockers = tuple(rebuilt)

    def fetch_open_issues(self) -> list[Issue]:
        """
        Everything not yet finished on the project.

        Done tickets are excluded here because standup only asks about live
        work. They're still reachable via search() when the cycle-time engine
        needs completed history.
        """
        issues = self.search(
            f"project = {self.config.project_key} "
            f"AND statusCategory != Done "
            f"ORDER BY duedate ASC, key ASC"
        )
        self._enrich_blockers(issues)
        return issues

    def issues_for(self, person: Person, issues: list[Issue]) -> dict[str, list[Issue]]:
        """
        Split one person's open work into what to ask about.

        Sorted by due date with undated tickets last, then capped by config.
        The caps exist because someone with twelve open tickets who receives
        twelve prompts answers none of them.
        """
        theirs = [i for i in issues if i.assignee_id == person.jira_account_id]

        def by_due(issue: Issue):
            # datetime.max keeps undated tickets at the end without needing a
            # second sort pass or a None check in the comparison.
            return issue.due_date or datetime.max.replace(tzinfo=timezone.utc)

        active = sorted(
            [i for i in theirs if i.state in self.config.active_states], key=by_due
        )
        todo = sorted([i for i in theirs if i.state is State.TODO], key=by_due)

        return {
            "active": active[: self.config.max_in_progress],
            "todo": todo[: self.config.max_todo],
        }


if __name__ == "__main__":
    try:
        config = load_config()
        secrets = load_secrets()
    except ConfigError as e:
        print(f"Config problem: {e}")
        sys.exit(1)

    client = JiraClient(secrets, config)

    try:
        issues = client.fetch_open_issues()
    except JiraError as e:
        print(f"Jira problem: {e}")
        sys.exit(1)
    except ConfigError as e:
        print(f"Board has a status not in config: {e}")
        sys.exit(1)

    print(f"Open issues on {config.project_key}: {len(issues)}\n")

    unassigned = [i for i in issues if not i.assignee_id]

    for person in config.team:
        buckets = client.issues_for(person, issues)
        print(f"{person.name} ({person.discipline})")

        if not buckets["active"] and not buckets["todo"]:
            print("  nothing open\n")
            continue

        for issue in buckets["active"]:
            estimate = f"{issue.estimate_hours:.0f}h" if issue.estimate_hours else "no est"
            print(f"  [{issue.status_name}] {issue.key}  {issue.summary[:44]:<46} {estimate}")

        for issue in buckets["todo"]:
            due = issue.due_date.strftime("%a %d %b") if issue.due_date else "no due date"
            print(f"  [To Do]  {issue.key}  {issue.summary[:44]:<46} {due}")

            # The two branches the whole handoff feature turns on.
            for blocker in issue.open_blockers:
                print(f"      waiting on {blocker.key} ({blocker.status_name}) "
                      f"— cannot start, no question asked")
            for blocker in issue.done_blockers:
                who = blocker.assignee_name or "someone"
                print(f"      {blocker.key} is Done ({who}) "
                      f"— ASK: do you have what you need?")
        print()

    if unassigned:
        print(f"Unassigned ({len(unassigned)}): "
              + ", ".join(i.key for i in unassigned))
        print("These are invisible to standup — nobody gets asked about them.")
