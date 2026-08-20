"""Mongo-backed support for the Production Task Confirmation page:
- `deviation_reason_master`: editable dropdown taxonomy for the SOAP
  DeviationReason code (free 4-char token in SAP, tenant-configurable -
  seeded here with SAP ByDesign's own standard/default code list per
  help.sap.com, editable via the page's "Manage Reasons" action in case
  this tenant's Fine-Tuning activity differs).
- `production_confirmation_history`: append-only audit log of every
  confirmation submitted (who/when/what/result) - same pattern as
  po_selection_service.py's audit trail, no login system exists so
  "who" is a free-text actor name from the browser.
"""
from datetime import datetime, timezone

REASON_COLLECTION = "deviation_reason_master"
HISTORY_COLLECTION = "production_confirmation_history"

DEFAULT_DEVIATION_REASONS = [
    {"code": "001", "label": "Resource Failure"},
    {"code": "002", "label": "Resource Unclean"},
    {"code": "003", "label": "Maintenance"},
    {"code": "004", "label": "Tool Missing"},
    {"code": "005", "label": "Tool Broken"},
    {"code": "006", "label": "Material Damage"},
    {"code": "007", "label": "Quality Issue"},
    {"code": "008", "label": "Invalid Operation"},
    {"code": "011", "label": "Missing Part"},
    {"code": "020", "label": "Peak Time"},
    {"code": "021", "label": "Unplanned Order"},
    {"code": "Z10", "label": "Broken Parts"},
]


def get_deviation_reasons(db) -> list:
    collection = db[REASON_COLLECTION]
    if collection.count_documents({}) == 0:
        now = datetime.now(timezone.utc)
        collection.insert_many([{"_id": r["code"], "label": r["label"], "source": "default", "created_at": now} for r in DEFAULT_DEVIATION_REASONS])
    return [{"code": doc["_id"], "label": doc["label"]} for doc in collection.find({}).sort("_id", 1)]


def add_deviation_reason(db, code: str, label: str) -> list:
    code = code.strip()
    label = label.strip()
    if not code or not label:
        return get_deviation_reasons(db)
    get_deviation_reasons(db)  # ensure seeded first
    db[REASON_COLLECTION].update_one(
        {"_id": code},
        {"$set": {"label": label, "source": "manual", "created_at": datetime.now(timezone.utc)}},
        upsert=True,
    )
    return get_deviation_reasons(db)


def delete_deviation_reason(db, code: str) -> list:
    db[REASON_COLLECTION].delete_one({"_id": code})
    return get_deviation_reasons(db)


def log_confirmation(db, actor: str, request_payload: dict, result: dict) -> None:
    db[HISTORY_COLLECTION].insert_one({
        "actor": actor,
        "production_lot_id": request_payload.get("production_lot_id"),
        "reporting_point_id": request_payload.get("reporting_point_id"),
        "main_output_product": request_payload.get("main_output_product"),
        "confirmed_quantity": request_payload.get("confirmed_quantity"),
        "confirmed_scrap": request_payload.get("confirmed_scrap"),
        "deviation_reason_code": request_payload.get("deviation_reason_code"),
        "confirmation_finished": request_payload.get("confirmation_finished"),
        "success": result.get("success"),
        "logs": result.get("logs"),
        "wip_clearing": result.get("wip_clearing"),
        "at": datetime.now(timezone.utc),
    })


def get_confirmation_history(db, production_lot_id: str = None, limit: int = 200) -> list:
    query = {}
    if production_lot_id:
        query["production_lot_id"] = production_lot_id
    docs = db[HISTORY_COLLECTION].find(query).sort("at", -1).limit(limit)
    return [
        {
            "actor": d.get("actor"),
            "production_lot_id": d.get("production_lot_id"),
            "reporting_point_id": d.get("reporting_point_id"),
            "main_output_product": d.get("main_output_product"),
            "confirmed_quantity": d.get("confirmed_quantity"),
            "confirmed_scrap": d.get("confirmed_scrap"),
            "deviation_reason_code": d.get("deviation_reason_code"),
            "confirmation_finished": d.get("confirmation_finished"),
            "success": d.get("success"),
            "logs": d.get("logs"),
            "wip_clearing": d.get("wip_clearing"),
            "at": d["at"].isoformat(),
        }
        for d in docs
    ]


PROPOSAL_HISTORY_COLLECTION = "production_order_creation_history"


def log_proposal_creation(db, actor: str, request_payload: dict, result: dict) -> None:
    db[PROPOSAL_HISTORY_COLLECTION].insert_one({
        "type": "proposal_created",
        "actor": actor,
        "material_id": request_payload.get("material_id"),
        "site_id": request_payload.get("site_id"),
        "quantity": request_payload.get("quantity"),
        "unit_code": request_payload.get("unit_code"),
        "production_proposal_id": result.get("production_proposal_id"),
        "at": datetime.now(timezone.utc),
    })


def log_order_release(db, actor: str, production_order_id: str, result: dict) -> None:
    db[PROPOSAL_HISTORY_COLLECTION].insert_one({
        "type": "order_released",
        "actor": actor,
        "production_order_id": production_order_id,
        "success": result.get("success"),
        "at": datetime.now(timezone.utc),
    })


def get_proposal_and_release_history(db, limit: int = 200) -> list:
    docs = db[PROPOSAL_HISTORY_COLLECTION].find({}).sort("at", -1).limit(limit)
    return [
        {
            "type": d.get("type"),
            "actor": d.get("actor"),
            "material_id": d.get("material_id"),
            "site_id": d.get("site_id"),
            "quantity": d.get("quantity"),
            "unit_code": d.get("unit_code"),
            "production_proposal_id": d.get("production_proposal_id"),
            "production_order_id": d.get("production_order_id"),
            "success": d.get("success"),
            "at": d["at"].isoformat(),
        }
        for d in docs
    ]
