# Slim base rather than alpine: discord.py and its dependencies build cleanly
# here, and alpine's musl libc regularly needs compilation workarounds for
# marginal size savings.
FROM python:3.12-slim

# The whole tool measures durations against per-person calendars, and Python's
# zoneinfo reads the system timezone database. Slim images don't ship one, so
# every calendar lookup would fail exactly the way it did on Windows.
ENV TZ=UTC
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Session files go to a mounted volume so a redeploy doesn't wipe the day's
# answers.
ENV PRODKIT_SESSION_DIR=/data/sessions

# Runtime settings changed from Discord live on the volume too — config.yaml
# ships with the code and is overwritten on every deploy.
ENV PRODKIT_SETTINGS_PATH=/data/settings.json

CMD ["python", "bot.py", "run"]
