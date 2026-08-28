import json
from dotenv import dotenv_values
from pymongo import MongoClient

env = dotenv_values("/app/backend/.env")
cli = MongoClient(env["MONGO_URL"])
db = cli[env["DB_NAME"]]
jobs = list(db["background_jobs"].find({"kind": "inbound_receipt"}).sort("created_at", -1).limit(6))
for j in jobs:
    print(json.dumps({k: str(v)[:160] for k, v in j.items() if k != "result"}, indent=1))
    print("  result_status:", (j.get("result") or {}).get("status"))
print("---- STO docs ----")
for sto in ["STO-000068", "STO-000067", "STO-000066", "STO-000065"]:
    d = db["stock_transfer_orders"].find_one({"_id": sto}) or {}
    print(sto, "receipt_status=", d.get("receipt_status"), "receipt_error=", str(d.get("receipt_error"))[:120], "gi_status=", d.get("gi_status"), "gi_line_status=", d.get("gi_line_status"))
