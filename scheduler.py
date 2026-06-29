import asyncio
import os
from datetime import datetime, timezone, timedelta
import discord
from db import (
    get_matches_for_date, mark_poll_created, set_match_guild_channel,
    save_poll_immediately
)
from polls import create_poll_message, extract_poll_results
from poll_utils import schedule_poll_capture

_bot_client = None

def set_bot_client(client):
    global _bot_client
    _bot_client = client

async def create_todays_polls():
    if _bot_client is None:
        print("[Scheduler] Bot client not set.")
        return

    today = datetime.now(timezone.utc).date()
    matches = get_matches_for_date(today)

    for match in matches:
        if match.get("poll_created", False):
            continue
        team1 = match["team_1"]
        team2 = match["team_2"]
        if "Winner of Match" in team1 or "Loser of Match" in team1:
            continue
        if "Winner of Match" in team2 or "Loser of Match" in team2:
            continue

        guild_id = match.get("guild_id")
        channel_id = match.get("channel_id")
        if not guild_id or not channel_id:
            guild_id = os.getenv("DEFAULT_GUILD_ID")
            channel_id = os.getenv("DEFAULT_CHANNEL_ID")
            if not guild_id or not channel_id:
                print(f"[Scheduler] No guild/channel for match {match['match_number']}, skipping.")
                continue
            set_match_guild_channel(match["match_number"], guild_id, channel_id)

        guild = _bot_client.get_guild(int(guild_id))
        if guild is None:
            print(f"[Scheduler] Guild {guild_id} not found.")
            continue
        channel = guild.get_channel(int(channel_id))
        if channel is None:
            print(f"[Scheduler] Channel {channel_id} not found.")
            continue

        question = f"Who wins? {team1} vs {team2} ({match['venue']}, {match['time']})"
        message = await create_poll_message(channel, team1, team2, question)

        # --- NEW: Save poll to DB and schedule capture ---
        result = await extract_poll_results(message.id, message.channel.id, _bot_client)
        if result:
            save_poll_immediately(
                message_id=message.id,
                channel_id=message.channel.id,
                guild_id=guild_id,
                result=result,
                expires_at=message.poll.expires_at
            )
            schedule_poll_capture(message)
        else:
            print(f"[Scheduler] Failed to extract results for poll {message.id}")

        mark_poll_created(match["match_number"], str(message.id))
        print(f"[Scheduler] ✅ Created poll for match {match['match_number']}")

async def scheduler_loop():
    while True:
        now = datetime.now(timezone.utc)
        next_midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        sleep_seconds = (next_midnight - now).total_seconds()
        await asyncio.sleep(sleep_seconds)
        await create_todays_polls()

def start_scheduler(client):
    set_bot_client(client)
    asyncio.create_task(scheduler_loop())