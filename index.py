import os
import asyncio
import json
import discord
import csv
import io

from dotenv import load_dotenv
load_dotenv()
TOKEN       = os.getenv("DISCORD_BOT_TOKEN")
ADMIN_ROLE  = os.getenv("ADMIN_ROLE_NAME", "Admin")
from polls import extract_poll_results
from db import (init_db, save_poll_immediately, finalize_poll_capture, 
                get_poll, get_uncaptured_polls, resolve_poll, 
                add_points, get_points, get_leaderboard,save_manual_poll)



intents = discord.Intents.default()
intents.guild_polls = True
intents.message_content = True
intents.members = True

client = discord.Client(intents=intents)

PREFIX = "!"


# ---------------------------------------------------------------------------
# Poll capture
# ---------------------------------------------------------------------------

def schedule_poll_capture(message: discord.Message):
    """Schedule a poll capture for when it expires."""
    poll = message.poll
    if poll is None:
        print(f"[Poll] No poll object found on message {message.id}")
        return
    
    if poll.expires_at is None:
        print(f"[Poll] Poll {message.id} has no expiry time")
        return

    now   = discord.utils.utcnow()
    delay = (poll.expires_at - now).total_seconds()

    if delay <= 0:
        print(f"[Poll] Poll {message.id} already expired, capturing immediately")
        asyncio.create_task(capture_and_store(message))
    else:
        print(f"[Poll] Scheduled capture in {delay:.0f}s (expires {poll.expires_at.isoformat()})")
        asyncio.create_task(_wait_then_capture(delay, message))


async def _wait_then_capture(delay: float, message: discord.Message):
    """Wait for poll to expire, then capture."""
    try:
        await asyncio.sleep(delay)
        try:
            fresh = await message.channel.fetch_message(message.id)
        except discord.NotFound:
            print(f"[Poll] Message {message.id} gone before capture")
            return
        await capture_and_store(fresh)
    except Exception as e:
        print(f"[Poll] ❌ Error in _wait_then_capture: {e}")


async def capture_and_store(message: discord.Message):
    """Extract final votes and finalize the poll in DB."""
    try:
        print(f"[Poll] 📊 Capturing final votes for {message.id}...")

        result = await extract_poll_results(message.id, message.channel.id, client)
        if result is None:
            print(f"[Poll] ❌ Failed to extract results for {message.id}")
            return

        # Update DB with final voter list
        finalize_poll_capture(str(message.id), result)
        
        print("[Poll] ✅ Stored!")
        print("[Poll] Captured JSON:")
        print(json.dumps(result, indent=2, ensure_ascii=False))
    except Exception as e:
        print(f"[Poll] ❌ Error in capture_and_store: {e}")
        import traceback
        traceback.print_exc()


# ---------------------------------------------------------------------------
# Startup recovery: check for uncaptured polls
# ---------------------------------------------------------------------------

async def recover_uncaptured_polls():
    """
    On startup, check DB for any polls that haven't been captured yet.
    If expired, capture them. Otherwise, reschedule.
    """
    print("[Recovery] Checking for uncaptured polls...")

    uncaptured = get_uncaptured_polls()
    if not uncaptured:
        print("[Recovery] ✅ No uncaptured polls found")
        return

    print(f"[Recovery] Found {len(uncaptured)} uncaptured poll(s), checking expiry...")

    # Use timezone-aware UTC for comparison
    now = discord.utils.utcnow()

    expired_count = 0
    rescheduled_count = 0

    for poll_doc in uncaptured:
        msg_id = int(poll_doc["message_id"])
        channel_id = int(poll_doc["channel_id"])
        expires_at = poll_doc["expires_at"]

        # Make expires_at offset-aware if it's naive
        if expires_at.tzinfo is None:
            from datetime import timezone as tz
            expires_at = expires_at.replace(tzinfo=tz.utc)

        # Try to fetch the message
        try:
            channel = client.get_channel(channel_id)

            if channel is None:
                print(
                    f"[Recovery] ⚠️ Channel {channel_id} not found for poll {msg_id}"
                )
                continue

            message = await channel.fetch_message(msg_id)

        except discord.NotFound:
            print(f"[Recovery] ⚠️ Message {msg_id} not found (deleted?)")
            continue

        except Exception as e:
            print(f"[Recovery] ⚠️ Error fetching message {msg_id}: {e}")
            continue

        # Check if expired
        if now >= expires_at:
            print(f"[Recovery] 🔴 Poll {msg_id} has expired, capturing now...")
            await capture_and_store(message)
            expired_count += 1

        else:
            delay = (expires_at - now).total_seconds()
            print(
                f"[Recovery] 🟡 Poll {msg_id} not expired yet ({delay:.0f}s remaining), rescheduling..."
            )

            schedule_poll_capture(message)
            rescheduled_count += 1

    print(
        f"[Recovery] ✅ Complete — {expired_count} captured, {rescheduled_count} rescheduled"
    )

# ---------------------------------------------------------------------------
# Admin helpers
# ---------------------------------------------------------------------------

def _is_admin(member: discord.Member) -> bool:
    if member.guild_permissions.administrator:
        return True
    return any(r.name == ADMIN_ROLE for r in member.roles)


def _make_resolve_embed(poll: dict, winning_key: str, rewarded: list[dict], tournament_name: str, points_per_winner: int) -> discord.Embed:
    option_text = poll["options"][winning_key]["text"]
    
    # Calculate total voters
    total_voters = sum(len(option["voters"]) for option in poll["options"].values())
    
    embed = discord.Embed(
        title="✅ Poll Resolved",
        description=f"**{poll['question']}**",
        color=discord.Color.green(),
    )
    embed.add_field(name="Winning option", value=option_text, inline=False)
    embed.add_field(name="Tournament", value=tournament_name, inline=False)

    if rewarded:
        names = ", ".join(f"@{v['username']}" for v in rewarded)
        embed.add_field(
            name=f"+{points_per_winner} point(s) awarded to ({len(rewarded)})",
            value=names,
            inline=False,
        )
        
        # Show calculation
        embed.add_field(
            name="Points Formula",
            value=f"⌊{total_voters} ÷ {len(rewarded)}⌋ = **{points_per_winner}** pts",
            inline=False,
        )
    else:
        embed.add_field(name="Points awarded", value="No voters to reward.", inline=False)

    return embed

async def generate_leaderboard_csv(server_id: str, tournament_name: str) -> str:
    """Generate a CSV leaderboard and return the CSV content as string."""
    from db import get_leaderboard_stats
    
    stats = get_leaderboard_stats(server_id, tournament_name)
    
    if not stats:
        return None
    
    # Create CSV in memory
    output = io.StringIO()
    writer = csv.writer(output)
    
    # Write header
    writer.writerow(["Rank", "Name", "User ID", "Polls Voted On", "Polls Correct", "Total Points"])
    
    # Write data
    for rank, user in enumerate(stats, 1):
        writer.writerow([
            rank,
            user["username"],
            user["user_id"],
            user["polls_voted"],
            user["polls_correct"],
            user["total_points"],
        ])
    
    return output.getvalue()
# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

async def handle_commands(message: discord.Message):
    content = message.content.strip()

    # ---- !resolve <message_id> <answer_id> [tournament_name] --------
    if content.startswith(f"{PREFIX}resolve"):
        if not _is_admin(message.author):
            await message.reply("❌ You need the Admin role to resolve polls.")
            return

        parts = content.split()
        if len(parts) < 3:
            await message.reply(
                f"Usage: `{PREFIX}resolve <message_id> <answer_id> [tournament_name]`\n"
                "Example: `!resolve 1234567890 2 WorldCup2026`\n"
                "Tournament name defaults to 'WorldCup2026' if not specified."
            )
            return

        msg_id, ans_id = parts[1], parts[2]
        tournament_name = parts[3] if len(parts) > 3 else "WorldCup2026"

        if not msg_id.isdigit() or not ans_id.isdigit():
            await message.reply("Both `message_id` and `answer_id` must be integers.")
            return

        poll = get_poll(msg_id)
        if poll is None:
            await message.reply(f"❌ No poll found for message ID `{msg_id}`.")
            return

        if poll.get("resolved_answer_id") is not None:
            prev = poll["resolved_answer_id"]
            await message.reply(
                f"⚠️ This poll was already resolved with answer ID `{prev}`. "
                "Use `!update-points` to change it or contact an admin."
            )
            return

        try:
            winners = resolve_poll(msg_id, int(ans_id))
        except ValueError as e:
            await message.reply(f"❌ {e}")
            return

        winning_key = str(ans_id)
        correct_voter_count = len(winners)
        total_voters = sum(len(option["voters"]) for option in poll["options"].values())

        if correct_voter_count == 0:
            points_per_winner = 0
        else:
            points_per_winner = round(total_voters / correct_voter_count)

        guild_id = str(message.guild.id) if message.guild else "0"
        for voter in winners:
            add_points(
                voter["user_id"],
                voter["username"],
                guild_id,
                tournament_name,
                amount=points_per_winner
            )

        embed = _make_resolve_embed(poll, winning_key, winners, tournament_name, points_per_winner)
        await message.channel.send(embed=embed)
        return

    # ---- !update-points <message_id> <answer_id> [tournament_name] ----
    if content.startswith(f"{PREFIX}update-points"):
        if not _is_admin(message.author):
            await message.reply("❌ You need the Admin role to update points.")
            return

        parts = content.split()
        if len(parts) < 3:
            await message.reply(
                f"Usage: `{PREFIX}update-points <message_id> <answer_id> [tournament_name]`\n"
                "Example: `!update-points 1234567890 2 WorldCup2026`\n"
                "Use this for polls already captured by the bot."
            )
            return

        msg_id, ans_id = parts[1], parts[2]
        tournament_name = parts[3] if len(parts) > 3 else "WorldCup2026"

        if not msg_id.isdigit() or not ans_id.isdigit():
            await message.reply("Both `message_id` and `answer_id` must be integers.")
            return

        poll = get_poll(msg_id)
        if poll is None:
            await message.reply(
                f"❌ No poll found for message ID `{msg_id}`. "
                "Make sure it was captured and saved to DB."
            )
            return

        if not poll.get("captured"):
            await message.reply(
                f"⚠️ Poll {msg_id} hasn't been captured yet. "
                "Use `!resolve` instead for active polls."
            )
            return

        key = str(ans_id)
        if key not in poll["options"]:
            valid_ids = ", ".join(poll["options"].keys())
            await message.reply(f"❌ Answer ID {ans_id} not found. Valid options: {valid_ids}")
            return

        winners = poll["options"][key]["voters"]
        correct_voter_count = len(winners)
        total_voters = sum(len(option["voters"]) for option in poll["options"].values())

        if correct_voter_count == 0:
            points_per_winner = 0
        else:
            points_per_winner = round(total_voters / correct_voter_count)

        guild_id = str(message.guild.id) if message.guild else "0"
        for voter in winners:
            add_points(
                voter["user_id"],
                voter["username"],
                guild_id,
                tournament_name,
                amount=points_per_winner
            )

        embed = _make_resolve_embed(poll, key, winners, tournament_name, points_per_winner)
        embed.title = "✅ Points Updated"
        await message.channel.send(embed=embed)
        return

    # ---- !points [@user] [tournament_name] ----
    if content.startswith(f"{PREFIX}points"):
        parts = content.split()

        tournament_name = "WorldCup2026"
        if len(parts) > 1 and not parts[-1].startswith("<@"):
            tournament_name = parts[-1]

        if message.mentions:
            target = message.mentions[0]
        elif len(parts) == 1:
            target = message.author
        else:
            await message.reply(f"Usage: `{PREFIX}points` or `{PREFIX}points @user [tournament]`")
            return

        guild_id = str(message.guild.id) if message.guild else "0"
        pts = get_points(str(target.id), guild_id, tournament_name)
        await message.reply(
            f"**{target.display_name}** has **{pts}** point(s) in **{tournament_name}**."
        )
        return

    # ---- !leaderboard [tournament_name] ----
    if content.startswith(f"{PREFIX}leaderboard"):
        parts = content.split()
        tournament_name = parts[1] if len(parts) > 1 else "WorldCup2026"

        guild_id = str(message.guild.id) if message.guild else "0"
        print(f"[DEBUG] guild_id={guild_id}, tournament={tournament_name}")
        board = get_leaderboard(guild_id, tournament_name, limit=10)

        if not board:
            await message.reply(f"No points recorded yet for **{tournament_name}**.")
            return

        lines = [f"**🏆 {tournament_name} Leaderboard**"]
        medals = ["🥇", "🥈", "🥉"]
        for i, entry in enumerate(board):
            prefix = medals[i] if i < 3 else f"{i+1}."
            lines.append(f"{prefix} **@{entry['username']}** — {entry['points']} pt(s)")

        await message.channel.send("\n".join(lines))
        return

    # ---- !create-manual-poll ----
    if content.startswith(f"{PREFIX}create-manual-poll"):
        if not _is_admin(message.author):
            await message.reply("❌ You need the Admin role to create manual polls.")
            return

        await message.reply(
            "📝 **Creating Manual Poll**\n"
            "Please reply with the poll **question** in your next message.\n"
            "Example: `Who will win the World Cup?`"
        )

        try:
            question_msg = await client.wait_for(
                "message",
                check=lambda m: m.author == message.author and m.channel == message.channel,
                timeout=300.0
            )
            question = question_msg.content.strip()

            if len(question) < 3:
                await message.reply("❌ Question too short!")
                return

            await message.reply(
                "🎯 **Add Options**\n"
                "Reply with each option on a **new line**.\n"
                "Example:\n```\nFrance\nArgentina\nBrazil\n```"
            )

            options_msg = await client.wait_for(
                "message",
                check=lambda m: m.author == message.author and m.channel == message.channel,
                timeout=300.0
            )

            option_texts = [opt.strip() for opt in options_msg.content.strip().split("\n") if opt.strip()]

            if len(option_texts) < 2:
                await message.reply("❌ You need at least 2 options!")
                return

            options = [
                {"answer_id": i + 1, "text": text, "voters": []}
                for i, text in enumerate(option_texts)
            ]

            options_list = "\n".join([f"{i+1}. {text}" for i, text in enumerate(option_texts)])
            await message.reply(
                f"✅ Options created:\n{options_list}\n\n"
                f"Now add voters for each option.\n"
                f"Reply with: `<option_number> @user1 @user2 @user3`\n"
                f"Example: `1 @aryan @john @sarah`\n"
                f"Send `done` when finished."
            )

            while True:
                voter_msg = await client.wait_for(
                    "message",
                    check=lambda m: m.author == message.author and m.channel == message.channel,
                    timeout=300.0
                )

                if voter_msg.content.strip().lower() == "done":
                    break

                parts = voter_msg.content.strip().split()
                if not parts or not parts[0].isdigit():
                    await message.reply("❌ Invalid format. Reply with: `<option_number> @user1 @user2`")
                    continue

                option_num = int(parts[0])
                if option_num < 1 or option_num > len(options):
                    await message.reply(f"❌ Invalid option number. Valid: 1-{len(options)}")
                    continue

                if not voter_msg.mentions:
                    await message.reply("❌ No users mentioned. Use @username")
                    continue

                for user in voter_msg.mentions:
                    options[option_num - 1]["voters"].append({
                        "user_id": str(user.id),
                        "username": user.name,
                    })

                await message.reply(
                    f"✅ Added {len(voter_msg.mentions)} voter(s) to option {option_num}\n"
                    f"Reply with next option or `done` to finish."
                )

            guild_id = str(message.guild.id) if message.guild else "0"
            poll_id = save_manual_poll(guild_id, question, options)

            summary_lines = [
                f"✅ **Manual Poll Created!**",
                f"**Poll ID:** `{poll_id}`",
                f"**Question:** {question}",
                ""
            ]
            for opt in options:
                summary_lines.append(f"**{opt['answer_id']}. {opt['text']}** - {len(opt['voters'])} voters")
            summary_lines.append("")
            summary_lines.append(f"To resolve: `!resolve {poll_id} <option_id> [tournament]`")

            await message.reply("\n".join(summary_lines))

        except asyncio.TimeoutError:
            await message.reply("⏱️ Timeout! Manual poll creation cancelled.")
        except Exception as e:
            await message.reply(f"❌ Error: {e}")
            print(f"[Error] in create-manual-poll: {e}")
            import traceback
            traceback.print_exc()
        return

    # ---- !export-leaderboard [tournament_name] ----
    if content.startswith(f"{PREFIX}export-leaderboard"):
        if not _is_admin(message.author):
            await message.reply("❌ You need the Admin role to export leaderboards.")
            return

        parts = content.split()
        tournament_name = parts[1] if len(parts) > 1 else "WorldCup2026"

        guild_id = str(message.guild.id) if message.guild else "0"

        try:
            csv_content = await generate_leaderboard_csv(guild_id, tournament_name)

            if not csv_content:
                await message.reply(f"No leaderboard data for **{tournament_name}**.")
                return

            csv_file = discord.File(
                io.BytesIO(csv_content.encode()),
                filename=f"{tournament_name}_leaderboard.csv"
            )
            await message.reply(
                f"📊 **{tournament_name} Leaderboard Export**",
                file=csv_file
            )
            print(f"[Export] Generated leaderboard CSV for {tournament_name}")

        except Exception as e:
            await message.reply(f"❌ Error generating CSV: {e}")
            print(f"[Error] in export-leaderboard: {e}")
            import traceback
            traceback.print_exc()
        return

    # ---- !capture-history <channel_id> [days] ----
    if content.startswith(f"{PREFIX}capture-history"):
        if not _is_admin(message.author):
            await message.reply("❌ You need the Admin role to capture history.")
            return

        parts = content.split()
        if len(parts) < 2:
            await message.reply(
                f"Usage: `{PREFIX}capture-history <channel_id> [days]`\n"
                "Example: `!capture-history 1234567890123456789 30`\n"
                "Days defaults to 30 if not specified."
            )
            return

        ch_id_str = parts[1]
        days = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 30

        if not ch_id_str.isdigit():
            await message.reply("`channel_id` must be an integer.")
            return

        target_channel = client.get_channel(int(ch_id_str))
        if target_channel is None:
            await message.reply(f"❌ Channel `{ch_id_str}` not found in bot's cache.")
            return

        from datetime import timedelta
        after_dt = discord.utils.utcnow() - timedelta(days=days)

        status_msg = await message.reply(
            f"🔍 Scanning `#{target_channel.name}` for polls in the last **{days}** day(s)...\n"
            "This may take a moment."
        )

        found = 0
        saved = 0
        skipped = 0
        errors = 0

        try:
            async for hist_msg in target_channel.history(limit=None, after=after_dt, oldest_first=True):
                if hist_msg.poll is None:
                    continue

                found += 1
                msg_id_str = str(hist_msg.id)

                if get_poll(msg_id_str):
                    skipped += 1
                    continue

                try:
                    result = await extract_poll_results(hist_msg.id, hist_msg.channel.id, client)
                    if result is None:
                        errors += 1
                        continue

                    guild_id = hist_msg.guild.id if hist_msg.guild else None
                    expires_at = hist_msg.poll.expires_at or discord.utils.utcnow()

                    save_poll_immediately(
                        message_id=hist_msg.id,
                        channel_id=hist_msg.channel.id,
                        guild_id=guild_id,
                        result=result,
                        expires_at=expires_at,
                    )

                    if expires_at <= discord.utils.utcnow():
                        finalize_poll_capture(msg_id_str, result)
                    else:
                        schedule_poll_capture(hist_msg)

                    saved += 1

                except Exception as e:
                    print(f"[capture-history] ❌ Error on message {hist_msg.id}: {e}")
                    errors += 1

        except discord.Forbidden:
            await status_msg.edit(content="❌ Bot doesn't have permission to read that channel's history.")
            return
        except Exception as e:
            await status_msg.edit(content=f"❌ Unexpected error: {e}")
            return

        await status_msg.edit(
            content=(
                f"✅ **History scan complete** for `#{target_channel.name}` (last {days} days)\n"
                f"📊 Polls found: **{found}**\n"
                f"💾 Newly saved: **{saved}**\n"
                f"⏭️ Already in DB: **{skipped}**\n"
                f"❌ Errors: **{errors}**"
            )
        )
        return
# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------

@client.event
async def on_ready():
    init_db()
    print(f"✅ Logged in as {client.user}")
    
    # Recover uncaptured polls on startup
    await recover_uncaptured_polls()


@client.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    # Handle bot commands first
    if message.content.startswith(PREFIX):
        await handle_commands(message)
        return

    # Detect new polls and save immediately
    await asyncio.sleep(0.5)  # Give Discord time to populate poll data
    
    try:
        fresh = await message.channel.fetch_message(message.id)
        if fresh.poll:
            print(f"[Poll] 🔍 Detected poll in message {fresh.id}")
            
            # Extract and save immediately
            result = await extract_poll_results(fresh.id, fresh.channel.id, client)
            if result:
                guild_id = fresh.guild.id if fresh.guild else None
                save_poll_immediately(
                    message_id=fresh.id,
                    channel_id=fresh.channel.id,
                    guild_id=guild_id,
                    result=result,
                    expires_at=fresh.poll.expires_at,
                )
                
                # Schedule capture for when it expires
                schedule_poll_capture(fresh)
            else:
                print(f"[Poll] ❌ Could not extract results for {fresh.id}")
            return
            
    except discord.NotFound:
        print(f"[Message] Message {message.id} not found during poll check")
    except Exception as e:
        print(f"[Error] Unexpected error checking for poll: {e}")


client.run(TOKEN)


