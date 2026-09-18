"""Seed two synthetic Entra ID sessions for Act as Supplier feature testing:
  session A: has 'act_as_supplier' permission (positive case)
  session B: does NOT have 'act_as_supplier' permission (negative case)
Also picks an approved supplier account we'll target and prints its _id + email.
"""
import os
from datetime import datetime, timezone, timedelta
from pymongo import MongoClient

MONGO_URL = os.environ.get("MONGO_URL", "mongodb://localhost:27017")
DB_NAME = os.environ.get("DB_NAME", "test_database")
client = MongoClient(MONGO_URL)
db = client[DB_NAME]

now = datetime.now(timezone.utc)
expires = now + timedelta(days=7)

# Positive session
db["auth_users"].update_one({"_id": "test-tid:iter183-pos"}, {"$set": {
    "_id": "test-tid:iter183-pos",
    "tid": "test-tid", "oid": "iter183-pos",
    "email": "iter183.pos@internal.test", "name": "Iter183 Pos Tester",
    "role": "user",
    "allowed_pages": ["act_as_supplier", "supplier_dashboard", "vendor_goods_receipt", "supplier_portal_admin"],
    "bound_sites": [],
    "created_at": now, "last_login_at": now,
}}, upsert=True)
POS_TOKEN = "iter183actassupplierpos000000000000000000000000"
db["auth_sessions"].update_one({"_id": POS_TOKEN}, {"$set": {
    "_id": POS_TOKEN, "user_id": "test-tid:iter183-pos", "expires_at": expires,
}}, upsert=True)

# Negative session (no act_as_supplier)
db["auth_users"].update_one({"_id": "test-tid:iter183-neg"}, {"$set": {
    "_id": "test-tid:iter183-neg",
    "tid": "test-tid", "oid": "iter183-neg",
    "email": "iter183.neg@internal.test", "name": "Iter183 Neg Tester",
    "role": "user",
    "allowed_pages": ["supplier_dashboard"],
    "bound_sites": [],
    "created_at": now, "last_login_at": now,
}}, upsert=True)
NEG_TOKEN = "iter183actassupplierneg000000000000000000000000"
db["auth_sessions"].update_one({"_id": NEG_TOKEN}, {"$set": {
    "_id": NEG_TOKEN, "user_id": "test-tid:iter183-neg", "expires_at": expires,
}}, upsert=True)

print(f"POS_TOKEN={POS_TOKEN}")
print(f"NEG_TOKEN={NEG_TOKEN}")

# Show approved supplier accounts
approved = list(db["supplier_portal_accounts"].find({"status": "approved"}, {"_id": 1, "vendor_code": 1, "email": 1, "company_name": 1}))
for a in approved:
    print(f"APPROVED: id={a['_id']} vendor={a.get('vendor_code')} email={a.get('email')} name={a.get('company_name')}")

# Also show open POs for S9999 (which has dummy fixture data)
s9999 = list(db["supplier_portal_po_cache"].find({"vendor_code": "S9999"}, {"_id": 0}))
print(f"S9999 PO cache entries: {len(s9999)}")
for p in s9999[:5]:
    print(f"  po={p.get('po_number')} item={p.get('item_number')} product={p.get('product_id')} open_qty={p.get('remaining_qty') or p.get('open_qty')}  buyer_entity={p.get('buyer_entity_name')}")
