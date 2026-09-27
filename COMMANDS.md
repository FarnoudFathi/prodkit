# ProdKit — command reference

All commands are under `/standup` and restricted to members with **Manage
Server** permission. Anyone on the team could otherwise move everyone's standup,
and the failure would be silent until nobody got prompted.

Every reply is ephemeral — only you see it.

---

## Where settings live

Two layers:

| File | What it is | Changed by |
|---|---|---|
| `config.yaml` | Committed defaults. Ships with the code. | Editing the repo and deploying |
| `settings.json` | Runtime overrides. Lives on the persistent volume. | These commands |

The overlay is merged over the defaults at load. **Deleting `settings.json`, or
running `/standup reset`, returns everything to the committed values** — which
makes the repo the single source of truth for what a fresh install looks like.

### What these commands can change

Times, workdays, the three channels, and the four question caps.

### What they can't

**Not editable at runtime at all** — the board column mapping, the team roster
and the Jira and Discord credentials. Those are setup decisions, not day-to-day
ones, and a typo in Discord should not be able to take the bot down. Change them
in `config.yaml` and redeploy.

**Editable in the overlay but not exposed as a command** — `review.enabled`,
`schedule.eod_enabled` and `discord.cleanup_keep_days`. The storage layer
accepts them; there is no slash command for them yet. Set them in `config.yaml`,
or edit `settings.json` on the volume directly. `/standup status` shows their
current state either way.

---

## `/standup status`

Run this first when something behaves unexpectedly. Shows everything currently
in effect:

- **Schedule** — prompt, nudge and cutoff times; end-of-day check-in and close,
  or `off` if end of day is disabled; which days the bot runs
- **Channels** — standup, digest, and notes if one is set
- **Limits** — all four caps
- **Review** — on with the reviewer source, or off
- **Runtime overrides** — every value currently coming from `settings.json`
  rather than `config.yaml`, or `none — running on config.yaml`

That last field is the one that answers "why isn't it doing what the repo says."

---

## `/standup time`

Change one of the day's times.

| Option | What it controls |
|---|---|
| Morning prompt | When each person receives their standup |
| Nudge | Reminder sent only to people who haven't answered |
| Cutoff (digest posts) | When the digest posts and the standup closes |
| End of day check-in | The evening wrap on what they said they'd do |
| End of day close | After this, answers are recorded as late |

Format is 24-hour, e.g. `09:30`. Anything that isn't a clock time is rejected
before it's written.

**Times are per person, in their own timezone.** `09:30` means 09:30 wherever
each person is, not one shared instant. On a single-timezone team those are the
same thing; the machinery exists so adding a remote teammate needs a config line
rather than a code change.

**Ordering is enforced:** prompt < nudge < cutoff < end of day < close. A value
that breaks the order is written, fails validation on reload, and is then
**rolled back automatically** — the bot reloads on the previous value and tells
you what the ordering rule is. A bad time can't leave it unable to start.

Takes effect on the next scheduled run. It won't retroactively fire something
already past.

---

## `/standup workdays`

Which days the bot runs.

```
/standup workdays days: mon,tue,wed,thu,fri
/standup workdays days: weekdays
/standup workdays days: mon,tue,wed,thu,fri,sat
```

`weekdays` is shorthand for Monday to Friday. Day names are matched on the first
three letters, so `monday` and `mon` both work. An unrecognised day is named
back to you and nothing is saved. An empty list is refused — the bot would never
run.

Evaluated per person in their local timezone, so someone whose Monday starts
while it's still Sunday elsewhere gets prompted on their Monday.

Useful for a crunch weekend, and worth setting back afterwards.

---

## `/standup channel`

Move where the bot posts. Pick the channel from Discord's own picker.

| Option | What goes there |
|---|---|
| Standup threads | One thread per person, each morning |
| Producer digest | The attention message, with the roster in a thread on it |
| Team notes | Notes people added during standup, posted once at cutoff |

**Permissions are checked before the change is saved.** The bot needs *send
messages*, *embed links* and *create public threads* in the target channel. If
any are missing the command refuses and names exactly which ones — rather than
accepting it and failing at 11:00 with people waiting.

---

## `/standup limits`

How many questions each person gets.

| Option | Default | What it does |
|---|---|---|
| Active tickets asked about | 3 | In progress, ranked by longest untouched |
| Review tickets asked about | 3 | Ranked by longest waiting |
| To Do tickets asked about | 2 | Ranked newly-unblocked first, then due date |
| WIP cap before flagging | 3 | Above this, the digest flags them as spread thin |

Accepts 1 to 10. Past ten, people stop answering — which is the failure the caps
exist to prevent.

**Caps limit taps, never information.** Anything dropped by a cap is still named
— as a no-button notice in the prompt, or in the digest. Nothing is silently
hidden.

Review tickets are ranked by how long they have been waiting, not by due date.
Reviews frequently have no due date, and ranking by one dropped the oldest
review off the bottom of the list.

---

## `/standup cleanup`

Tidy old standup threads and digest posts. Both options are required.

```
/standup cleanup what: Standup threads  older_than: 14 days
/standup cleanup what: Digest posts     older_than: 7 days   delete: True
/standup cleanup what: Everything       older_than: Any age  delete: True
```

**`what`** — Standup threads, Digest posts, or Everything.

**`older_than`** — Any age, 1 day, 7 days, 14 days, or 30 days. *Any age* exists
for one situation: clearing a test run immediately, where waiting a week defeats
the purpose.

**`delete`** — off by default. Archiving is reversible; deleting is not.

**Digest posts can only be deleted, not archived.** They're plain channel
messages with no archive concept, so archiving them would silently do nothing.
Choosing Digest posts or Everything without `delete: True` is refused with a
message saying so, rather than reporting a success that did nothing.

**Session records are never touched.** Threads and digest posts are
presentation; the JSON files on the volume are the record. Wiping the channel
doesn't lose who said what — those files are also the evidence when you write up
a sprint.

---

## `/standup post` · `/standup digest`

Trigger either manually, outside the schedule.

`post` won't double-post: anyone already prompted today is skipped, whether the
scheduler or a person triggered it. Both paths write and check the same per-day
marker, which is what stopped a manual run creating a second set of threads
alongside the scheduled ones.

---

## `/standup reset`

Discards every runtime override and returns to `config.yaml`.

Safe to run any time. It doesn't touch sessions, threads or anything in Jira.

---

## When a command says it worked but nothing changed

Fixed, but worth knowing what it looked like. The scheduler used to hold a copy
of the config taken when it was constructed, so changing a time through
`/standup time` updated the file and the reply said so, while the scheduler kept
running on the old value indefinitely.

It now reads config through the bot rather than holding its own copy, so a
change applies from the next scheduled run. If you ever see that symptom again,
`/standup status` is the check: if the override is listed there but the
behaviour hasn't moved, it's a reload problem, not a storage one.

---

## When commands don't appear

They're registered per-guild and synced on startup, so they appear within
seconds of the bot connecting. If they're missing:

- The bot may have been invited without the `applications.commands` scope.
  Re-run the invite URL with that scope ticked.
- You may not have **Manage Server**, in which case Discord hides them.
- The bot may not be running. Check the deploy logs.
