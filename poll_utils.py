import asyncio
import discord
import json
from polls import extract_poll_results
from db import finalize_poll_capture

def schedule_poll_capture(message: discord.Message):
    """Schedule a poll capture for when it expires."""
    poll = message.poll
    if poll is None:
        print(f"[Poll] No poll object found on message {message.id}")
        return

    if poll.expires_at is None:
        print(f"[Poll] Poll {message.id} has no expiry time")
        return

    now = discord.utils.utcnow()
    delay = (poll.expires_at - now).total_seconds()

    if delay <= 0:
        print(f"[Poll] Poll {message.id} already expired, capturing immediately")
        asyncio.create_task(capture_and_store(message))
    else:
        print(f"[Poll] Scheduled capture in {delay:.0f}s (expires {poll.expires_at.isoformat()})")
        asyncio.create_task(_wait_then_capture(delay, message))

async def _wait_then_capture(delay: float, message: discord.Message):
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
        result = await extract_poll_results(message.id, message.channel.id, message._state._get_client())  # need client
        if result is None:
            print(f"[Poll] ❌ Failed to extract results for {message.id}")
            return

        finalize_poll_capture(str(message.id), result)
        print("[Poll] ✅ Stored!")
        print("[Poll] Captured JSON:")
        print(json.dumps(result, indent=2, ensure_ascii=False))
    except Exception as e:
        print(f"[Poll] ❌ Error in capture_and_store: {e}")
        import traceback
        traceback.print_exc()