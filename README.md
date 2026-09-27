# ProdKit

Async standup for small distributed teams. Discord front end, Jira back end, no
manual time tracking.

The bot DMs each person a short set of questions every morning, built from their
own live Jira board. Every answer is a button tap, and tapping writes back to
Jira — status transitions, blocked labels, comments. At cutoff it posts a digest
to the producer's channel combining what people said with what the board says.

Target: under 30 seconds per person, no typing, no one opening the tracker.

---

## Why it exists

Across several remote and partly-remote teams the same failure kept recurring:
the board didn't reflect reality. People weren't opening the tracker, statuses
drifted, and every report built on top of them inherited the drift.

The usual fixes make it worse. Asking people to log hours doesn't work — they
forget, they backfill from memory on Friday, and it reads as surveillance.
Chasing people individually doesn't scale and turns a producer into an
interruption.

So the constraint was set deliberately: **make the board more accurate while
asking the team to do less, not more.** Two ideas make that possible.

**Derive, don't ask.** Jira records every status change with a timestamp, for
free, whether or not anyone tracks time. That record is enough to reconstruct
how long work actually took.

**Write back.** If answering the standup *is* the board update, the team does
strictly less than before and accuracy becomes a side effect rather than a
chore.

---

## What it does that off-the-shelf standup bots don't

**Handoff verification.** A blocking ticket being marked Done does not mean the
person waiting received anything. The retopo is finished, the ticket is closed,
and the file never reached the rigger. Both tickets look healthy. Work is
stopped, and nobody finds out until someone asks.

When a blocker is marked Done, the bot asks the waiting person whether they
actually have what they need. "Got it" settles it. "Still haven't received it"
re-asks every morning and the digest counts the days — day one is a note, day
three is a rigger idle on work everyone believes is finished.

**Idle detection at zero cost.** If everything someone has active is done or
blocked, and everything in their To Do is blocked, they have nothing to work on.
That's derivable from answers already given — nobody has to volunteer "I have
nothing to do." It becomes the top line of the digest, and the producer finds
out at 13:00 instead of at Friday's check-in.

**Cycle time without time tracking.** Durations are reconstructed from Jira's
changelog and measured in business hours against each person's own calendar and
timezone — not wall clock, and not the producer's hours.

---

## The rule the whole design hangs on

> The bot asks only what cannot be read off the board. The digest reports what
> can.

| Signal | On the board? | Channel |
|---|---|---|
| Is this active ticket actually finished? | No | Ask |
| Did the handoff actually happen? | No | Ask |
| Can you start this today? | No | Ask |
| Ticket untouched for two working days | Yes | Flag to producer |
| Overdue or due soon | Yes | Flag to producer |
| Blocker still open | Yes | Notice, no tap |

This arrived late, after several rounds arguing about how to rank tickets when
only a few can be asked about. Once staleness moved to the digest the ranking
question mostly dissolved: every active ticket gets the same neutral question,
and the producer handles staleness human-to-human. Nobody gets nagged by a bot
about something their producer could already see.

---

## Quickstart

Requires Python 3.12+, a Jira Cloud site, and a Discord server you can add a
bot to.

```bash
git clone https://github.com/FarnoudFathi/prodkit.git
cd prodkit
pip install -r requirements.txt
cp .env.example .env        # fill in the four values below
```

`.env`:

```
DISCORD_TOKEN=
JIRA_SITE=yourteam.atlassian.net
JIRA_EMAIL=you@example.com
JIRA_TOKEN=
```

> `JIRA_SITE` is the bare hostname — no `https://`, no trailing slash, and no
> trailing newline. A stray newline resolves as a literal part of the hostname
> and every request fails DNS lookup.

Then:

```bash
python check.py      # verifies both tokens, dumps every Discord and Jira ID
python doctor.py     # validates config.yaml against the live board and server
python bot.py run
```

`check.py` prints the guild, channel and user IDs you need for `config.yaml`, so
you don't have to hunt them in Discord's developer mode. `doctor.py` checks the
column mapping, the roster, timezones and channel permissions before you find
out at 11:00 with people waiting.

The Discord invite URL needs the `applications.commands` scope, or the slash
commands won't register.

---

## Configuration

Everything team-specific is in `config.yaml`. No project details live in code,
which is what makes it deployable elsewhere.

```yaml
jira:
  project_key: SR71
  status_map: {"To Do": todo, "In Progress": in_progress,
               "In Review": in_review, "Done": done}
  blocked_label: blocked
  sprint_only: true

discord:
  guild_id: 000000000000000000
  standup_channel_id: 000000000000000000
  digest_channel_id: 000000000000000000
  mode: thread            # thread | dm

schedule:
  prompt_time: "11:00"
  nudge_time: "12:30"
  cutoff_time: "13:00"
  eod_time: "18:00"
  active_days: [0,1,2,3,4]

limits:
  max_in_progress: 3
  max_todo: 2
  max_review: 3
```

Times are **per person, in their own timezone.** `11:00` means 11:00 wherever
each person is, not one shared instant. Ordering is enforced — a change that
breaks `prompt < nudge < cutoff < eod` is rejected and rolled back rather than
leaving the bot unable to start.

**Caps limit taps, never information.** Anything dropped by a cap is still named
— as a no-button notice in the prompt, or in the digest. Nothing is silently
hidden.

### Runtime settings

Schedule, channels, limits and the review toggle can be changed from Discord
without a redeploy. Those overrides live in `settings.json` on the persistent
volume and are merged over `config.yaml` at load.

Board column mapping, the roster and credentials are deliberately **not**
runtime-editable. Those are setup decisions, and a typo in Discord shouldn't be
able to take the bot down.

`/standup reset` discards every override and returns to the committed values,
which keeps this repo the single source of truth for what a fresh install looks
like.

---

## Commands

All under `/standup`, restricted to **Manage Server**, all replies ephemeral.
Full reference in [`COMMANDS.md`](COMMANDS.md).

| Command | What it does |
|---|---|
| `/standup status` | Everything in effect, and which values are overrides |
| `/standup time` | Change prompt, nudge, cutoff or end-of-day |
| `/standup workdays` | Which days the bot runs |
| `/standup channel` | Move where it posts (permissions checked first) |
| `/standup limits` | How many questions each person gets |
| `/standup post` · `digest` | Trigger either manually, outside the schedule |
| `/standup cleanup` | Archive old threads (sessions are never touched) |
| `/standup reset` | Back to `config.yaml` |

---

## Architecture

Six layers. The separation is load-bearing: the parts that know about Discord,
about Jira, and about standup logic are kept apart so each can be replaced
without touching the others.

| File | Responsibility |
|---|---|
| `config.py` · `settings.py` | Load, validate, overlay. Fails at startup with a clear message rather than three hours later when the digest posts into nothing. |
| `models.py` | Plain data shapes — `Issue`, `BlockerRef`, `Person`, `WorkCalendar`. Nothing downstream touches raw API JSON. |
| `business_time.py` | Working-hours arithmetic against each person's own calendar and timezone. |
| `jira_client.py` | The only file that knows Jira's API shape. Board reads, blocker resolution, changelogs, transitions, comments. |
| `questions.py` | Pure logic. Turns one person's tickets into their message set and the flags their board state raises. No network, so it's testable at a terminal. |
| `writeback.py` | Maps button actions to board operations, and defines which actions deliberately do nothing. |
| `digest.py` | Assembles the producer digest, ordered by urgency. |
| `scheduler.py` | Per-person local times for prompt, nudge, cutoff and end of day. |
| `bot.py` | Discord only. Threads, buttons, modals. Takes `Question` objects and never touches Jira directly. |

### Why business hours, not wall clock

A ticket moved to In Progress on Friday at 16:30 and untouched until Monday at
10:15 shows 65.75 hours elapsed. Real working time is 2.75 hours.

Report the first number and every ticket that touches a weekend looks abandoned.
The digest fills with false alarms and stops being read within a week. So every
duration is measured against the assignee's own calendar — and on a distributed
team the same interval differs per person. That Friday-to-Monday span is 2.75
working hours in Vancouver and 4.25 in Toronto, because Friday 16:30 Pacific is
already past end of day Eastern.

The implementation walks the interval one local day at a time rather than doing
modular arithmetic, which handles daylight saving correctly for free — each
day's 09:00 and 18:00 is constructed by the timezone library instead of assuming
every day is 24 hours long.

### Why buttons survive restarts

Discord stores a button's identifier but not the code that handles it. A normal
button lives in the bot's memory, so after a restart every previously sent
button reports "This interaction failed."

The fix is stateless handlers — everything needed to process a tap is encoded in
the identifier:

```
su:2026-08-20:got_starting:SR71-17
```

Date, action, ticket. The bot needs no memory of what it sent and rebuilds the
handler from the string.

One clarification found during testing: the widely-quoted 15-minute limit
applies to the response window *after* a click, not to the button's lifetime.
Buttons stay live indefinitely — but the process must be running when someone
taps, which is a hosting requirement, not a code one.

---

## Deployment

Any host that keeps a process running. Included `Dockerfile` targets
`python:3.12-slim` with two things that matter:

- `tzdata` is installed explicitly. Slim images ship no timezone database, and
  every calendar lookup fails without it — the same way it fails on Windows.
- Session files and `settings.json` go to a mounted volume, so a redeploy
  doesn't wipe the day's answers or the runtime overrides.

```
PRODKIT_SESSION_DIR=/data/sessions
PRODKIT_SETTINGS_PATH=/data/settings.json
```

On the host, `.env` is replaced by environment variables. `.env` is gitignored
and must stay that way.

---

## Deliberate limitations

Stated rather than hidden. Knowing what a tool can't do is part of knowing what
it does.

- **A ticket forgotten in In Progress accrues active hours** whether or not
  anyone is touching it. The tool can't distinguish "being worked on" from "left
  in the wrong column" — which is exactly why stall detection sits alongside the
  raw number rather than replacing it.
- **The blocked-lookahead depends on issue links being maintained.** Nothing
  maintains them automatically, so the producer does. When no links exist the
  digest says so explicitly rather than showing an empty section that reads like
  good news.
- **Undoing a write-back needs the status the ticket came from,** held in
  memory. If the process restarts between the tap and the undo, the bot says so
  and asks the person to fix it manually rather than guessing — a wrong guess
  would corrupt the changelog the cycle-time analysis depends on.
- **Setup friction is real.** A Jira API token, a Discord bot token, and an
  identity mapping. Roughly twenty minutes of unglamorous work, and where most
  self-hosted tools lose people.

---

## Status

Feature-complete and deployed. Ran unattended for a week against a live Jira
board with a six-person team; that run surfaced eleven bugs across scheduling,
storage and state handling, each documented with its cause and fixed.

Design decisions, rejected alternatives, reversals and the full bug list are in
the process document — written because the reasoning is the part worth showing.
Anyone can install a standup bot; the useful question is why this one works
differently.

---

## License

MIT