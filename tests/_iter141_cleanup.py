"""iteration_141 cleanup: removes every fixture created during testing.
python /app/tests/_iter141_cleanup.py
"""
from dotenv import dotenv_values
from pymongo import MongoClient

env = dotenv_values("/app/backend/.env")
db = MongoClient(env["MONGO_URL"])[env["DB_NAME"]]

print("po_cache removed:", db["supplier_portal_po_cache"].delete_many({"po_number": {"$regex": "^ZZRT141"}}).deleted_count)
print("shipments removed:", db["supplier_portal_shipments"].delete_many(
    {"$or": [{"_id": {"$in": ["ZZ141R", "ZZ141T", "JUVM4A"]}},
             {"company_name": {"$in": ["TEST_ENTITY_141", "TEST_ENTITY_141_UI", "TEST_ENTITY_SCOPE_CO"]}}]}).deleted_count)
print("sessions removed:", db["auth_sessions"].delete_many({"_id": {"$regex": "^zz141"}}).deleted_count)
print("users removed:", db["auth_users"].delete_many({"_id": {"$regex": "^zz141"}}).deleted_count)

print("RESIDUE po_cache ZZ:", db["supplier_portal_po_cache"].count_documents({"po_number": {"$regex": "^ZZ"}}))
print("RESIDUE shipments ZZ/JUVM4A:", db["supplier_portal_shipments"].count_documents({"_id": {"$regex": "^(ZZ|JUVM4A)"}}))
print("RESIDUE zz141 sessions:", db["auth_sessions"].count_documents({"_id": {"$regex": "^zz141"}}))
print("RESIDUE zz141 users:", db["auth_users"].count_documents({"_id": {"$regex": "^zz141"}}))
