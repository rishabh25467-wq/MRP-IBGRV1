"""Temporarily sets/reverts a test net_weight_kg on component_master for a
product so the scrap auto-calc 'available' UI path can be exercised.
Usage: python _temp_net_weight.py set 632163-1 0.015 | python _temp_net_weight.py revert 632163-1"""
import os
import sys

from dotenv import load_dotenv
from pymongo import MongoClient

load_dotenv("/app/backend/.env")
db = MongoClient(os.environ["MONGO_URL"])[os.environ["DB_NAME"]]
action, pid = sys.argv[1], sys.argv[2]
col = db["component_master"]
if action == "set":
    col.update_one({"_id": pid}, {"$set": {"net_weight_kg": float(sys.argv[3])}}, upsert=True)
else:
    col.update_one({"_id": pid}, {"$unset": {"net_weight_kg": ""}})
print(pid, "net_weight_kg ->", (col.find_one({"_id": pid}) or {}).get("net_weight_kg"))
