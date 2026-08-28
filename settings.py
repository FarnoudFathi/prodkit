"""
Runtime settings overlay.

config.yaml ships with the code, so anything written to it is wiped on the next
deploy. Settings changed from Discord have to live somewhere that survives —
the same persistent volume the session files use.

So there are two layers:

    config.yaml      committed defaults, edited by hand, versioned
    settings.json    runtime overrides, written by slash commands, on /data

The overlay is merged over the defaults at load time. Deleting settings.json
returns everything to the committed values, which makes it a safe reset and
keeps the repo the single source of truth for what a fresh install looks like.

Only a small, explicit set of keys can be overridden. A slash command should not
be able to remap board columns or rewrite the team roster — those are setup
decisions, not day-to-day ones, and a typo in Discord should not be able to take
the bot down.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

SETTINGS_PATH = Path(
    os.environ.get("PRODKIT_SETTINGS_PATH", "/data/settings.json")
)

# Dotted paths into config.yaml that slash commands are allowed to change.
# Everything else requires a code change and a deploy, on purpose.
EDITABLE = {
    "schedule.prompt_time",
    "schedule.nudge_time",
    "schedule.cutoff_time",
    "schedule.eod_time",
    "schedule.eod_close_time",
    "schedule.eod_enabled",
    "schedule.active_days",
    "discord.standup_channel_id",
    "discord.digest_channel_id",
    "discord.notes_channel_id",
    "discord.cleanup_keep_days",
    "limits.max_in_progress",
    "limits.max_todo",
    "limits.max_review",
    "limits.max_active_wip",
    "review.enabled",
}


class Overlay:
    def __init__(self, path: Path = SETTINGS_PATH):
        self.path = path
        self.data: dict = {}
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            self.data = {}
            return
        try:
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            # A broken overlay must not stop the bot. Fall back to the committed
            # defaults and say so — running on defaults beats not running.
            print(f"settings.json unreadable ({e}); using config.yaml defaults")
            self.data = {}

    def save(self) -> None:
        """Atomic write — a crash mid-save leaves the previous file intact."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=2, sort_keys=True)
            os.replace(tmp, self.path)
        except Exception:
            Path(tmp).unlink(missing_ok=True)
            raise

    def set(self, dotted: str, value) -> None:
        if dotted not in EDITABLE:
            raise ValueError(f"{dotted} is not editable at runtime")
        section, _, key = dotted.partition(".")
        self.data.setdefault(section, {})[key] = value
        self.save()

    def unset(self, dotted: str) -> None:
        section, _, key = dotted.partition(".")
        if section in self.data:
            self.data[section].pop(key, None)
            if not self.data[section]:
                self.data.pop(section)
        self.save()

    def clear(self) -> None:
        self.data = {}
        self.save()

    def apply(self, raw: dict) -> dict:
        """Merge the overlay over a parsed config.yaml, one level deep."""
        merged = {k: (dict(v) if isinstance(v, dict) else v) for k, v in raw.items()}
        for section, values in self.data.items():
            if isinstance(merged.get(section), dict) and isinstance(values, dict):
                merged[section].update(values)
            else:
                merged[section] = values
        return merged

    def describe(self) -> list[str]:
        """Human-readable list of what's currently overridden."""
        out = []
        for section, values in sorted(self.data.items()):
            for key, value in sorted(values.items()):
                out.append(f"{section}.{key} = {value}")
        return out
