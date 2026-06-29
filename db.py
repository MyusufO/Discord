from pymongo import MongoClient, ASCENDING
from pymongo.errors import DuplicateKeyError
from datetime import datetime, timezone
import os

MONGO_URI = os.getenv("MONGO_URI")
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

    # matches collection (new)
    db["matches"].create_index([("match_number", ASCENDING)], unique=True)
    db["matches"].create_index([("date", ASCENDING)])
    db["matches"].create_index([("poll_created", ASCENDING)])

    print("[DB] MongoDB collections ready.")


def add_points(user_id: str, username: str, server_id: str, tournament_name: str, amount: int = 1):
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
    doc = _db()["points"].find_one({"user_id": user_id, "server_id": server_id})
    if doc and "tournaments" in doc:
        return doc["tournaments"].get(tournament_name, 0)
    return 0


def get_leaderboard(server_id: str, tournament_name: str, limit: int = 10) -> list[dict]:
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


def save_poll_immediately(message_id, channel_id, guild_id, result: dict, expires_at: datetime):
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
    return list(
        _db()["polls"]
        .find({"captured": False}, {"_id": 0})
        .sort("expires_at", 1)
    )


def resolve_poll(message_id: str, winning_answer_id: int) -> list[dict]:
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
    import uuid
    message_id = str(uuid.uuid4())[:16]

    options_dict = {}
    for answer in options:
        options_dict[str(answer["answer_id"])] = {
            "text": answer["text"],
            "voters": answer["voters"],
        }

    doc = {
        "message_id":         message_id,
        "channel_id":         "manual",
        "guild_id":           guild_id,
        "question":           question,
        "options":            options_dict,
        "expires_at":         datetime.now(timezone.utc),
        "captured":           True,
        "resolved_answer_id": None,
        "captured_at":        datetime.now(timezone.utc),
        "created_at":         datetime.now(timezone.utc),
        "is_manual":          True,
    }

    _db()["polls"].insert_one(doc)
    print(f"[DB] ✅ Created manual poll {message_id}")
    return message_id


def get_leaderboard_stats(server_id: str, tournament_name: str) -> list[dict]:
    from collections import defaultdict

    users_data = defaultdict(lambda: {
        "user_id": None,
        "username": None,
        "points": 0,
        "polls_voted": set(),
        "polls_correct": set(),
    })

    resolved_polls = list(
        _db()["polls"]
        .find(
            {"guild_id": server_id, "resolved_answer_id": {"$ne": None}},
            {"_id": 0}
        )
    )

    for poll in resolved_polls:
        poll_id = poll["message_id"]
        winning_key = str(poll["resolved_answer_id"])

        for option_key, option_data in poll["options"].items():
            for voter in option_data.get("voters", []):
                user_id = voter["user_id"]
                username = voter["username"]

                if user_id not in users_data:
                    users_data[user_id]["user_id"] = user_id
                    users_data[user_id]["username"] = username

                users_data[user_id]["polls_voted"].add(poll_id)

                if option_key == winning_key:
                    users_data[user_id]["polls_correct"].add(poll_id)

    points_data = list(
        _db()["points"]
        .find(
            {"server_id": server_id},
            {"user_id": 1, "username": 1, "tournaments": 1}
        )
    )

    for user_points in points_data:
        user_id = user_points["user_id"]
        if user_id in users_data:
            points = user_points.get("tournaments", {}).get(tournament_name, 0)
            users_data[user_id]["points"] = points

    result = []
    for user_id, data in users_data.items():
        result.append({
            "user_id": data["user_id"],
            "username": data["username"],
            "polls_voted": len(data["polls_voted"]),
            "polls_correct": len(data["polls_correct"]),
            "total_points": data["points"],
        })

    result.sort(key=lambda x: x["total_points"], reverse=True)
    return result


# ===========================================================================
# NEW: Matches collection for tournament bracket
# ===========================================================================

def load_tournament_schedule(json_path="polls.json"):
    """
    Load the tournament schedule from the JSON file and insert into MongoDB.
    Only inserts if the collection is empty.
    Returns the number of inserted matches.
    """
    import json
    from datetime import datetime

    db = _db()
    if db["matches"].count_documents({}) > 0:
        print("[DB] Matches already loaded, skipping.")
        return 0

    try:
        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        print(f"[DB] {json_path} not found. Skipping tournament load.")
        return 0

    tournament_name = list(data.keys())[0]  # "world_cup_2026"
    rounds = data[tournament_name]

    matches = []
    for round_name, match_list in rounds.items():
        for match in match_list:
            # Parse date (e.g., "June 29" -> assume year 2026)
            date_str = match["date"] + " 2026"
            try:
                match_date = datetime.strptime(date_str, "%B %d %Y").date()
            except ValueError:
                # Try abbreviated month
                match_date = datetime.strptime(date_str, "%b %d %Y").date()

            doc = {
                "match_number": match["match_number"],
                "round": round_name,
                "date": match_date,
                "time": match.get("time", ""),
                "venue": match.get("venue", ""),
                "team_1": match["team_1"],
                "team_2": match["team_2"],
                "poll_created": False,
                "poll_message_id": None,
                "winner": None,
                "resolved": False,
                "guild_id": None,      # will be set when poll is created
                "channel_id": None,
            }
            matches.append(doc)

    if matches:
        db["matches"].insert_many(matches)
        print(f"[DB] Loaded {len(matches)} matches from {json_path}")
    return len(matches)


def get_matches_for_date(date_obj):
    """Return list of matches for a given date (datetime.date)."""
    db = _db()
    return list(db["matches"].find({"date": date_obj}, {"_id": 0}))


def get_match_by_number(match_num):
    db = _db()
    return db["matches"].find_one({"match_number": match_num}, {"_id": 0})


def get_match_by_poll_message_id(message_id):
    db = _db()
    return db["matches"].find_one({"poll_message_id": str(message_id)}, {"_id": 0})


def update_match_winner(match_num, winner_name):
    db = _db()
    db["matches"].update_one(
        {"match_number": match_num},
        {"$set": {"winner": winner_name, "resolved": True}}
    )
    print(f"[DB] Match {match_num} winner set to {winner_name}")


def mark_poll_created(match_num, message_id):
    db = _db()
    db["matches"].update_one(
        {"match_number": match_num},
        {"$set": {"poll_created": True, "poll_message_id": str(message_id)}}
    )


def propagate_winner_to_future(match_num, winner_name):
    """
    Replace 'Winner of Match X' or 'Loser of Match X' placeholders
    in all future matches with the actual winner name.
    Returns the number of matches updated.
    """
    db = _db()
    pattern = f"Match {match_num}"
    query = {
        "$or": [
            {"team_1": {"$regex": pattern}},
            {"team_2": {"$regex": pattern}}
        ]
    }
    matches = db["matches"].find(query)

    updated_count = 0
    for match in matches:
        new_team_1 = match["team_1"]
        new_team_2 = match["team_2"]

        if pattern in new_team_1:
            if "Winner of Match" in new_team_1:
                new_team_1 = winner_name
            elif "Loser of Match" in new_team_1:
                # We don't have a loser stored yet – could extend later
                pass

        if pattern in new_team_2:
            if "Winner of Match" in new_team_2:
                new_team_2 = winner_name
            elif "Loser of Match" in new_team_2:
                pass

        if new_team_1 != match["team_1"] or new_team_2 != match["team_2"]:
            db["matches"].update_one(
                {"match_number": match["match_number"]},
                {"$set": {"team_1": new_team_1, "team_2": new_team_2}}
            )
            updated_count += 1

    print(f"[DB] Propagated winner {winner_name} to {updated_count} future matches.")
    return updated_count


def set_match_guild_channel(match_num, guild_id, channel_id):
    db = _db()
    db["matches"].update_one(
        {"match_number": match_num},
        {"$set": {"guild_id": str(guild_id), "channel_id": str(channel_id)}}
    )