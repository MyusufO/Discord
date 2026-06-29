from pymongo import MongoClient
from dotenv import load_dotenv
import os

load_dotenv()

MONGO_URI = os.getenv("MONGO_URI")
DB_NAME = os.getenv("MONGO_DB_NAME", "discord_polls")

print(f"[TEST] MONGO_URI: {MONGO_URI}")
print(f"[TEST] DB_NAME: {DB_NAME}")

# 1. Test connection
try:
    client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
    client.admin.command("ping")
    print("[TEST] ✅ Connection successful")
except Exception as e:
    print(f"[TEST] ❌ Connection FAILED: {e}")
    exit(1)

db = client[DB_NAME]

# 2. List all collections
print(f"\n[TEST] Collections in '{DB_NAME}': {db.list_collection_names()}")

# 3. Dump all points documents
points = list(db["points"].find({}, {"_id": 0}))
print(f"\n[TEST] Points collection ({len(points)} docs):")
for p in points:
    print(p)

# 4. Dump all polls documents (just key fields)
polls = list(db["polls"].find({}, {"_id": 0, "message_id": 1, "guild_id": 1, "captured": 1, "resolved_answer_id": 1}))
print(f"\n[TEST] Polls collection ({len(polls)} docs):")
for p in polls:
    print(p)
# Simulate exactly what !leaderboard does
server_id = "1029577794935595048"
tournament_name = "WorldCup2026"




pipeline_result = list(db["points"].aggregate([
    {"$match": {"server_id": "1029577794935595048"}},
    {"$project": {"user_id": 1, "username": 1, "points": {"$ifNull": ["$tournaments.WorldCup2026", 0]}}},
    {"$sort": {"points": -1}},
    {"$limit": 10},
]))
print(f"Aggregation: {len(pipeline_result)} docs")
print(pipeline_result)