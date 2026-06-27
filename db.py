from pymongo import MongoClient, ASCENDING
from pymongo.errors import DuplicateKeyError
from datetime import datetime, timezone
import os

MONGO_URI = os.getenv("MONGO_URI", "mongodb+srv://OmarAgamy:supersecretpassword@cluster0.lkfhux0.mongodb.net/?appName=Cluster0")
DB_NAME   = os.getenv("MONGO_DB_NAME", "discord_polls")

_client: MongoClient | None = None


def _db():
    global _client
    if _client is None:
        _client = MongoClient(MONGO_URI)
    return _client[DB_NAME]


def init_db():
    db = _db()

    # polls collection
    db["polls"].create_index([("message_id", ASCENDING)], unique=True)
    db["polls"].create_index([("expires_at", ASCENDING)])
    db["polls"].create_index([("captured", ASCENDING)])

    # points collection — unique on (user_id, server_id)
    db["points"].create_index([("user_id", ASCENDING), ("server_id", ASCENDING)], unique=True)

    print("[DB] MongoDB collections ready.")


# ---------------------------------------------------------------------------
# Schema: points collection
#
# {
#   "user_id":   "123456789",
#   "username":  "aryan",
#   "server_id": "999999999",
#   "tournaments": {
#     "WorldCup2026": 5,
#     "Champions2026": 2,
#     ...
#   },
#   "created_at": <datetime>
# }
# ---------------------------------------------------------------------------

def add_points(user_id: str, username: str, server_id: str, tournament_name: str, amount: int = 1):
    """
    Add points to a user for a specific tournament in a specific server.
    Creates doc if absent.
    """
    _db()["points"].update_one(
        {"user_id": user_id, "server_id": server_id},
        {
            "$inc": {f"tournaments.{tournament_name}": amount},
            "$set": {"username": username},
            "$setOnInsert": {"user_id": user_id, "server_id": server_id, "created_at": datetime.now(timezone.utc)},
        },
        upsert=True,
    )


def get_points(user_id: str, server_id: str, tournament_name: str) -> int:
    """Get points for a user in a specific tournament on a specific server."""
    doc = _db()["points"].find_one(
        {"user_id": user_id, "server_id": server_id}
    )
    if doc and "tournaments" in doc:
        return doc["tournaments"].get(tournament_name, 0)
    return 0


def get_leaderboard(server_id: str, tournament_name: str, limit: int = 10) -> list[dict]:
    """Get top users by points in a specific tournament on a specific server."""
    pipeline = [
        {"$match": {"server_id": server_id}},
        {
            "$project": {
                "user_id": 1,
                "username": 1,
                "points": {
                    "$ifNull": [f"$tournaments.{tournament_name}", 0]
                },
            }
        },
        {"$sort": {"points": -1}},
        {"$limit": limit},
    ]
    
    return list(_db()["points"].aggregate(pipeline))


# ... rest of polls functions stay the same ...
def save_poll_immediately(message_id, channel_id, guild_id, result: dict, expires_at: datetime):
    """Save a poll IMMEDIATELY upon creation with the expiry time."""
    options = {}
    for answer in result["answers"]:
        options[str(answer["answer_id"])] = {
            "text":   answer["text"],
            "voters": answer["voters"],
        }

    doc = {
        "message_id":         str(message_id),
        "channel_id":         str(channel_id),
        "guild_id":           str(guild_id) if guild_id else None,
        "question":           result["question"],
        "options":            options,
        "expires_at":         expires_at,
        "captured":           False,
        "resolved_answer_id": None,
        "captured_at":        None,
        "created_at":         datetime.now(timezone.utc),
    }

    try:
        _db()["polls"].insert_one(doc)
        print(f"[DB] ✅ Saved poll {message_id} (expires {expires_at.isoformat()})")
    except DuplicateKeyError:
        _db()["polls"].replace_one({"message_id": str(message_id)}, doc)
        print(f"[DB] ⚠️ Updated existing poll {message_id}")


def finalize_poll_capture(message_id: str, result: dict):
    """Update the options with latest voter data and mark as captured."""
    options = {}
    for answer in result["answers"]:
        options[str(answer["answer_id"])] = {
            "text":   answer["text"],
            "voters": answer["voters"],
        }

    _db()["polls"].update_one(
        {"message_id": str(message_id)},
        {
            "$set": {
                "options":    options,
                "captured":   True,
                "captured_at": datetime.now(timezone.utc),
            }
        },
    )
    print(f"[DB] ✅ Finalized poll {message_id}")


def get_poll(message_id: str) -> dict | None:
    return _db()["polls"].find_one({"message_id": str(message_id)}, {"_id": 0})


def get_uncaptured_polls() -> list[dict]:
    """Return all polls that haven't been captured yet."""
    return list(
        _db()["polls"]
        .find({"captured": False}, {"_id": 0})
        .sort("expires_at", 1)
    )


def resolve_poll(message_id: str, winning_answer_id: int) -> list[dict]:
    """Mark the poll resolved and return the list of winning voters."""
    poll = get_poll(message_id)
    if poll is None:
        raise ValueError(f"Poll {message_id} not found in DB.")

    key = str(winning_answer_id)
    if key not in poll["options"]:
        raise ValueError(
            f"Answer id {winning_answer_id} not in poll. "
            f"Valid ids: {list(poll['options'].keys())}"
        )

    _db()["polls"].update_one(
        {"message_id": str(message_id)},
        {"$set": {"resolved_answer_id": winning_answer_id}}
    )

    return poll["options"][key]["voters"]

def save_manual_poll(guild_id: str, question: str, options: list[dict]) -> str:
    """
    Save a manually created poll to DB.
    
    options = [
        {"answer_id": 1, "text": "Option A", "voters": [{"user_id": "123", "username": "aryan"}, ...]},
        {"answer_id": 2, "text": "Option B", "voters": [...]},
    ]
    
    Returns the message_id (unique ID for this manual poll)
    """
    import uuid
    from datetime import datetime, timezone
    
    message_id = str(uuid.uuid4())[:16]  # Generate unique ID
    
    options_dict = {}
    for answer in options:
        options_dict[str(answer["answer_id"])] = {
            "text": answer["text"],
            "voters": answer["voters"],
        }

    doc = {
        "message_id":         message_id,
        "channel_id":         "manual",      # Indicate it's manual
        "guild_id":           guild_id,
        "question":           question,
        "options":            options_dict,
        "expires_at":         datetime.now(timezone.utc),
        "captured":           True,          # Manual polls are already "captured"
        "resolved_answer_id": None,
        "captured_at":        datetime.now(timezone.utc),
        "created_at":         datetime.now(timezone.utc),
        "is_manual":          True,          # Flag to indicate manual poll
    }

    _db()["polls"].insert_one(doc)
    print(f"[DB] ✅ Created manual poll {message_id}")
    return message_id

def get_leaderboard_stats(server_id: str, tournament_name: str) -> list[dict]:
    """
    Get detailed stats for each user in a tournament.
    Scans all resolved polls to calculate:
    - Total points
    - Polls voted on
    - Polls voted correctly
    """
    from collections import defaultdict
    
    # Get all users with points in this tournament
    users_data = defaultdict(lambda: {
        "user_id": None,
        "username": None,
        "points": 0,
        "polls_voted": set(),
        "polls_correct": set(),
    })
    
    # Get all resolved polls for this server
    resolved_polls = list(
        _db()["polls"]
        .find(
            {"guild_id": server_id, "resolved_answer_id": {"$ne": None}},
            {"_id": 0}
        )
    )
    
    # Process each resolved poll
    for poll in resolved_polls:
        poll_id = poll["message_id"]
        winning_key = str(poll["resolved_answer_id"])
        
        # Track all voters in this poll
        for option_key, option_data in poll["options"].items():
            for voter in option_data.get("voters", []):
                user_id = voter["user_id"]
                username = voter["username"]
                
                if user_id not in users_data:
                    users_data[user_id]["user_id"] = user_id
                    users_data[user_id]["username"] = username
                
                # Track that this user voted on this poll
                users_data[user_id]["polls_voted"].add(poll_id)
                
                # If they voted for the winning option, mark as correct
                if option_key == winning_key:
                    users_data[user_id]["polls_correct"].add(poll_id)
    
    # Get points from points collection
    points_data = list(
        _db()["points"]
        .find(
            {"server_id": server_id},
            {"user_id": 1, "username": 1, "tournaments": 1}
        )
    )
    
    # Merge points data
    for user_points in points_data:
        user_id = user_points["user_id"]
        if user_id in users_data:
            points = user_points.get("tournaments", {}).get(tournament_name, 0)
            users_data[user_id]["points"] = points
    
    # Convert to list and calculate final stats
    result = []
    for user_id, data in users_data.items():
        result.append({
            "user_id": data["user_id"],
            "username": data["username"],
            "polls_voted": len(data["polls_voted"]),
            "polls_correct": len(data["polls_correct"]),
            "total_points": data["points"],
        })
    
    # Sort by points descending
    result.sort(key=lambda x: x["total_points"], reverse=True)
    return result