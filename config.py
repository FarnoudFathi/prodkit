"""
Loads and validates config.yaml.

Validation happens here rather than at the point of use, on purpose. A missing
channel ID should fail at startup with a clear message, not three hours later
when the digest tries to post into nothing.

Run it directly to see what it parsed:

    python config.py
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import yaml
from dotenv import load_dotenv

from models import Person, State, WorkCalendar

CONFIG_PATH = Path(__file__).parent / "config.yaml"


class ConfigError(Exception):
    """Raised when config.yaml is wrong in a way that would break the bot."""


@dataclass
class Secrets:
    """Values from .env. Never printed, never logged, never committed."""
    jira_site: str
    jira_email: str
    jira_api_token: str
    discord_bot_token: str


@dataclass
class ReviewSettings:
    enabled: bool
    reviewer: str            # component_lead | fixed | none
    fixed_reviewer: str

    @property
    def completion_state(self) -> State:
        """Where the completion button sends a ticket."""
        return State.IN_REVIEW if self.enabled else State.DONE

    @property
    def completion_label(self) -> str:
        return "✅ Ready for review" if self.enabled else "✅ Done"


@dataclass
class Config:
    project_key: str
    status_map: dict[str, State]
    active_states: set[State]
    blocked_label: str
    review: ReviewSettings

    guild_id: int
    standup_channel_id: int
    digest_channel_id: int
    mode: str

    prompt_time: str
    nudge_time: str
    cutoff_time: str
    eod_time: str
    eod_close_time: str
    eod_enabled: bool
    active_days: tuple[int, ...]

    max_in_progress: int
    max_todo: int
    dropdown_options: int
    max_active_wip: int

    rank_active: tuple[str, ...]
    rank_todo: tuple[str, ...]
    rank_dropdown: tuple[str, ...]

    dm_thread_link: bool
    cleanup_keep_days: int

    team: list[Person]

    @property
    def project_timezone(self):
        """
        The timezone that defines when "today" starts and ends.

        The session file is shared by the whole team, so the day boundary has to
        be one agreed instant rather than each person's midnight. The first team
        member's zone is used, which on a single-timezone team is simply the
        team's zone, and on a distributed one anchors the day to the producer.

        This is not cosmetic. Deriving the date in UTC means the day rolls over
        at 17:00 Pacific — mid-afternoon, hours before anyone has finished. The
        session would reset while people are still answering, and the digest
        would fire again against an empty day.
        """
        return self.team[0].calendar.tz

    def date_key(self, now: datetime) -> str:
        """Today's session key, in project-local time."""
        return now.astimezone(self.project_timezone).strftime("%Y-%m-%d")

    def person_by_jira_id(self, account_id: str) -> Person | None:
        for person in self.team:
            if person.jira_account_id == account_id:
                return person
        return None

    def person_by_discord_id(self, user_id: int) -> Person | None:
        for person in self.team:
            if person.discord_user_id == user_id:
                return person
        return None

    def state_for(self, jira_status_name: str) -> State:
        """
        Map a Jira column name onto a canonical state.

        Unknown columns raise rather than defaulting. A column that appeared on
        the board without being added to config is a real configuration gap —
        silently treating it as To Do would make tickets vanish from prompts
        with no error anywhere.
        """
        if jira_status_name not in self.status_map:
            raise ConfigError(
                f"Jira status {jira_status_name!r} is not in status_map. "
                f"Known: {', '.join(sorted(self.status_map))}"
            )
        return self.status_map[jira_status_name]


def _clean_env(name: str) -> str | None:
    """
    Read an environment variable and strip surrounding whitespace.

    Pasting a value into a hosting dashboard frequently carries a trailing
    newline, and nothing downstream notices: the newline gets URL-encoded as
    %0a and lands in a DNS lookup, producing a "name or service not known"
    error that names a hostname looking correct to the eye.

    Stripping here rather than at each use means the whole program can trust
    these values. Cheap, and it removes a class of failure that is genuinely
    hard to read when it happens.
    """
    value = os.getenv(name)
    return value.strip() if value else value


def load_secrets() -> Secrets:
    # No .env file on a host — python-dotenv is a no-op there and os.getenv
    # falls through to real environment variables.
    load_dotenv()
    required = {
        "JIRA_SITE": _clean_env("JIRA_SITE"),
        "JIRA_EMAIL": _clean_env("JIRA_EMAIL"),
        "JIRA_API_TOKEN": _clean_env("JIRA_API_TOKEN"),
        "DISCORD_BOT_TOKEN": _clean_env("DISCORD_BOT_TOKEN"),
    }
    missing = [key for key, value in required.items() if not value]
    if missing:
        raise ConfigError(
            f"Missing: {', '.join(missing)} — set these in .env locally, "
            f"or as environment variables on your host"
        )

    site = required["JIRA_SITE"]
    # A site value carrying a scheme or path would produce a malformed URL that
    # fails somewhere less obvious than here.
    if site.startswith("http"):
        raise ConfigError(
            f"JIRA_SITE should be a bare hostname like yourteam.atlassian.net, "
            f"not a URL — got {site!r}"
        )
    if "/" in site:
        raise ConfigError(f"JIRA_SITE should not contain a path — got {site!r}")

    return Secrets(
        jira_site=site,
        jira_email=required["JIRA_EMAIL"],
        jira_api_token=required["JIRA_API_TOKEN"],
        discord_bot_token=required["DISCORD_BOT_TOKEN"],
    )


def _require(section: dict, key: str, where: str):
    if key not in section:
        raise ConfigError(f"Missing {key!r} under {where} in config.yaml")
    return section[key]


def _check_clock(value: str, name: str) -> str:
    """Times are strings like "11:00" — catch typos now, not at 11:00."""
    try:
        hour, minute = (int(part) for part in value.split(":"))
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError
    except (ValueError, AttributeError):
        raise ConfigError(f"{name} must look like \"11:00\", got {value!r}")
    return value


def load_config(path: Path = CONFIG_PATH) -> Config:
    if not path.exists():
        raise ConfigError(f"No config file at {path}")

    raw = yaml.safe_load(path.read_text(encoding="utf-8"))

    jira = _require(raw, "jira", "root")
    discord_cfg = _require(raw, "discord", "root")
    schedule = _require(raw, "schedule", "root")
    limits = raw.get("limits", {})
    team_raw = _require(raw, "team", "root")

    # --- status map -----------------------------------------------------
    status_map: dict[str, State] = {}
    for column_name, state_name in _require(jira, "status_map", "jira").items():
        try:
            status_map[column_name] = State(state_name)
        except ValueError:
            raise ConfigError(
                f"status_map maps {column_name!r} to unknown state "
                f"{state_name!r}. Valid: {', '.join(s.value for s in State)}"
            )

    # Every canonical state must be reachable. If a board has no Done column
    # mapped, nothing can ever be completed and the bug would be baffling.
    mapped = set(status_map.values())
    for required_state in (State.TODO, State.IN_PROGRESS, State.DONE):
        if required_state not in mapped:
            raise ConfigError(
                f"No board column is mapped to {required_state.value!r}"
            )

    active_states = {
        State(name) for name in jira.get("active_states", ["in_progress"])
    }

    # --- team -----------------------------------------------------------
    if not team_raw:
        raise ConfigError("team is empty — nobody to ask")

    team: list[Person] = []
    seen_jira: set[str] = set()
    seen_discord: set[int] = set()

    for entry in team_raw:
        name = _require(entry, "name", "a team entry")
        jira_id = _require(entry, "jira_account_id", f"team entry {name}")
        discord_id = _require(entry, "discord_user_id", f"team entry {name}")

        if not jira_id:
            raise ConfigError(f"{name} has an empty jira_account_id")
        if not discord_id:
            raise ConfigError(f"{name} has an empty discord_user_id")

        # Duplicate IDs mean two people share an identity — prompts would be
        # sent twice to one person and never to the other.
        if jira_id in seen_jira:
            raise ConfigError(f"Duplicate jira_account_id on {name}")
        if int(discord_id) in seen_discord:
            raise ConfigError(f"Duplicate discord_user_id on {name}")
        seen_jira.add(jira_id)
        seen_discord.add(int(discord_id))

        try:
            calendar = WorkCalendar(
                timezone=entry.get("timezone", "America/Vancouver"),
                start_hour=int(entry.get("work_start", 9)),
                end_hour=int(entry.get("work_end", 18)),
            )
            # Touch .tz so an invalid timezone name fails here rather than at
            # the first scheduling calculation.
            _ = calendar.tz
        except Exception as e:
            raise ConfigError(f"{name} has an invalid calendar: {e}")

        if calendar.start_hour >= calendar.end_hour:
            raise ConfigError(
                f"{name}: work_start must be before work_end"
            )

        team.append(
            Person(
                name=name,
                discipline=entry.get("discipline", "Unknown"),
                jira_account_id=jira_id,
                discord_user_id=int(discord_id),
                calendar=calendar,
            )
        )

    # --- schedule -------------------------------------------------------
    prompt = _check_clock(_require(schedule, "prompt_time", "schedule"), "prompt_time")
    nudge = _check_clock(_require(schedule, "nudge_time", "schedule"), "nudge_time")
    cutoff = _check_clock(_require(schedule, "cutoff_time", "schedule"), "cutoff_time")

    # Ordering matters: a nudge after cutoff would never fire, and a cutoff
    # before the prompt would close the standup before anyone was asked.
    if not (prompt < nudge < cutoff):
        raise ConfigError(
            f"Times must run prompt < nudge < cutoff, got "
            f"{prompt} / {nudge} / {cutoff}"
        )

    eod = _check_clock(schedule.get("eod_time", "18:00"), "eod_time")
    eod_close = _check_clock(
        schedule.get("eod_close_time", "19:00"), "eod_close_time"
    )
    if eod_close <= eod:
        raise ConfigError(
            f"eod_close_time ({eod_close}) must come after eod_time ({eod})"
        )
    if eod <= cutoff:
        # An end-of-day check-in before the standup has even closed would ask
        # people how their day went while they're still answering the morning.
        raise ConfigError(f"eod_time ({eod}) must come after cutoff_time ({cutoff})")

    # --- review -----------------------------------------------------------
    review_raw = raw.get("review", {})
    review = ReviewSettings(
        enabled=bool(review_raw.get("enabled", False)),
        reviewer=review_raw.get("reviewer", "none"),
        fixed_reviewer=review_raw.get("fixed_reviewer", ""),
    )
    if review.reviewer not in ("component_lead", "fixed", "none"):
        raise ConfigError(
            f"review.reviewer must be component_lead, fixed or none — "
            f"got {review.reviewer!r}"
        )
    if review.reviewer == "fixed" and not review.fixed_reviewer:
        raise ConfigError("review.reviewer is 'fixed' but fixed_reviewer is empty")
    if review.enabled and State.IN_REVIEW not in mapped:
        # Turning review on without a review column would send the completion
        # button somewhere that doesn't exist.
        raise ConfigError(
            "review.enabled is true but no board column maps to 'in_review'"
        )

    ranking = raw.get("ranking", {})

    mode = discord_cfg.get("mode", "thread")
    if mode not in ("thread", "dm"):
        raise ConfigError(f"discord.mode must be 'thread' or 'dm', got {mode!r}")

    return Config(
        project_key=_require(jira, "project_key", "jira"),
        status_map=status_map,
        active_states=active_states,
        blocked_label=jira.get("blocked_label", "blocked"),
        review=review,
        guild_id=int(_require(discord_cfg, "guild_id", "discord")),
        standup_channel_id=int(_require(discord_cfg, "standup_channel_id", "discord")),
        digest_channel_id=int(_require(discord_cfg, "digest_channel_id", "discord")),
        mode=mode,
        prompt_time=prompt,
        nudge_time=nudge,
        cutoff_time=cutoff,
        eod_time=eod,
        eod_close_time=eod_close,
        eod_enabled=bool(schedule.get("eod_enabled", True)),
        active_days=tuple(schedule.get("active_days", [0, 1, 2, 3, 4])),
        max_in_progress=int(limits.get("max_in_progress", 3)),
        max_todo=int(limits.get("max_todo", 2)),
        dropdown_options=int(limits.get("dropdown_options", 3)),
        max_active_wip=int(limits.get("max_active_wip", 3)),
        rank_active=tuple(ranking.get("active", ["stalled", "due_soon", "oldest"])),
        rank_todo=tuple(ranking.get("todo", ["newly_unblocked", "due_soon", "oldest"])),
        rank_dropdown=tuple(ranking.get("dropdown", ["due_soon", "shortest", "oldest"])),
        dm_thread_link=bool(discord_cfg.get("dm_thread_link", True)),
        cleanup_keep_days=int(discord_cfg.get("cleanup_keep_days", 14)),
        team=team,
    )


if __name__ == "__main__":
    try:
        config = load_config()
        secrets = load_secrets()
    except ConfigError as e:
        print(f"Config problem: {e}")
        sys.exit(1)

    print(f"Project        : {config.project_key}")
    print(f"Mode           : {config.mode}")
    print(f"Schedule       : prompt {config.prompt_time} · "
          f"nudge {config.nudge_time} · cutoff {config.cutoff_time}"
          + (f" · EOD {config.eod_time}" if config.eod_enabled else " · no EOD"))
    print(f"Review         : "
          + (f"on, completion → In Review, reviewer = {config.review.reviewer}"
             if config.review.enabled else "off, completion → Done"))
    print(f"WIP cap        : {config.max_active_wip} active per person")
    print(f"Limits         : {config.max_in_progress} in progress, "
          f"{config.max_todo} to do")
    print(f"Blocked label  : {config.blocked_label}")

    print("\nColumn mapping:")
    for column, state in config.status_map.items():
        marker = " (active)" if state in config.active_states else ""
        print(f"  {column:<14} -> {state.value}{marker}")

    print(f"\nTeam ({len(config.team)}):")
    for person in config.team:
        print(f"  {person.name:<10} {person.discipline:<12} "
              f"{person.calendar.timezone} "
              f"{person.calendar.start_hour:02d}:00-{person.calendar.end_hour:02d}:00")

    # Confirms .env loaded without ever showing what's in it.
    print(f"\nSecrets loaded for {secrets.jira_site} as {secrets.jira_email}")
