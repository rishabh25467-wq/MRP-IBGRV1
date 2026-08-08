"""Persistence for supplier master data and per-part supplier assignments
(quota %, lead time, price, preferred/backup) - fully self-managed in this
app. The user's SAP Business ByDesign tenant does not maintain a Source
List / Approved Supplier List, so this is NOT synced with SAP; it exists
purely to help decide how to split a purchase requisition's quantity
across suppliers when a part has more than one."""
import uuid
from datetime import datetime, timezone

SUPPLIERS_COLLECTION = "suppliers"
PART_SUPPLIERS_COLLECTION = "part_suppliers"


def _now():
    return datetime.now(timezone.utc)


# ---- Supplier master list ----

def create_supplier(db, name, contact_person=None, email=None, phone=None,
                     sap_internal_id=None, sap_uuid=None) -> dict:
    doc = {
        "_id": str(uuid.uuid4()),
        "name": name,
        "contact_person": contact_person,
        "email": email,
        "phone": phone,
        "sap_internal_id": sap_internal_id,
        "sap_uuid": sap_uuid,
        "source": "sap" if sap_internal_id else "local",
        "created_at": _now(),
        "updated_at": _now(),
    }
    db[SUPPLIERS_COLLECTION].insert_one(doc)
    return doc


def sync_suppliers_from_sap(db, sap_suppliers: list) -> dict:
    """Upserts SAP-sourced suppliers by sap_internal_id (never duplicates a
    supplier already synced before, and never touches a purely local one
    with no SAP link). Returns {created, updated}."""
    created, updated = 0, 0
    for s in sap_suppliers:
        existing = db[SUPPLIERS_COLLECTION].find_one({"sap_internal_id": s["internal_id"]})
        if existing:
            db[SUPPLIERS_COLLECTION].update_one(
                {"_id": existing["_id"]},
                {"$set": {
                    "name": s["name"], "email": s.get("email"), "phone": s.get("phone"),
                    "sap_uuid": s.get("uuid"), "updated_at": _now(),
                }},
            )
            updated += 1
        else:
            db[SUPPLIERS_COLLECTION].insert_one({
                "_id": str(uuid.uuid4()),
                "name": s["name"],
                "contact_person": None,
                "email": s.get("email"),
                "phone": s.get("phone"),
                "sap_internal_id": s["internal_id"],
                "sap_uuid": s.get("uuid"),
                "source": "sap",
                "created_at": _now(),
                "updated_at": _now(),
            })
            created += 1
    return {"created": created, "updated": updated}


def list_suppliers(db) -> list:
    return list(db[SUPPLIERS_COLLECTION].find().sort("name", 1))


def get_supplier(db, supplier_id) -> dict:
    return db[SUPPLIERS_COLLECTION].find_one({"_id": supplier_id})


def update_supplier(db, supplier_id, updates: dict) -> dict:
    updates = {**updates, "updated_at": _now()}
    db[SUPPLIERS_COLLECTION].update_one({"_id": supplier_id}, {"$set": updates})
    return get_supplier(db, supplier_id)


def delete_supplier(db, supplier_id) -> bool:
    # A deleted supplier must not leave orphaned quota rows pointing at a
    # supplier that no longer exists - remove its part assignments too.
    db[PART_SUPPLIERS_COLLECTION].delete_many({"supplier_id": supplier_id})
    result = db[SUPPLIERS_COLLECTION].delete_one({"_id": supplier_id})
    return result.deleted_count > 0


# ---- Part <-> Supplier assignments ----

def assign_supplier_to_part(db, product_id, supplier_id, quota_percent=None,
                             lead_time_days=None, unit_price=None, currency=None,
                             preference="Preferred", notes=None) -> dict:
    doc = {
        "_id": str(uuid.uuid4()),
        "product_id": product_id,
        "supplier_id": supplier_id,
        "quota_percent": quota_percent,
        "lead_time_days": lead_time_days,
        "unit_price": unit_price,
        "currency": currency,
        "preference": preference,
        "notes": notes,
        "created_at": _now(),
        "updated_at": _now(),
    }
    db[PART_SUPPLIERS_COLLECTION].insert_one(doc)
    return doc


def list_suppliers_for_part(db, product_id) -> list:
    return list(db[PART_SUPPLIERS_COLLECTION].find({"product_id": product_id}).sort("created_at", 1))


def list_suppliers_for_parts(db, product_ids: list) -> dict:
    """Bulk fetch for the Purchasing Plan summary view.
    Returns {product_id: [assignment, ...]} for every id, even ones with no
    assignments (empty list), so callers don't need a membership check."""
    grouped = {pid: [] for pid in product_ids}
    for doc in db[PART_SUPPLIERS_COLLECTION].find({"product_id": {"$in": product_ids}}).sort("created_at", 1):
        grouped.setdefault(doc["product_id"], []).append(doc)
    return grouped


def get_part_supplier(db, assignment_id) -> dict:
    return db[PART_SUPPLIERS_COLLECTION].find_one({"_id": assignment_id})


def update_part_supplier(db, assignment_id, updates: dict) -> dict:
    updates = {**updates, "updated_at": _now()}
    db[PART_SUPPLIERS_COLLECTION].update_one({"_id": assignment_id}, {"$set": updates})
    return get_part_supplier(db, assignment_id)


def delete_part_supplier(db, assignment_id) -> bool:
    result = db[PART_SUPPLIERS_COLLECTION].delete_one({"_id": assignment_id})
    return result.deleted_count > 0
