"""Probe: inspect DB state for the buyer-entity supplier portal feature (iteration 140)."""
import os, sys
from datetime import datetime, timezone, timedelta
from pymongo import MongoClient
from dotenv import dotenv_values

env = dotenv_values("/app/backend/.env")
client = MongoClient(env["MONGO_URL"])
db = client[env["DB_NAME"]]

print("== auth_sessions (non-expired) ==")
for s in db["auth_sessions"].find():
    u = db["auth_users"].find_one({"_id": s.get("user_id")}) or {}
    print(s["_id"][:20] + "...", s.get("expires_at"), u.get("email"), u.get("role"), u.get("allowed_pages"), u.get("bound_sites"))

print("\n== po cache buyer_code distribution ==")
print(list(db["supplier_portal_po_cache"].aggregate([{"$group": {"_id": {"v": "$vendor_code", "b": "$buyer_code"}, "n": {"$sum": 1}}}])))

print("\n== shipments (latest 8) ==")
for d in db["supplier_shipments"].find().sort("created_at", -1).limit(8):
    print(d["_id"], d.get("vendor_code"), d.get("status"), [(i.get("po_number"), i.get("buyer_code")) for i in d.get("items", [])])

print("\n== inventory known sites ==")
sys.path.insert(0, "/app/backend")
import inventory_service
print(inventory_service.list_known_sites(db))
from server import company_and_set_of_books_for_site
for s in inventory_service.list_known_sites(db):
    print(s, company_and_set_of_books_for_site(s))
