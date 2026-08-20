import os, secrets, sys
from datetime import datetime, timedelta, timezone
from dotenv import dotenv_values
from pymongo import MongoClient

env = dotenv_values("/app/backend/.env")
client = MongoClient(env["MONGO_URL"])
db = client[env["DB_NAME"]]

UID = "testtid:testoid-prodconf2"
mode = sys.argv[1] if len(sys.argv) > 1 else "create"

if mode == "create":
    token = secrets.token_urlsafe(32)
    db.auth_users.replace_one({"_id": UID}, {
        "_id": UID, "tid": "testtid", "oid": "testoid-prodconf2",
        "email": "qa-prodconf2@example.test", "name": "QA ProdConf2",
        "role": "super_admin", "allowed_pages": ["production_confirmation"],
        "created_at": datetime.now(timezone.utc), "last_login_at": datetime.now(timezone.utc),
    }, upsert=True)
    db.auth_sessions.delete_many({"user_id": UID})
    db.auth_sessions.insert_one({"_id": token, "user_id": UID,
        "expires_at": datetime.now(timezone.utc) + timedelta(days=1)})
    open("/tmp/test_token2.txt", "w").write(token)
    print("TOKEN:", token)
    print("history count:", db.production_order_creation_history.count_documents({}))
else:
    db.auth_users.delete_many({"_id": UID})
    db.auth_sessions.delete_many({"user_id": UID})
    print("cleaned")
