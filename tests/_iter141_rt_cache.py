"""iteration_141 UI fixture: temp RT-entity po_cache row for vendor H1330.
Lives outside /app/backend so writing it doesn't trigger a uvicorn reload.

python /app/tests/_iter141_rt_cache.py seed | clean
"""
import sys
from datetime import datetime, timezone

from dotenv import dotenv_values
from pymongo import MongoClient

env = dotenv_values("/app/backend/.env")
db = MongoClient(env["MONGO_URL"])[env["DB_NAME"]]
CID = "H1330::ZZRT141::1"


def seed():
    db["supplier_portal_po_cache"].replace_one({"_id": CID}, {
        "_id": CID, "vendor_code": "H1330", "vendor_name": "HAMIDI EXPORTS",
        "po_number": "ZZRT141", "item_number": "1", "product_id": "ZZNOTREAL141",
        "description": "TEST_ RT entity line", "po_qty": 20, "unit_of_measure": "EA",
        "buyer_code": "RT", "updated_at": datetime.now(timezone.utc), "expired": False,
    }, upsert=True)
    print("seeded", CID)


def clean():
    print("deleted:", db["supplier_portal_po_cache"].delete_one({"_id": CID}).deleted_count)
    print("residue ZZ po_cache:", db["supplier_portal_po_cache"].count_documents({"po_number": {"$regex": "^ZZ"}}))


if __name__ == "__main__":
    (seed if sys.argv[1] == "seed" else clean)()
