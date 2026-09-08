"""Seed two synthetic admin sessions for iteration_146 testing:
  1. docs_only_session -> supplier_portal_documents ONLY (view-only expected)
  2. admin_session -> supplier_portal_admin (approve/reject expected)
Prints the two cookie tokens for the test file to consume via env vars.
"""
import os, sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[1] / ".env")
from pymongo import MongoClient

db = MongoClient(os.environ["MONGO_URL"], tz_aware=True)[os.environ["DB_NAME"]]

def seed(email, oid, allowed_pages, token):
    uid = f"test-tid:{oid}"
    db["auth_users"].update_one({"_id": uid}, {"$set": {
        "tid": "test-tid", "oid": oid, "email": email, "name": email,
        "role": "user", "allowed_pages": allowed_pages, "bound_sites": [],
    }}, upsert=True)
    db["auth_sessions"].update_one({"_id": token}, {"$set": {
        "user_id": uid, "expires_at": datetime.now(timezone.utc) + timedelta(days=1),
    }}, upsert=True)
    return token

DOCS_TOKEN = seed("docs.only.test@rampgroup.co.in", "iter146-docs-only", ["supplier_portal_documents"], "iter146docsonly000000000000000000000000000000")
ADMIN_TOKEN = seed("supplier.admin.test@rampgroup.co.in", "iter146-admin", ["supplier_portal_admin"], "iter146supplieradmin000000000000000000000000")
NONE_TOKEN = seed("nothing.test@rampgroup.co.in", "iter146-none", [], "iter146noaccess00000000000000000000000000000")

print(f"DOCS_TOKEN={DOCS_TOKEN}")
print(f"ADMIN_TOKEN={ADMIN_TOKEN}")
print(f"NONE_TOKEN={NONE_TOKEN}")
