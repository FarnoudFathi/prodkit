"""
ProdKit — connection check.

Run this before anything else. It proves four things:

  1. The Jira token works and can see the SR71 board
  2. The Discord token works and the bot can reach the server
  3. The bot has the permissions and intents it needs
  4. discord.py actually installs on this Python version

It also prints most of the values that go into the config file later —
Discord user IDs, channel IDs, and Jira account IDs — so you never have to
ask anyone for theirs.

Nothing here writes to anything. It reads and prints, then exits.

    python check.py
"""

import os
import sys

import requests
from dotenv import load_dotenv

# Reads the .env file and makes its contents available via os.getenv().
# Keeping secrets in .env rather than in the code is what allows this repo
# to be public without leaking anything.
load_dotenv()

JIRA_SITE = os.getenv("JIRA_SITE")
JIRA_EMAIL = os.getenv("JIRA_EMAIL")
JIRA_API_TOKEN = os.getenv("JIRA_API_TOKEN")
JIRA_PROJECT_KEY = os.getenv("JIRA_PROJECT_KEY", "SR71")
DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")


def line(char="-", width=64):
    print(char * width)


def require_env():
    """Fail early and clearly if something is missing from .env."""
    missing = [
        name
        for name, value in [
            ("JIRA_SITE", JIRA_SITE),
            ("JIRA_EMAIL", JIRA_EMAIL),
            ("JIRA_API_TOKEN", JIRA_API_TOKEN),
            ("DISCORD_BOT_TOKEN", DISCORD_BOT_TOKEN),
        ]
        if not value
    ]
    if missing:
        print("Missing from .env: " + ", ".join(missing))
        print("Check the file is named exactly .env and sits next to check.py.")
        sys.exit(1)


# ---------------------------------------------------------------- Jira

def jira_get(path, params=None):
    """
    One GET against the Jira REST API.

    Auth is email + API token via HTTP basic auth. That's Atlassian's
    documented scheme for Cloud — the token stands in for a password, which
    is why it carries your full permissions and must never be committed.
    """
    response = requests.get(
        f"https://{JIRA_SITE}{path}",
        params=params or {},
        auth=(JIRA_EMAIL, JIRA_API_TOKEN),
        headers={"Accept": "application/json"},
        timeout=30,
    )
    response.raise_for_status()
    return response.json()


def check_jira():
    print("\nJIRA")
    line()

    # /myself confirms the credentials are valid before we try anything else.
    # If this fails, every other Jira call would fail too, and the error here
    # is far easier to read than one buried in a search query.
    try:
        me = jira_get("/rest/api/3/myself")
    except requests.HTTPError as e:
        print(f"  FAILED to authenticate: {e}")
        print("  Usually means a wrong email, a typo'd token, or a revoked token.")
        return
    except requests.RequestException as e:
        print(f"  Could not reach {JIRA_SITE}: {e}")
        return

    print(f"  Authenticated as : {me.get('displayName')}")
    print(f"  Your accountId   : {me.get('accountId')}")

    # Atlassian has been migrating search endpoints. Rather than guess which
    # one this site uses, try the newer one and fall back. Whichever answers
    # here is the one the read layer will be built against.
    issues, endpoint = None, None

    try:
        payload = jira_get(
            "/rest/api/3/search/jql",
            {
                "jql": f"project = {JIRA_PROJECT_KEY} ORDER BY key ASC",
                "maxResults": 100,
                "fields": "summary,status,assignee,duedate,timetracking,"
                          "components,issuetype,issuelinks",
            },
        )
        issues = payload.get("issues", [])
        endpoint = "/rest/api/3/search/jql"
    except requests.HTTPError:
        try:
            payload = jira_get(
                "/rest/api/3/search",
                {
                    "jql": f"project = {JIRA_PROJECT_KEY} ORDER BY key ASC",
                    "maxResults": 100,
                    "fields": "summary,status,assignee,duedate,timetracking,"
                              "components,issuetype,issuelinks",
                },
            )
            issues = payload.get("issues", [])
            endpoint = "/rest/api/3/search (legacy)"
        except requests.HTTPError as e:
            print(f"  Search failed on both endpoints: {e}")
            return

    print(f"  Search endpoint  : {endpoint}")
    print(f"  Issues found     : {len(issues)}")

    # Collect the distinct people on the board. On a live team this is how you
    # get everyone's Jira accountId for the config without asking them.
    people = {}
    statuses = {}
    links_found = 0

    for issue in issues:
        fields = issue["fields"]

        status_name = fields["status"]["name"]
        statuses[status_name] = statuses.get(status_name, 0) + 1

        assignee = fields.get("assignee")
        if assignee:
            people[assignee["accountId"]] = assignee.get("displayName")

        for link in fields.get("issuelinks", []):
            if link.get("inwardIssue"):
                links_found += 1

    print(f"  Status spread    : " + ", ".join(
        f"{name} {count}" for name, count in sorted(statuses.items())
    ))
    print(f"  'blocked by' links: {links_found}")

    print("\n  Assignees on the board (accountId -> name):")
    if people:
        for account_id, name in people.items():
            print(f"    {account_id}  {name}")
    else:
        print("    none — tickets are unassigned")

    # Transitions are workflow-specific and vary per issue, so print the real
    # ones for a real ticket. These IDs are what write-back will POST later.
    if issues:
        sample_key = issues[0]["key"]
        try:
            transitions = jira_get(f"/rest/api/3/issue/{sample_key}/transitions")
            print(f"\n  Transitions available from {sample_key} "
                  f"(currently {issues[0]['fields']['status']['name']}):")
            for t in transitions.get("transitions", []):
                print(f"    id {t['id']:<5} -> {t['to']['name']}")
        except requests.HTTPError as e:
            print(f"  Could not read transitions: {e}")


# ------------------------------------------------------------- Discord

def check_discord():
    print("\nDISCORD")
    line()

    try:
        import discord
    except ImportError:
        print("  discord.py is not installed.")
        print("  Run: pip install -r requirements.txt")
        return
    except Exception as e:
        # Most likely on very new Python versions where a dependency hasn't
        # caught up yet. Worth reporting precisely rather than crashing.
        print(f"  discord.py failed to import on this Python version: {e}")
        print(f"  Python here is {sys.version.split()[0]}")
        return

    # Only the members intent is enabled. Message Content is deliberately off:
    # button clicks and modal submissions arrive as interaction events, so the
    # bot never needs to read what the team writes to each other.
    intents = discord.Intents.default()
    intents.members = True

    client = discord.Client(intents=intents)

    @client.event
    async def on_ready():
        print(f"  Logged in as     : {client.user}  (id {client.user.id})")
        print(f"  Servers visible  : {len(client.guilds)}")

        for guild in client.guilds:
            print(f"\n  Server: {guild.name}  (id {guild.id})")

            print("    Text channels (name -> id):")
            for channel in guild.text_channels:
                print(f"      #{channel.name:<20} {channel.id}")

            print("    Members (name -> id):")
            for member in guild.members:
                tag = " [BOT]" if member.bot else ""
                print(f"      {member.name:<20} {member.id}{tag}")

        await client.close()

    try:
        client.run(DISCORD_BOT_TOKEN, log_handler=None)
    except discord.LoginFailure:
        print("  Token rejected. Reset it in the Developer Portal and re-copy.")
    except discord.PrivilegedIntentsRequired:
        print("  Server Members Intent is not enabled.")
        print("  Developer Portal -> your app -> Bot -> Privileged Gateway Intents.")
    except Exception as e:
        print(f"  Discord connection failed: {e}")


if __name__ == "__main__":
    print(f"Python {sys.version.split()[0]}")
    require_env()
    check_jira()
    check_discord()
    print("\nDone.")
