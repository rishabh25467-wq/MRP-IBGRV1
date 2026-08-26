"""Removes every synthetic artifact created during iteration_119 testing."""
import os

from dotenv import dotenv_values
from pymongo import MongoClient

env = dotenv_values("/app/backend/.env")
client = MongoClient(os.environ.get("MONGO_URL") or env.get("MONGO_URL"))
db = client[os.environ.get("DB_NAME") or env.get("DB_NAME")]

print("jobs:", db["background_jobs"].delete_many({"_id": {"$regex": "^TEST-JOB-S19-"}}).deleted_count)
print("sessions:", db.auth_sessions.delete_many({"_id": {"$regex": "^TESTQA"}}).deleted_count)
print("users:", db.auth_users.delete_many({"_id": {"$regex": "TEST-QA-OID"}}).deleted_count)
print("history TEST docs:", db["production_confirmation_history"].delete_many({"_id": {"$regex": "^TEST_"}}).deleted_count)
print("history TESTLOT:", db["production_confirmation_history"].delete_many({"production_lot_id": "TESTLOT999"}).deleted_count)
