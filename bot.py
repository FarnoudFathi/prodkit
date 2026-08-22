"""
Discord layer.

Renders the question set into threads with buttons, and handles taps. This file
knows about Discord and nothing else — it takes Question objects from
questions.py and never touches Jira directly. Swapping in Slack later means
writing a sibling to this file and changing nothing else.

Write-back to Jira is deliberately NOT here yet; taps are recorded in memory so
the flow can be verified visually before anything writes to a live board.

    python bot.py run                # scheduled mode — fires everything itself
    python bot.py                    # listen only, handles taps
    python bot.py post               # post today's standup, then listen
    python bot.py digest             # post the digest now, then listen

    python bot.py clean threads      # archive standup threads older than N days
    python bot.py clean threads --delete
    python bot.py clean digest --delete
    python bot.py clean all --delete

Cleanup archives by default. Archiving hides a thread and is reversible;
deleting is not, so it needs the explicit flag. Session files on disk are never
touched by either — the record of who said what is the one thing worth keeping
after the threads are gone.
"""

from __future__ import annotations

import re
import sys
from datetime import datetime, timedelta, timezone

import discord

from config import Config, ConfigError, load_config, load_secrets
from jira_client import JiraClient, JiraError
from questions import Kind, PersonPrompt, Question, build_prompt, dropdown_options
from digest import DigestInput, DigestResult, build_digest
from scheduler import Event, Scheduler
from store import Store
from writeback import apply_action, undo_action

# Answers live on disk, one file per day. Written after every tap, because
# responses arrive over several hours and the process may restart at any point
# in that window — a crash at 12:00 must not lose the morning and report
# everyone as a non-responder.
STORE = Store()

# Every button's appearance, keyed by the action encoded in its custom_id.
# Kept in one place because a persistent button has to be REBUILT from its
# custom_id after a restart — Discord stores the id, not the handler, so the
# bot needs to know what "done" looked like without the original message.
ACTIONS: dict[str, tuple[str, discord.ButtonStyle, str]] = {
    # action        label                          style                    confirmation
    "done":         ("✅ Done",                     discord.ButtonStyle.success, "marked done"),
    "ongoing":      ("⏳ Still on it",              discord.ButtonStyle.secondary, "still in progress"),
    "blocked":      ("🚧 Blocked",                  discord.ButtonStyle.danger, "flagged as blocked"),
    "got_starting": ("▶️ Got it, starting today",   discord.ButtonStyle.success, "received it, starting today"),
    "got_later":    ("👍 Got it, not today",        discord.ButtonStyle.secondary, "received it, starting later"),
    "not_received": ("📭 Still haven't received it", discord.ButtonStyle.danger, "has NOT received it"),
    "starting":     ("▶️ Starting today",           discord.ButtonStyle.success, "starting today"),
    "can_start":    ("👍 Can start, not today",     discord.ButtonStyle.secondary, "able to start, not today"),
    "approve":      ("✅ Approve",                   discord.ButtonStyle.success, "approved"),
    "send_back":    ("↩︎ Send back",                 discord.ButtonStyle.danger, "sent back for changes"),
    "not_yet":      ("⏳ Not yet",                    discord.ButtonStyle.secondary, "not reviewed yet"),
    "note":         ("💬 Add a note",               discord.ButtonStyle.primary, "note added"),
    "finish":       ("✔️ Done with standup",         discord.ButtonStyle.success, "standup complete"),
    # Shown after an answer is recorded, so a mis-tap can be undone. The suffix
    # names which button set to restore — the bot has no memory of the original
    # message, so the custom_id has to carry it.
    "change_active":  ("↩︎ Change answer", discord.ButtonStyle.secondary, ""),
    "change_handoff": ("↩︎ Change answer", discord.ButtonStyle.secondary, ""),
    "change_start":   ("↩︎ Change answer", discord.ButtonStyle.secondary, ""),
    "change_review":  ("↩︎ Change answer", discord.ButtonStyle.secondary, ""),
}

# Marks where a recorded answer begins in a message. Everything from this
# character onward is regenerated each time, so changing an answer replaces the
# confirmation rather than stacking a new line under the old one.
CONFIRM_MARK = "\n\n▸ "

# Which button set each change action restores.
CHANGE_TARGETS = {
    "change_active":  ["done", "ongoing", "blocked"],
    "change_handoff": ["got_starting", "got_later", "not_received"],
    "change_start":   ["starting", "can_start", "blocked"],
    "change_review":  ["approve", "send_back", "not_yet"],
}

# Maps a recorded action back to the change button that can undo it.
UNDO_FOR = {
    "done": "change_active", "ongoing": "change_active",
    "blocked": "change_active",
    "got_starting": "change_handoff", "got_later": "change_handoff",
    "not_received": "change_handoff",
    "starting": "change_start", "can_start": "change_start",
    "approve": "change_review", "not_yet": "change_review",
    "send_back": "change_review",
}

# Actions that open a text box instead of recording immediately. A modal must be
# the FIRST response to an interaction — you cannot defer and then open one — so
# these are handled before any other work happens in the callback.
MODAL_ACTIONS = {"blocked", "note", "send_back"}

# Filled from config at startup so callbacks don't each need the config object.
BLOCKED_LABEL = "blocked"


# Set by __main__ so button callbacks can reach Jira. Callbacks are invoked by
# discord.py with no reference to the bot instance, so a module-level handle is
# the least awkward way to give them one.
JIRA = None


# Set at startup so the tap handler can tell a late answer from a timely one
# without loading config on every interaction.
CLOSE_TIME = "19:00"
TEAM_BY_DISCORD: dict[int, object] = {}
COMPLETION_STATE = None
REVIEW_CFG = None


def resolve_reviewer(issue_key: str) -> str | None:
    """
    Who should review this ticket.

    component_lead reads the Lead off the ticket's Jira component, so the
    mapping lives in one place and follows a team reorganisation without a
    config edit. Falls through to a fixed reviewer, then to nobody — an
    unassigned review is surfaced in the digest rather than silently dropped.
    """
    if not REVIEW_CFG or not REVIEW_CFG.enabled or not JIRA:
        return None

    if REVIEW_CFG.reviewer == "component_lead":
        try:
            issue = JIRA.search(f"key = {issue_key}")
            if issue and issue[0].components:
                lead = JIRA.component_lead(issue[0].components[0])
                if lead:
                    return lead
        except Exception as e:
            print(f"  could not resolve component lead for {issue_key}: {e}")

    if REVIEW_CFG.reviewer in ("component_lead", "fixed") and REVIEW_CFG.fixed_reviewer:
        for person in TEAM_BY_DISCORD.values():
            if person.name == REVIEW_CFG.fixed_reviewer:
                return person.jira_account_id
    return None


def _is_late(user_id: int) -> bool:
    """
    Did this answer arrive after the day's close time, in the person's own zone?

    Recorded rather than blocked. Buttons never expire, and someone answering at
    21:00 every night is information for the producer, not something to prevent.
    """
    person = TEAM_BY_DISCORD.get(user_id)
    if not person:
        return False
    now = datetime.now(timezone.utc)
    return now.astimezone(person.calendar.tz) >= person.calendar.local_time_today(
        CLOSE_TIME, now
    )


def record(date_key, user_id, issue_key, action, text="", previous_status=None):
    session = STORE.load(date_key)
    session.record(
        user_id, issue_key, action, text, previous_status, late=_is_late(user_id)
    )
    STORE.save(session)


def previous_answer(date_key, user_id, issue_key):
    return STORE.load(date_key).get(user_id, issue_key)


def clear(date_key: str, user_id: int, issue_key: str) -> None:
    """Remove a recorded answer so it can be given again."""
    session = STORE.load(date_key)
    session.clear(user_id, issue_key)
    STORE.save(session)


def base_content(content: str) -> str:
    """
    The message without any recorded answer attached.

    Confirmations are appended after a marker rather than accumulated, so
    changing an answer three times leaves one confirmation line, not three.
    """
    return content.split(CONFIRM_MARK)[0]


def undo_view(date_key: str, issue_key: str, action: str) -> "discord.ui.View | None":
    """A lone change-answer button, for actions that can be reversed."""
    undo_action = UNDO_FOR.get(action)
    if not undo_action:
        return None
    view = discord.ui.View(timeout=None)
    view.add_item(StandupButton(date_key, undo_action, issue_key))
    return view


class TextModal(discord.ui.Modal):
    """Blocker reason or free-form note."""

    def __init__(self, title: str, label: str, placeholder: str,
                 date_key: str, issue_key: str, action: str, required: bool):
        super().__init__(title=title, timeout=None)
        self.date_key = date_key
        self.issue_key = issue_key
        self.action = action
        self.field = discord.ui.TextInput(
            label=label,
            placeholder=placeholder,
            style=discord.TextStyle.paragraph,
            required=required,
            max_length=500,
        )
        self.add_item(self.field)

    async def on_submit(self, interaction: discord.Interaction):
        text = str(self.field.value or "").strip()
        _, _, confirmation = ACTIONS[self.action]
        note = f"\n> {text}" if text else ""

        if self.action == "note":
            # Notes go to the digest, not to Jira. A movie ticket is not board
            # state, and commenting every note onto a ticket would be noise.
            record(self.date_key, interaction.user.id, self.issue_key, self.action, text)
            await interaction.response.send_message(f"Noted.{note}", ephemeral=True)
            return

        await interaction.response.defer()

        result = apply_action(
            JIRA, self.issue_key, self.action, text,
            blocked_label=BLOCKED_LABEL,
            completion_state=COMPLETION_STATE,
        ) if JIRA else None

        record(self.date_key, interaction.user.id, self.issue_key, self.action, text)

        suffix = ""
        if result and result.attempted:
            suffix = f" · {result.summary}" if result.ok else f"\n⚠️ {result.summary}"

        await interaction.edit_original_response(
            content=base_content(interaction.message.content)
            + CONFIRM_MARK + f"**{confirmation}**{note}{suffix}",
            view=undo_view(self.date_key, self.issue_key, self.action),
        )


class StandupButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"su:(?P<date>[\d-]+):(?P<action>[a-z_]+):(?P<key>[A-Za-z0-9\-]+)",
):
    """
    A button that survives a bot restart.

    Discord does not store handlers — only the custom_id string that goes out
    with the message. A normal View lives in the bot's memory, so after a
    restart every old button reports "This interaction failed".

    A DynamicItem avoids that by making the handler stateless: everything needed
    to process a tap is encoded in the custom_id itself, and the class is
    rebuilt from a regex match whenever a tap arrives. The bot needs no memory
    of what it sent. Buttons stay live indefinitely, and someone answering at
    4pm gets the same behaviour as someone answering at 9am.

    (The often-quoted 15-minute limit is on the response window AFTER a click,
    not a countdown on the button. Each tap opens a fresh window.)
    """

    def __init__(self, date_key: str, action: str, issue_key: str):
        label, style, _ = ACTIONS[action]
        super().__init__(
            discord.ui.Button(
                label=label,
                style=style,
                custom_id=f"su:{date_key}:{action}:{issue_key}",
            )
        )
        self.date_key = date_key
        self.action = action
        self.issue_key = issue_key

    @classmethod
    async def from_custom_id(cls, interaction, item, match: re.Match):
        return cls(match["date"], match["action"], match["key"])

    async def callback(self, interaction: discord.Interaction):
        # Modals must be the first response — no defer beforehand.
        if self.action in MODAL_ACTIONS:
            if self.action == "blocked":
                modal = TextModal(
                    title="What's blocking it?",
                    label="Blocker",
                    placeholder="Waiting on the retopo from Mia",
                    date_key=self.date_key,
                    issue_key=self.issue_key,
                    action=self.action,
                    required=True,
                )
            elif self.action == "send_back":
                modal = TextModal(
                    title="What needs changing?",
                    label="Feedback",
                    placeholder="Weights on the shoulder need another pass",
                    date_key=self.date_key,
                    issue_key=self.issue_key,
                    action=self.action,
                    required=True,
                )
            else:
                modal = TextModal(
                    title="Add a note",
                    label="Anything for the team",
                    placeholder="Spare ticket for Friday, first to shout",
                    date_key=self.date_key,
                    issue_key=self.issue_key,
                    action=self.action,
                    required=False,
                )
            await interaction.response.send_modal(modal)
            return

        # Change answer: undo any board change, clear the record, restore buttons.
        if self.action in CHANGE_TARGETS:
            await interaction.response.defer()

            prior = previous_answer(self.date_key, interaction.user.id, self.issue_key)
            note = ""
            if prior and JIRA:
                result = undo_action(
                    JIRA, self.issue_key, prior.action,
                    prior.previous_status,
                    blocked_label=BLOCKED_LABEL,
                )
                if result.attempted:
                    note = CONFIRM_MARK + f"_{result.summary}_"

            clear(self.date_key, interaction.user.id, self.issue_key)

            view = discord.ui.View(timeout=None)
            for action in CHANGE_TARGETS[self.action]:
                view.add_item(StandupButton(self.date_key, action, self.issue_key))

            await interaction.edit_original_response(
                content=base_content(interaction.message.content) + note, view=view
            )
            return

        # Jira calls take longer than the 3 seconds Discord allows for an
        # acknowledgement, so defer first. Deferring buys 15 minutes; the write
        # and the message edit both happen after.
        await interaction.response.defer()

        _, _, confirmation = ACTIONS[self.action]

        if self.action == "finish":
            record(self.date_key, interaction.user.id, self.issue_key, self.action)
            await interaction.edit_original_response(
                content=base_content(interaction.message.content)
                + CONFIRM_MARK + "**Thanks — that's it for today.**",
                view=None,
            )
            return

        result = apply_action(
            JIRA, self.issue_key, self.action,
            blocked_label=BLOCKED_LABEL,
            completion_state=COMPLETION_STATE,
            reviewer_id=resolve_reviewer(self.issue_key)
            if self.action == "done" else None,
        ) if JIRA else None

        record(
            self.date_key, interaction.user.id, self.issue_key, self.action,
            previous_status=result.previous_status if result else None,
        )

        # The answer is recorded either way. A failed write is reported plainly
        # rather than swallowed — someone told the board is updated when it
        # isn't will not check, and the error would surface days later.
        suffix = ""
        if result and result.attempted:
            suffix = f" · {result.summary}" if result.ok else f"\n⚠️ {result.summary}"

        await interaction.edit_original_response(
            content=base_content(interaction.message.content)
            + CONFIRM_MARK + f"**Recorded: {confirmation}**{suffix}",
            view=undo_view(self.date_key, self.issue_key, self.action),
        )


# Left-bar colour by worst severity in the digest. A producer glancing at the
# channel should know whether to read it before reading a word of it.
SEVERITY_COLOUR = {
    0: discord.Colour(0x4F9D69),   # green — nothing needs you
    1: discord.Colour(0xC8922B),   # amber — worth a look
    2: discord.Colour(0xB4453A),   # red — someone is stuck right now
}


def digest_embed(result: DigestResult) -> discord.Embed:
    """
    Render the digest as an embed.

    Plain markdown gives no visual separation, so sections run together and the
    post has to be read start to finish. An embed gives each section its own
    field with a heading, and the coloured bar carries severity at a glance.

    Embed fields cap at 1024 characters. Long sections are truncated with a
    count rather than silently dropped — a digest that quietly omits three
    blockers is worse than one that says it did.
    """
    embed = discord.Embed(
        title=result.title,
        description=f"_{result.subtitle}_",
        colour=SEVERITY_COLOUR[result.severity],
    )

    for section in result.sections:
        body = "\n".join(section.lines)
        if len(body) > 1024:
            kept, length = [], 0
            for line in section.lines:
                if length + len(line) + 1 > 950:
                    break
                kept.append(line)
                length += len(line) + 1
            hidden = len(section.lines) - len(kept)
            body = "\n".join(kept) + f"\n_… and {hidden} more_"
        embed.add_field(name=section.heading, value=body, inline=False)

    if not result.sections:
        embed.add_field(
            name="✅ Nothing needs you",
            value="No blockers, no handoff failures, nothing at risk.",
            inline=False,
        )

    embed.set_footer(text=result.footer)
    return embed


class StartSelect(
    discord.ui.DynamicItem[discord.ui.Select],
    template=r"sel:(?P<date>[\d-]+):start",
):
    """
    A dropdown for "starting anything else today?".

    Capped at three options rather than showing everything. Choosing what to
    start is the decision people struggle with, and a list of twelve makes it
    harder, not easier — the whole point is to remove friction, not relocate it.

    Stateless in the same way the buttons are: the ticket keys ride in the
    option values, so a restart doesn't break it. Multi-select, because
    someone genuinely might pick up two small things.
    """

    def __init__(self, date_key: str, options: list[discord.SelectOption] | None = None):
        super().__init__(
            discord.ui.Select(
                custom_id=f"sel:{date_key}:start",
                placeholder="Starting anything else today?",
                min_values=0,
                max_values=len(options) if options else 1,
                options=options or [discord.SelectOption(label="none", value="none")],
            )
        )
        self.date_key = date_key

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["date"])

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        chosen = [v for v in self.item.values if v != "none"]
        if not chosen:
            await interaction.edit_original_response(
                content=base_content(interaction.message.content)
                + CONFIRM_MARK + "**Nothing else today.**",
                view=None,
            )
            return

        results = []
        for key in chosen:
            result = apply_action(
                JIRA, key, "starting",
                blocked_label=BLOCKED_LABEL, completion_state=COMPLETION_STATE,
            ) if JIRA else None
            record(
                self.date_key, interaction.user.id, key, "starting",
                previous_status=result.previous_status if result else None,
            )
            if result and result.attempted:
                results.append(result.summary if result.ok else f"⚠️ {result.summary}")

        await interaction.edit_original_response(
            content=base_content(interaction.message.content)
            + CONFIRM_MARK + f"**Starting: {', '.join(chosen)}**"
            + ("\n_" + " · ".join(results) + "_" if results else ""),
            view=None,
        )


def build_view(date_key: str, question: Question) -> discord.ui.View | None:
    if not question.buttons:
        return None

    view = discord.ui.View(timeout=None)
    issue_key = question.issue.key if question.issue else "none"
    for button in question.buttons:
        view.add_item(StandupButton(date_key, button.action, issue_key))
    return view


def render_message(question: Question) -> str:
    lines = [f"**{question.headline}**"] if question.kind is not Kind.INFO else [question.headline]
    for detail in question.detail_lines:
        if detail:
            lines.append(detail)
    return "\n".join(lines)


class StandupBot(discord.Client):
    def __init__(self, config: Config, jira: JiraClient,
                 post_on_start: bool = False, digest_on_start: bool = False,
                 clean_args: tuple | None = None, scheduled: bool = False):
        intents = discord.Intents.default()
        intents.members = True          # needed to resolve people and add them to threads
        super().__init__(intents=intents)
        self.config = config
        self.jira = jira
        self.post_on_start = post_on_start
        self.digest_on_start = digest_on_start
        self.clean_args = clean_args
        self.scheduled = scheduled
        self.scheduler: Scheduler | None = None
        self._issues_cache: list = []

    async def setup_hook(self):
        # Registering the dynamic item is what lets taps on messages from
        # previous runs still resolve to a handler.
        self.add_dynamic_items(StandupButton, StartSelect)

    async def on_ready(self):
        print(f"Connected as {self.user}")
        if self.post_on_start:
            try:
                await self.post_standup()
            except Exception as e:
                print(f"Failed to post standup: {e}")
        if self.digest_on_start:
            try:
                await self.post_digest()
            except Exception as e:
                print(f"Failed to post digest: {e}")
        if self.clean_args:
            try:
                await self.clean(*self.clean_args)
            except Exception as e:
                print(f"Cleanup failed: {e}")
            # Cleanup is a one-shot maintenance task, not a reason to hold a
            # gateway connection open.
            await self.close()
            return
        if self.scheduled:
            self.scheduler = Scheduler(self, self.config, STORE)
            self.scheduler.start()

        print("Listening for taps. Ctrl+C to stop.")

    async def run_event(self, event: Event, date_key: str, now: datetime):
        """Dispatch one scheduled event. Called by the scheduler."""
        if event.name == "prompt":
            await self.post_standup(only=event.person)
        elif event.name == "nudge":
            await self.send_nudge(event.person, date_key)
        elif event.name == "digest":
            await self.post_digest()
        elif event.name == "eod":
            await self.post_eod(event.person, date_key, now)

    async def send_nudge(self, person, date_key: str):
        """
        A quiet reminder before cutoff, to whoever hasn't answered.

        Posted into their existing thread rather than as a new message, and
        phrased as a time check rather than a chase. The scheduler only asks for
        this when they genuinely haven't responded, so nobody who has already
        answered gets pinged.
        """
        session = STORE.load(date_key)
        thread_id = session.threads.get(str(person.discord_user_id))
        if not thread_id:
            return

        thread = self.get_channel(thread_id)
        if thread is None:
            try:
                thread = await self.fetch_channel(thread_id)
            except discord.HTTPException:
                return

        await thread.send(
            f"{person.mention} standup closes at {self.config.cutoff_time} — "
            f"the buttons above are still live."
        )

    async def post_eod(self, person, date_key: str, now: datetime):
        """
        End-of-day check-in on whatever they said they'd work on.

        A new message rather than an expectation that they scroll back into the
        morning thread. Once someone has tapped through, that thread is closed
        in their head — asking them to return is asking for a behaviour change,
        which is the thing this tool exists to avoid.
        """
        session = STORE.load(date_key)
        started = session.active_starts(person.discord_user_id)
        if not started:
            return

        thread_id = session.threads.get(str(person.discord_user_id))
        if not thread_id:
            return
        thread = self.get_channel(thread_id) or await self.fetch_channel(thread_id)

        issues = {i.key: i for i in self.jira.fetch_open_issues()}

        await thread.send(
            f"{person.mention} how did today go? "
            f"One tap each — no need to open Jira."
        )

        for key in started:
            issue = issues.get(key)
            if issue is None:
                # Already closed during the day, so there's nothing to ask.
                continue

            view = discord.ui.View(timeout=None)
            for action in ("done", "ongoing", "blocked"):
                view.add_item(StandupButton(date_key, action, key))

            await thread.send(
                content=f"**{key} · {issue.summary}**",
                view=view,
            )

    async def clean(self, targets: str, delete: bool, older_than_days: int):
        """
        Tidy up threads and digest posts.

        Standup threads accumulate — five people times five days is 25 threads
        in one channel, and by the second sprint the channel is unreadable and
        useless for demo capture.

        Archiving is the default because it is reversible. Deletion needs an
        explicit flag, because a deleted thread takes its answers with it as
        far as Discord is concerned. The session files are the durable record
        and are never touched here.
        """
        guild = self.get_guild(self.config.guild_id)
        if guild is None:
            raise RuntimeError("Bot cannot see the configured guild")

        cutoff = datetime.now(timezone.utc) - timedelta(days=older_than_days)
        verb = "Deleting" if delete else "Archiving"

        if targets in ("threads", "all"):
            channel = guild.get_channel(self.config.standup_channel_id)
            if channel is None:
                raise RuntimeError("Cannot see the standup channel")

            # Active threads plus archived ones, so a second cleanup run with
            # --delete can finish what an archive-only run started.
            threads = list(channel.threads)
            async for archived in channel.archived_threads(limit=100):
                threads.append(archived)

            touched = 0
            for thread in threads:
                if thread.created_at and thread.created_at > cutoff:
                    continue
                try:
                    if delete:
                        await thread.delete()
                    elif not thread.archived:
                        await thread.edit(archived=True)
                    else:
                        continue
                    touched += 1
                except discord.HTTPException as e:
                    print(f"  could not touch {thread.name}: {e}")
            print(f"{verb} threads older than {older_than_days}d: {touched}")

        if targets in ("digest", "all"):
            if not delete:
                # There is no archive concept for plain channel messages, so
                # asking to clean the digest channel without --delete would
                # silently do nothing.
                print("Digest cleanup needs --delete (messages cannot be archived)")
                return

            channel = guild.get_channel(self.config.digest_channel_id)
            if channel is None:
                raise RuntimeError("Cannot see the digest channel")

            # Discord refuses bulk deletion of messages older than 14 days, so
            # anything past that is removed one at a time.
            removed = 0
            async for message in channel.history(limit=200, before=cutoff):
                if message.author != self.user:
                    continue      # never touch anything a human posted
                try:
                    await message.delete()
                    removed += 1
                except discord.HTTPException as e:
                    print(f"  could not delete a message: {e}")
            print(f"Deleted digest posts older than {older_than_days}d: {removed}")

    async def post_digest(self):
        """Assemble and post the producer digest."""
        guild = self.get_guild(self.config.guild_id)
        channel = guild.get_channel(self.config.digest_channel_id)
        if channel is None:
            raise RuntimeError("Cannot see the digest channel — check permissions")

        now = datetime.now(timezone.utc)
        date_key = self.config.date_key(now)
        session = STORE.load(date_key)

        # Nothing was asked today, so there is nothing to report. Without this
        # the bot posts an empty digest on any day it happens to be running but
        # not prompting — a weekend, or a day the schedule was changed.
        if not session.asked:
            print(f"No standup was posted for {date_key} — skipping digest")
            session.mark_fired("digest")
            STORE.save(session)
            return

        issues = self.jira.fetch_open_issues()
        issues_by_key = {i.key: i for i in issues}

        # Flags are rebuilt from the board at digest time rather than reused
        # from the morning. Six hours have passed; a ticket that was fine at
        # 11:00 may be overdue by 13:00, and the producer should see now.
        last_moved, bounces = {}, {}
        for issue in issues:
            if issue.state in self.config.active_states:
                entered, bounced = self.jira.time_in_current_status(issue.key)
                last_moved[issue.key] = entered
                if bounced:
                    bounces[issue.key] = bounced

        flags = []
        for person in self.config.team:
            prompt = build_prompt(person, issues, self.config, now, last_moved)
            flags.extend(prompt.flags)

        # Repeated corrections on one ticket are worth surfacing — an honest
        # mis-tap happens once, a pattern is something else.
        board_flags = [
            f"🔁 `{key}` moved and reverted {count}x — staleness measured from "
            f"before the corrections"
            for key, count in bounces.items() if count >= 2
        ]

        result = build_digest(DigestInput(
            date_label=now.strftime("%a %d %b"),
            config=self.config,
            session=session,
            store=STORE,
            date_key=date_key,
            people=self.config.team,
            issues_by_key=issues_by_key,
            flags=flags,
            board_flags=board_flags,
        ))

        await channel.send(embed=digest_embed(result))
        session.digest_posted = True
        session.mark_fired("digest")
        STORE.save(session)
        print(f"Digest posted to #{channel.name}")

    async def post_standup(self, only=None):
        guild = self.get_guild(self.config.guild_id)
        if guild is None:
            raise RuntimeError("Bot cannot see the configured guild")

        channel = guild.get_channel(self.config.standup_channel_id)
        if channel is None:
            raise RuntimeError("Cannot see the standup channel — check permissions")

        now = datetime.now(timezone.utc)
        date_key = now.strftime("%Y-%m-%d")

        # Posting twice for one day would give people two sets of buttons for
        # the same tickets, and the second set silently overwrites answers from
        # the first. Existing threads are the check, since they're the record
        # that survives a restart.
        # Posting twice for one person in a day gives them two sets of buttons
        # for the same tickets, and the second set silently overwrites answers
        # from the first. When the scheduler drives this, its fired-event record
        # already prevents it — this guards the manual `post` command.
        if self.config.mode == "thread" and only is None:
            stamp = now.strftime("%d %b")
            existing = [t.name for t in channel.threads if t.name.endswith(stamp)]
            if existing:
                print(f"Standup already posted for {stamp} "
                      f"({len(existing)} threads). Nothing sent.")
                print("Delete the threads in Discord to re-post.")
                return

        issues = self.jira.fetch_open_issues()
        self._issues_cache = issues

        last_moved = {}
        for issue in issues:
            if issue.state in self.config.active_states:
                entered, _ = self.jira.time_in_current_status(issue.key)
                last_moved[issue.key] = entered

        session = STORE.load(date_key)

        recipients = [only] if only else self.config.team

        already = STORE.load(date_key).fired
        skipped = [p.name for p in recipients
                   if f"prompt:{p.discord_user_id}" in already]
        recipients = [p for p in recipients
                      if f"prompt:{p.discord_user_id}" not in already]
        if skipped:
            print(f"  already prompted today, skipping: {', '.join(skipped)}")
        if not recipients:
            return

        for person in recipients:
            settled = STORE.settled_handoffs(person.discord_user_id, date_key)
            prompt = build_prompt(
                person, issues, self.config, now, last_moved, settled
            )
            thread_id = await self.deliver(
                guild, channel, person, prompt, date_key, now
            )

            # Recording who was asked is what distinguishes a non-responder
            # from someone who simply had nothing to answer.
            asked = [q.issue.key for q in prompt.questions
                     if q.needs_tap and q.issue]
            session.mark_asked(person.discord_user_id, asked)
            if thread_id:
                session.set_thread(person.discord_user_id, thread_id)

            # Record the prompt as fired even when triggered by hand. Manual
            # and scheduled runs were previously guarded by two different
            # mechanisms — thread names for one, the fired list for the other —
            # so a manual post left no trace the scheduler could see, and it
            # prompted the same person a second time.
            session.mark_fired(f"prompt:{person.discord_user_id}")

            print(f"  {person.name}: {prompt.tap_count} taps, "
                  f"{len(prompt.flags)} flags"
                  + (f", {len(settled)} handoffs already settled" if settled else ""))

        STORE.save(session)

    async def deliver(self, guild, channel, person, prompt: PersonPrompt,
                      date_key: str, now: datetime) -> int | None:
        """
        Send one person's messages.

        Thread mode puts each person's exchange in its own visible thread inside
        one channel. That's the demo mode: the whole flow is capturable in a
        single frame, and nobody's private messages are exposed. DM mode is what
        a real deployment uses. The messages themselves are identical.
        """
        if self.config.mode == "dm":
            member = guild.get_member(person.discord_user_id)
            if member is None:
                print(f"  {person.name}: not in the server, skipped")
                return None
            target = member
            thread_id = None
        else:
            thread = await channel.create_thread(
                name=f"{person.name} — {now.strftime('%d %b')}",
                type=discord.ChannelType.public_thread,
                auto_archive_duration=1440,
            )
            member = guild.get_member(person.discord_user_id)
            if member:
                # Adding them to the thread is what triggers their notification.
                await thread.add_user(member)
            target = thread
            thread_id = thread.id

        for question in prompt.questions:
            await target.send(
                content=render_message(question),
                view=build_view(date_key, question),
            )

        # Offered after the capped questions, as the escape hatch for when the
        # bot's two guesses weren't what they're actually picking up. Skipped
        # entirely when they're already at the WIP cap.
        asked_keys = {q.issue.key for q in prompt.questions if q.issue}
        extras = dropdown_options(
            person, self._issues_cache, self.config, now, asked_keys
        )
        if extras:
            options = [
                discord.SelectOption(
                    label=f"{i.key} · {i.summary[:60]}",
                    value=i.key,
                    description=(f"{i.estimate_hours:.0f}h" if i.estimate_hours else "no estimate")
                    + (f" · due {i.due_date.strftime('%a %d %b')}" if i.due_date else ""),
                )
                for i in extras
            ]
            view = discord.ui.View(timeout=None)
            view.add_item(StartSelect(date_key, options))
            await target.send(
                content="**Starting anything else today?**\nOptional — pick any, or ignore this.",
                view=view,
            )

        # A direct link means they tap a notification and land in the right
        # place, rather than navigating to a channel and finding their thread.
        # Threads stay visible for demo capture; the team never has to browse.
        if self.config.mode == "thread" and self.config.dm_thread_link and member:
            try:
                await member.send(
                    f"Standup's up — {thread.mention}\n"
                    f"{prompt.tap_count} taps, cutoff {self.config.cutoff_time}."
                )
            except discord.Forbidden:
                # People can block DMs from server members. Not a failure worth
                # stopping for — they're already in the thread and notified.
                print(f"  {person.name}: DMs closed, thread notification only")

        return thread_id


if __name__ == "__main__":
    try:
        config = load_config()
        secrets = load_secrets()
    except ConfigError as e:
        print(f"Config problem: {e}")
        sys.exit(1)

    jira = JiraClient(secrets, config)

    # Startup check. Distinguishes a configuration problem from a transient one:
    # a rejected token means the bot can never work and should stop loudly, but
    # a DNS blip or a brief Atlassian outage should not put a hosted service
    # into a crash-restart loop. The scheduler retries failed events anyway.
    try:
        jira.fetch_open_issues()
        print(f"Jira reachable — {config.project_key}")
    except JiraError as e:
        message = str(e)
        fatal = "401" in message or "403" in message or "credentials" in message.lower()
        if fatal:
            print(f"Jira rejected the credentials: {e}")
            sys.exit(1)
        print(f"Jira unreachable at startup: {e}")
        print("Continuing — scheduled events will retry.")

    JIRA = jira
    BLOCKED_LABEL = config.blocked_label
    CLOSE_TIME = config.eod_close_time
    TEAM_BY_DISCORD = {p.discord_user_id: p for p in config.team}
    COMPLETION_STATE = config.review.completion_state
    REVIEW_CFG = config.review

    command = sys.argv[1] if len(sys.argv) > 1 else ""

    clean_args = None
    if command == "clean":
        target = sys.argv[2] if len(sys.argv) > 2 else "threads"
        if target not in ("threads", "digest", "all"):
            print("Usage: python bot.py clean [threads|digest|all] [--delete] [--days N]")
            sys.exit(1)
        delete = "--delete" in sys.argv
        days = config.cleanup_keep_days
        if "--days" in sys.argv:
            days = int(sys.argv[sys.argv.index("--days") + 1])

        action = "DELETE" if delete else "archive"
        print(f"About to {action} {target} older than {days} days.")
        if delete:
            # Deletion is irreversible and this is a maintenance command run by
            # hand, so it asks once rather than trusting a typed flag alone.
            if input("Type DELETE to confirm: ").strip() != "DELETE":
                print("Cancelled.")
                sys.exit(0)
        clean_args = (target, delete, days)

    bot = StandupBot(
        config, jira,
        post_on_start=(command == "post"),
        digest_on_start=(command == "digest"),
        clean_args=clean_args,
        scheduled=(command == "run"),
    )
    try:
        bot.run(secrets.discord_bot_token, log_handler=None)
    except discord.LoginFailure:
        print("Discord rejected the token.")
    except discord.PrivilegedIntentsRequired:
        print("Server Members Intent is off in the Developer Portal.")
