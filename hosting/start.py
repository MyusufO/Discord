"""
start.py — P2P failover using Ably with main node priority
Everyone runs: python start.py
Main node: DESKTOP-RBE8OKC (always takes over when it comes online)
"""

import os
import sys
import socket
import asyncio
import subprocess
from datetime import datetime, timezone
from dotenv import load_dotenv
from pathlib import Path
from ably import AblyRealtime


load_dotenv()
print(os.getenv("ABLY_API_KEY"))

ABLY_KEY    = os.getenv("ABLY_API_KEY")
NODE_ID     = socket.gethostname()
MAIN_NODE   = os.getenv("MAIN_NODE")
TIMEOUT     = 30   # seconds before assuming active node is dead
INTERVAL    = 10   # seconds between heartbeats
CHANNEL     = os.getenv("GET_CHANNEL")
IS_MAIN     = NODE_ID == MAIN_NODE

bot         = None
active_node = None
last_seen   = None


def start_bot():
    global bot
    if bot is None or bot.poll() is not None:
        print(f"[{NODE_ID}] 🟢 Starting bot...")
        bot = subprocess.Popen([sys.executable, "index.py"])


def stop_bot():
    global bot
    if bot and bot.poll() is None:
        print(f"[{NODE_ID}] 🔴 Stopping bot...")
        bot.terminate()
        bot = None


async def main():
    global bot, active_node, last_seen

    ably    = AblyRealtime(ABLY_KEY)
    channel = ably.channels.get(CHANNEL)

    async def on_heartbeat(message):
        global active_node, last_seen
        sender = message.data.get("node_id")
        is_sender_main = message.data.get("is_main", False)

        # If main node checks in and we are not main → always yield to it
        if is_sender_main and not IS_MAIN:
            if active_node != MAIN_NODE:
                print(f"[{NODE_ID}] 👑 Main node online. Yielding...")
                stop_bot()
            active_node = sender
            last_seen   = datetime.now(timezone.utc)

        elif sender != NODE_ID:
            # Only update active node if main isn't already running
            if active_node != MAIN_NODE:
                active_node = sender
                last_seen   = datetime.now(timezone.utc)
            print(f"[{NODE_ID}] 💤 Standby. Active: {sender}")

    await channel.subscribe("heartbeat", on_heartbeat)

    if IS_MAIN:
        print(f"[{NODE_ID}] 👑 Main node. Starting immediately...")
        await channel.publish("heartbeat", {"node_id": NODE_ID, "is_main": True})
        active_node = NODE_ID
        last_seen   = datetime.now(timezone.utc)
        start_bot()
    else:
        print(f"[{NODE_ID}] Standby node. Waiting {TIMEOUT}s to check for active node...")
        await asyncio.sleep(TIMEOUT)

    while True:
        try:
            now      = datetime.now(timezone.utc)
            is_dead  = (
                last_seen is None or
                (now - last_seen).total_seconds() > TIMEOUT
            )
            i_am_active = active_node == NODE_ID

            if IS_MAIN:
                # Main node always runs the bot
                await channel.publish("heartbeat", {"node_id": NODE_ID, "is_main": True})
                active_node = NODE_ID
                last_seen   = now
                start_bot()

            elif is_dead or i_am_active:
                # No active node — take over as standby
                print(f"[{NODE_ID}] ⚡ No active node. Taking over...")
                await channel.publish("heartbeat", {"node_id": NODE_ID, "is_main": False})
                active_node = NODE_ID
                last_seen   = now
                start_bot()

            else:
                # Someone else is active — stay standby
                stop_bot()

        except Exception as e:
            print(f"[{NODE_ID}] Error: {e}")

        await asyncio.sleep(INTERVAL)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print(f"\n[{NODE_ID}] Shutting down...")
        stop_bot()