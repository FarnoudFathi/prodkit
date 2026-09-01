"""
Setup validator.

Checks the config against reality — the live Jira board and the live Discord
server — and reports everything wrong at once.

WHY THIS EXISTS
---------------
config.py validates that the file is internally consistent. It cannot know
whether the column named in status_map actually exists on the board, whether an
account id belongs to a real person, or whether someone has blocked DMs. Those
only surface at runtime, and runtime is 11:00 on test day with five people
waiting.

Nearly every check here corresponds to something that actually broke during
development: a column renamed on the board but not in config, Windows shipping
no timezone database, the board measuring story points while the tool read
hours, a component with no lead silently swallowing the review step.

    python doctor.py
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone

from config import ConfigError, load_config, load_secrets
from jira_client import JiraClient, JiraError
from models import State

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"

ICON = {PASS: "  ok  ", WARN: " warn ", FAIL: " FAIL "}


class Report:
    def __init__(self):
        self.rows: list[tuple[str, str, str]] = []

    def add(self, level: str, check: str, detail: str = "") -> None:
        self.rows.append((level, check, detail))

    def show(self) -> int:
        width = max(len(c) for _, c, _ in self.rows) + 2
        current_section = None
        for level, check, detail in self.rows:
            if check.startswith("#"):
                current_section = check[1:].strip()
                print(f"\n{current_section}")
                print("-" * 64)
                continue
            line = f"[{ICON[level]}] {check.ljust(width)}"
            if detail:
                line += detail
            print(line)

        fails = sum(1 for l, c, _ in self.rows if l == FAIL and not c.startswith("#"))
        warns = sum(1 for l, c, _ in self.rows if l == WARN and not c.startswith("#"))
        print()
        if fails:
            print(f"{fails} failure(s), {warns} warning(s) — fix failures before running.")
        elif warns:
            print(f"No failures, {warns} warning(s) — safe to run, worth a look.")
        else:
            print("All checks passed.")
        return 1 if fails else 0


def check_timezones(config, report: Report) -> None:
    report.add(PASS, "# Timezones")
    for person in config.team:
        try:
            _ = person.calendar.tz
            now_local = datetime.now(timezone.utc).astimezone(person.calendar.tz)
            report.add(PASS, person.name,
                       f"{person.calendar.timezone} · now {now_local:%H:%M}")
        except Exception as e:
            # On Windows this fails for everyone at once: Python reads the OS
            # timezone database and Windows ships none. Install tzdata.
            report.add(FAIL, person.name, f"{e} — try: pip install tzdata")


def check_jira(config, jira: JiraClient, report: Report) -> None:
    report.add(PASS, "# Jira")

    try:
        me = jira._get("/rest/api/3/myself")
        report.add(PASS, "authentication", f"as {me.get('displayName')}")
    except JiraError as e:
        report.add(FAIL, "authentication", str(e))
        return

    # Every status on the board must be in status_map. A column that exists but
    # isn't mapped raises at runtime — mid-standup, on whichever ticket happens
    # to be in it.
    try:
        statuses = jira._get(f"/rest/api/3/project/{config.project_key}/statuses")
        found = {s["name"] for group in statuses for s in group.get("statuses", [])}
        unmapped = found - set(config.status_map)
        missing = set(config.status_map) - found

        if unmapped:
            report.add(FAIL, "all board columns mapped",
                       f"not in config: {', '.join(sorted(unmapped))}")
        else:
            report.add(PASS, "all board columns mapped", f"{len(found)} statuses")

        if missing:
            report.add(WARN, "config columns exist on board",
                       f"configured but absent: {', '.join(sorted(missing))}")
    except JiraError as e:
        report.add(WARN, "status list", str(e))

    # The write-back path. If no transition reaches the completion state from a
    # ticket's current position, the main button silently fails.
    try:
        issues = jira.search(
            f"project = {config.project_key} AND statusCategory != Done"
        )
        report.add(PASS, "open issues readable", f"{len(issues)} found")

        if issues:
            sample = issues[0]
            payload = jira._get(f"/rest/api/3/issue/{sample.key}/transitions")
            reachable = set()
            for t in payload.get("transitions", []):
                try:
                    reachable.add(config.state_for(t["to"]["name"]))
                except ConfigError:
                    pass
            target = config.review.completion_state
            if target in reachable:
                report.add(PASS, "completion transition",
                           f"{sample.key} can reach {target.value}")
            else:
                report.add(FAIL, "completion transition",
                           f"{sample.key} cannot reach {target.value}")

        # sprint_only silently returns nothing when no sprint is running, and
        # the symptom is that nobody gets prompted with no error anywhere.
        if config.sprint_only and not issues:
            report.add(FAIL, "open sprint",
                       "sprint_only is on but the query returned nothing — "
                       "start a sprint, or set jira.sprint_only: false")
        elif config.sprint_only:
            report.add(PASS, "open sprint", f"{len(issues)} issues in scope")

        unassigned = [i for i in issues if not i.assignee_id]
        if unassigned:
            # Unassigned tickets belong to nobody's standup, so nobody is ever
            # asked about them and they drift silently.
            report.add(WARN, "all issues assigned",
                       f"{len(unassigned)} unassigned: "
                       + ", ".join(i.key for i in unassigned[:5]))
        else:
            report.add(PASS, "all issues assigned")

        estimated = [i for i in issues if i.estimate_hours]
        report.add(
            PASS if estimated else WARN,
            "estimates present",
            f"{len(estimated)}/{len(issues)} have an original estimate",
        )

        linked = sum(len(i.blockers) for i in issues)
        report.add(
            PASS if linked else WARN,
            "blocker links",
            f"{linked} 'is blocked by' links"
            + ("" if linked else " — handoff verification will never fire"),
        )
    except (JiraError, ConfigError) as e:
        report.add(FAIL, "board readable", str(e))

    # Everyone in config must be a real Jira account, or their tickets are
    # invisible to the tool.
    for person in config.team:
        try:
            jira._get("/rest/api/3/user", {"accountId": person.jira_account_id})
            report.add(PASS, f"jira account · {person.name}")
        except JiraError:
            report.add(FAIL, f"jira account · {person.name}",
                       "account id not found")

    if config.review.enabled and config.review.reviewer == "component_lead":
        try:
            components = jira._get(
                f"/rest/api/3/project/{config.project_key}/components"
            )
            items = components if isinstance(components, list) else components.get("values", [])
            if not items:
                report.add(FAIL, "components exist",
                           "review is on but the project has no components")
            for component in items:
                lead = component.get("lead") or component.get("componentLead")
                if lead:
                    report.add(PASS, f"component lead · {component['name']}",
                               lead.get("displayName", ""))
                else:
                    # Silent failure: tickets in this component move to review
                    # and land on nobody.
                    report.add(WARN, f"component lead · {component['name']}",
                               "no lead — reviews here go unassigned")
        except JiraError as e:
            report.add(WARN, "components", str(e))


async def check_discord(config, secrets, report: Report) -> None:
    import discord

    intents = discord.Intents.default()
    intents.members = True
    client = discord.Client(intents=intents)
    done = {"ok": False}

    @client.event
    async def on_ready():
        report.add(PASS, "# Discord")
        report.add(PASS, "authentication", f"as {client.user}")

        guild = client.get_guild(config.guild_id)
        if guild is None:
            report.add(FAIL, "guild visible", "bot is not in the configured server")
            await client.close()
            return
        report.add(PASS, "guild visible", guild.name)

        for label, channel_id in (
            ("standup channel", config.standup_channel_id),
            ("digest channel", config.digest_channel_id),
        ):
            channel = guild.get_channel(channel_id)
            if channel is None:
                report.add(FAIL, label, "not found or not visible")
                continue
            perms = channel.permissions_for(guild.me)
            needed = {
                "send messages": perms.send_messages,
                "embed links": perms.embed_links,
                "create threads": perms.create_public_threads,
                "send in threads": perms.send_messages_in_threads,
            }
            missing = [n for n, ok in needed.items() if not ok]
            if missing:
                report.add(FAIL, label, f"#{channel.name} missing: {', '.join(missing)}")
            else:
                report.add(PASS, label, f"#{channel.name}")

        for person in config.team:
            member = guild.get_member(person.discord_user_id)
            if member is None:
                report.add(FAIL, f"discord member · {person.name}",
                           "not in the server")
                continue
            report.add(PASS, f"discord member · {person.name}", member.name)

            if config.dm_thread_link:
                # People can block DMs from server members. Better to find out
                # now than to have thread links silently not arrive.
                try:
                    dm = await member.create_dm()
                    await dm.typing()
                    report.add(PASS, f"dm reachable · {person.name}")
                except discord.Forbidden:
                    report.add(WARN, f"dm reachable · {person.name}",
                               "DMs closed — thread link won't arrive")
                except discord.HTTPException as e:
                    report.add(WARN, f"dm reachable · {person.name}", str(e))

        done["ok"] = True
        await client.close()

    try:
        await client.start(secrets.discord_bot_token)
    except discord.LoginFailure:
        report.add(FAIL, "discord authentication", "token rejected")
    except discord.PrivilegedIntentsRequired:
        report.add(FAIL, "discord intents", "Server Members Intent is off")


def check_config_coherence(config, report: Report) -> None:
    report.add(PASS, "# Configuration")
    report.add(PASS, "schedule",
               f"{config.prompt_time} → {config.nudge_time} → {config.cutoff_time}"
               + (f" → {config.eod_time}" if config.eod_enabled else ""))
    report.add(PASS, "mode", config.mode)
    report.add(
        PASS if not config.review.enabled or State.IN_REVIEW in config.status_map.values()
        else FAIL,
        "review setting",
        f"{'on' if config.review.enabled else 'off'} · "
        f"completion → {config.review.completion_state.value}",
    )
    if len(config.team) == 1:
        report.add(WARN, "team size",
                   "one person — handoff verification cannot be demonstrated solo")


def main() -> int:
    try:
        config = load_config()
        secrets = load_secrets()
    except ConfigError as e:
        print(f"Config problem: {e}")
        return 1

    report = Report()
    check_config_coherence(config, report)
    check_timezones(config, report)

    try:
        check_jira(config, JiraClient(secrets, config), report)
    except Exception as e:
        report.add(FAIL, "jira", str(e))

    try:
        import asyncio
        asyncio.run(check_discord(config, secrets, report))
    except Exception as e:
        report.add(FAIL, "discord", str(e))

    return report.show()


if __name__ == "__main__":
    sys.exit(main())
