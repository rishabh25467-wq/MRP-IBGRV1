"""Verifies the seeded lot's wip_clearing was overwritten by the UI Retry click."""
from dotenv import dotenv_values
from pymongo import MongoClient

benv = dotenv_values("/app/backend/.env")
db = MongoClient(benv["MONGO_URL"])[benv["DB_NAME"]]
for d in db["production_confirmation_history"].find({"production_lot_id": "24168"}).sort("at", -1).limit(3):
    print(d.get("actor"), d.get("at"), d.get("wip_clearing"))
print("---- UI jobs ----")
for d in db.background_jobs.find({"_id": {"$regex": "^TEST_QA_UIJOB"}}):
    print(d["_id"], d.get("status"), d.get("result"))
