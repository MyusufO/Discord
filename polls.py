import discord
from datetime import datetime, timezone


async def extract_poll_results(message_id: int, channel_id: int, client: discord.Client) -> dict | None:
    """
    Fetches a poll from Discord and returns:
    {
        "question": "...",
        "answers": [
            {
                "answer_id": 1,
                "text": "Option A",
                "vote_count": 3,
                "voters": [{"user_id": "123", "username": "aryan"}, ...]
            },
            ...
        ]
    }
    Returns None if the message has no poll.
    """
    channel = client.get_channel(channel_id)
    if channel is None:
        print(f"[Poll] Channel {channel_id} not found in cache.")
        return None

    try:
        message = await channel.fetch_message(message_id)
    except discord.NotFound:
        print(f"[Poll] Message {message_id} not found.")
        return None

    poll = message.poll
    if poll is None:
        print(f"[Poll] No poll on message {message_id}.")
        return None

    question_text = poll.question.text if hasattr(poll.question, "text") else str(poll.question)

    result = {
        "question": question_text,
        "answers": [],
    }

    for answer in poll.answers:
        answer_data = {
            "answer_id":  answer.id,
            "text":       answer.text,
            "vote_count": answer.vote_count,
            "voters":     [],
        }

        try:
            async for voter in answer.voters():
                answer_data["voters"].append({
                    "user_id":  str(voter.id),
                    "username": voter.name,
                })
        except RuntimeError as e:
            print(f"[Poll] Could not fetch voters for '{answer.text}': {e}")
        except discord.HTTPException as e:
            print(f"[Poll] HTTP error fetching voters for '{answer.text}': {e}")

        result["answers"].append(answer_data)

    return result