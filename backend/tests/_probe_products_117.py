"""Finds candidate products for the by-product backend-enforcement tests."""
import os
from dotenv import dotenv_values
from pymongo import MongoClient

env = dotenv_values("/app/backend/.env")
db = MongoClient(os.environ.get("MONGO_URL") or env.get("MONGO_URL"))[
    os.environ.get("DB_NAME") or env.get("DB_NAME")
]

no_mass = []
with_mass_no_nw = []
with_mass_with_nw = []
for doc in db["bom_node_cache"].find({}, {"groups": 1}).limit(4000):
    pid = doc["_id"]
    mass = [
        it for g in doc.get("groups", []) for it in g.get("items", [])
        if it.get("unit_of_measure") == "MASS" and it.get("quantity") is not None
    ]
    cm = db["component_master"].find_one({"_id": pid}, {"net_weight_kg": 1}) or {}
    nw = cm.get("net_weight_kg")
    if not mass:
        if len(no_mass) < 5:
            no_mass.append(pid)
    elif nw is None:
        if len(with_mass_no_nw) < 5:
            with_mass_no_nw.append((pid, [m["product_id"] for m in mass][:3]))
    else:
        if len(with_mass_with_nw) < 5:
            with_mass_with_nw.append((pid, nw, [m["product_id"] for m in mass][:3]))

print("NO_MASS:", no_mass)
print("MASS_NO_NETWEIGHT:", with_mass_no_nw)
print("MASS_WITH_NETWEIGHT:", with_mass_with_nw)
print("history docs with wip_clearing:", db["production_confirmation_history"].count_documents({"wip_clearing": {"$exists": True}}))
for d in db["production_confirmation_history"].find({}, {"production_lot_id": 1, "wip_clearing": 1, "at": 1}).sort("at", -1).limit(5):
    print(" hist:", d.get("production_lot_id"), (d.get("wip_clearing") or {}).get("success"), d.get("at"))
print("bg jobs:", db["background_jobs"].count_documents({}))
